import { randomUUID } from "node:crypto";
import { fetch } from "undici";
import type { MimoAccount } from "../config/types.js";
import { ApiError } from "../core/errors.js";
import { decodeSse } from "./sse.js";

export interface MimoMedia {
  mediaType: "image" | "file";
  fileUrl: string;
  compressedVideoUrl: string;
  audioTrackUrl: string;
  name: string;
  size: number;
  status: "completed";
  objectName: string;
  tokenUsage: number;
  url: string;
}

export interface MimoRequest {
  query: string;
  model: string;
  thinking: boolean;
  media?: MimoMedia[];
  conversationId?: string;
}

export type MimoEvent =
  | { type: "text"; text: string }
  | { type: "usage"; inputTokens: number; outputTokens: number; totalTokens: number };

const MIMO_PREFIXES = new Set([
  "webSearch", "getTime", "getTimeInfo", "sessionSearch", "imageSearch",
  "fileSearch", "getLocation", "webExtract", "getWeather", "calculator",
]);

export class MimoClient {
  static readonly chatUrl = "https://aistudio.xiaomimimo.com/open-apis/bot/chat";
  readonly requestTimeoutMs = Number(process.env.MIMO2API_REQUEST_TIMEOUT_MS ?? 600_000);
  readonly idleTimeoutMs = Number(process.env.MIMO2API_STREAM_IDLE_TIMEOUT_MS ?? 180_000);

  constructor(readonly account: MimoAccount) {}

  headers(contentType = "application/json"): Record<string, string> {
    return {
      Accept: "*/*",
      "Content-Type": contentType,
      Origin: "https://aistudio.xiaomimimo.com",
      Referer: "https://aistudio.xiaomimimo.com/",
      "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/143 Safari/537.36",
      "x-timezone": "Asia/Shanghai",
      Cookie: this.cookie(),
    };
  }

  cookie(): string {
    return [
      `serviceToken=${this.account.service_token}`,
      `userId=${this.account.user_id}`,
      `xiaomichatbot_ph=${this.account.xiaomichatbot_ph}`,
    ].join("; ");
  }

  async *stream(request: MimoRequest, signal: AbortSignal): AsyncGenerator<MimoEvent> {
    const timeout = AbortSignal.timeout(this.requestTimeoutMs);
    const combined = AbortSignal.any([signal, timeout]);
    const url = new URL(MimoClient.chatUrl);
    url.searchParams.set("xiaomichatbot_ph", this.account.xiaomichatbot_ph);
    const response = await fetch(url, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({
        msgId: randomUUID().replaceAll("-", ""),
        conversationId: request.conversationId ?? randomUUID().replaceAll("-", ""),
        query: request.query,
        modelConfig: {
          enableThinking: request.thinking,
          temperature: 0.8,
          topP: 0.95,
          webSearchStatus: "disabled",
          model: request.model,
        },
        multiMedias: request.media ?? [],
        attachments: [],
      }),
      signal: combined,
    });
    if (!response.ok || !response.body) {
      const body = (await response.text()).slice(0, 1_000);
      throw new ApiError(response.status || 502, "mimo_upstream_error", `MiMo returned HTTP ${response.status}: ${body}`);
    }

    for await (const raw of decodeSse(
      response.body as unknown as ReadableStream<Uint8Array>,
      combined,
      this.idleTimeoutMs,
    )) {
      if (!raw) continue;
      // MiMo can keep the HTTP connection open after sending the SSE terminal marker.
      // The marker is the completion boundary; waiting for socket close leaves tool calls hanging.
      if (raw === "[DONE]") return;
      let event: unknown;
      try {
        event = JSON.parse(raw);
      } catch {
        continue;
      }
      if (!event || Array.isArray(event) || typeof event !== "object") continue;
      const data = event as Record<string, unknown>;
      if (data.type === "text" && typeof data.content === "string" && data.content) {
        if (!MIMO_PREFIXES.has(data.content.trim())) yield { type: "text", text: data.content };
      } else if (typeof data.promptTokens === "number") {
        const inputTokens = data.promptTokens;
        const outputTokens = Number(data.completionTokens ?? 0);
        yield {
          type: "usage",
          inputTokens,
          outputTokens,
          totalTokens: Number(data.totalTokens ?? inputTokens + outputTokens),
        };
      }
    }
  }
}
