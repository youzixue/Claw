// Opt-in real-provider smoke: synthetic input only; no chat/config/credential writes.
// Run with DSH_LIVE_DEEPSEEK=1 under the serving Electron Node runtime.
import { createRequire, registerHooks } from "node:module";
import { pathToFileURL } from "node:url";
import { readFile, writeFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { optimizeText } from "../src/optimize.js";

if (process.env.DSH_LIVE_DEEPSEEK !== "1") throw new Error("Set DSH_LIVE_DEEPSEEK=1 to allow the synthetic live request");
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
const { parseCredentialsDocument } = await import("@deepseek-ai/dsh-credentials-local");
const { parse } = runtimeRequire("yaml");
const patchPath = process.env.DSH_TEST_PROFILE_PATCH || "/Users/youzix/.dsh/profiles/desktop/cordis.patch.yml";
const patch = parse(await readFile(patchPath, "utf8"));
const config = patch.find(entry => entry.id === "llm-deepseek")?.config;
if (!config) throw new Error("No DeepSeek profile configuration found");
const ref = config.apiKeyEnv || "DEEPSEEK_API_KEY";
const credentialPath = process.env.DSH_TEST_CREDENTIALS || "/Users/youzix/.dsh/.credentials.yaml";
// Do not log document text or credential values, including on failure.
let key;
try { key = process.env[ref] || parseCredentialsDocument(await readFile(credentialPath, "utf8"), credentialPath).refs.get(ref); }
catch { throw new Error("Unable to resolve the configured credential safely"); }
if (!key) throw new Error("No configured credential for the isolated request");
const connection = resolveAdapterOptions(config);
const route = { provider: "deepseek-official", model: process.env.DSH_TEST_MODEL || config.models[0].id };
const llm = new LlmRuntime(new Context());
const telemetry = [];
const nativeFetch = globalThis.fetch;
globalThis.fetch = async (url, options) => {
  const body = JSON.parse(options.body);
  const response = await nativeFetch(url, options);
  const info = { status: response.status, hasTools: Object.hasOwn(body, "tools"), toolsCount: body.tools?.length, thinking: body.thinking?.type, effort: body.output_config?.effort, maxTokens: body.max_tokens };
  if (!response.ok) {
    // Allowlist diagnostics; never persist arbitrary backend messages.
    const raw = await response.clone().json().catch(() => null);
    const message = raw?.error?.message || "";
    info.classification = /tools?.*empty|empty.*tools?/i.test(message) ? "empty-tools"
      : /max[_ ]?tokens/i.test(message) ? "max-tokens"
      : /effort|thinking|reasoning/i.test(message) ? "reasoning"
      : /model/i.test(message) ? "model"
      : "other";
  }
  telemetry.push(info);
  return response;
};
const unregister = llm.registerAdapter([route.provider], new DeepSeekAdapter({
  options: () => connection,
  resolveAuth: async () => ({ headers: { "x-api-key": key } }),
  resolveUserId: () => "prompt-optimizer-smoke",
  prepareExtensions: async () => ({ fields: {}, accept: async () => {} }),
}));
const start = performance.now();
let result;
try {
  const response = await optimizeText(llm, { ...route, text: "写一个 Python 脚本，读取 data.csv，输出缺失值数量；不要联网，不安装依赖。" }, AbortSignal.timeout(90000));
  result = { status: "PASS", ...route, outputChars: response.text.length, retainsPath: response.text.includes("data.csv") };
} catch (error) {
  result = { status: "FAIL", ...route, code: error?.code || "MODEL_ERROR" };
} finally { unregister(); globalThis.fetch = nativeFetch; }
const evidence = { ...result, totalMs: Math.round(performance.now() - start), telemetry, noChatTurn: true };
const file = new URL("../artifacts/deepseek-smoke-" + Date.now() + "-" + randomUUID().slice(0, 8) + ".json", import.meta.url);
await writeFile(file, JSON.stringify(evidence, null, 2));
console.log(JSON.stringify(evidence));
console.log("Evidence: " + file.pathname);
if (result.status !== "PASS") process.exitCode = 1;
