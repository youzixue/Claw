// Offline integration: actual Cordis scopes, SystemPrompt and native model-selection
// waterfalls from the serving DSH runtime. No network, LLM call or live chat mutation.
import { createRequire, registerHooks } from "node:module";
import { pathToFileURL } from "node:url";
import assert from "node:assert/strict";
import { installTaskModelLock } from "../src/model-lock.js";

const runtime = process.env.DSH_TEST_RUNTIME || "/Applications/DeepSeek Harness.app/Contents/Resources/app.asar/dsh/package.json";
const runtimeRequire = createRequire(runtime);
let resolvingRuntime = false;
registerHooks({ resolve(specifier, context, next) {
  if (specifier.startsWith("@deepseek-ai/") && !resolvingRuntime) {
    resolvingRuntime = true;
    try { return { url: pathToFileURL(runtimeRequire.resolve(specifier)).href, shortCircuit: true }; }
    finally { resolvingRuntime = false; }
  }
  return next(specifier, context);
} });
const { Context } = await import("@deepseek-ai/cordis");
const { createScope } = await import("@deepseek-ai/dsh-scope");
const { installModelSelection, agentEvents, assembleContextFor } = await import("@deepseek-ai/dsh-agent");
const { SystemPrompt } = await import("@deepseek-ai/dsh-system-prompt");
const root = new Context();
new SystemPrompt(root, { includeHarnessIdentity: false, includeRuntimeContext: false });
const a = { provider: "openai-codex", model: "test-codex", reasoningEffort: "low" };
const b = { provider: "deepseek-official", model: "test-flash", reasoningEffort: "high" };
function subject(id, origin, parent) {
  let header;
  const history = [];
  const agent = { id, session: {
    header: { origin },
    requestHeader: () => header,
    snapshotEvents: () => history,
  } };
  const scope = createScope(root, agent, parent ? { parent } : undefined);
  agent.ctx = scope.ctx;
  const selection = { current: a };
  const nativeDispose = installModelSelection(agent.ctx, selection);
  const dispatch = agentEvents(root, agent);
  return { agent, selection, dispatch, scope, nativeDispose, commit(config, turn) {
    header = { config };
    history.push({ type: "turn/start", data: { turn } }, { type: "request/header", data: { header } });
  } };
}
const first = subject("task-a"), second = subject("task-b"), child = subject("child", "subagent", first.agent);
const unlock = installTaskModelLock({ on: root.on.bind(root), agents: { list: () => [first.agent, second.agent, child.agent] } });
const signal = new AbortController().signal;
async function step(subject, turn = 1) {
  const assembly = await root.systemPrompt.assemble(assembleContextFor(subject.agent, signal));
  const decision = await subject.dispatch.waterfall("agent/pre-step", { turn, step: 2, signal, messages: [] },
    async () => ({ kind: "enter", messages: [] }));
  const config = await subject.dispatch.waterfall("agent/request", { turn, step: 2, signal }, async () => a);
  subject.commit(config, turn);
  return { assembly, decision, config };
}
try {
  first.dispatch.emit("agent/inbox/claimed", { turn: 1, message: { source: { kind: "user" } } });
  assert.deepEqual((await step(first)).config, a);
  first.selection.current = b; // The native composer selection, with NO new prompt.
  const ongoing = await step(first);
  assert.deepEqual(ongoing.config, a);
  assert.equal(ongoing.assembly.variables.provider, a.provider);
  assert.equal(ongoing.assembly.variables.model, a.model);
  assert.equal(ongoing.decision.messages.filter(m => m.source.kind === "model-selection").length, 0);
  assert.deepEqual((await step(first, 2)).config, a); // Automatic continuation.
  child.selection.current = b;
  const childStep = await step(child);
  assert.deepEqual(childStep.config, b); // Parent is still pinned to A.
  assert.equal(childStep.assembly.variables.model, b.model);
  first.dispatch.emit("agent/inbox/claimed", { turn: 2, message: { source: { kind: "user" } } });
  assert.deepEqual((await step(first, 2)).config, a); // Steering in this turn.
  first.dispatch.emit("agent/inbox/claimed", { turn: 3, message: { source: { kind: "user" } } });
  const next = await step(first, 3);
  assert.deepEqual(next.config, b);
  assert.equal(next.assembly.variables.model, b.model);
  assert.equal(next.decision.messages.filter(m => m.source.kind === "model-selection").length, 1);
  second.selection.current = b;
  assert.deepEqual((await step(second)).config, b); // Scope isolation.
  console.log("PASS: native DSH scope/assembly/notice/request integration; no-send switch isolated, new task adopts choice, subagents unchanged; no model calls");
} finally {
  unlock();
  for (const subject of [first, second, child]) {
    subject.nativeDispose();
    await subject.scope.dispose();
  }
}
