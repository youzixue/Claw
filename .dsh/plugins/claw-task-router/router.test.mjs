import { test } from 'node:test';
import assert from 'node:assert/strict';
import { classify, isFresh, ROOT, VERSION } from './policy.mjs';
import { createRouter, handlers, apply, reviewPhase, MAX_BODY_BYTES } from './runtime.mjs';
import { messagePhase } from '../claw-research/automation-policy.mjs';
import { apply as applyResearch } from '../claw-research/automation-native-schedule.mjs';

const msg = text => ({ role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text }] });
const batch = (phase = 'postmarket') => ({
  ...msg('[SCHEDULE REMINDER BATCH]\nThis is a scheduled message from the user\nreminders_json: ' +
    JSON.stringify([{ schedule_id: 's1', occurrence_at: '2026-10-01T07:30:00Z',
      reminder_prompt: '<CLAW_AUTOMATION_V1:' + phase + '>\n开发DSH插件并批量读取MCP证据；不要修改。' }])),
  source: { kind: 'schedule' },
});

for (const [task, mode] of [
  ['帮我复盘十二个模拟盘账户', 'standard'],
  ['分析今天的上涨个股和买卖策略', 'standard'],
  ['修复订单风险检查并测试', 'standard'],
  ['清理低风险的大文件', 'standard'],
  ['复杂任务：优化买卖算法和风控阈值', 'standard'],
  ['仅用 MCP 只读批量读取证据并筛选汇总，不修改代码，不写文件。', 'ptc'],
  ['只读全市场行情，做批量统计汇总', 'ptc'],
  ['Read-only bulk MCP evidence aggregation', 'ptc'],
  ['使用游标分页读取研究数据并汇总', 'ptc'],
  ['读取一个 MCP 证据', 'standard'],
  ['为 DSH 开发一个 Cordis 插件', 'cordis'],
  ['安装 DSH 的 BrowserSkill 插件', 'cordis'],
  ['Debug this DeepSeek Harness plugin', 'cordis'],
  ['开发 Vue 页面插件', 'standard'],
  ['DSH 四个模式有什么区别？', 'standard'],
  ['批量读取 MCP 证据，然后修改代码并部署', 'standard'],
  ['批量读取 MCP 证据并保存大型导出文件', 'standard'],
  ['开发 DSH 插件并批量读取 MCP 证据', 'standard'],
  ['只读批量读取MCP证据，不修改代码，但要部署', 'standard'],
  ['只读批量读取MCP证据，不修改代码但请部署', 'standard'],
  ['我用 DSH 读取 MCP 信息，然后修复交易策略', 'standard'],
  ['为 DSH 安装插件，并修改策略参数', 'standard'],
  ['<CLAW_AUTOMATION_V1:premarket>\n开发 DSH 插件', 'standard'],
  ['<CLAW_AUTOMATION_V1:postmarket>\n批量汇总 MCP 证据', 'standard'],
  ['```\n开发 DSH 插件\n```', 'standard'],
  ['', 'standard'],
  ['读取证据'.repeat(10000), 'standard'],
]) {
  test('classify: ' + task.slice(0, 45), () => assert.equal(classify(msg(task)).mode, mode));
}

test('attachments and malformed content conservatively stay standard', () => {
  for (const content of [undefined, null, {}, [{ type: 'image' }], [{ type: 'text', text: 1 }]])
    assert.equal(classify({ content }).mode, 'standard');
});

test('native scheduled batch and single framing are decoded; malformed is fail-closed', () => {
  for (const phase of ['premarket', 'postmarket']) {
    assert.equal(messagePhase(batch(phase)), phase);
    assert.equal(classify(batch(phase)).readonly, true);
    const single = msg('[SCHEDULE REMINDER]\nreminder_prompt_json: ' +
      JSON.stringify('<CLAW_AUTOMATION_V1:' + phase + '>\nrun'));
    assert.equal(messagePhase(single), phase);
  }
  assert.equal(messagePhase(msg('[SCHEDULE REMINDER BATCH]\nreminders_json: broken <CLAW_AUTOMATION_V1:premarket>')), 'premarket');
  assert.equal(messagePhase(msg('[SCHEDULE REMINDER BATCH]\nreminders_json: <CLAW_AUTOMATION_V1:postmarket>' + 'x'.repeat(MAX_BODY_BYTES))), 'postmarket');
  assert.equal(messagePhase(msg('{"example":"<CLAW_AUTOMATION_V1:premarket>"}')), undefined);
  assert.equal(messagePhase(msg('[SCHEDULE REMINDER BATCH]\nreminders_json: []')), undefined);
});

