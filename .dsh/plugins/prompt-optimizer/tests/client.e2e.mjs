import { mkdir } from "node:fs/promises";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import path from "node:path";
import assert from "node:assert/strict";
const root = new URL("../", import.meta.url);
const require = createRequire(import.meta.url);
const playwright = process.env.PLAYWRIGHT_MODULE || require.resolve("playwright", { paths: [new URL("../../../frontend/", root).pathname] });
const imported = await import(pathToFileURL(playwright).href);
const { chromium } = imported.default || imported;
const reactRoot = process.env.TEST_REACT_ROOT || "/tmp/dsh-prompt-ui-tests-react/node_modules";
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1000, height: 650 } });
const errors = [];
page.on("pageerror", error => errors.push(error.message));
const artifacts = new URL("artifacts/", root);
await mkdir(artifacts, { recursive: true });
try {
  await page.setContent(`<!doctype html><html><head><style>
/* Real DSH theme tokens (light) so this preview matches the app, not a private palette. */
:root{--dsw-radius-xs:4px;--dsw-radius-sm:8px;--dsw-radius-md:12px;--dsw-radius-lg:16px;--dsw-radius-panel:28px;
--dsw-focus-ring-width:2px;--dsw-focus-ring-color:#4176e6;--dsw-elevation-stroke-color:#0000000a;
--dsw-elevation-prominent:0 0 0 .5px var(--dsw-elevation-stroke-color),0 3px 8px rgba(0,0,0,.04),0 0 20px rgba(0,0,0,.05);
--dsw-menu-surface-fill:#f8f9fa94;--dsw-menu-backdrop-filter:blur(40px) saturate(150%);
--dsw-alias-label-primary:#0f1115;--dsw-alias-label-secondary:#61666b;--dsw-alias-label-caption:#adb2b8;--dsw-alias-label-tertiary:#81858c;--dsw-alias-label-dimmed:#e1e5ee;--dsw-alias-menu-icon:#353638;
--dsw-alias-bg-base:#fff;--dsw-alias-bg-layer-1:#fff;--dsw-alias-bg-layer-2:#fff;--dsw-alias-bg-overlay:#e9ecf2;
--dsw-alias-border-l1:#0000000a;--dsw-alias-border-l2:#0000001a;--dsw-alias-border-l3:#0000001f;
--dsw-alias-interactive-bg-hover:#2631480f;--dsw-alias-interactive-bg-hover-solid:#f1f3f5;--dsw-alias-interactive-bg-hover-danger:#ec13130d;
--dsw-alias-state-business-primary:#4176e6;--dsw-alias-state-success-primary:#22c55e;--dsw-alias-state-error-primary:#ec1313;
--dsw-specific-selector:#f5f6f7;--dsw-alias-brand-primary:#4176e6}
html[data-dark=true]{--dsw-alias-label-primary:#f9fafb;--dsw-alias-label-secondary:#cfd3d6;--dsw-alias-label-caption:#81858c;--dsw-alias-label-tertiary:#adb2b8;--dsw-alias-label-dimmed:#43454a;--dsw-alias-menu-icon:#ebeef2;
--dsw-alias-bg-base:#151517;--dsw-alias-bg-layer-1:#232324;--dsw-alias-bg-layer-2:#2c2c2e;--dsw-alias-bg-overlay:#61666b;
--dsw-alias-border-l1:#ffffff0f;--dsw-alias-border-l2:#ffffff1f;--dsw-alias-border-l3:#ffffff29;
--dsw-alias-interactive-bg-hover:#ffffff14;--dsw-alias-interactive-bg-hover-solid:#353638;--dsw-alias-interactive-bg-hover-danger:#f25a5a26;
--dsw-alias-state-business-primary:#4f83ea;--dsw-alias-state-success-primary:#22c55e;--dsw-alias-state-error-primary:#f25a5a;
--dsw-menu-surface-fill:#43454a73;--dsw-specific-selector:#353638}
body{font:14px/22px -apple-system,system-ui;background:var(--dsw-alias-bg-base);color:var(--dsw-alias-label-primary);margin:0;padding:90px 24px}
main{max-width:800px;margin:auto}h1{font-size:22px;font-weight:600;text-align:center;margin-bottom:70px}
.card{padding:16px;border-radius:var(--dsw-radius-panel);background:var(--dsw-alias-bg-layer-1);box-shadow:0 0 0 .5px var(--dsw-alias-border-l2)}
textarea{box-sizing:border-box;display:block;width:100%;height:120px;border:0;outline:none;resize:vertical;font:16px/1.6 inherit;background:transparent;color:inherit}
.row,.standard{display:flex;align-items:center}.row{justify-content:flex-end;gap:12px}.standard{gap:12px;min-width:0}.model{height:28px;font-size:13px;font-weight:500;color:var(--dsw-alias-label-secondary);border:0;background:transparent;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mic,.send{box-sizing:border-box;border:0;background:transparent;width:28px;height:28px;color:var(--dsw-alias-label-secondary)}.send{border-radius:999px;color:#fff;background:var(--dsw-alias-state-business-primary)}
</style></head><body><main><h1>提示词优化视觉验收（隔离测试，使用 DSH 主题令牌）</h1><div id="root"></div></main></body></html>`);
  await page.addScriptTag({ path: path.join(reactRoot, "react/umd/react.development.js") });
  await page.addScriptTag({ path: path.join(reactRoot, "react-dom/umd/react-dom.development.js") });
  await page.evaluate(() => {
    window.__ModuleLoader__ = { load({ factory }) { window.optimizerPlugin = factory(name => { if (name === "react") return React; throw Error(name); }); } };
  });
  await page.addScriptTag({ path: new URL("lib/client.js", root).pathname });
  await page.evaluate(() => {
    let Tools;
    const disposers = [];
    window.optimizerPlugin.apply({
      effect(fn) { const dispose = fn(); if (dispose) disposers.push(dispose); },
      slots: { inject(_, fn) { fn(); }, register(options, component) { Tools = component; return () => {}; } },
    });
    let state = { draft: "", draftRev: 0, phase: "plain", occurrences: [], attachmentIds: ["attachment"] };
    let sessionId = "session-a";
    let model = { provider: "mock-provider", model: "mock-model" };
    const listeners = new Set();
    const read = () => state;
    const subscribe = fn => { listeners.add(fn); return () => listeners.delete(fn); };
    function notify() { for (const fn of listeners) fn(); }
    window.setDraft = text => { state = { ...state, draft: text, draftRev: state.draftRev + 1 }; notify(); };
    window.setInputMeta = patch => { state = { ...state, ...patch }; notify(); };
    window.selectModel = (provider, name) => { model = { provider, model: name }; state = { ...state }; notify(); };
    window.switchSession = id => { sessionId = id; state = { ...state }; notify(); };
    window.submitted = 0;
    window.requests = [];
    window.fetchMode = "success";
    window.fetch = async (url, options) => {
      const payload = JSON.parse(options.body);
      window.requests.push({ url, payload, accept: options.headers.accept });
      if (window.fetchMode === "stream") {
        window.streamCancelled = false;
        let writer;
        return new Response(new ReadableStream({
          start(value) {
            writer = value;
            window.streamRecord = record => {
              if (!window.streamCancelled) writer.enqueue(new TextEncoder().encode(JSON.stringify(record) + "\n"));
            };
            window.finishStream = () => { writer.close(); };
            window.streamRecord({ type: "progress", phase: "waiting", outputChars: 0 });
          },
          cancel() { window.streamCancelled = true; },
        }), { headers: { "content-type": "application/x-ndjson" } });
      }
      if (window.fetchMode === "pending") {
        await new Promise((resolve, reject) => {
          window.finishRequest = resolve;
          options.signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
        });
      }
      if (window.fetchMode === "same") return new Response(JSON.stringify({ text: payload.text }), { status: 200 });
      if (window.fetchMode === "oversized") return new Response(JSON.stringify({ text: "a".repeat(16001) }), { status: 200 });
      if (window.fetchMode === "error") return new Response(JSON.stringify({ error: { message: "模型请求失败，原文已保留" } }), { status: 502 });
      return new Response(JSON.stringify({ text: payload.text + "\n请明确约束并提供验收标准。" }), { status: 200 });
    };
    function App() {
      const input = React.useSyncExternalStore(subscribe, read);
      return React.createElement("section", { className: "card", "data-composer-card": true },
        React.createElement("textarea", { "aria-label": "提示词", value: input.draft, onChange: e => window.setDraft(e.target.value) }),
        React.createElement("div", { className: "row" },
          React.createElement("div", { className: "standard" },
            React.createElement(Tools, { key: sessionId, sessionId,
              useInput: selector => selector(React.useSyncExternalStore(subscribe, read)),
              useProjection: () => ({ next: model }),
              inputActions: { insertText(text, span) { if (span.draftRev !== state.draftRev) return false; window.setDraft(text); return true; } } }),
            React.createElement("button", { className: "model", "aria-label": "模型" }, model.provider + "/" + model.model)),
          React.createElement("button", { className: "mic", "aria-label": "麦克风" }, "♩"),
          React.createElement("button", { className: "send", "aria-label": "发送", onClick: () => { window.submitted++; } }, "↑")));
    }
    ReactDOM.createRoot(document.getElementById("root")).render(React.createElement(App));
  });
  const optimize = page.getByRole("button", { name: "优化提示词", exact: true });
  const cancel = page.getByRole("button", { name: "取消提示词优化", exact: true });
  const more = page.getByRole("button", { name: "提示词优化操作", exact: true });
  const undo = page.getByRole("menuitem", { name: "撤回上一次优化", exact: false });
  const control = page.locator(".dsh-prompt-control");
  const menu = page.getByRole("menu");
  const draft = page.getByRole("textbox", { name: "提示词" });
  const waitCount = count => page.waitForFunction(n => {
    const title = document.querySelector('[aria-label="提示词优化操作"]')?.title || "";
    return n ? title.includes("剩余 " + n + " 次") : !title.includes("剩余");
  }, count);
  async function undoLast() {
    await more.click();
    await undo.click();
    assert.equal(await menu.count(), 0);
  }
  async function aligned(reference) {
    const group = await control.boundingBox();
    const mic = await page.getByRole("button", { name: "麦克风", exact: true }).boundingBox();
    assert.equal(group.width, 48); // 6px + 22px glyph cell + 18px chevron cell + 2px
    assert.equal(group.height, 28);
    assert.ok(Math.abs(group.y + group.height / 2 - mic.y - mic.height / 2) < .5);
    const buttons = await control.locator("button").all();
    assert.equal(buttons.length, 2); // No detached loading/cancel/undo toolbar icons.
    for (const item of buttons) {
      const box = await item.boundingBox();
      const svg = await item.locator("svg").first().boundingBox();
      assert.ok(Math.abs(box.y + box.height / 2 - svg.y - svg.height / 2) < .5);
      assert.ok(Math.abs(box.x + box.width / 2 - svg.x - svg.width / 2) < .5);
    }
    if (reference) { assert.equal(group.x, reference.x); assert.equal(mic.x, reference.micX); }
    return { ...group, micX: mic.x };
  }
  // The plugin must speak DSH's design language, not carry its own palette.
  const styleText = await page.locator("style[data-prompt-optimizer]").textContent();
  assert.ok(!/#[0-9a-f]{3,8}\b/i.test(styleText), "plugin stylesheet must not hardcode hex colors");
  assert.ok(/prefers-reduced-motion/.test(styleText));
  await optimize.waitFor();
  assert.equal(await page.locator(".dsh-prompt-control").evaluate(el => getComputedStyle(el).borderRadius), "8px"); // --dsw-radius-sm
  assert.equal(await optimize.evaluate(el => getComputedStyle(el).color), "rgb(225, 229, 238)"); // --dsw-alias-label-dimmed while empty
  assert.equal(await more.locator("svg").evaluate(el => getComputedStyle(el).width), "12px");
  assert.equal(await optimize.isDisabled(), true);
  assert.equal(await more.isDisabled(), true);
  await draft.fill(" ");
  assert.equal(await optimize.isDisabled(), true);
  await draft.fill("帮我实现提示词优化，不自动发送。");
  assert.equal(await optimize.isEnabled(), true);
  // Colors transition, so settle before reading a resolved token value.
  const settledColor = (locator, expected) => locator.evaluate((el, want) => new Promise(resolve => {
    if (getComputedStyle(el).color === want) return resolve(want);
    const done = () => requestAnimationFrame(() => getComputedStyle(el).color === want ? resolve(want) : setTimeout(done, 30));
    done();
  }), expected);
  assert.equal(await settledColor(more, "rgb(173, 178, 184)"), "rgb(173, 178, 184)"); // --dsw-alias-label-caption
  const layout = await aligned();
  const modelBox = await page.getByRole("button", { name: "模型", exact: true }).boundingBox();
  assert.ok(modelBox.x + modelBox.width <= layout.x);
  assert.ok(layout.x + layout.width <= layout.micX);
  const original = await draft.inputValue();
  for (let n = 0; n < 3; n++) {
    await optimize.click();
    await waitCount(n + 1);
    assert.equal(await control.getAttribute("data-state"), "success");
    await aligned(layout);
  }
  // Real models often return an unchanged second version; append-only mocks hid
  // its missing completion animation/message. Each click must issue a fresh request,
  // including a click during the previous success animation.
  const unchangedDraft = await draft.inputValue();
  const beforeSame = await page.evaluate(() => window.requests.length);
  await page.evaluate(() => { window.fetchMode = "same"; });
  for (let n = 0; n < 2; n++) {
    await optimize.click();
    await page.waitForFunction(want => window.requests.length === want && document.querySelector(".dsh-prompt-main").getAttribute("aria-busy") === "false", beforeSame + n + 1);
    assert.equal(await control.getAttribute("data-state"), "success");
    assert.ok((await optimize.getAttribute("title")).includes("优化完成，模型未作进一步修改"));
    assert.equal(await draft.inputValue(), unchangedDraft);
    assert.equal(await optimize.isEnabled(), true);
    await waitCount(3);
  }
  const samePayloads = await page.evaluate(start => window.requests.slice(start).map(value => value.payload.text), beforeSame);
  assert.deepEqual(samePayloads, [unchangedDraft, unchangedDraft]);
  await page.evaluate(() => { window.fetchMode = "success"; });
  assert.equal(await page.evaluate(() => window.submitted), 0);
  assert.equal(await page.locator(".dsh-prompt-notice").count(), 0);
  assert.ok((await optimize.getAttribute("title")).includes("再次优化"));
  await page.waitForFunction(() => document.querySelector(".dsh-prompt-control").dataset.state === "idle");
  await optimize.hover();
  await page.screenshot({ animations: "disabled", path: new URL("optimizer-light.png", artifacts).pathname });
  // Resting state: the control must be flat like the mic, with no box of its own.
  await page.mouse.move(20, 20);
  await page.screenshot({ animations: "disabled", path: new URL("optimizer-idle.png", artifacts).pathname });
  assert.equal(await control.evaluate(el => getComputedStyle(el).backgroundColor), "rgba(0, 0, 0, 0)");
  assert.equal(await control.evaluate(el => getComputedStyle(el).boxShadow), "none");
  // Explicit action menu: focus, keyboard traversal, Escape and outside dismissal.
  await more.focus();
  await more.press("ArrowDown");
  assert.equal(await menu.count(), 1);
  assert.equal(await more.getAttribute("aria-expanded"), "true");
  assert.match(await menu.evaluate(el => getComputedStyle(el).backgroundColor), /^rgba\(248, 249, 250, 0\.58\)$/); // --dsw-menu-surface-fill (light)
  await page.keyboard.press("ArrowDown");
  assert.equal(await undo.evaluate(el => el === document.activeElement), true);
  await page.keyboard.press("Escape");
  assert.equal(await menu.count(), 0);
  assert.equal(await more.evaluate(el => el === document.activeElement), true);
  await more.click();
  await draft.click();
  assert.equal(await menu.count(), 0);
  for (let n = 0; n < 3; n++) {
    await undoLast();
    await waitCount(2 - n);
    assert.equal(await control.getAttribute("data-state"), "undo");
    await aligned(layout);
  }
  await page.screenshot({ animations: "disabled", path: new URL("optimizer-undo.png", artifacts).pathname });
  assert.equal(await draft.inputValue(), original);
  await more.click();
  assert.equal(await undo.isDisabled(), true);
  await page.keyboard.press("Escape");
  assert.equal(await page.locator(".dsh-prompt-notice").count(), 0);
  await optimize.click();
  await waitCount(1);
  // Existing history must survive cancel and fail, without changing toolbar size.
  const optimized = await draft.inputValue();
  await page.evaluate(() => { window.fetchMode = "pending"; });
  await optimize.click();
  await cancel.waitFor();
  assert.equal(await cancel.isEnabled(), true);
  assert.equal(await more.isDisabled(), true);
  assert.equal(await control.getAttribute("data-state"), "loading");
  await aligned(layout);
  await page.screenshot({ animations: "disabled", path: new URL("optimizer-loading.png", artifacts).pathname });
  await cancel.click();
  await waitCount(1);
  assert.equal(await draft.inputValue(), optimized);
  assert.equal(await menu.count(), 0);
  // Editing while pending protects the newer draft.
  await optimize.click();
  await cancel.waitFor();
  await draft.fill("优化期间的新编辑");
  await page.evaluate(() => window.finishRequest());
  await optimize.waitFor();
  assert.equal(await draft.inputValue(), "优化期间的新编辑");
  await page.evaluate(() => { window.fetchMode = "error"; });
  await optimize.click();
  await page.waitForFunction(() => document.querySelector(".dsh-prompt-control").dataset.state === "error");
  assert.ok((await optimize.getAttribute("title")).includes("模型请求失败"));
  assert.equal(await page.getByRole("alert").count(), 0);
  assert.equal(await page.locator(".dsh-prompt-notice").count(), 0);
  assert.equal(await draft.inputValue(), "优化期间的新编辑");
  await aligned(layout);
  await undoLast();
  await page.getByRole("button", { name: "保留当前编辑" }).click();
  assert.equal(await draft.inputValue(), "优化期间的新编辑");
  await undoLast();
  await page.keyboard.press("Escape");
  assert.equal(await page.getByRole("dialog").count(), 0);
  assert.equal(await draft.inputValue(), "优化期间的新编辑");
  await undoLast();
  await page.getByRole("button", { name: "确认撤回" }).click();
  assert.equal(await draft.inputValue(), original);
  await waitCount(0);
  // Oversized model output must not turn the next click into a disabled control.
  await page.evaluate(() => { window.fetchMode = "oversized"; });
  await optimize.click();
  await page.waitForFunction(() => document.querySelector(".dsh-prompt-control").dataset.state === "error");
  assert.ok((await optimize.getAttribute("title")).includes("超过 16000"));
  assert.equal(await draft.inputValue(), original);
  assert.equal(await optimize.isEnabled(), true);
  await waitCount(0);
  // Real streamed progress changes only the compact loader/tooltip, never the draft.
  await page.evaluate(() => { window.fetchMode = "stream"; });
  await optimize.click();
  await cancel.waitFor();
  await page.waitForFunction(() => document.querySelector(".dsh-prompt-main").title.includes("等待正文"));
  assert.equal(await page.evaluate(() => window.requests.at(-1).accept), "application/x-ndjson");
  await page.evaluate(() => window.streamRecord({ type: "progress", phase: "generating", outputChars: 12 }));
  await page.waitForFunction(() => document.querySelector(".dsh-prompt-main").title.includes("12 字符"));
  assert.equal(await page.locator(".dsh-prompt-loader").getAttribute("data-generating"), "true");
  assert.equal(await draft.inputValue(), original);
  assert.equal(await page.locator(".dsh-prompt-notice").count(), 0);
  await aligned(layout);
  await page.evaluate(value => {
    window.streamRecord({ type: "result", text: value + "（流式完整结果）" });
    window.finishStream();
  }, original);
  await waitCount(1);
  assert.equal(await draft.inputValue(), original + "（流式完整结果）");
  await undoLast();
  await waitCount(0);
  assert.equal(await draft.inputValue(), original);
  // Cancel and terminal errors discard all partial output and preserve the undo stack.
  await optimize.click();
  await cancel.waitFor();
  await page.evaluate(() => window.streamRecord({ type: "progress", phase: "generating", outputChars: 20 }));
  await cancel.click();
  await optimize.waitFor();
  await page.waitForFunction(() => window.streamCancelled);
  assert.equal(await draft.inputValue(), original);
  await waitCount(0);
  await optimize.click();
  await cancel.waitFor();
  await page.evaluate(() => {
    window.streamRecord({ type: "progress", phase: "generating", outputChars: 20 });
    window.streamRecord({ type: "error", error: { message: "流式生成失败，原文已保留" } });
    window.finishStream();
  });
  await page.waitForFunction(() => document.querySelector(".dsh-prompt-control").dataset.state === "error");
  assert.ok((await optimize.getAttribute("title")).includes("流式生成失败"));
  assert.equal(await draft.inputValue(), original);
  await waitCount(0);
  // New models and sessions keep the controller's isolation guarantees.
  await page.evaluate(() => { window.fetchMode = "success"; window.selectModel("other-provider", "other-model"); });
  await optimize.click();
  await waitCount(1);
  assert.equal(await page.evaluate(() => window.requests.at(-1).payload.provider), "other-provider");
  await page.evaluate(() => { window.switchSession("session-b"); });
  await waitCount(0);
  await page.evaluate(() => { window.switchSession("session-a"); });
  await waitCount(1);
  await draft.fill("");
  await waitCount(0);
  assert.equal(await optimize.isDisabled(), true);
  await draft.fill("请整理目标、约束与验收要求。");
  await page.evaluate(() => window.setInputMeta({ occurrences: [{}] }));
  assert.equal(await optimize.isDisabled(), true);
  await page.evaluate(() => window.setInputMeta({ occurrences: [], phase: "submitting" }));
  assert.equal(await optimize.isDisabled(), true);
  await page.evaluate(() => window.setInputMeta({ phase: "plain" }));
  await optimize.click();
  await waitCount(1);
  await page.waitForFunction(() => document.querySelector(".dsh-prompt-control").dataset.state === "idle");
  await page.evaluate(() => document.documentElement.dataset.dark = "true");
  await more.click();
  // DSH menu geometry and material, resolved through the dark theme tokens.
  assert.equal(await menu.evaluate(el => getComputedStyle(el).borderRadius), "16px");
  assert.equal(await menu.evaluate(el => getComputedStyle(el).padding), "4px");
  assert.match(await menu.evaluate(el => getComputedStyle(el).backgroundColor), /^rgba\(67, 69, 74, 0\.45\)$/); // --dsw-menu-surface-fill (dark)
  assert.notEqual(await menu.evaluate(el => getComputedStyle(el).backdropFilter), "none");
  assert.equal(await menu.evaluate(el => getComputedStyle(el).color), "rgb(249, 250, 251)");
  assert.equal(await menu.locator("button").first().evaluate(el => getComputedStyle(el).minHeight), "34px");
  assert.equal(await menu.locator("button").first().evaluate(el => getComputedStyle(el).borderRadius), "12px");
  assert.equal(await menu.locator("button").first().evaluate(el => getComputedStyle(el).fontSize), "13px");
  assert.equal(await menu.locator(".dsh-prompt-menu-icon svg").first().evaluate(el => getComputedStyle(el).width), "14px");
  assert.equal(await menu.locator(".dsh-prompt-menu-icon").first().evaluate(el => getComputedStyle(el).color), "rgb(235, 238, 242)");
  assert.equal(await page.locator(".dsh-prompt-menu-count").evaluate(el => getComputedStyle(el).fontSize), "11px");
  assert.equal(await page.locator("style[data-prompt-optimizer]").textContent(), styleText);
  await page.screenshot({ animations: "disabled", path: new URL("optimizer-dark.png", artifacts).pathname });
  await page.keyboard.press("Escape");
  await page.setViewportSize({ width: 390, height: 700 });
  await aligned();
  assert.equal(await page.locator(".dsh-prompt-notice").count(), 0);
  await more.click();
  const menuBox = await menu.boundingBox();
  assert.ok(menuBox.x >= 0 && menuBox.x + menuBox.width <= 390);
  await page.screenshot({ animations: "disabled", path: new URL("optimizer-mobile.png", artifacts).pathname });
  await page.keyboard.press("Escape");
  assert.ok((await optimize.boundingBox()).x >= 0);
  assert.ok((await page.getByRole("button", { name: "发送", exact: true }).boundingBox()).x < 390);
  // Reduced-motion users get static, recognizable states, with cancel still operable.
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.evaluate(() => { window.fetchMode = "pending"; });
  await optimize.click();
  await cancel.waitFor();
  assert.equal(await page.locator(".dsh-prompt-spin").evaluate(el => getComputedStyle(el).animationName), "none");
  await cancel.focus();
  await page.keyboard.press("Enter");
  await optimize.waitFor();
  await waitCount(1);
  await page.emulateMedia({ reducedMotion: "no-preference" });
  // Switching sessions during work aborts it and cannot flash or overwrite the new session.
  await optimize.click();
  await cancel.waitFor();
  await page.evaluate(() => window.switchSession("session-c"));
  await optimize.waitFor();
  await waitCount(0);
  assert.equal(await control.getAttribute("data-state"), "idle");
  assert.equal(await page.evaluate(() => window.submitted), 0);
  assert.deepEqual(errors, []);
  console.log("PASS: composite button, stable icon/toolbar alignment, streamed progress/atomic apply/cancel/error, loading/cancel/check/undo/error states, action menu/keyboard/Escape/outside click, three optimizations/undos, edit confirmation, stale results, cancel/failure history, model/session isolation, clear/references, dark/mobile/reduced-motion, no status popups or chat sends");
} finally {
  await browser.close();
}
