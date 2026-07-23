import path from "node:path";
import { AppConfigSchema, type AppConfig, type MimoAccount } from "./types.js";
import { AppDatabase } from "../db/database.js";

const envBoolean = (value: string | undefined, fallback: boolean): boolean => {
  if (!value?.trim()) return fallback;
  return !["0", "false", "no", "off"].includes(value.trim().toLowerCase());
};

const envOverride = <T>(value: string | undefined, fallback: T): string | T => value?.trim() ? value : fallback;

export class ConfigStore {
  readonly legacyFile: string;
  readonly database: AppDatabase;
  #config: AppConfig = AppConfigSchema.parse({});
  #accountIndex = 0;

  private constructor(database: AppDatabase, legacyFile: string) {
    this.database = database;
    this.legacyFile = path.resolve(legacyFile);
  }

  static async open(
    legacyFile = process.env.MIMO2API_CONFIG_FILE ?? "config.json",
    databaseFile = process.env.MIMO2API_DATABASE_FILE,
  ): Promise<ConfigStore> {
    const database = await AppDatabase.open(databaseFile);
    const store = new ConfigStore(database, legacyFile);
    await store.load();
    return store;
  }

  async load(): Promise<AppConfig> {
    await this.database.importLegacyConfig(this.legacyFile);
    this.refreshFromDatabase();
    return this.snapshot();
  }

  refreshFromDatabase(): void {
    this.#config = this.#applyEnvironment(this.database.readConfig());
  }

  snapshot(): AppConfig {
    return structuredClone(this.#config);
  }

  validateApiKey(key: string | undefined): boolean {
    if (!key) return false;
    return this.#config.api_keys.split(",").map((item) => item.trim()).includes(key);
  }

  nextAccount(): MimoAccount | undefined {
    const accounts = this.usableAccounts();
    if (accounts.length === 0) return undefined;
    const account = accounts[this.#accountIndex % accounts.length];
    this.#accountIndex = (this.#accountIndex + 1) % accounts.length;
    return structuredClone(account);
  }

  usableAccounts(): MimoAccount[] {
    return this.#config.mimo_accounts.filter((account) => (
      account.is_valid && account.service_token && account.user_id && account.xiaomichatbot_ph
    )).map((account) => structuredClone(account));
  }

  accountByUserId(userId: string): MimoAccount | undefined {
    const account = this.#config.mimo_accounts.find((item) => (
      item.user_id === userId && item.is_valid && item.service_token && item.xiaomichatbot_ph
    ));
    return account ? structuredClone(account) : undefined;
  }

  async update(patch: Partial<AppConfig>): Promise<AppConfig> {
    this.#config = AppConfigSchema.parse({
      ...this.#config,
      ...patch,
      temp_mail: { ...this.#config.temp_mail, ...(patch.temp_mail ?? {}) },
      proxy_pool: { ...this.#config.proxy_pool, ...(patch.proxy_pool ?? {}) },
      captcha_ai: { ...this.#config.captcha_ai, ...(patch.captcha_ai ?? {}) },
    });
    this.database.writeConfig(this.#config);
    return this.snapshot();
  }

  async replaceAccounts(accounts: MimoAccount[]): Promise<void> {
    this.#config.mimo_accounts = AppConfigSchema.shape.mimo_accounts.parse(accounts);
    this.database.replaceAccounts(this.#config.mimo_accounts);
  }

  publicView(): AppConfig {
    const config = this.snapshot();
    config.mimo_accounts = config.mimo_accounts.map((account) => ({
      ...account,
      service_token: "",
      token_masked: mask(account.service_token, 12, 6),
      password: account.password ? "***" : "",
      pass_token: mask(account.pass_token, 10, 6),
      mail_jwt: mask(account.mail_jwt, 10, 6),
      has_password: Boolean(account.password),
      has_pass_token: Boolean(account.pass_token),
      has_mail_jwt: Boolean(account.mail_jwt),
    }));
    return config;
  }

  #applyEnvironment(config: AppConfig): AppConfig {
    return AppConfigSchema.parse({
      ...config,
      api_keys: envOverride(process.env.MIMO2API_API_KEYS, config.api_keys),
      admin_password: envOverride(process.env.MIMO2API_ADMIN_PASSWORD, config.admin_password),
      temp_mail: {
        ...config.temp_mail,
        api_base: envOverride(process.env.MIMO2API_TEMP_MAIL_API_BASE, config.temp_mail.api_base),
        admin_password: envOverride(process.env.MIMO2API_TEMP_MAIL_ADMIN_PASSWORD, config.temp_mail.admin_password),
        site_password: envOverride(process.env.MIMO2API_TEMP_MAIL_SITE_PASSWORD, config.temp_mail.site_password),
        domain: envOverride(process.env.MIMO2API_TEMP_MAIL_DOMAIN, config.temp_mail.domain),
      },
      proxy_pool: {
        ...config.proxy_pool,
        enabled: envBoolean(process.env.MIMO2API_PROXY_ENABLED, config.proxy_pool.enabled),
        sub_url: envOverride(process.env.MIMO2API_PROXY_SUB_URL, config.proxy_pool.sub_url),
        fetch_sub_each_time: envBoolean(
          process.env.MIMO2API_PROXY_FETCH_SUB_EACH_TIME,
          config.proxy_pool.fetch_sub_each_time,
        ),
      },
      captcha_ai: {
        ...config.captcha_ai,
        enabled: envBoolean(process.env.MIMO2API_CAPTCHA_AI_ENABLED, config.captcha_ai.enabled),
        api_base: envOverride(process.env.MIMO2API_CAPTCHA_AI_API_BASE, config.captcha_ai.api_base),
        api_key: envOverride(process.env.MIMO2API_CAPTCHA_AI_API_KEY, config.captcha_ai.api_key),
        model: envOverride(process.env.MIMO2API_CAPTCHA_AI_MODEL, config.captcha_ai.model),
        timeout: envOverride(process.env.MIMO2API_CAPTCHA_AI_TIMEOUT, config.captcha_ai.timeout),
      },
    });
  }
}

const mask = (value: string, head: number, tail: number): string => {
  if (!value) return "";
  if (value.length <= head + tail) return "***";
  return `${value.slice(0, head)}...${value.slice(-tail)}`;
};