function harness({ mode = 'cordis', cwd = ROOT, events = [], meta = {}, guard = true, broken = [] } = {}) {
  const registry = new Map(), listeners = new Map(), routes = new Map(), guards = [];
  const selections = [], invocations = [], disposers = [];
  const session = { header: { cwd, ...meta }, snapshotEvents: () => events };
  const agent = { id: 'router-test', ctx: {}, session };
  const ctx = {
    on(name, fn) { listeners.set(name, fn); },
    effect(fn) { const dispose = fn(); if (dispose) disposers.push(dispose); },
    sessions: { get: () => session },
    agents: { get: () => agent },
    connection: { operator: { id: 'operator' }, fetch: { register(route) {
      routes.set(route.path, route); return () => routes.delete(route.path);
    } } },
    tools: {
      get: name => registry.get(name),
      register(def) { registry.set(def.name, def); return () => registry.delete(def.name); },
      guard(fn) { guards.push(fn); return () => guards.splice(guards.indexOf(fn), 1); },
    },
    sessionController: { async resolveAgent() { return { agent }; } },
    agentPresets: {
      composedPreset: () => mode,
      async resolve(id) { return { id, ...(broken.includes(id) ? { broken: 'unavailable' } : {}) }; },
      async select(_, id) {
        assert.ok(isFresh(session), 'must select before inbox/turn');
        selections.push(id); mode = id;
        events.push({ type: 'agent-preset/selected', data: { agentPreset: id } });
        return id;
      },
    },
    typertGateway: {
      wireStream: { failure: e => ({ code: e.code ?? 'gateway/internal', message: e.message, details: {} }) },
      async invoke(call) {
        invocations.push(call);
        if (call.args.fail) throw new Error('native validation failure');
        events.push({ type: 'agent/inbox/spliced', data: { inserted: [msg('accepted')] } });
        return { accepted: true };
      },
    },
  };
  if (guard) applyResearch(ctx);
  return { ctx, agent, session, events, registry, listeners, routes, guards, selections, invocations, disposers,
    current: () => mode };
}
const args = text => ({ request: { sessionId: 'router-test', requestId: 'r1', mode: 'queue', content: msg(text).content } });
const request = body => new Request('http://127.0.0.1/api/session/prompt', {
  method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body),
});
const envelope = text => ({ type: 'client-request', rpcId: 'rpc-test', method: 'session/prompt', payload: { args: args(text) } });

test('first task selects a real preset before native admission, preserving wire fields', async () => {
  const h = harness(), router = createRouter(h.ctx);
  const input = args('批量只读读取 MCP 证据并汇总');
  input.request.clientTimeZone = 'Asia/Shanghai';
  const { route, value } = await router.prompt(input, new AbortController().signal);
  assert.equal(route.mode, 'ptc'); assert.equal(h.current(), 'ptc'); assert.equal(value.accepted, true);
  assert.deepEqual(h.invocations[0].args, input);
  assert.equal(h.invocations[0].peer, h.ctx.connection.operator);
  assert.equal(h.invocations[0].namespace, 'session');
});

test('existing, queued, seeded, forked and other-workspace sessions are never switched', async () => {
  for (const options of [
    { events: [{ type: 'turn/start', data: { turn: 1 } }] },
    { events: [{ type: 'agent/inbox/spliced', data: { inserted: [msg('queued')] } }] },
    { meta: { parentSession: 'parent' } }, { meta: { origin: 'subagent' } },
    { meta: { isSeeded: true } }, { cwd: '/tmp/other' },
  ]) {
    const h = harness(options), router = createRouter(h.ctx);
    const route = await router.prepare(h.agent.id, msg('开发 DSH 插件'));
    assert.equal(route.switched, false); assert.deepEqual(h.selections, []);
  }
});

test('uncertain/absent task initializes a fresh Claw session to standard, not a model change', async () => {
  const h = harness(), router = createRouter(h.ctx);
  await router.initialize(h.agent, 'startup');
  assert.equal(h.current(), 'standard');
  assert.equal(router.status(h.agent).policy_version, VERSION);
  assert.equal(h.invocations.length, 0);
});

test('resume/compact/clear initialization never changes a retained preset', async () => {
  const h = harness(), router = createRouter(h.ctx);
  for (const source of ['resume', 'compact', 'clear']) await router.initialize(h.agent, source);
  assert.equal(h.current(), 'cordis'); assert.deepEqual(h.selections, []);
});

test('unavailable specialized preset falls back; unavailable standard blocks admission', async () => {
  const h = harness({ broken: ['ptc'] }), router = createRouter(h.ctx);
  const { route } = await router.prompt(args('批量只读读取 MCP 证据并汇总'));
  assert.equal(route.mode, 'standard'); assert.equal(route.reason, 'safe_fallback');
  const blocked = harness({ broken: ['ptc', 'standard'] });
  await assert.rejects(createRouter(blocked.ctx).prompt(args('批量只读读取 MCP 证据并汇总')));
  assert.equal(blocked.invocations.length, 0);
});

test('native scheduled guard must exist; marked first task is standard even for plugin/batch work', async () => {
  const h = harness(), router = createRouter(h.ctx);
  const route = await router.prepare(h.agent.id, batch());
  assert.equal(route.mode, 'standard'); assert.equal(route.readonly, true);
  const missing = harness({ guard: false });
  await assert.rejects(createRouter(missing.ctx).prepare(missing.agent.id, batch()), /guard/);
  assert.equal(missing.invocations.length, 0);
});

