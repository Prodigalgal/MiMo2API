import { describe, expect, it } from "vitest";
import { decodeSse } from "../src/mimo/sse.js";

const streamFrom = (chunks: string[]): ReadableStream<Uint8Array> => new ReadableStream({
  start(controller) {
    const encoder = new TextEncoder();
    for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
    controller.close();
  },
});

describe("MiMo SSE decoding", () => {
  it("preserves the terminal marker for the client to terminate the upstream stream", async () => {
    const received: string[] = [];
    for await (const data of decodeSse(
      streamFrom(["data: {\"type\":\"text\",\"content\":\"hello\"}\n\ndata: [DONE]\n\n"]),
      new AbortController().signal,
      1_000,
    )) received.push(data);
    expect(received).toEqual(['{"type":"text","content":"hello"}', "[DONE]"]);
  });

  it("does not treat comment heartbeats as upstream activity", async () => {
    let timer: NodeJS.Timeout | undefined;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        const encoder = new TextEncoder();
        timer = setInterval(() => controller.enqueue(encoder.encode(": ping\n\n")), 2);
      },
      cancel() { if (timer) clearInterval(timer); },
    });
    const iterator = decodeSse(stream, new AbortController().signal, 20);
    await expect(iterator.next()).rejects.toMatchObject({ code: "upstream_idle_timeout" });
  });
});
