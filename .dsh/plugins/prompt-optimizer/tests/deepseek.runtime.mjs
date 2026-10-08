// Offline integration with the serving DSH's real DeepSeek Messages adapter.
// Synthetic SSE/HTTP only: no network, credentials, chat, or profile writes.
import { createRequire, registerHooks } from "node:module";
import { pathToFileURL } from "node:url";
import assert from "node:assert/strict";
import { createRequestHandler } from "../src/host.js";
import { readOptimizerResponse } from "../src/client.js";
import { createOptimizerController } from "../src/core.js";

const runtimeRequire = createRequire(process.env.DSH_TEST_RUNTIME || "/Applications/DeepSeek Harness.app/Contents/Resources/app.asar/dsh/package.json");
let resolving = false;
registerHooks({ resolve(specifier, context, next) {
  if (specifier.startsWith("@deepseek-ai/") && !resolving) {
    resolving = true;
    try { return { url: pathToFileURL(runtimeRequire.resolve(specifier)).href, shortCircuit: true }; }
    finally { resolving = false; }
  }
  return next(specifier, context);
} });
const { Context } = await import("@deepseek-ai/cordis");
const { LlmRuntime } = await import("@deepseek-ai/dsh-llm");
const { DeepSeekAdapter, resolveAdapterOptions } = await import("@deepseek-ai/dsh-llm-deepseek");
const llm = new LlmRuntime(new Context());
const connection = resolveAdapterOptions({
  baseURL: "https://synthetic-deepseek.invalid/anthropic", maxTokens: 4096,
  models: [{ id: "deepseek-flash" }, { id: "deepseek-v4-pro" }],
});
const privateThought = "hidden-reasoning-must-not-leave-the-stream";
const output = "写一个 Python 脚本读取 data.csv；不要联网。";
const events = [
  { type: "message_start", message: { usage: { input_tokens: 12, output_tokens: 0 } } },
  { type: "content_block_start", index: 0, content_block: { type: "thinking", thinking: "" } },
  { type: "content_block_delta", index: 0, delta: { type: "thinking_delta", thinking: privateThought } },
  { type: "content_block_stop", index: 0 },
  { type: "content_block_start", index: 1, content_block: { type: "text", text: "" } },
  { type: "content_block_delta", index: 1, delta: { type: "text_delta", text: output } },
  { type: "content_block_stop", index: 1 },
  { type: "message_delta", delta: { stop_reason: "end_turn" }, usage: { output_tokens: 20 } },
  { type: "message_stop" },
];
let mode = "success";
const requests = [];
const sessionIds = [];
const nativeFetch = globalThis.fetch;
globalThis.fetch = async (url, options) => {
  assert.equal(url, "https://synthetic-deepseek.invalid/anthropic/v1/messages");
  const body = JSON.parse(options.body);
  requests.push(body);
  sessionIds.push(options.headers["x-deepseek-harness-session-id"]);
  if (mode === "rejected") return new Response(JSON.stringify({ error: { type: "invalid_request_error", message: "private-provider-detail" } }), { status: 400 });
  const sequence = mode === "truncated" ? events.slice(0, 7) : events;
  return new Response(sequence.map(event => "event: " + event.type + "\ndata: " + JSON.stringify(event) + "\n\n").join(""), {
    headers: { "content-type": "text/event-stream" },
  });
};
const unregister = llm.registerAdapter(["deepseek-official"], new DeepSeekAdapter({
  options: () => connection,
  resolveAuth: async () => ({ headers: { "x-api-key": "synthetic-not-a-real-key" } }),
  resolveUserId: () => "synthetic-test",
  prepareExtensions: async () => ({ fields: {}, accept: async () => {} }),
}));
const handler = createRequestHandler(llm);
const request = (model, text = "写 Python 读取 data.csv，不要联网") => new Request("http://localhost/api/prompt-optimizer", {
  method: "POST", headers: { "content-type": "application/json", accept: "application/x-ndjson" },
  body: JSON.stringify({ provider: "deepseek-official", model, text }),
});
try {
  for (const model of ["deepseek-flash", "deepseek-v4-pro"]) {
    const progress = [];
    const result = await readOptimizerResponse(await handler.fetch(request(model)), undefined, value => progress.push(value));
    assert.equal(result.text, output);
    assert.equal(result.provider, "deepseek-official");
    assert.equal(result.model, model);
    assert.ok(!JSON.stringify(progress).includes(privateThought));
    assert.equal(progress.findLast(value => value.phase === "generating").outputChars, output.length);
    const body = requests.at(-1);
    assert.equal(body.model, model);
    assert.deepEqual(body.tools, []);
    assert.equal(body.output_config.effort, "low");
    assert.equal(body.max_tokens, 4096);
  }
  // Follow the actual edited draft through three independent prepared calls.
  // The fixed fixture optimizes once, then legitimately returns the same text.
  let draft = { draft: "写 Python 读取 data.csv，不要联网", draftRev: 1, phase: "plain", occurrences: [] };
  let edits = 0;
  const controller = createOptimizerController({
    readInput: () => draft,
    readModel: () => ({ provider: "deepseek-official", model: "deepseek-flash" }),
    replace(text, span) {
      assert.equal(span.draftRev, draft.draftRev);
      draft = { ...draft, draft: text, draftRev: draft.draftRev + 1 };
      edits++;
      return true;
    },
    request: async (payload, signal, progress) => readOptimizerResponse(await handler.fetch(request(payload.model, payload.text)), signal, progress),
  });
  for (let n = 0; n < 3; n++) {
    assert.equal(await controller.optimize(), true);
    assert.equal(draft.draft, output);
    assert.equal(controller.getSnapshot().count, 1);
    assert.equal(controller.getSnapshot().busy, false);
    assert.equal(handler.activeCount, 0);
  }
  assert.equal(edits, 1);
  assert.deepEqual(requests.slice(2).map(body => JSON.parse(body.messages[0].content[0].text).text), ["写 Python 读取 data.csv，不要联网", output, output]);
  assert.match(controller.getSnapshot().message, /优化完成.*未作进一步修改/);
  controller.dispose();
  mode = "truncated";
  await assert.rejects(readOptimizerResponse(await handler.fetch(request("deepseek-flash"))), error => error.code === "STREAM_CLOSED");
  mode = "rejected";
  await assert.rejects(readOptimizerResponse(await handler.fetch(request("deepseek-flash"))), error =>
    error.code === "INVALID_REQUEST" && error.message.includes("[INVALID_REQUEST]") && !error.message.includes("private-provider-detail"));
  assert.equal(requests.length, 7); // No retries, fallback, or tools.
  assert.equal(new Set(sessionIds).size, 7);
  console.log("PASS: real DeepSeek adapter -> optimizer HTTP -> client/controller; Flash/Pro routing, three consecutive independent calls, unchanged completion/undo, reasoning filtering, rejection/truncation diagnostics; no network/chat writes");
} finally { handler.dispose(); unregister(); globalThis.fetch = nativeFetch; }
