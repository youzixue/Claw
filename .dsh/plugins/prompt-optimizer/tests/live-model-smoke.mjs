// Explicit, opt-in live smoke test; creates an isolated no-tools model call, not a DSH chat turn.
import { createRequire, registerHooks } from "node:module";
import { pathToFileURL } from "node:url";
import { writeFile } from "node:fs/promises";
import assert from "node:assert/strict";
import { optimizeText } from "../src/optimize.js";
const runtime = process.env.DSH_TEST_RUNTIME || "/Applications/DeepSeek Harness.app/Contents/Resources/app.asar/dsh/package.json";
const runtimeRequire = createRequire(runtime);
let resolvingRuntime = false;
registerHooks({
  resolve(specifier, context, next) {
    if (specifier.startsWith("@deepseek-ai/") && !resolvingRuntime) {
      resolvingRuntime = true;
      try { return { url: pathToFileURL(runtimeRequire.resolve(specifier)).href, shortCircuit: true }; }
      finally { resolvingRuntime = false; }
    }
    return next(specifier, context);
  },
});
const { Context } = await import("@deepseek-ai/cordis");
const { LlmRuntime } = await import("@deepseek-ai/dsh-llm");
const adapterRoot = process.env.DSH_TEST_CODEX_ADAPTER || "/Users/youzix/.dsh/profiles/desktop/node_modules/dsh-openai-oauth/lib/";
const { AppServer } = await import(pathToFileURL(adapterRoot + "app-server.js").href);
const { CodexAppServerAdapter } = await import(pathToFileURL(adapterRoot + "index.js").href);
const ctx = new Context();
const llm = new LlmRuntime(ctx);
const server = new AppServer();
const dispose = llm.registerAdapter(["openai-codex"], new CodexAppServerAdapter(server));
const controller = new AbortController();
const timeout = setTimeout(() => controller.abort(new Error("Live model test timed out")), 90000);
try {
  const source = "为我写一个 Python 脚本，读取 data.csv，输出各列缺失值数量；不要联网；不要安装任何依赖。";
  const route = { provider: "openai-codex", model: process.env.DSH_TEST_MODEL || "gpt-6.1-sol" };
  const benchmark = process.env.DSH_TEST_BENCHMARK === "1";
  // Warm capability discovery once so process startup is not charged only to the baseline.
  const info = await llm.resolveModelInfo(route.provider, route.model, controller.signal);
  const results = [];
  for (const mode of benchmark ? ["default", "quick", "quick", "default"] : ["quick"]) {
    let firstTextMs, effort;
    const started = performance.now();
    const measured = {
      ...(mode === "quick" ? { resolveModelInfo: (...args) => llm.resolveModelInfo(...args) } : {}),
      async prepareCall(...args) {
        const prepared = await llm.prepareCall(...args);
        effort = prepared.config.reasoningEffort;
        return { ...prepared, async *stream(options) {
          assert.deepEqual(options.tools, []);
          for await (const chunk of prepared.stream(options)) {
            if (firstTextMs === undefined && (chunk.type === "text-delta" || (chunk.type === "block-end" && chunk.block?.type === "text"))) firstTextMs = performance.now() - started;
            yield chunk;
          }
        } };
      },
    };
    const result = await optimizeText(measured, { ...route, text: source }, controller.signal);
    assert.ok(result.text.includes("data.csv"));
    assert.ok(result.text.includes("Python"));
    assert.ok(/不要联网|禁止联网|不联网|不得联网/.test(result.text));
    assert.ok(/依赖|标准库|第三方库/.test(result.text));
    assert.ok(!result.text.includes("import pandas"));
    const record = { mode, effort, elapsedMs: Math.round(performance.now() - started), firstTextMs: Math.round(firstTextMs), outputChars: result.text.length, text: result.text };
    results.push(record);
    console.log(JSON.stringify({ status: "PASS", ...route, ...record, text: undefined, noChatTurn: true, noTools: true }));
  }
  const artifact = benchmark ? "live-speed.json" : "live-model.json";
  await writeFile(new URL("../artifacts/" + artifact, import.meta.url), JSON.stringify({
    source, ...route, supportedEfforts: info.reasoning?.efforts.map(value => value.id), results,
    ...(benchmark ? { note: "Four isolated requests for one short sample, alternating order, not a production latency guarantee." } : {}),
  }, null, 2));
} finally {
  clearTimeout(timeout);
  dispose();
  server.close();
}
