import test from "node:test";
import assert from "node:assert/strict";
import { createOptimizerController, disabledReason, MAX_HISTORY } from "../src/core.js";

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function harness(request = async ({ text }) => ({ text: text + " optimized" })) {
  let input = { draft: "original", draftRev: 1, phase: "plain", occurrences: [], attachmentIds: ["image-1"] };
  let model = { provider: "provider-a", model: "model-a", reasoningEffort: "xhigh" };
  const calls = [];
  const controller = createOptimizerController({
    readInput: () => input, readModel: () => model,
    replace(text, span) {
      calls.push({ text, span });
      if (span.draftRev !== input.draftRev) return false;
      input = { ...input, draft: text, draftRev: input.draftRev + 1 };
      return true;
    },
    request,
  });
  return { controller, calls, get input() { return input; },
    edit(text) { input = { ...input, draft: text, draftRev: input.draftRev + 1 }; },
    setInput(value) { input = { ...input, ...value }; },
    setModel(value) { model = value; } };
}

test("empty/whitespace, unsupported states, references and missing models disable", () => {
  const input = { draft: "", phase: "plain", occurrences: [] };
  const model = { provider: "p", model: "m" };
  assert.ok(disabledReason(input, model, false));
  assert.ok(disabledReason({ ...input, draft: " \n " }, model, false));
  assert.ok(disabledReason({ ...input, draft: "a", phase: "submitting" }, model, false));
  assert.ok(disabledReason({ ...input, draft: "a", occurrences: [{}] }, model, false));
  assert.ok(disabledReason({ ...input, draft: "a" }, {}, false));
  assert.ok(disabledReason({ ...input, draft: "a".repeat(16001) }, model, false));
  assert.ok(disabledReason({ ...input, draft: "a" }, model, true));
  assert.equal(disabledReason({ ...input, draft: "valid" }, model, false), "");
});

test("three optimizations can be undone individually to the exact original", async () => {
  const h = harness();
  const revisions = [h.input.draft];
  for (let n = 0; n < 3; n++) {
    assert.equal(await h.controller.optimize(), true);
    revisions.push(h.input.draft);
  }
  assert.equal(h.controller.getSnapshot().count, 3);
  for (let n = 2; n >= 0; n--) {
    assert.equal(h.controller.undo(), true);
    assert.equal(h.input.draft, revisions[n]);
  }
  assert.equal(h.controller.undo(), false);
  assert.deepEqual(h.input.attachmentIds, ["image-1"]);
});

test("re-optimizing after undo branches from the current version", async () => {
  const h = harness();
  await h.controller.optimize();
  await h.controller.optimize();
  h.controller.undo();
  await h.controller.optimize();
  assert.equal(h.controller.getSnapshot().count, 2);
  h.controller.undo();
  h.controller.undo();
  assert.equal(h.input.draft, "original");
});

test("history is bounded to the last 30 reversible edits", async () => {
  const h = harness();
  for (let n = 0; n < MAX_HISTORY + 4; n++) await h.controller.optimize();
  assert.equal(h.controller.getSnapshot().count, MAX_HISTORY);
});

test("failures and empty results preserve draft and existing undo history", async () => {
  let error = false;
  const h = harness(async ({ text }) => {
    if (error) throw new Error("network failed");
    return { text: text + " improved" };
  });
  await h.controller.optimize();
  const before = h.input.draft;
  error = true;
  assert.equal(await h.controller.optimize(), false);
  assert.equal(h.input.draft, before);
  assert.equal(h.controller.getSnapshot().count, 1);
  assert.equal(h.controller.getSnapshot().error, true);
  const empty = harness(async () => ({ text: " \n " }));
  assert.equal(await empty.controller.optimize(), false);
  assert.equal(empty.input.draft, "original");
});

test("busy requests are deduplicated and undo is disabled until complete", async () => {
  const pending = deferred();
  let count = 0;
  const h = harness(() => { count++; return pending.promise; });
  const first = h.controller.optimize();
  assert.equal(await h.controller.optimize(), false);
  assert.equal(h.controller.undo(), false);
  assert.equal(count, 1);
  pending.resolve({ text: "improved" });
  await first;
  assert.equal(h.controller.getSnapshot().busy, false);
});

test("late optimization must not overwrite user edits or reverted revision", async () => {
  for (const text of ["new edit", "original"]) {
    const pending = deferred();
    const h = harness(() => pending.promise);
    const work = h.controller.optimize();
    h.edit(text);
    pending.resolve({ text: "improved" });
    assert.equal(await work, false);
    assert.equal(h.input.draft, text);
    assert.equal(h.calls.length, 0);
    assert.equal(h.controller.getSnapshot().count, 0);
  }
});

test("atomic replacement refusal does not create undo history", async () => {
  let input = { draft: "original", phase: "plain", draftRev: 1 };
  const controller = createOptimizerController({
    readInput: () => input, readModel: () => ({ provider: "p", model: "m" }),
    request: async () => ({ text: "better" }), replace: () => false,
  });
  assert.equal(await controller.optimize(), false);
  assert.equal(controller.getSnapshot().count, 0);
});

