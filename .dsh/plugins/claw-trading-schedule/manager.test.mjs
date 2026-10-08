import assert from 'node:assert/strict';
import { test } from 'node:test';
import { PHASES, VERSION, nextOccurrence, markedPrompt, metadata,
  reminderAdmission, shanghaiDay, recordOf, repairedResearchPrompt, RESEARCH_REPAIR_VERSION } from './policy.mjs';
import { TradingScheduleManager } from './manager.mjs';
import { messagePhase, denyReason } from '../claw-research/automation-policy.mjs';

const clock = day => Date.parse(day + '+08:00');
function calendar(days = 40) {
  const rows = [];
  for (let i = 0; i < days; i++) {
    const day = new Date(Date.parse('2026-10-01T00:00:00Z') + i * 86400000).toISOString().slice(0, 10);
    const dow = new Date(day).getUTCDay();
    rows.push([day, day > '2026-10-07' && dow !== 0 && dow !== 6]);
  }
  return { source: 'stored_trade_calendar_no_sync', read_only: true, rows,
    row_count: rows.length, sha256: 'c'.repeat(64), last_stored_date: rows.at(-1)?.[0] };
}
function fixture() {
  const rows = Object.entries(PHASES).map(([phase, config]) => ({
    id: config.rootId, sessionId: 'original', status: 'active', kind: 'weekly',
    title: 'Claw ' + phase, prompt: '<CLAW_AUTOMATION_V1:' + phase + '>\nretain readonly prompt',
    time: config.time + '.000', timeZone: 'Asia/Shanghai', weekdays: [1, 2, 3, 4, 5],
    scheduledAt: new Date(clock('2026-10-02T' + config.time)).toISOString(),
  }));
  let stored = { version: VERSION, phases: {} }, now = clock('2026-10-01T22:36:08');
  let cal = calendar(), writes = 0, creates = 0, saves = 0;
  const schedule = {
    catalog: async () => structuredClone(rows),
    update: async request => {
      const row = rows.find(x => x.id === request.id && x.sessionId === request.sessionId);
      assert.deepEqual(recordOf(row), request.expected);
      const record = { id: row.id, kind: 'at', title: row.title, prompt: request.prompt,
        scheduledAt: new Date(request.change.at).toISOString() };
      Object.keys(row).forEach(k => delete row[k]);
      Object.assign(row, record, { sessionId: request.sessionId, status: 'active' });
      writes++;
      return { id: row.id, updated: true, record };
    },
    create: async (sessionId, request) => {
      creates++;
      const record = { id: 'child-' + creates, kind: 'at', title: request.title,
        prompt: request.prompt, scheduledAt: new Date(request.at).toISOString() };
      rows.push({ ...record, sessionId, status: 'active' });
      return structuredClone(record);
    },
  };
  const options = { schedule, now: () => now, load: async () => structuredClone(stored),
    save: async state => { stored = structuredClone(state); saves++; },
    readCalendar: async () => cal };
  const manager = new TradingScheduleManager(options);
  return { rows, schedule, options, manager, setNow: x => { now = x; },
    setCalendar: x => { cal = x; }, stored: () => stored,
    counts: () => ({ writes, creates, saves }),
    deliver: phase => {
      const row = rows.find(x => x.id === stored.phases[phase].id);
      row.status = 'inactive';
      row.lastDelivery = { scheduledAt: row.scheduledAt, deliveredAt: new Date(now).toISOString(), messageId: 'receipt' };
    } };
}
test('repair prompt is idempotent, independent raw analysis and readable output keep readonly guard',()=>{
  const old='<CLAW_AUTOMATION_V1:premarket>\n不采集不下单。本版complete_overnight_coverage=false，交易日只允许partial；09:15后用late_research。';
  const value=repairedResearchPrompt(old,'premarket');
  assert.ok(value.includes(RESEARCH_REPAIR_VERSION));
  assert.ok(value.includes('section=news_page'));
  assert.ok(value.includes('本次研究模型研判'));
  assert.ok(value.includes('html_path'));
  assert.ok(!value.includes('交易日只允许partial'));
  assert.ok(value.includes('不采集不下单'));
  assert.equal(repairedResearchPrompt(value,'premarket'),value);
});
test('Shanghai clock, not host/UTC natural date', () => {
  assert.equal(shanghaiDay(Date.parse('2026-10-07T16:01:00Z')), '2026-10-08');
});
for (const phase of Object.keys(PHASES)) {
  test(phase + ': full National Day holiday skips to October 8', () => {
    const result = nextOccurrence(calendar(), phase, clock('2026-10-01T22:36:08'));
    assert.equal(result.trade_date, '2026-10-08');
    assert.match(result.at, new RegExp(PHASES[phase].time));
  });
  test(phase + ': October 10 adjusted work Saturday is not a trading day', () => {
    assert.equal(nextOccurrence(calendar(), phase, clock('2026-10-09T18:00:00')).trade_date, '2026-10-12');
  });
}
test('missing holiday row blocks instead of leaping to the next open row', () => {
  const cal = calendar(); cal.rows = cal.rows.filter(([day]) => day !== '2026-10-04');
  assert.deepEqual(nextOccurrence(cal, 'premarket', clock('2026-10-01T22:36:08')),
    { status: 'blocked_calendar_unknown', missing_date: '2026-10-04' });
});
test('empty calendar is not a weekday fallback', () => {
  assert.equal(nextOccurrence({ rows: [] }, 'premarket', clock('2026-10-01T22:36:08')).status,
    'blocked_calendar_unknown');
});
test('expired phase skips today; another phase can still schedule today', () => {
  const now = clock('2026-10-08T08:10:00');
  assert.equal(nextOccurrence(calendar(), 'premarket', now).trade_date, '2026-10-09');
  assert.equal(nextOccurrence(calendar(), 'postmarket', now).trade_date, '2026-10-08');
});
test('at exact phase time creation is strictly future', () => {
  assert.equal(nextOccurrence(calendar(), 'premarket', clock('2026-10-08T08:00:00')).trade_date, '2026-10-09');
});
test('clock rollback never rearms a delivered date', () => {
  assert.equal(nextOccurrence(calendar(), 'premarket', clock('2026-10-08T07:00:00'),
    '2026-10-08').trade_date, '2026-10-09');
});
test('duplicate calendar dates reject ambiguous input', () => {
  assert.throws(() => nextOccurrence({ rows: [['2026-10-01', false], ['2026-10-01', true]] },
    'premarket', clock('2026-10-01T06:00:00')));
});
test('initial conversion preserves two ids, binding and original instructions', async () => {
  const f = fixture(); await f.manager.reconcile();
  assert.equal(f.counts().writes, 2); assert.equal(f.counts().creates, 0);
  for (const [phase, c] of Object.entries(f.stored().phases)) {
    assert.equal(c.id, PHASES[phase].rootId); assert.equal(c.sessionId, 'original');
    assert.equal(c.record.kind, 'at');
    assert.equal(metadata(c.record.prompt).trade_date, '2026-10-08');
    assert.ok(c.record.prompt.includes('retain readonly prompt'));
  }
});
test('reconcile and restart are no-op/idempotent for unchanged inputs', async () => {
  const f = fixture(); await f.manager.reconcile(); await f.manager.reconcile();
  await new TradingScheduleManager(f.options).reconcile();
  assert.equal(f.counts().writes, 2); assert.equal(f.counts().creates, 0);
});
test('native delivery rolls exactly once and keeps delivered row/history', async () => {
  const f = fixture(); await f.manager.reconcile();
  f.setNow(clock('2026-10-08T08:00:01')); f.deliver('premarket');
  await f.manager.reconcile(); await f.manager.reconcile();
  assert.equal(f.counts().creates, 1);
  assert.equal(f.stored().phases.premarket.record.scheduledAt, '2026-10-09T00:00:00.000Z');
  assert.equal(f.rows.filter(x => x.status === 'active').length, 2);
  assert.ok(f.rows.find(x => x.id === PHASES.premarket.rootId).lastDelivery);
});
test('both delivered phases skip weekend to October 12', async () => {
  const f = fixture(); await f.manager.reconcile();
  f.setNow(clock('2026-10-09T18:00:00')); f.deliver('premarket'); f.deliver('postmarket');
  await f.manager.reconcile();
  for (const c of Object.values(f.stored().phases)) assert.ok(c.record.scheduledAt.startsWith('2026-10-12'));
});
test('unknown next calendar day creates no replacement', async () => {
  const f = fixture(); await f.manager.reconcile();
  f.setNow(clock('2026-10-08T08:00:01')); f.deliver('premarket');
  f.setCalendar(calendar(8)); await f.manager.reconcile();
  assert.equal(f.counts().creates, 0);
  assert.equal(f.stored().phases.premarket.status, 'blocked_calendar_unknown');
});
test('native deletion is respected; restart never resurrects the task', async () => {
  const f = fixture(); await f.manager.reconcile();
  f.rows.splice(f.rows.findIndex(x => x.id === PHASES.premarket.rootId), 1);
  await f.manager.reconcile(); await new TradingScheduleManager(f.options).reconcile();
  assert.equal(f.stored().phases.premarket.status, 'stopped_deleted');
  assert.equal(f.counts().creates, 0);
});
test('manual timing or prompt edit stops management, never overwrites it', async () => {
  const f = fixture(); await f.manager.reconcile(); f.rows[0].prompt += '\nmanual edit';
  await f.manager.reconcile();
  assert.equal(f.stored().phases.premarket.status, 'stopped_manual_edit');
  assert.equal(f.counts().writes, 2);
});
test('due active one-shot is not moved before native acknowledgment', async () => {
  const f = fixture(); await f.manager.reconcile(); f.setNow(clock('2026-10-08T08:00:01'));
  await f.manager.reconcile();
  assert.equal(f.stored().phases.premarket.status, 'awaiting_native_delivery');
  assert.equal(f.counts().writes, 2); assert.equal(f.counts().creates, 0);
});
test('missing/invalid roots do not adopt unrelated reminders', async () => {
  const f = fixture(); f.rows[0].id = 'unrelated';
  await f.manager.reconcile(); assert.equal(f.counts().writes, 1);
  assert.equal(f.stored().phases.premarket.status, 'stopped_root_missing_or_invalid');
});
test('crash after native create recovers the unique child, never creates a duplicate', async () => {
  const f = fixture(); await f.manager.reconcile();
  f.setNow(clock('2026-10-08T08:00:01')); f.deliver('premarket');
  const baseSave = f.options.save;
  const broken = new TradingScheduleManager({ ...f.options, save: async state => {
    if (state.phases.premarket.id.startsWith('child-')) throw new Error('simulated cursor write crash');
    await baseSave(state);
  } });
  await assert.rejects(broken.reconcile());
  assert.ok(f.stored().phases.premarket.pendingCreate);
  assert.equal(f.counts().creates, 1);
  await new TradingScheduleManager(f.options).reconcile();
  assert.equal(f.counts().creates, 1);
  assert.equal(f.stored().phases.premarket.id, 'child-1');
});
test('uncertain create without a surviving child fails closed', async () => {
  const f = fixture(); await f.manager.reconcile();
  f.setNow(clock('2026-10-08T08:00:01')); f.deliver('premarket');
  const broken = new TradingScheduleManager({ ...f.options, schedule: {
    ...f.schedule, create: async () => { throw new Error('unknown commit outcome'); } } });
  await assert.rejects(broken.reconcile());
  await new TradingScheduleManager(f.options).reconcile();
  assert.equal(f.counts().creates, 0);
  assert.equal(f.stored().phases.premarket.status, 'stopped_uncertain_create');
});
test('crash after native update recovers its exact pending target', async () => {
  const f = fixture(), baseSave = f.options.save;
  const broken = new TradingScheduleManager({ ...f.options, save: async state => {
    if (state.phases.premarket.record.kind === 'at' && !state.phases.premarket.pendingUpdate)
      throw new Error('simulated cursor write crash');
    await baseSave(state);
  } });
  await assert.rejects(broken.reconcile());
  assert.ok(f.stored().phases.premarket.pendingUpdate);
  await new TradingScheduleManager(f.options).reconcile();
  assert.equal(f.counts().writes, 2); assert.equal(f.counts().creates, 0);
  assert.equal(f.stored().phases.premarket.status, 'scheduled');
});
test('native CAS conflict stops rather than overwriting the user', async () => {
  const f = fixture();
  const manager = new TradingScheduleManager({ ...f.options, schedule: {
    ...f.schedule, update: async req => ({ id: req.id, code: 'schedule_conflict', updated: false }) } });
  await manager.reconcile(); await manager.reconcile();
  assert.equal(f.counts().writes, 0);
  assert.equal(f.stored().phases.premarket.status, 'stopped_update_schedule_conflict');
});
function envelope(phase, day) {
  const prompt = markedPrompt('<CLAW_AUTOMATION_V1:' + phase + '>\nreadonly',
    phase, PHASES[phase].rootId, day);
  return { source: { kind: 'schedule' }, content: [{ type: 'text', text:
    '[SCHEDULE REMINDER]\nThis is a scheduled message from the user\n' +
    'schedule_id_json: "test"\noccurrence_at: ' + day + 'T' + PHASES[phase].time + '+08:00\n' +
    'reminder_prompt_json: ' + JSON.stringify(prompt) }] };
}
const states = { premarket: { sessionId: 'original' }, postmarket: { sessionId: 'original' } };
test('native one-shot still activates original readonly guard', () => {
  assert.equal(messagePhase(envelope('premarket', '2026-10-08')), 'premarket');
  assert.ok(denyReason('/Users/youzix/WorkBuddy/Claw', 'bash', {}));
  assert.ok(denyReason('/Users/youzix/WorkBuddy/Claw', 'schedule_create', {}));
});
test('same-day open reminder passes pre-model gate', () => {
  assert.equal(reminderAdmission(envelope('premarket', '2026-10-08'), 'original', states,
    calendar(), clock('2026-10-08T08:01:00')), undefined);
});
test('delayed cross-day reminder is rejected, not treated as today', () => {
  assert.equal(reminderAdmission(envelope('premarket', '2026-10-08'), 'original', states,
    calendar(), clock('2026-10-12T08:01:00')), 'cross_day_delivery');
});
test('holiday reminder is rejected before model execution', () => {
  assert.equal(reminderAdmission(envelope('premarket', '2026-10-02'), 'original', states,
    calendar(), clock('2026-10-02T08:01:00')), 'non_trading_day');
});
test('unknown calendar blocks pre-model execution', () => {
  assert.equal(reminderAdmission(envelope('premarket', '2026-10-08'), 'original', states,
    { rows: [] }, clock('2026-10-08T08:01:00')), 'calendar_unknown');
});
test('ordinary human/other-session work is not intercepted', () => {
  const message = envelope('premarket', '2026-10-02');
  assert.equal(reminderAdmission(message, 'another', states, calendar(), clock('2026-10-02T08:01:00')), undefined);
  message.source.kind = 'user';
  assert.equal(reminderAdmission(message, 'original', states, calendar(), clock('2026-10-02T08:01:00')), undefined);
});
