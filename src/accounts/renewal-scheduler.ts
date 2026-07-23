import type { ConfigStore } from "../config/store.js";
import { asApiError } from "../core/errors.js";
import { XiaomiTokenRenewer } from "./xiaomi-renewer.js";
import { AccountValidator } from "./account-validator.js";
import { AccountRequestCoordinator, type AccountLease } from "./request-coordinator.js";

export class RenewalScheduler {
  #stopped = true;
  #workers: Promise<void>[] = [];
  #controller = new AbortController();
  readonly concurrency = Math.max(1, Math.min(4, Number(process.env.MIMO2API_RENEW_CONCURRENCY ?? 1)));
  readonly intervalMs = Math.max(900_000, Number(process.env.MIMO2API_RENEW_INTERVAL_SECONDS ?? 21_600) * 1_000);
  readonly leaseMs = Math.max(300_000, Number(process.env.MIMO2API_RENEW_LEASE_SECONDS ?? 900) * 1_000);
  readonly pollMs = Math.max(5_000, Number(process.env.MIMO2API_RENEW_POLL_SECONDS ?? 30) * 1_000);
  readonly gapMs = Math.max(0, Number(process.env.MIMO2API_RENEW_GAP_MS ?? 3_000));

  constructor(
    private readonly config: ConfigStore,
    private readonly requests: AccountRequestCoordinator,
    private readonly renewer = new XiaomiTokenRenewer(),
    private readonly validator = new AccountValidator(),
  ) {}

  start(): void {
    if (!this.#stopped) return;
    this.#stopped = false;
    this.#controller = new AbortController();
    this.#workers = Array.from({ length: this.concurrency }, (_, index) => this.#worker(index));
  }

  async stop(): Promise<void> {
    this.#stopped = true;
    this.#controller.abort(new DOMException("Scheduler stopped", "AbortError"));
    await Promise.allSettled(this.#workers);
    this.#workers = [];
  }

  status(): object {
    return {
      running: !this.#stopped,
      concurrency: this.concurrency,
      interval_seconds: Math.floor(this.intervalMs / 1_000),
      ...this.config.database.renewalStatus(),
    };
  }

  async renewIndex(index: number): Promise<object> {
    const accounts = this.config.snapshot().mimo_accounts;
    const account = accounts[index];
    if (!account) return { ok: false, error: "account not found" };
    const signal = AbortSignal.timeout(this.leaseMs);
    const lease = await this.requests.acquire(account, signal);
    try {
      const renewed = await this.renewer.renew(account, signal);
      accounts[index] = await this.validator.validate(renewed, signal);
      await this.config.replaceAccounts(accounts);
      return { ok: true, user_id: accounts[index]?.user_id, last_renew: accounts[index]?.last_renew };
    } catch (error) {
      return { ok: false, error: asApiError(error).message };
    } finally {
      lease.release();
    }
  }

  async validateIndex(index: number): Promise<object> {
    const accounts = this.config.snapshot().mimo_accounts;
    const account = accounts[index];
    if (!account) return { ok: false, error: "account not found" };
    const signal = AbortSignal.timeout(this.leaseMs);
    const lease = await this.requests.acquire(account, signal);
    try {
      accounts[index] = await this.validator.validate(account, signal);
      await this.config.replaceAccounts(accounts);
      return { ok: true, user_id: accounts[index]?.user_id, last_test: accounts[index]?.last_test };
    } catch (error) {
      accounts[index] = { ...account, is_valid: false, last_test: new Date().toISOString(), renew_error: asApiError(error).message };
      await this.config.replaceAccounts(accounts);
      return { ok: false, error: asApiError(error).message };
    } finally {
      lease.release();
    }
  }

  async #worker(index: number): Promise<void> {
    if (index > 0) await delay(index * this.gapMs, this.#controller.signal).catch(() => undefined);
    while (!this.#stopped) {
      const claimed = this.config.database.claimRenewal(Date.now(), this.leaseMs);
      if (!claimed) {
        await delay(this.pollMs, this.#controller.signal).catch(() => undefined);
        continue;
      }
      const signal = AbortSignal.any([
        this.#controller.signal,
        AbortSignal.timeout(this.leaseMs),
      ]);
      let lease: AccountLease | undefined;
      try {
        lease = await this.requests.acquire(claimed.account, signal);
        const renewed = await this.renewer.renew(claimed.account, signal);
        const validated = await this.validator.validate(renewed, signal);
        this.config.database.completeRenewal(claimed.key, validated, Date.now() + jitter(this.intervalMs, 0.2));
      } catch (error) {
        if (this.#controller.signal.aborted) break;
        const backoff = Math.min(this.intervalMs, 900_000 * 2 ** Math.min(claimed.attempts, 4));
        this.config.database.failRenewal(claimed.key, asApiError(error).message, Date.now() + jitter(backoff, 0.25));
      } finally {
        lease?.release();
        this.config.refreshFromDatabase();
      }
      await delay(this.gapMs, this.#controller.signal).catch(() => undefined);
    }
  }
}

const jitter = (base: number, ratio: number): number => Math.round(base * (1 - ratio + Math.random() * ratio * 2));

const delay = (ms: number, signal: AbortSignal): Promise<void> => new Promise((resolve, reject) => {
  if (signal.aborted) return reject(signal.reason);
  const onAbort = () => {
    clearTimeout(timer);
    reject(signal.reason);
  };
  const timer = setTimeout(() => {
    signal.removeEventListener("abort", onAbort);
    resolve();
  }, ms);
  signal.addEventListener("abort", onAbort, { once: true });
});