test("cancelled request ignores late result; a new request can succeed", async () => {
  const pending = deferred();
  let count = 0;
  const h = harness(() => ++count === 1 ? pending.promise : Promise.resolve({ text: "second" }));
  const first = h.controller.optimize();
  h.controller.cancel();
  assert.equal(h.input.draft, "original");
  assert.equal(h.controller.getSnapshot().busy, false);
  assert.equal(await h.controller.optimize(), true);
  pending.resolve({ text: "obsolete" });
  assert.equal(await first, false);
  assert.equal(h.input.draft, "second");
  assert.equal(h.controller.getSnapshot().count, 1);
});

test("clear/send cancels pending work and clears undo instead of resurrecting old text", async () => {
  const pending = deferred();
  const h = harness(() => pending.promise);
  const work = h.controller.optimize();
  h.edit("");
  h.controller.observe(h.input);
  pending.resolve({ text: "obsolete" });
  assert.equal(await work, false);
  assert.equal(h.input.draft, "");
  const done = harness();
  await done.controller.optimize();
  done.edit("");
  done.controller.observe(done.input);
  assert.equal(done.controller.getSnapshot().count, 0);
  assert.equal(done.controller.undo(), false);
});

test("undo after manual edits requires confirmation, cancellation preserves them", async () => {
  const h = harness();
  await h.controller.optimize();
  h.edit("my edits");
  assert.equal(h.controller.undo(), false);
  assert.equal(h.controller.getSnapshot().confirmUndo, true);
  assert.equal(h.input.draft, "my edits");
  h.controller.dismiss();
  assert.equal(h.input.draft, "my edits");
  h.controller.undo();
  assert.equal(h.controller.undo(true), true);
  assert.equal(h.input.draft, "original");
});

test("model route follows current selection but does not leak model-specific options", async () => {
  const payloads = [];
  const h = harness(async payload => { payloads.push(payload); return { text: payload.text + " improved" }; });
  await h.controller.optimize();
  h.setModel({ provider: "provider-b", model: "model-b" });
  await h.controller.optimize();
  assert.deepEqual(payloads.map(p => [p.provider, p.model]), [["provider-a", "model-a"], ["provider-b", "model-b"]]);
  assert.equal("reasoningEffort" in payloads[0], false);
  assert.equal("sessionId" in payloads[0], false);
});

test("unchanged second result completes successfully and later clicks still optimize the current draft", async () => {
  const payloads = [];
  const h = harness(async payload => {
    payloads.push(payload);
    return { text: payloads.length === 2 || payloads.length === 3 ? payload.text : payload.text + " improved" };
  });
  assert.equal(await h.controller.optimize(), true);
  const improved = h.input.draft;
  const rev = h.input.draftRev;
  for (let n = 0; n < 2; n++) {
    assert.equal(await h.controller.optimize(), true);
    assert.equal(h.controller.getSnapshot().busy, false);
    assert.equal(h.controller.getSnapshot().error, false);
    assert.match(h.controller.getSnapshot().message, /优化完成.*未作进一步修改/);
    assert.equal(h.controller.getSnapshot().count, 1);
    assert.equal(h.input.draft, improved);
    assert.equal(h.input.draftRev, rev);
    assert.equal(h.calls.length, 1);
  }
  assert.equal(await h.controller.optimize(), true);
  assert.deepEqual(payloads.map(p => p.text), ["original", improved, improved, improved]);
  assert.equal(h.calls.length, 2);
  assert.equal(h.controller.getSnapshot().count, 2);
  h.controller.undo(); h.controller.undo();
  assert.equal(h.input.draft, "original");
});

test("oversized results cannot install a draft that disables subsequent optimizations", async () => {
  let attempts = 0;
  const h = harness(async ({ text }) => ({ text: ++attempts === 2 ? "a".repeat(16001) : text + " improved" }));
  await h.controller.optimize();
  const before = h.input.draft;
  assert.equal(await h.controller.optimize(), false);
  assert.equal(h.input.draft, before);
  assert.equal(h.controller.getSnapshot().count, 1);
  assert.equal(h.controller.getSnapshot().busy, false);
  assert.match(h.controller.getSnapshot().message, /超过 16000/);
  assert.equal(disabledReason(h.input, { provider: "p", model: "m" }, false), "");
  assert.equal(await h.controller.optimize(), true);
  assert.equal(attempts, 3);
  assert.equal(h.controller.getSnapshot().count, 2);
});

test("same output is a no-op, and controllers isolate sessions", async () => {
  const same = harness(async ({ text }) => ({ text }));
  await same.controller.optimize();
  assert.equal(same.controller.getSnapshot().count, 0);
  const a = harness(), b = harness();
  await a.controller.optimize();
  assert.equal(b.controller.getSnapshot().count, 0);
  assert.equal(b.input.draft, "original");
});
