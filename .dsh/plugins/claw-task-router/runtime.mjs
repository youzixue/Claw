import { classify, isFresh, ROOT, VERSION } from './policy.mjs?router-v1.1';
import { messagePhase, denyReason } from '../claw-research/automation-policy.mjs?native-schedule-v2';

export const name = 'claw-task-router';
export const inject = ['connection', 'sessionController', 'agentPresets', 'agents', 'tools', 'typertGateway'];
export const PROMPT_PATH = '/api/session/prompt';
export const PREPARE_PATH = '/api/claw-task-router/prepare';
export const MAX_BODY_BYTES = 1024 * 1024;
const MAX_DECISIONS = 200;

function failure(code, message) {
  const error = new Error(message);
  error.code = code;
  return error;
}

export function reviewPhase(session) {
  if (session?.header?.cwd !== ROOT) return undefined;
  const events = session.snapshotEvents();
  for (let i = events.length - 1; i >= 0; i--) {
    if (events[i].type === 'turn/start') break;
    if (events[i].type === 'user/message') {
      const phase = messagePhase(events[i].data);
      if (phase) return phase;
    }
  }
  return undefined;
}

export function validPromptForRouting(args) {
  if (!args || typeof args !== 'object' || Array.isArray(args) ||
      Object.keys(args).length !== 1 || !args.request) return false;
  const request = args.request;
  if (typeof request.sessionId !== 'string' || !request.sessionId ||
      typeof request.requestId !== 'string' || !request.requestId ||
      !['queue', 'steer'].includes(request.mode) || !Array.isArray(request.content)) return false;
  if (Object.keys(request).some(key =>
    !['sessionId', 'requestId', 'mode', 'content', 'clientTimeZone'].includes(key))) return false;
  if (request.clientTimeZone !== undefined) {
    try { new Intl.DateTimeFormat('en', { timeZone: request.clientTimeZone }); }
    catch { return false; }
  }
  return request.content.length > 0 && request.content.every(part => part && (
    part.type === 'text' && typeof part.text === 'string' ||
    part.type === 'image' || part.type === 'file'
  )) && request.content.some(part => part.type !== 'text' || part.text.trim());
}