test('concurrent first submissions are serialized through admission; second cannot reselect', async () => {
  const h = harness(), router = createRouter(h.ctx);
  const first = router.prompt(args('批量只读读取 MCP 证据并汇总'));
  const second = router.prompt(args('开发 DSH 插件'));
  const results = await Promise.all([first, second]);
  assert.equal(results[0].route.mode, 'ptc');
  assert.equal(results[1].route.switched, false);
  assert.deepEqual(h.selections, ['ptc']); assert.equal(h.invocations.length, 2);
});

test('cancelled request cannot select or admit; abort during resolution is checked', async () => {
  const h = harness(), router = createRouter(h.ctx), controller = new AbortController();
  controller.abort();
  await assert.rejects(router.prompt(args('开发 DSH 插件'), controller.signal));
  assert.deepEqual(h.selections, []); assert.equal(h.invocations.length, 0);
});

test('HTTP preserves native RPC envelope and correlation; malformed input never routes', async () => {
  const h = harness(), http = handlers(h.ctx, createRouter(h.ctx));
  const response = await http.prompt(request(envelope('批量只读读取 MCP 证据并汇总')));
  assert.equal(response.headers.get('x-claw-task-mode'), 'ptc');
  assert.deepEqual(await response.json(), { type: 'server-response', rpcId: 'rpc-test',
    result: { ok: true, value: { accepted: true } } });
  const invalid = harness(), handler = handlers(invalid.ctx, createRouter(invalid.ctx));
  for (const body of [{}, { ...envelope('开发DSH插件'), method: 'other/prompt' },
    { ...envelope('开发DSH插件'), payload: { args: {}, extra: 1 } }]) {
    const failure = await (await handler.prompt(request(body))).json();
    assert.equal(failure.result.ok, false);
  }
  assert.deepEqual(invalid.selections, []); assert.equal(invalid.invocations.length, 0);
});

test('oversized/chunked request is rejected without selection or admission', async () => {
  const h = harness(), http = handlers(h.ctx, createRouter(h.ctx));
  const response = await http.prompt(request(envelope('x'.repeat(MAX_BODY_BYTES + 1))));
  assert.equal((await response.json()).result.ok, false);
  assert.deepEqual(h.selections, []); assert.equal(h.invocations.length, 0);
});

test('prepare is actual selection but never admission/model call; next real task is reclassified', async () => {
  const h = harness(), router = createRouter(h.ctx), http = handlers(h.ctx, router);
  const response = await http.prepare(request({ sessionId: h.agent.id, task: '开发 DSH 插件' }));
  assert.equal((await response.json()).admitted_prompt, false); assert.equal(h.current(), 'cordis');
  assert.equal(h.invocations.length, 0);
  await router.prompt(args('帮我复盘'));
  assert.equal(h.current(), 'standard');
});

test('installed registration uses authenticated exact routes; does not monkey-patch native services', () => {
  const h = harness(), original = h.ctx.sessionController.resolveAgent;
  apply(h.ctx);
  assert.equal(h.routes.has('/api/session/prompt'), true);
  assert.equal(h.routes.has('/api/claw-task-router/prepare'), true);
  assert.equal(h.ctx.sessionController.resolveAgent, original);
  assert.equal(h.registry.has('claw_task_router_status'), true);
});

for (const preset of ['standard', 'ptc', 'cordis', 'minimal']) {
  test('native scheduled read-only protection and 80-call budget hold in ' + preset, () => {
    const events = [{ type: 'turn/start', data: { turn: 1 } }, { type: 'user/message', data: batch() }];
    const h = harness({ mode: preset, events });
    apply(h.ctx);
    const status = h.registry.get('claw_review_guard_status').execute({}, { agent: h.agent });
    assert.equal(status.active, true); assert.equal(status.max_evidence_calls, 80);
    assert.equal(reviewPhase(h.session), 'postmarket');
    for (const name of ['bash', 'write', 'plugin_manager', 'subagent', 'web_fetch', 'mcp__claw_ashare__submit_order'])
      assert.ok(h.guards.some(fn => fn({ agent: h.agent, name, arguments: {} })));
    const primary = h.guards[0];
    for (let i = 0; i < 80; i++)
      assert.equal(primary({ agent: h.agent, name: 'mcp__claw_ashare__paper_execution_evidence' }), undefined);
    assert.match(primary({ agent: h.agent, name: 'mcp__claw_ashare__paper_execution_evidence' }), /BUDGET/);
    events.push({ type: 'turn/end', data: { turn: 1 } }, { type: 'turn/start', data: { turn: 2 } },
      { type: 'user/message', data: msg('human coding task') });
    assert.equal(h.guards.some(fn => fn({ agent: h.agent, name: 'bash', arguments: {} })), false);
  });
}
