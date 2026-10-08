/** Opt-in, temporary Host smoke verification; no model calls or business data. */
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { writeFile } from 'node:fs/promises';

export const name = 'claw-task-router-verification';
export const inject = ['agents', 'agentPresets', 'connection', 'tools'];
const ROOT = '/Users/youzix/WorkBuddy/Claw';
const artifact = ROOT + '/.dsh/plugins/claw-task-router/verification/live-result-confirmed.json';

export async function apply(ctx) {
  const result = { protocol: 'claw_router_live_smoke_v1', started_at: new Date().toISOString(), cases: [] };
  let handle;
  try {
    handle = await ctx.agents.create({
      sessionId: randomUUID(), meta: { cwd: ROOT },
      setup: async agentCtx => { await ctx.agentPresets.mount(agentCtx, 'cordis'); },
    });
    const agent = handle.agent;
    result.verification_session = agent.id;
    const actual = () => ctx.agentPresets.composedPreset(agent.ctx);
    assert.equal(actual(), 'standard', 'startup initialization must choose standard');
    result.cases.push({ name: 'new_session_startup_default', actual: actual(), passed: true });
    const http = ctx.connection.createSharedFetchHandler('/api');
    const prepare = async (task, expected) => {
      const response = await http.fetch(new Request('http://127.0.0.1/api/claw-task-router/prepare', {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ sessionId: agent.id, task }),
      }));
      const value = await response.json();
      assert.equal(value.mode, expected, JSON.stringify(value));
      assert.equal(actual(), expected);
      assert.equal(value.admitted_prompt, false);
      result.cases.push({ name: 'live_prepare_' + expected, actual: actual(), passed: true });
    };
    await prepare('只读批量读取 MCP 证据并筛选汇总', 'ptc');
    await prepare('为 DSH 开发一个 Cordis 插件', 'cordis');
    await prepare('帮我复盘十二个模拟盘策略', 'standard');
    await prepare('只读批量读取MCP证据，不修改代码但请部署', 'standard');
    await prepare('我用 DSH 读取 MCP 信息，然后修复交易策略', 'standard');
    await prepare('<CLAW_AUTOMATION_V1:postmarket>\n为 DSH 开发插件并批量读取 MCP 证据', 'standard');
    // Exercise the real GUI submission route without admitting a task: an invalid
    // attachment must be rejected by native gateway validation AFTER real routing.
    const response = await http.fetch(new Request('http://127.0.0.1/api/session/prompt', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ type: 'client-request', rpcId: 'router-verification',
        method: 'session/prompt', payload: { args: { request: {
          sessionId: agent.id, requestId: randomUUID(), mode: 'queue',
          content: [{ type: 'text', text: '只读批量读取 MCP 证据并汇总' }, { type: 'image' }],
        } } } }),
    }));
    const value = await response.json();
    assert.equal(actual(), 'ptc');
    assert.equal(value.rpcId, 'router-verification');
    assert.equal(value.result.ok, false, 'invalid attachment must not admit a task');
    result.cases.push({ name: 'live_native_prompt_route_and_validation',
      actual: actual(), native_rejection_code: value.result.error?.code, passed: true });
    const events = agent.session.snapshotEvents();
    assert.equal(events.some(e => ['turn/start', 'user/message', 'assistant/message', 'tool/call'].includes(e.type)), false);
    result.model_calls = 0;
    result.admitted_prompts = 0;
    result.business_reads_or_writes = 0;
    result.session_events = events.length;
    result.guard_registered = !!ctx.tools.get('claw_review_guard_status');
    assert.ok(result.guard_registered);
    result.status = 'passed';
  } catch (error) {
    result.status = 'failed';
    result.error = String(error);
  } finally {
    await handle?.dispose();
    result.finished_at = new Date().toISOString();
    const data = JSON.stringify(result, null, 2) + '\n';
    assert.ok(Buffer.byteLength(data) < 8192);
    await writeFile(artifact, data, { flag: 'wx', mode: 0o600 });
  }
}