export function createRouter(ctx) {
  const tails = new Map(), decisions = new Map();
  const remember = (id, decision) => {
    decisions.delete(id);
    decisions.set(id, decision);
    while (decisions.size > MAX_DECISIONS) decisions.delete(decisions.keys().next().value);
  };
  const actual = agent => ctx.agentPresets.composedPreset(agent.ctx) ?? agent.session.header.agentPreset;
  async function serial(id, operation) {
    const work = (tails.get(id) ?? Promise.resolve()).then(operation);
    const tail = work.catch(() => {});
    tails.set(id, tail);
    try { return await work; }
    finally { if (tails.get(id) === tail) tails.delete(id); }
  }
  async function choose(agent, message, signal) {
    signal?.throwIfAborted();
    if (!isFresh(agent.session))
      return { mode: actual(agent), reason: 'existing_fork_or_other_workspace', switched: false };
    const decision = classify(message);
    if (decision.readonly && !ctx.tools.get('claw_review_guard_status'))
      throw failure('claw-router/guard-missing', 'Claw review guard is unavailable; task was not admitted.');
    let selected = decision.mode;
    let reason = decision.reason;
    try {
      const preset = await ctx.agentPresets.resolve(selected);
      if (preset.broken) throw failure('claw-router/preset-unavailable', 'Requested preset is unavailable.');
      signal?.throwIfAborted();
      // Recheck after async resolution; select() also enforces the native turn lock.
      if (!isFresh(agent.session))
        return { mode: actual(agent), reason: 'session_started_during_routing', switched: false };
      if (actual(agent) !== selected) await ctx.agentPresets.select(agent, selected);
    } catch (error) {
      signal?.throwIfAborted();
      if (!isFresh(agent.session))
        return { mode: actual(agent), reason: 'session_started_during_routing', switched: false };
      if (selected === 'standard') throw error;
      const fallback = await ctx.agentPresets.resolve('standard');
      if (fallback.broken) throw failure('claw-router/no-safe-fallback', 'Standard preset unavailable; task was not admitted.');
      if (actual(agent) !== 'standard') await ctx.agentPresets.select(agent, 'standard');
      selected = 'standard';
      reason = 'safe_fallback';
    }
    signal?.throwIfAborted();
    if (actual(agent) !== selected)
      throw failure('claw-router/selection-mismatch', 'Preset selection did not commit; task was not admitted.');
    const result = { ...decision, mode: selected, reason, switched: true, policy_version: VERSION };
    remember(agent.id, result);
    return result;
  }
  async function resolve(id, signal) {
    signal?.throwIfAborted();
    const result = await ctx.sessionController.resolveAgent(id);
    if (result.error) throw result.error;
    signal?.throwIfAborted();
    return result.agent;
  }
  return {
    async initialize(agent, source, signal) {
      if (source !== 'startup' || !isFresh(agent.session)) return;
      await serial(agent.id, () => choose(agent, { content: [] }, signal));
    },
    prepare(sessionId, message, signal) {
      return serial(sessionId, async () => choose(await resolve(sessionId, signal), message, signal));
    },
    // Selection AND native admission are serialized per session, not just selection.
    prompt(args, signal) {
      if (!validPromptForRouting(args))
        return ctx.typertGateway.invoke({
          namespace: 'session', method: 'prompt', args,
          peer: ctx.connection.operator, signal,
        }).then(value => ({ value }));
      return serial(args.request.sessionId, async () => {
        const agent = await resolve(args.request.sessionId, signal);
        const route = await choose(agent, { role: 'user', source: { kind: 'user' },
          content: args.request.content }, signal);
        signal?.throwIfAborted();
        // Preserve native validation, attachment admission, request-ID idempotency,
        // cancellation, model selection and source metadata. No direct followup().
        const value = await ctx.typertGateway.invoke({
          namespace: 'session', method: 'prompt', args,
          peer: ctx.connection.operator, signal,
        });
        return { value, route };
      });
    },
    status(agent, task) {
      return {
        enabled: true, policy_version: VERSION, workspace: ROOT,
        affects: 'new_unseeded_claw_sessions_only', classifier: 'deterministic_no_model_call',
        current_mode: agent ? actual(agent) : undefined,
        eligible: agent ? isFresh(agent.session) : undefined,
        last_decision: agent ? decisions.get(agent.id) ?? null : null,
        preview: typeof task === 'string' ? classify({ content: [{ type: 'text', text: task }] }) : undefined,
        review_guard_registered: !!ctx.tools.get('claw_review_guard_status'),
        writes_task_files: false,
      };
    },
    forget(agent) { decisions.delete(agent.id); },
  };
}

async function readJson(request) {
  if (!request.headers.get('content-type')?.toLowerCase().startsWith('application/json'))
    throw failure('gateway/bad-request', 'JSON content-type required.');
  const declared = Number(request.headers.get('content-length'));
  if (Number.isFinite(declared) && declared > MAX_BODY_BYTES)
    throw failure('claw-router/body-too-large', 'Prompt exceeds the bounded routing transport budget.');
  if (!request.body) throw failure('gateway/bad-request', 'Request body required.');
  const reader = request.body.getReader();
  const parts = [];
  let total = 0;
  const cancel = () => { void reader.cancel().catch(() => {}); };
  request.signal.addEventListener('abort', cancel, { once: true });
  try {
    while (true) {
      request.signal.throwIfAborted();
      const { value, done } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > MAX_BODY_BYTES) {
        cancel();
        throw failure('claw-router/body-too-large', 'Prompt exceeds the bounded routing transport budget.');
      }
      parts.push(value);
    }
    request.signal.throwIfAborted();
    const bytes = new Uint8Array(total);
    let offset = 0;
    for (const part of parts) { bytes.set(part, offset); offset += part.byteLength; }
    try { return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes)); }
    catch { throw failure('gateway/bad-request', 'Invalid JSON request.'); }
  } finally {
    request.signal.removeEventListener('abort', cancel);
    reader.releaseLock();
  }
}

