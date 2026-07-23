import { ApiError } from "../core/errors.js";

export async function* decodeSse(
  body: ReadableStream<Uint8Array>,
  signal: AbortSignal,
  idleTimeoutMs: number,
): AsyncGenerator<string> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let dataLines: string[] = [];
  let lastDataAt = Date.now();

  const dispatchLine = (line: string): string | undefined => {
    if (line === "") {
      const data = dataLines.length > 0 ? dataLines.join("\n") : undefined;
      dataLines = [];
      return data;
    }
    if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
    return undefined;
  };

  try {
    while (true) {
      const remainingIdleMs = idleTimeoutMs - (Date.now() - lastDataAt);
      if (remainingIdleMs <= 0) {
        throw new ApiError(504, "upstream_idle_timeout", "MiMo stream became idle");
      }
      const result = await readWithTimeout(reader, signal, remainingIdleMs);
      if (result.done) break;
      buffer += decoder.decode(result.value, { stream: true });
      let newline = buffer.indexOf("\n");
      while (newline >= 0) {
        const line = buffer.slice(0, newline).replace(/\r$/, "");
        buffer = buffer.slice(newline + 1);
        const data = dispatchLine(line);
        if (data !== undefined) {
          if (data.trim()) lastDataAt = Date.now();
          yield data;
        }
        newline = buffer.indexOf("\n");
      }
    }
    buffer += decoder.decode();
    if (buffer) {
      const data = dispatchLine(buffer.replace(/\r$/, ""));
      if (data !== undefined) {
        if (data.trim()) lastDataAt = Date.now();
        yield data;
      }
    }
    const remaining = dispatchLine("");
    if (remaining !== undefined) {
      if (remaining.trim()) lastDataAt = Date.now();
      yield remaining;
    }
  } catch (error) {
    await reader.cancel(error).catch(() => undefined);
    throw error;
  } finally {
    try { reader.releaseLock(); } catch { /* reader may still be cancelling */ }
  }
}

async function readWithTimeout(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  signal: AbortSignal,
  timeoutMs: number,
): Promise<ReadableStreamReadResult<Uint8Array>> {
  if (signal.aborted) throw signal.reason ?? new DOMException("Aborted", "AbortError");
  let timer: NodeJS.Timeout | undefined;
  let abortHandler: (() => void) | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new ApiError(504, "upstream_idle_timeout", "MiMo stream became idle")), timeoutMs);
  });
  const aborted = new Promise<never>((_, reject) => {
    abortHandler = () => reject(signal.reason ?? new DOMException("Aborted", "AbortError"));
    signal.addEventListener("abort", abortHandler, { once: true });
  });
  try {
    return await Promise.race([reader.read(), timeout, aborted]);
  } finally {
    if (timer) clearTimeout(timer);
    if (abortHandler) signal.removeEventListener("abort", abortHandler);
  }
}
