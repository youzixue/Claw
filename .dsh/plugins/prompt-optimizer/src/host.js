import { optimizeText, OptimizerError } from "./optimize.js";
import { installTaskModelLock } from "./model-lock.js";

export const name = "local-prompt-optimizer";
export const inject = ["llm", "connection", "agents"];
export const ENDPOINT = "/api/prompt-optimizer";
const MAX_BODY_BYTES = 128000;

function json(value, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });
}

async function readBody(request, signal) {
  if (!request.headers.get("content-type")?.toLowerCase().startsWith("application/json")) {
    throw new OptimizerError("请求必须为 JSON", "INVALID_CONTENT_TYPE", 415);
  }
  const length = Number(request.headers.get("content-length"));
  if (Number.isFinite(length) && length > MAX_BODY_BYTES) {
    throw new OptimizerError("请求内容过长", "INPUT_TOO_LONG", 413);
  }
  if (!request.body) throw new OptimizerError("请求内容为空", "INVALID_REQUEST", 400);
  const reader = request.body.getReader();
  const parts = [];
  let size = 0;
  const cancel = () => { void reader.cancel().catch(() => {}); };
  signal.addEventListener("abort", cancel, { once: true });
  try {
    while (true) {
      signal.throwIfAborted();
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_BODY_BYTES) {
        cancel();
        throw new OptimizerError("请求内容过长", "INPUT_TOO_LONG", 413);
      }
      parts.push(value);
    }
    signal.throwIfAborted();
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const part of parts) { bytes.set(part, offset); offset += part.byteLength; }
    try { return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)); }
    catch { throw new OptimizerError("请求 JSON 不正确", "INVALID_REQUEST", 400); }
  } finally {
    signal.removeEventListener("abort", cancel);
    reader.releaseLock();
  }
}

export function createRequestHandler(llm, { timeoutMs = 90000, maxConcurrent = 4 } = {}) {
  const active = new Set();
  let disposed = false;
  return {
    dispose() {
      disposed = true;
      for (const controller of active) controller.abort(new OptimizerError("优化已取消", "CANCELLED", 499));
    },
    get activeCount() { return active.size; },
    async fetch(request) {
      if (request.method !== "POST") return json({ error: { message: "仅支持 POST", code: "METHOD_NOT_ALLOWED" } }, 405);
      if (disposed) return json({ error: { message: "插件已停止", code: "UNAVAILABLE" } }, 503);
      if (active.size >= maxConcurrent) return json({ error: { message: "优化请求较多，请稍后重试", code: "BUSY" } }, 429);
      const controller = new AbortController();
      const signal = controller.signal;
      active.add(controller);
      const cancel = () => controller.abort(new OptimizerError("优化已取消，原文未改动", "CANCELLED", 499));
      request.signal.addEventListener("abort", cancel, { once: true });
      if (request.signal.aborted) cancel();
      const timer = setTimeout(() => controller.abort(new OptimizerError("优化超时，原文已保留，请重试或切换模型", "TIMEOUT", 504)), timeoutMs);
      let rejectAbort;
      const aborted = new Promise((_, reject) => {
        rejectAbort = () => reject(signal.reason);
        signal.addEventListener("abort", rejectAbort, { once: true });
        if (signal.aborted) rejectAbort();
      });
      let progress = () => {};
      const work = (async () => {
        const body = await readBody(request, signal);
        return optimizeText(llm, body, signal, value => progress(value));
      })();
      // A non-cooperative adapter keeps its slot until it settles, even after HTTP timeout.
      const release = () => {
        active.delete(controller);
        clearTimeout(timer);
        signal.removeEventListener("abort", rejectAbort);
        request.signal.removeEventListener("abort", cancel);
      };
      work.then(release, release);
      const safeError = error => signal.aborted && signal.reason instanceof OptimizerError ? signal.reason
        : error instanceof OptimizerError ? error
          : new OptimizerError("优化失败，原文已保留，请重试或切换模型");
      const result = Promise.race([work, aborted]);
      if (request.headers.get("accept")?.includes("application/x-ndjson")) {
        const encoder = new TextEncoder();
        let closed = false;
        const body = new ReadableStream({
          start(writer) {
            let lastPhase, lastAt = -Infinity;
            const send = value => {
              if (closed) return;
              try { writer.enqueue(encoder.encode(JSON.stringify(value) + "\n")); }
              catch { closed = true; cancel(); }
            };
            // Counts are real model output, not invented percentages. Throttle to
            // keep slow consumers bounded without buffering/repainting every token.
            progress = value => {
              if (signal.aborted || closed) return;
              const now = performance.now();
              if (value.phase !== lastPhase || now - lastAt >= 120) {
                send({ type: "progress", ...value });
                lastPhase = value.phase;
                lastAt = now;
              }
            };
            result.then(value => send({ type: "result", ...value }), error => {
              const safe = safeError(error);
              send({ type: "error", error: { code: safe.code, message: safe.message } });
            }).finally(() => {
              if (!closed) {
                closed = true;
                try { writer.close(); } catch { cancel(); }
              }
            });
          },
          cancel() { closed = true; cancel(); },
        });
        return new Response(body, { headers: {
          "content-type": "application/x-ndjson; charset=utf-8",
          "cache-control": "no-store",
          "x-accel-buffering": "no",
        } });
      }
      try {
        return json(await result);
      } catch (error) {
        const safe = safeError(error);
        return json({ error: { code: safe.code, message: safe.message } }, safe.status);
      }
    },
  };
}

export function apply(ctx) {
  if (typeof ctx.llm?.prepareCall !== "function" || typeof ctx.connection?.fetch?.register !== "function") {
    throw new Error("Prompt optimizer requires DSH 0.2 model-call and authenticated Fetch-route contracts");
  }
  const handler = createRequestHandler(ctx.llm);
  // Connection owns login cookies, Host/Origin checks and disconnect cancellation.
  // Never add an unauthenticated webServer route or expose provider credentials.
  ctx.connection.fetch.register({ path: ENDPOINT, methods: ["POST"], requestBody: "streaming", fetch: request => handler.fetch(request) });
  ctx.effect(() => {
    const unlock = installTaskModelLock(ctx);
    return () => { unlock(); handler.dispose(); };
  }, "prompt-optimizer: task model isolation and request cancellation");
}
