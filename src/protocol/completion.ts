import type { ConfigStore } from "../config/store.js";
import { ApiError } from "../core/errors.js";
import { MimoClient } from "../mimo/client.js";
import { uploadSources } from "../mimo/media.js";
import { SemanticDecoder } from "./decoder.js";
import { prepareMessages } from "./messages.js";
import type { ProtocolMessage, SemanticEvent, ToolDefinition } from "./types.js";
import type { UsageRepository } from "../usage/repository.js";
import { ConversationStore, contextFingerprint, type ConversationSession } from "../sessions/store.js";
import { AccountRequestCoordinator, KeyedSerialQueue, type AccountLease } from "../accounts/request-coordinator.js";
import { sessionKey } from "../sessions/store.js";

export interface CompletionRequest {
  model?: string;
  messages: ProtocolMessage[];
  tools?: ToolDefinition[];
  toolChoice?: unknown;
  reasoningEffort?: string;
  thinking?: boolean;
  sessionId?: string;
  sessionTenant?: string;
}

export class CompletionService {
  readonly sessions: ConversationStore;
  readonly #sessionQueue = new KeyedSerialQueue();

  constructor(
    private readonly config: ConfigStore,
    private readonly usage?: UsageRepository,
    private readonly accounts = new AccountRequestCoordinator(),
  ) {
    this.sessions = new ConversationStore(config.database);
  }

  async *events(request: CompletionRequest, signal: AbortSignal): AsyncGenerator<SemanticEvent> {
    const model = validateModel(request.model ?? "mimo-v2.5-pro");
    const sessionInput = request.sessionId && request.sessionTenant
      ? { tenant: request.sessionTenant, sessionId: request.sessionId, model }
      : undefined;
    const releaseSession = sessionInput ? await this.#sessionQueue.acquire(sessionKey(sessionInput), signal) : undefined;
    let accountLease: AccountLease | undefined;
    try {
      let session = sessionInput ? this.sessions.find(sessionInput, this.config) : undefined;
      const acquired = await this.#acquireFreshAccount(session?.account.user_id, signal);
      accountLease = acquired.lease;
      const account = acquired.account;
      if (sessionInput && (!session || session.account.user_id !== account.user_id)) {
        session = this.sessions.create(sessionInput, account);
      }
      let messages = request.messages;
      if (session?.shouldCompact) {
        session = await this.compactSession(session, model, request.messages, signal);
        messages = this.withSummary(session.summary, request.messages);
      }
      const fingerprint = session ? contextFingerprint(messages, request.tools, this.config.snapshot().tools_passthrough) : "";
      const prepared = prepareMessages(
        messages,
        request.tools,
        this.config.snapshot().tools_passthrough,
        session ? this.sessions.cachedQuery(session, fingerprint) : undefined,
      );
      const client = new MimoClient(account);
      const media = await uploadSources(client, prepared.media, model, signal);
      const decoder = new SemanticDecoder(prepared.tools, toolChoiceRequired(request.toolChoice));
      let usageSeen = false;
      let finalUsage = { inputTokens: 0, outputTokens: 0, totalTokens: 0 };
      for await (const upstream of client.stream({
        query: prepared.query,
        model,
        thinking: request.thinking ?? shouldThink(request.reasoningEffort),
        media,
        conversationId: session?.conversationId,
      }, signal)) {
        if (upstream.type === "text") {
          for (const event of decoder.push(upstream.text)) yield event;
        } else {
          usageSeen = true;
          finalUsage = {
            inputTokens: upstream.inputTokens,
            outputTokens: upstream.outputTokens,
            totalTokens: upstream.totalTokens,
          };
          yield {
            type: "usage",
            usage: finalUsage,
          };
        }
      }
      if (session) this.sessions.rememberContext(session, fingerprint, prepared.query, request.messages);
      for (const event of decoder.flush()) yield event;
      if (usageSeen) {
        this.usage?.record(model, finalUsage);
        if (session) this.sessions.recordUsage(session, finalUsage.inputTokens);
      }
      if (!usageSeen) yield { type: "usage", usage: { inputTokens: 0, outputTokens: 0, totalTokens: 0 } };
    } finally {
      accountLease?.release();
      releaseSession?.();
    }
  }

  capacityStatus(): object {
    return { ...this.accounts.status(), ...this.#sessionQueue.status() };
  }

  close(): void {
    this.#sessionQueue.close();
    this.accounts.close();
  }

  async #acquireFreshAccount(
    preferredUserId: string | undefined,
    signal: AbortSignal,
  ): Promise<{ lease: AccountLease; account: AccountLease["account"] }> {
    let preferred = preferredUserId;
    while (true) {
      const preferredAccount = preferred ? this.config.accountByUserId(preferred) : undefined;
      const lease = preferredAccount
        ? await this.accounts.acquire(preferredAccount, signal)
        : await this.accounts.acquireAny(this.config.usableAccounts(), signal);
      const current = this.config.accountByUserId(lease.account.user_id);
      if (current) return { lease, account: current };
      lease.release();
      preferred = undefined;
    }
  }

  private async compactSession(
    session: ConversationSession,
    model: string,
    messages: ProtocolMessage[],
    signal: AbortSignal,
  ): Promise<ConversationSession> {
    const client = new MimoClient(session.account);
    const parts: string[] = [];
    for await (const event of client.stream({
      query: "Compact the current conversation for a new context window. Preserve user requirements, decisions, identifiers, tool results, unresolved work, and constraints. Return only the compacted context.",
      model,
      thinking: false,
      conversationId: session.conversationId,
    }, signal)) if (event.type === "text") parts.push(event.text);
    const summary = parts.join("").trim();
    if (!summary) throw new ApiError(502, "session_compaction_failed", "MiMo session compaction returned no text");
    return this.sessions.rotateAfterCompaction(session, summary);
  }

  private withSummary(summary: string, messages: ProtocolMessage[]): ProtocolMessage[] {
    return [{ role: "system", content: `Compacted conversation context:\n${summary}` }, ...messages];
  }
}

export const validateModel = (model: string): string => {
  if (/(?:tts|asr|speech|audio|voice(?:clone|design)?)/i.test(model)) {
    throw new ApiError(400, "unsupported_model", `audio model ${model} is not supported`, "model");
  }
  return model;
};

const shouldThink = (effort: string | undefined): boolean => (
  Boolean(effort) && !["none", "minimal"].includes(effort!.toLowerCase())
);

const toolChoiceRequired = (choice: unknown): boolean => {
  if (typeof choice === "string") return ["required", "any"].includes(choice.toLowerCase());
  return Boolean(choice && typeof choice === "object" && (choice as { type?: string }).type === "function");
};
