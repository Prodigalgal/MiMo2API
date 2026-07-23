import { createHash, randomUUID } from "node:crypto";
import type { ConfigStore } from "../config/store.js";
import type { MimoAccount } from "../config/types.js";
import type { AppDatabase } from "../db/database.js";
import type { ProtocolMessage } from "../protocol/types.js";

export interface ConversationSessionInput {
  tenant: string;
  sessionId: string;
  model: string;
}

export interface ConversationSession {
  key: string;
  account: MimoAccount;
  conversationId: string;
  promptTokens: number;
  shouldCompact: boolean;
  previousMessages: ProtocolMessage[];
  summary: string;
}

interface SessionRow {
  session_key: string;
  account_user_id: string;
  conversation_id: string;
  prompt_tokens: number;
  context_hash: string;
  context_query: string;
  messages_json: string;
  summary_text: string;
  expires_at: number;
}

const ttlMs = () => Math.max(3_600_000, Number(process.env.MIMO2API_SESSION_TTL_SECONDS ?? 259_200) * 1_000);
const compactThreshold = () => Math.max(16_000, Number(process.env.MIMO2API_SESSION_COMPACT_THRESHOLD_TOKENS ?? 150_000));
const maxCacheBytes = () => Math.max(65_536, Number(process.env.MIMO2API_SESSION_CACHE_MAX_BYTES ?? 5 * 1024 * 1024));

export class ConversationStore {
  constructor(private readonly database: AppDatabase) {}

  resolve(input: ConversationSessionInput, config: ConfigStore): ConversationSession | undefined {
    const existing = this.find(input, config);
    if (existing) return existing;
    const account = config.nextAccount();
    return account ? this.create(input, account) : undefined;
  }

  find(input: ConversationSessionInput, config: ConfigStore): ConversationSession | undefined {
    this.cleanup();
    const key = sessionKey(input);
    const row = this.database.connection.prepare(`
      SELECT session_key, account_user_id, conversation_id, prompt_tokens,
             context_hash, context_query, messages_json, summary_text, expires_at
      FROM conversation_sessions WHERE session_key = ? AND expires_at > ?
    `).get(key, Date.now()) as SessionRow | undefined;
    const account = row ? config.accountByUserId(row.account_user_id) : undefined;
    if (account) {
      this.touch(key);
      return {
        key,
        account,
        conversationId: row!.conversation_id,
        promptTokens: row!.prompt_tokens,
        shouldCompact: row!.prompt_tokens >= compactThreshold() && Boolean(row!.context_query),
        previousMessages: parseMessages(row!.messages_json),
        summary: row!.summary_text,
      };
    }
    return undefined;
  }

  create(input: ConversationSessionInput, account: MimoAccount): ConversationSession {
    const key = sessionKey(input);
    const conversationId = newConversationId();
    this.database.connection.prepare(`
      INSERT INTO conversation_sessions(
        session_key, account_user_id, conversation_id, prompt_tokens, context_hash,
        context_query, messages_json, summary_text, created_at, last_used_at, expires_at
      ) VALUES (?, ?, ?, 0, '', '', '[]', '', ?, ?, ?)
      ON CONFLICT(session_key) DO UPDATE SET
        account_user_id = excluded.account_user_id, conversation_id = excluded.conversation_id,
        prompt_tokens = 0, context_hash = '', context_query = '', messages_json = '[]',
        summary_text = '', last_used_at = excluded.last_used_at, expires_at = excluded.expires_at
    `).run(key, account.user_id, conversationId, Date.now(), Date.now(), Date.now() + ttlMs());
    return { key, account, conversationId, promptTokens: 0, shouldCompact: false, previousMessages: [], summary: "" };
  }

  cachedQuery(session: ConversationSession, contextHash: string): string | undefined {
    const row = this.database.connection.prepare(`
      SELECT context_query FROM conversation_sessions
      WHERE session_key = ? AND context_hash = ? AND expires_at > ?
    `).get(session.key, contextHash, Date.now()) as { context_query: string } | undefined;
    return row?.context_query || undefined;
  }

  rememberContext(session: ConversationSession, contextHash: string, query: string, messages: ProtocolMessage[]): void {
    const messagesJson = JSON.stringify(messages);
    const cacheable = Buffer.byteLength(query) + Buffer.byteLength(messagesJson) <= maxCacheBytes();
    this.database.connection.prepare(`
      UPDATE conversation_sessions SET
        context_hash = ?, context_query = ?, messages_json = ?, last_used_at = ?, expires_at = ?
      WHERE session_key = ?
    `).run(
      cacheable ? contextHash : "",
      cacheable ? query : "",
      cacheable ? messagesJson : "[]",
      Date.now(),
      Date.now() + ttlMs(),
      session.key,
    );
  }

  recordUsage(session: ConversationSession, promptTokens: number): void {
    if (!Number.isFinite(promptTokens) || promptTokens <= 0) return;
    this.database.connection.prepare(`
      UPDATE conversation_sessions SET prompt_tokens = prompt_tokens + ?, last_used_at = ?, expires_at = ?
      WHERE session_key = ?
    `).run(Math.floor(promptTokens), Date.now(), Date.now() + ttlMs(), session.key);
  }

  rotateAfterCompaction(session: ConversationSession, summary: string): ConversationSession {
    const conversationId = newConversationId();
    this.database.connection.prepare(`
      UPDATE conversation_sessions SET
        conversation_id = ?, prompt_tokens = 0, context_hash = '', context_query = '',
        messages_json = '[]', summary_text = ?, last_used_at = ?, expires_at = ?
      WHERE session_key = ?
    `).run(conversationId, summary.slice(0, maxCacheBytes()), Date.now(), Date.now() + ttlMs(), session.key);
    return { ...session, conversationId, promptTokens: 0, shouldCompact: false, previousMessages: [], summary };
  }

  cleanup(): number {
    return this.database.connection.prepare("DELETE FROM conversation_sessions WHERE expires_at <= ?").run(Date.now()).changes;
  }

  private touch(key: string): void {
    this.database.connection.prepare(`
      UPDATE conversation_sessions SET last_used_at = ?, expires_at = ? WHERE session_key = ?
    `).run(Date.now(), Date.now() + ttlMs(), key);
  }
}

export const contextFingerprint = (messages: ProtocolMessage[], tools: unknown, passthrough: boolean): string => createHash("sha256")
  .update(stableJson({ messages, tools: tools ?? [], passthrough }))
  .digest("hex");

export const sessionKey = (input: ConversationSessionInput): string => createHash("sha256")
  .update(`${input.tenant}\u0000${input.model}\u0000${input.sessionId}`)
  .digest("hex");

const newConversationId = (): string => randomUUID().replaceAll("-", "");

const parseMessages = (value: string): ProtocolMessage[] => {
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) ? parsed as ProtocolMessage[] : [];
  } catch {
    return [];
  }
};

const stableJson = (value: unknown): string => JSON.stringify(value, (_key, item) => {
  if (!item || typeof item !== "object" || Array.isArray(item)) return item;
  return Object.fromEntries(Object.entries(item).sort(([left], [right]) => left.localeCompare(right)));
});
