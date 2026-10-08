import test from "node:test";
import assert from "node:assert/strict";
import { optimizeText, validateRequest, SYSTEM_PROMPT, MAX_OUTPUT_CHARS } from "../src/optimize.js";
import { createRequestHandler, apply } from "../src/host.js";

const input = { provider: "openai-codex", model: "example", text: "实现功能，不自动发送，金额100元，路径 /src/a.js" };
const stop = { type: "finish", reason: { kind: "stop" } };
function llm(chunks, config = {}) {
  const calls = [];
  return { calls,
    async prepareCall(route, signal) {
      calls.push({ route, signal });
      return { config: { ...route, ...config }, async *stream(options) {
        calls.push(options);
        for (const chunk of chunks) yield chunk;
      } };
    },
  };
}
function req(body = input, options = {}) {
  return new Request("http://127.0.0.1/api/prompt-optimizer", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body), ...options });
}
test("input validates empty, oversized and unexpected fields before model I/O", () => {
  for (const value of [null, [], {}, { ...input, text: " \n " }, { ...input, model: "" }, { ...input, text: "a".repeat(16001) }, { ...input, apiKey: "secret" }]) {
    assert.throws(() => validateRequest(value));
  }
  assert.deepEqual(validateRequest(input), input);
});
test("Codex route carries no unsupported sampling fields, no tools, isolated session IDs", async () => {
  const adapter = llm([{ type: "text-delta", index: 0, text: "优化后的提示词" }, stop], { reasoningEffort: "medium" });
  for (let n = 0; n < 2; n++) await optimizeText(adapter, input);
  const [first, second] = [adapter.calls[1], adapter.calls[3]];
  assert.deepEqual(adapter.calls[0].route, { provider: input.provider, model: input.model });
  assert.equal(first.temperature, undefined);
  assert.equal(first.stop, undefined);
  assert.equal(first.maxTokens, undefined);
  assert.deepEqual(first.tools, []);
  assert.notEqual(first.sessionId, second.sessionId);
  assert.equal(first.system, SYSTEM_PROMPT);
  assert.equal(JSON.parse(first.messages[0].content[0].text).text, input.text);
});
test("other model routes use adapter-normalized token and reasoning defaults", async () => {
  const adapter = llm([{ type: "block-end", index: 0, block: { type: "text", text: "改写" } }, stop], { maxTokens: 4096 });
  const output = await optimizeText(adapter, { ...input, provider: "generic-provider" });
  assert.equal(output.text, "改写");
  assert.equal(adapter.calls[1].maxTokens, 4096);
  assert.equal(adapter.calls[1].reasoningEffort, undefined);
});
test("quick optimization uses only the exact model's advertised lightweight reasoning", async () => {
  for (const [ids, expected] of [
    [["high", "medium", "low"], "low"],
    [["minimal", "low", "high"], "minimal"],
    [["low", "none"], "none"],
    [["provider-custom"], undefined],
    [[], undefined],
  ]) {
    const adapter = llm([{ type: "text-delta", index: 0, text: "改写" }, stop]);
    const capabilityCalls = [];
    adapter.resolveModelInfo = async (...args) => {
      capabilityCalls.push(args);
      return { reasoning: { efforts: ids.map(id => ({ id })), defaultEffort: ids[0] } };
    };
    const signal = new AbortController().signal;
    await optimizeText(adapter, input, signal);
    assert.deepEqual(capabilityCalls, [[input.provider, input.model, signal]]);
    assert.equal(adapter.calls[0].route.reasoningEffort, expected);
    assert.equal(adapter.calls[1].reasoningEffort, expected);
    assert.equal(adapter.calls[1].provider, input.provider);
    assert.equal(adapter.calls[1].model, input.model);
    for (const key of ["temperature", "maxTokens", "stop"]) assert.equal(adapter.calls[1][key], undefined);
  }
});
test("models without reasoning metadata retain defaults and no speculative effort", async () => {
  const adapter = llm([{ type: "text-delta", index: 0, text: "改写" }, stop], { maxTokens: 4096 });
  adapter.resolveModelInfo = async () => ({ provider: input.provider, id: input.model });
  await optimizeText(adapter, input);
  assert.equal(adapter.calls[0].route.reasoningEffort, undefined);
  assert.equal(adapter.calls[1].maxTokens, 4096);
});
test("capability changes retry preparation only, never inference or a different route", async () => {
  let preparations = 0, streams = 0;
  const adapter = {
    resolveModelInfo: async () => ({ reasoning: { efforts: [{ id: "low" }] } }),
    prepareCall: async route => {
      preparations++;
      if (route.reasoningEffort) throw Object.assign(new Error("stale capability"), { code: "UNSUPPORTED_REASONING_EFFORT" });
      assert.deepEqual(route, { provider: input.provider, model: input.model });
      return { config: route, async *stream() { streams++; yield { type: "text-delta", index: 0, text: "改写" }; yield stop; } };
    },
  };
  await optimizeText(adapter, input);
  assert.equal(preparations, 2);
  assert.equal(streams, 1);
});
test("capability lookup failure is redacted and does not dispatch inference", async () => {
  const adapter = llm([]);
  adapter.resolveModelInfo = async () => { throw Object.assign(new Error("secret credential"), { code: "MISSING_CREDENTIAL" }); };
  await assert.rejects(optimizeText(adapter, input), error => error.code === "MISSING_CREDENTIAL" && !error.message.includes("secret"));
  assert.equal(adapter.calls.length, 0);
});
test("cancellation during capability lookup prevents model preparation", async () => {
  const controller = new AbortController();
  const adapter = llm([]);
  adapter.resolveModelInfo = async () => { controller.abort(); return { reasoning: { efforts: [{ id: "low" }] } }; };
  await assert.rejects(optimizeText(adapter, input, controller.signal));
  assert.equal(adapter.calls.length, 0);
});
test("canonical block ends replace deltas, reasoning is never returned", async () => {
  const adapter = llm([
    { type: "reasoning-delta", index: 0, text: "hidden thought" },
    { type: "block-end", index: 0, block: { type: "reasoning", text: "hidden thought" } },
    { type: "text-delta", index: 2, text: "world" },
    { type: "text-delta", index: 1, text: "hello " },
    { type: "block-end", index: 1, block: { type: "text", text: "hello " } },
    { type: "block-end", index: 2, block: { type: "text", text: "world" } }, stop,
  ]);
  assert.equal((await optimizeText(adapter, input)).text, "hello world");
});
test("empty, truncated, aborted, failed and tool-call output never succeeds", async () => {
  const cases = [
    [stop],
    [{ type: "text-delta", index: 0, text: "partial" }],
    [{ type: "text-delta", index: 0, text: "partial" }, { type: "finish", reason: { kind: "max-tokens" } }],
    [{ type: "finish", reason: { kind: "error", failure: { code: "RATE_LIMIT" } } }],
    [{ type: "finish", reason: { kind: "aborted", failure: { code: "CANCELLED" } } }],
    [{ type: "tool-call-delta", index: 0, id: "call", name: "bash", argumentsDelta: "{}" }, stop],
    [{ type: "block-end", index: 0, block: { type: "tool-call", name: "bash" } }, stop],
    [{ type: "text-delta", index: 0, text: "a".repeat(MAX_OUTPUT_CHARS + 1) }, stop],
  ];
  for (const chunks of cases) await assert.rejects(optimizeText(llm(chunks), input));
});
test("provider failure text is redacted, credentials are never echoed", async () => {
  const secret = "api-key-private";
  const failed = { prepareCall: async () => { throw Object.assign(new Error(secret), { code: "MISSING_CREDENTIAL" }); } };
  await assert.rejects(optimizeText(failed, input), error => !error.message.includes(secret) && error.code === "MISSING_CREDENTIAL");
});
test("aborted signal rejects before model request", async () => {
  const controller = new AbortController();
  controller.abort();
  const adapter = llm([]);
  await assert.rejects(optimizeText(adapter, input, controller.signal));
  assert.equal(adapter.calls.length, 0);
});
test("HTTP endpoint accepts JSON and returns no-store results", async () => {
  const handler = createRequestHandler(llm([{ type: "text-delta", index: 0, text: "better" }, stop]));
  const response = await handler.fetch(req());
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.equal((await response.json()).text, "better");
  assert.equal(handler.activeCount, 0);
});
test("HTTP endpoint rejects invalid JSON, non-JSON, wrong method and oversized bodies", async () => {
  const handler = createRequestHandler(llm([]));
  assert.equal((await handler.fetch(req({}, { body: "{" }))).status, 400);
  assert.equal((await handler.fetch(req({}, { headers: { "content-type": "text/plain" } }))).status, 415);
  assert.equal((await handler.fetch(new Request("http://localhost/api/prompt-optimizer"))).status, 405);
  assert.equal((await handler.fetch(req({}, { body: "a".repeat(128001) }))).status, 413);
});
test("HTTP timeout returns promptly while non-cooperative calls retain concurrency slot", async () => {
  let resolve;
  const pending = new Promise(yes => { resolve = yes; });
  const adapter = { prepareCall: () => pending };
  const handler = createRequestHandler(adapter, { timeoutMs: 15, maxConcurrent: 1 });
  const response = await handler.fetch(req());
  assert.equal(response.status, 504);
  assert.equal(handler.activeCount, 1);
  assert.equal((await handler.fetch(req())).status, 429);
  resolve({ config: {}, async *stream() {} });
  await new Promise(yes => setImmediate(yes));
  assert.equal(handler.activeCount, 0);
});
test("disconnect and unload cancel calls and avoid exposing raw unexpected errors", async () => {
  const adapter = { prepareCall: async (_, signal) => {
    await new Promise((_, reject) => signal.addEventListener("abort", () => reject(signal.reason), { once: true }));
  } };
  const handler = createRequestHandler(adapter);
  const controller = new AbortController();
  const work = handler.fetch(req(input, { signal: controller.signal }));
  await new Promise(yes => setImmediate(yes));
  controller.abort();
  assert.equal((await work).status, 499);
  handler.dispose();
  assert.equal((await handler.fetch(req())).status, 503);
});
test("DeepSeek failure categories stay actionable while arbitrary backend data is redacted", async () => {
  for (const code of ["AUTH", "INVALID_REQUEST", "UNSUPPORTED_REASONING_EFFORT", "REQUEST_EXTENSION", "STREAM_CLOSED", "TRANSPORT"]) {
    const failure = { type: "finish", reason: { kind: "error", failure: { code, message: "private credential/url" } } };
    await assert.rejects(optimizeText(llm([failure]), { ...input, provider: "deepseek-official" }), error =>
      error.code === code && !error.message.includes("private") && !error.message.includes("模型调用失败"));
  }
  const unknown = { type: "finish", reason: { kind: "error", failure: { code: "private credential", message: "secret" } } };
  await assert.rejects(optimizeText(llm([unknown]), input), error => error.code === "MODEL_ERROR" && !error.message.includes("secret"));
});

test("plugin registers only an authenticated Connection route and cancels on disposal", () => {
  let route, dispose;
  apply({
    llm: { prepareCall() {} },
    agents: { list: () => [] },
    on: () => () => {},
    connection: { fetch: { register(value) { route = value; } } },
    effect(callback) { dispose = callback(); },
  });
  assert.equal(route.path, "/api/prompt-optimizer");
  assert.deepEqual(route.methods, ["POST"]);
  assert.equal(route.requestBody, "streaming");
  dispose();
});
