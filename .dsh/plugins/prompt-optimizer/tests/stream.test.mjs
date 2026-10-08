import test from "node:test";
import assert from "node:assert/strict";
import { optimizeText } from "../src/optimize.js";
import { createRequestHandler } from "../src/host.js";
import { readOptimizerResponse } from "../src/client.js";
import { createOptimizerController } from "../src/core.js";

const input = { provider: "example", model: "example", text: "原文：金额100元，不要联网，路径 /src/a.js" };
const stop = { type: "finish", reason: { kind: "stop" } };
const text = value => ({ type: "text-delta", index: 0, text: value });
const req = () => new Request("http://localhost/api/prompt-optimizer", {
  method: "POST", headers: { "content-type": "application/json", accept: "application/x-ndjson" },
  body: JSON.stringify(input),
});
function deferred() {
  let resolve;
  const promise = new Promise(yes => { resolve = yes; });
  return { promise, resolve };
}
function adapter(chunks) {
  return { async prepareCall(route) { return { config: route, async *stream() { yield* chunks; } }; } };
}
function response(records) {
  return new Response(records.map(value => JSON.stringify(value) + "\n").join(""), {
    headers: { "content-type": "application/x-ndjson" },
  });
}
test("model progress contains only bounded plain-text counts, canonical blocks do not double count", async () => {
  const values = [];
  const result = await optimizeText(adapter([
    { type: "reasoning-delta", index: 1, text: "private thought" },
    text("你好"),
    { type: "block-end", index: 0, block: { type: "text", text: "你好" } },
    { type: "block-end", index: 0, block: { type: "text", text: "修改" } },
    text("！"), stop,
  ]), input, undefined, value => values.push(value));
  assert.deepEqual(values, [
    { phase: "preparing", outputChars: 0 }, { phase: "waiting", outputChars: 0 },
    { phase: "generating", outputChars: 2 }, { phase: "generating", outputChars: 2 },
    { phase: "generating", outputChars: 3 },
  ]);
  assert.equal(result.text, "修改！");
  assert.ok(!JSON.stringify(values).includes("private"));
});
test("streaming route delivers real progress before completion, then one validated result", async () => {
  const gate = deferred();
  const handler = createRequestHandler({
    async prepareCall(route) { return { config: route, async *stream() {
      yield text("优化正文"); await gate.promise; yield stop;
    } }; },
  });
  const res = await handler.fetch(req());
  assert.match(res.headers.get("content-type"), /x-ndjson/);
  assert.equal(res.headers.get("cache-control"), "no-store");
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  const progress = [];
  while (!progress.some(value => value.phase === "generating")) {
    progress.push(JSON.parse(decoder.decode((await reader.read()).value)));
  }
  assert.equal(handler.activeCount, 1);
  assert.ok(progress.every(value => !("text" in value)));
  gate.resolve();
  const final = JSON.parse(decoder.decode((await reader.read()).value));
  assert.equal(final.type, "result");
  assert.equal(final.text, "优化正文");
  assert.equal((await reader.read()).done, true);
  assert.equal(handler.activeCount, 0);
});
test("streaming errors are redacted and never become partial results", async () => {
  const handler = createRequestHandler(adapter([text("partial"), {
    type: "finish", reason: { kind: "error", failure: { code: "MISSING_CREDENTIAL", message: "secret API key" } },
  }]));
  const res = await handler.fetch(req());
  const records = (await res.text()).trim().split("\n").map(JSON.parse);
  assert.equal(records.at(-1).type, "error");
  assert.equal(records.at(-1).error.code, "MISSING_CREDENTIAL");
  assert.ok(records.every(value => value.type !== "result"));
  assert.ok(!JSON.stringify(records).includes("secret"));
});
test("streaming timeout closes promptly while non-cooperative calls retain their slot", async () => {
  const gate = deferred();
  const handler = createRequestHandler({ prepareCall: () => gate.promise }, { timeoutMs: 15, maxConcurrent: 1 });
  const res = await handler.fetch(req());
  await assert.rejects(readOptimizerResponse(res), /超时/);
  assert.equal(handler.activeCount, 1);
  assert.equal((await handler.fetch(req())).status, 429);
  gate.resolve({ config: input, async *stream() {} });
  await new Promise(yes => setImmediate(yes));
  assert.equal(handler.activeCount, 0);
});
test("cancelling the response body aborts the isolated model call", async () => {
  let captured, started = deferred();
  const handler = createRequestHandler({
    async prepareCall(route, signal) {
      captured = signal;
      return { config: route, async *stream() {
        started.resolve();
        await new Promise(yes => signal.addEventListener("abort", yes, { once: true }));
        signal.throwIfAborted();
      } };
    },
  });
  const res = await handler.fetch(req());
  await started.promise;
  await res.body.cancel();
  await new Promise(yes => setImmediate(yes));
  assert.equal(captured.aborted, true);
  assert.equal(handler.activeCount, 0);
});
test("stream parser handles UTF-8 split at every byte without exposing intermediate text", async () => {
  const bytes = new TextEncoder().encode([
    { type: "progress", phase: "generating", outputChars: 2 },
    { type: "result", text: "你好😀", provider: input.provider, model: input.model },
  ].map(value => JSON.stringify(value) + "\n").join(""));
  const res = new Response(new ReadableStream({ start(writer) {
    for (const byte of bytes) writer.enqueue(Uint8Array.of(byte));
    writer.close();
  } }), { headers: { "content-type": "application/x-ndjson" } });
  const progress = [];
  assert.equal((await readOptimizerResponse(res, undefined, value => progress.push(value))).text, "你好😀");
  assert.deepEqual(progress, [{ phase: "generating", outputChars: 2 }]);
});
test("stream parser rejects missing terminal records, malformed records and records after result", async () => {
  for (const values of [
    [], [{ type: "progress", phase: "waiting", outputChars: 0 }],
    [{ type: "unknown" }],
    [{ type: "progress", phase: "generating", outputChars: 32001 }],
    [{ type: "progress", phase: "fake", outputChars: 1 }],
    [{ type: "result", text: "" }],
    [{ type: "result", text: "x".repeat(32001) }],
    [{ type: "result", text: "valid" }, { type: "progress", phase: "waiting", outputChars: 0 }],
    [{ type: "result", text: "valid" }, { type: "result", text: "extra" }],
  ]) await assert.rejects(readOptimizerResponse(response(values)));
  await assert.rejects(readOptimizerResponse(new Response('{"type":', { headers: { "content-type": "application/x-ndjson" } })));
});
test("stream parser keeps backward JSON compatibility and authentication errors", async () => {
  assert.equal((await readOptimizerResponse(new Response('{"text":"legacy"}'))).text, "legacy");
  for (const status of [401, 403]) await assert.rejects(readOptimizerResponse(new Response("", { status })), /连接验证/);
  await assert.rejects(readOptimizerResponse(new Response('{"error":{"message":"safe error"}}', { status: 502 })), /safe error/);
});
test("stream parser checks its byte bound and cancellation on stalled reads", async () => {
  await assert.rejects(readOptimizerResponse(new Response(" ".repeat(524289), {
    headers: { "content-type": "application/x-ndjson" },
  })));
  let cancelled = false;
  const res = new Response(new ReadableStream({
    cancel() { cancelled = true; },
  }), { headers: { "content-type": "application/x-ndjson" } });
  const controller = new AbortController();
  const promise = readOptimizerResponse(res, controller.signal);
  controller.abort();
  await assert.rejects(promise);
  assert.equal(cancelled, true);
});
test("progress never edits a draft; final success is one atomic editor/undo operation", async () => {
  const gate = deferred();
  let emit, edits = 0, draft = { draft: "before", draftRev: 1, phase: "plain", occurrences: [] };
  const controller = createOptimizerController({
    readInput: () => draft, readModel: () => input,
    request: async (_, signal, onProgress) => { emit = onProgress; return gate.promise; },
    replace: value => { edits++; draft = { ...draft, draft: value, draftRev: 2 }; return true; },
  });
  const pending = controller.optimize();
  emit({ phase: "generating", outputChars: 123 });
  assert.equal(controller.getSnapshot().outputChars, 123);
  assert.equal(draft.draft, "before");
  assert.equal(edits, 0);
  gate.resolve({ text: "after" });
  assert.equal(await pending, true);
  assert.equal(edits, 1);
  assert.equal(controller.getSnapshot().count, 1);
  controller.dispose();
});
test("late progress after cancel or new request cannot revive an old operation", async () => {
  const requests = [];
  const controller = createOptimizerController({
    readInput: () => ({ draft: "before", draftRev: 1, phase: "plain", occurrences: [] }),
    readModel: () => input, replace: () => { throw Error("must not edit"); },
    request: async (_, signal, emit) => {
      const gate = deferred(); requests.push({ gate, emit }); return gate.promise;
    },
  });
  const first = controller.optimize();
  controller.cancel();
  const second = controller.optimize();
  requests[0].emit({ phase: "generating", outputChars: 999 });
  assert.equal(controller.getSnapshot().outputChars, 0);
  requests[0].gate.resolve({ text: "old" });
  await first;
  controller.cancel();
  requests[1].gate.resolve({ text: "new" });
  await second;
  assert.equal(controller.getSnapshot().busy, false);
  assert.equal(controller.getSnapshot().count, 0);
});
test("JSON and NDJSON error codes reach hover feedback, legacy errors remain readable", async () => {
  const error = { code: "INVALID_REQUEST", message: "模型服务拒绝优化请求，原文已保留" };
  for (const res of [
    new Response(JSON.stringify({ error }), { status: 502 }),
    response([{ type: "error", error }]),
  ]) {
    await assert.rejects(readOptimizerResponse(res), value =>
      value.code === "INVALID_REQUEST" && value.message === "[INVALID_REQUEST] " + error.message);
  }
  await assert.rejects(readOptimizerResponse(response([{ type: "error", error: { message: "legacy" } }])), value =>
    value.code === undefined && value.message === "legacy");
  await assert.rejects(readOptimizerResponse(response([{ type: "error", error: { code: "bad\\ncode", message: "safe" } }])), value =>
    value.code === undefined && value.message === "safe");
});
test("DeepSeek failure diagnostics leave the draft and undo history untouched", async () => {
  let replacements = 0;
  const controller = createOptimizerController({
    readInput: () => ({ draft: "原文", draftRev: 1, phase: "plain", occurrences: [] }),
    readModel: () => ({ provider: "deepseek-official", model: "deepseek-flash" }),
    replace: () => { replacements++; return true; },
    request: () => readOptimizerResponse(response([{ type: "error", error: { code: "REQUEST_EXTENSION", message: "请求扩展失败" } }])),
  });
  assert.equal(await controller.optimize(), false);
  assert.equal(replacements, 0);
  assert.equal(controller.getSnapshot().count, 0);
  assert.equal(controller.getSnapshot().busy, false);
  assert.equal(controller.getSnapshot().message, "[REQUEST_EXTENSION] 请求扩展失败");
});

test("end-to-end streaming truncation rejects instead of replacing the input", async () => {
  const handler = createRequestHandler(adapter([text("partial")]));
  const progress = [];
  await assert.rejects(readOptimizerResponse(await handler.fetch(req()), undefined, value => progress.push(value)), /完整结束|中断/);
  assert.ok(progress.some(value => value.phase === "generating"));
});
