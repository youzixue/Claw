import test from "node:test";
import assert from "node:assert/strict";
import { installTaskModelLock } from "../src/model-lock.js";
import { optimizeText } from "../src/optimize.js";

const a = { provider: "codex", model: "model-a", reasoningEffort: "low" };
const b = { provider: "deepseek", model: "model-b", reasoningEffort: "high" };
function events() {
  const listeners = new Map();
  return {
    on(name, callback, { prepend = false } = {}) {
      const list = listeners.get(name) ?? [];
      prepend ? list.unshift(callback) : list.push(callback);
      listeners.set(name, list);
      return () => { const index = list.indexOf(callback); if (index >= 0) list.splice(index, 1); };
    },
    emit(name, payload) { for (const callback of [...(listeners.get(name) ?? [])]) callback(payload); },
    waterfall(name, args, fallback) {
      const list = [...(listeners.get(name) ?? [])];
      const next = index => index === list.length ? fallback() : list[index](...args, () => next(index + 1));
      return next(0);
    },
    count() { return [...listeners.values()].reduce((n, list) => n + list.length, 0); },
  };
}
function agent({ config, turn = 1, origin } = {}) {
  const ctx = events();
  const history = config ? [{ type: "turn/start", data: { turn } }, { type: "request/header", data: { header: { config } } }] : [];
  return { ctx, session: {
    header: { origin },
    requestHeader: () => config && { config },
    snapshotEvents: () => history,
  } };
}
function harness(agents = [agent()]) {
  const ctx = { ...events(), agents: { list: () => agents } };
  const dispose = installTaskModelLock(ctx);
  const signal = new AbortController().signal;
  return { ctx, agents, dispose, signal,
    request(subject, proposed, turn = 1, next = async () => proposed) {
      return subject.ctx.waterfall("agent/request", [{ agent: subject, turn, signal }], next);
    },
    claim(subject, turn, kind = "user") { subject.ctx.emit("agent/inbox/claimed", { agent: subject, turn, message: { source: { kind } } }); },
  };
}
test("selecting B without sending keeps A through tool steps and retries", async () => {
  const h = harness();
  const subject = h.agents[0];
  h.claim(subject, 1);
  assert.deepEqual(await h.request(subject, a), a);
  for (let n = 0; n < 4; n++) assert.deepEqual(await h.request(subject, b), a);
  h.dispose();
});
test("new user task adopts pending choice; same-turn steering does not", async () => {
  const h = harness([agent({ config: a })]);
  const subject = h.agents[0];
  h.claim(subject, 1);
  assert.deepEqual(await h.request(subject, b), a);
  h.claim(subject, 2);
  assert.deepEqual(await h.request(subject, b, 2), b);
  h.dispose();
});
test("automatic goal rounds and question replies do not consume draft choice", async () => {
  const h = harness([agent({ config: a })]);
  const subject = h.agents[0];
  for (const kind of ["goal", "schedule", "user-question-reply", "model-selection", "agent-message"]) {
    h.claim(subject, 2, kind);
    assert.deepEqual(await h.request(subject, b, 2), a);
  }
  h.dispose();
});
test("loading during an existing task seeds actual header, not pending model intent", async () => {
  const h = harness([agent({ config: a, turn: 8 })]);
  assert.deepEqual(await h.request(h.agents[0], b, 8), a);
  h.dispose();
});
test("frozen route and effort also govern prompt variables and newly generated notices", async () => {
  const h = harness([agent({ config: a })]);
  const subject = h.agents[0];
  const assembly = await subject.ctx.waterfall("system-prompt/assemble", [{}, { agent: subject, signal: h.signal }],
    async () => ({ variables: { provider: b.provider, model: b.model, cwd: "/example" }, contexts: ["keep"], tools: [] }));
  assert.deepEqual(assembly.variables, { provider: a.provider, model: a.model, cwd: "/example" });
  const message = { source: { kind: "user" }, content: "keep steering" };
  const decision = await subject.ctx.waterfall("agent/pre-step", [{ agent: subject, signal: h.signal }],
    async () => ({ kind: "enter", messages: [message, { source: { kind: "model-selection" } }] }));
  assert.deepEqual(decision.messages, [message]);
  assert.deepEqual(await h.request(subject, b), a);
  h.dispose();
});
test("first new task preserves a legitimate model switch notice and rejected steps", async () => {
  const h = harness([agent({ config: a })]);
  const subject = h.agents[0];
  h.claim(subject, 2);
  const expected = { kind: "enter", messages: [{ source: { kind: "model-selection" } }] };
  assert.equal(await subject.ctx.waterfall("agent/pre-step", [{ agent: subject, signal: h.signal }], async () => expected), expected);
  const rejected = { kind: "reject" };
  assert.equal(await subject.ctx.waterfall("agent/pre-step", [{ agent: subject, signal: h.signal }], async () => rejected), rejected);
  h.dispose();
});
test("agent identities and subagents remain independent, with disposal removing all hooks", async () => {
  const first = agent({ config: a }), second = agent(), child = agent({ config: a, origin: "subagent" });
  const h = harness([first, second, child]);
  assert.deepEqual(await h.request(first, b), a);
  assert.deepEqual(await h.request(second, b), b);
  assert.deepEqual(await h.request(child, b), b);
  const created = agent();
  h.ctx.emit("agent/created", { agent: created });
  assert.deepEqual(await h.request(created, a), a);
  h.ctx.emit("agent/disposed", { agent: created });
  assert.equal(created.ctx.count(), 0);
  h.dispose();
  for (const subject of [first, second, child]) assert.equal(subject.ctx.count(), 0);
  assert.equal(h.ctx.count(), 0);
});
test("cancellation or failed downstream preparation cannot publish a new lock", async () => {
  const h = harness();
  const subject = h.agents[0];
  await assert.rejects(h.request(subject, a, 1, async () => { throw Error("failed"); }));
  const controller = new AbortController();
  const pending = subject.ctx.waterfall("agent/request", [{ agent: subject, turn: 1, signal: controller.signal }],
    async () => { controller.abort(); return a; });
  await assert.rejects(pending);
  assert.deepEqual(await h.request(subject, b), b);
  h.dispose();
});
test("returned configs are detached from mutable selections and caller mutations", async () => {
  const h = harness();
  const subject = h.agents[0];
  const chosen = { ...a, stop: ["end"] };
  const first = await h.request(subject, chosen);
  first.stop.push("bad");
  chosen.provider = "bad";
  assert.deepEqual(await h.request(subject, b), { ...a, stop: ["end"] });
  h.dispose();
});
test("isolated optimizer can use B while the existing chat continues on A", async () => {
  const h = harness([agent({ config: a })]);
  let used;
  const output = await optimizeText({
    prepareCall: async route => ({ config: route, async *stream(options) {
      used = options;
      yield { type: "text-delta", index: 0, text: "optimized draft" };
      yield { type: "finish", reason: { kind: "stop" } };
    } }),
  }, { provider: b.provider, model: b.model, text: "draft" });
  assert.equal(output.provider, b.provider);
  assert.equal(used.model, b.model);
  assert.deepEqual(used.tools, []);
  assert.deepEqual(await h.request(h.agents[0], b), a);
  h.dispose();
});