const json = (body, status = 200, headers = {}) => new Response(JSON.stringify(body), {
  status, headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store', ...headers },
});

export function handlers(ctx, router) {
  return {
    async prompt(request) {
      let rpcId = 'invalid-request';
      try {
        const body = await readJson(request);
        if (typeof body?.rpcId === 'string') rpcId = body.rpcId;
        if (body?.type !== 'client-request' || typeof body.rpcId !== 'string' ||
            body.method !== 'session/prompt' || !body.payload ||
            Object.keys(body.payload).length !== 1 || !body.payload.args ||
            typeof body.payload.args !== 'object' || Array.isArray(body.payload.args))
          throw failure('gateway/bad-request', 'Invalid session/prompt RPC envelope.');
        const { value, route } = await router.prompt(body.payload.args, request.signal);
        return json({ type: 'server-response', rpcId, result: { ok: true, value } }, 200,
          route ? { 'x-claw-task-mode': route.mode ?? 'unchanged', 'x-claw-task-reason': route.reason } : {});
      } catch (error) {
        const native = ctx.typertGateway.wireStream.failure(error);
        if (error.code?.startsWith('claw-router/'))
          return json({ type: 'server-response', rpcId, result: { ok: false,
            error: { code: error.code, message: error.message, details: {} } } });
        return json({ type: 'server-response', rpcId, result: { ok: false, error: native } });
      }
    },
    // Authenticated diagnostic: real blank-session selection, never admits a prompt.
    async prepare(request) {
      try {
        const body = await readJson(request);
        if (typeof body?.sessionId !== 'string' || !body.sessionId ||
            typeof body.task !== 'string' || Buffer.byteLength(body.task) > 32 * 1024)
          throw failure('gateway/bad-request', 'Bounded sessionId and task required.');
        const route = await router.prepare(body.sessionId,
          { role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text: body.task }] }, request.signal);
        return json({ ...route, admitted_prompt: false });
      } catch (error) {
        return json({ error: { code: error.code ?? 'claw-router/prepare-failed',
          message: 'Routing preparation did not complete; no prompt was admitted.' } }, 409);
      }
    },
  };
}

export function apply(ctx) {
  const router = createRouter(ctx), http = handlers(ctx, router);
  ctx.connection.fetch.register({ path: PROMPT_PATH, methods: ['POST'], requestBody: 'streaming', fetch: http.prompt });
  ctx.connection.fetch.register({ path: PREPARE_PATH, methods: ['POST'], requestBody: 'streaming', fetch: http.prepare });
  ctx.on('agent/created', async ({ agent, source, signal }) => {
    await router.initialize(agent, source, signal);
  });
  ctx.on('agent/disposed', ({ agent }) => router.forget(agent));
  // Additional deny-only belt: switching presets never weakens unattended scope.
  ctx.tools.guard(exec => {
    if (!reviewPhase(exec.agent?.session)) return undefined;
    if (!ctx.tools.get('claw_review_guard_status'))
      return 'CLAW_TASK_ROUTER_READ_ONLY: review guard missing; unattended tools are blocked';
    return denyReason(ROOT, exec.name, exec.arguments);
  });
  ctx.tools.register({
    name: 'claw_task_router_status',
    description: 'Read Claw new-session task routing status or preview classification. Never switches a preset or starts a task.',
    parameters: { type: 'object', properties: { task: { type: 'string', maxLength: 32768 } }, additionalProperties: false },
    output: { schema: { type: 'object' }, render: (_, value) => [{ type: 'text', text: JSON.stringify(value) }] },
    execute: (args, exec) => router.status(exec.agent, args.task),
  });
}
