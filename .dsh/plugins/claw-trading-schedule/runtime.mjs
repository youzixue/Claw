import fs from 'node:fs/promises';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { ROOT, VERSION, RESEARCH_REPAIR_VERSION, shanghaiDay, reminderAdmission } from './policy.mjs?repair-20261008-v1';
import { TradingScheduleManager } from './manager.mjs?repair-20261008-v1';

export const name = 'claw-trading-schedule';
export const inject = ['schedule', 'timer', 'tools'];
const DIRECTORY = path.join(ROOT, '.dsh/plugins/claw-trading-schedule');
const STATE = path.join(DIRECTORY, 'state.json');
const runFile = promisify(execFile);
async function readCalendar(day, signal) {
  const { stdout } = await runFile('/Library/Frameworks/Python.framework/Versions/3.11/bin/python3',
    [fileURLToPath(new URL('./calendar_reader.py', import.meta.url)), day],
    { timeout: 5000, maxBuffer: 64 * 1024, signal,
      env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' } });
  const value = JSON.parse(stdout);
  if (value.source !== 'stored_trade_calendar_no_sync' || value.read_only !== true ||
      !Array.isArray(value.rows) || value.rows.length > 371) throw new Error('invalid calendar projection');
  return value;
}
async function load() {
  if (await fs.realpath(DIRECTORY) !== DIRECTORY) throw new Error('unsafe schedule directory');
  try {
    const info = await fs.lstat(STATE);
    if (!info.isFile() || info.size > 64 * 1024) throw new Error('invalid cursor file');
    return JSON.parse(await fs.readFile(STATE, 'utf8'));
  } catch (e) {
    if (e.code === 'ENOENT') return { version: VERSION, phases: {} };
    throw e;
  }
}
async function save(value) {
  if (await fs.realpath(DIRECTORY) !== DIRECTORY) throw new Error('unsafe schedule directory');
  const data = JSON.stringify(value, null, 2) + '\n';
  if (Buffer.byteLength(data) > 64 * 1024) throw new Error('cursor byte budget exceeded');
  const temporary = STATE + '.tmp';
  const handle = await fs.open(temporary, 'w', 0o600);
  try { await handle.writeFile(data); await handle.sync(); } finally { await handle.close(); }
  await fs.rename(temporary, STATE);
}
export function phaseStatus(cursor) {
  return { status: cursor.status, sessionId: cursor.sessionId, id: cursor.id,
    scheduledAt: cursor.record?.scheduledAt, kind: cursor.record?.kind,
    prompt_has_repair_marker: cursor.record?.prompt?.includes(RESEARCH_REPAIR_VERSION) ?? false,
    prompt_scope: 'last_manager_observed_record_not_task_execution_receipt',
    missing_date: cursor.missing_date ?? null };
}
export async function apply(ctx) {
  const manager = new TradingScheduleManager({ schedule: ctx.schedule, load, save, readCalendar });
  let running, requested = false, disposed = false, lastError = null;
  const reconcile = () => {
    requested = true;
    if (running) return running;
    running = (async () => {
      while (requested && !disposed) {
        requested = false;
        try { await manager.reconcile(); lastError = null; }
        catch (e) { lastError = String(e); ctx.logger.warn('Claw trading schedule: ' + lastError); }
      }
    })().finally(() => { running = undefined; });
    return running;
  };
  const changed = ctx.on('schedule/changed', () => { void reconcile(); });
  // Calendar reads are small local SELECTs. This timer never invokes a model or the backend collectors.
  const interval = ctx.timer.interval(() => { void reconcile(); }, 15 * 60 * 1000);
  const gate = ctx.on('agent/pre-step', async (payload, next) => {
    const decision = await next();
    if (decision.kind !== 'enter') return decision;
    const relevant = decision.messages.some(message => message.source?.kind === 'schedule' &&
      (message.content ?? []).some(block => block.type === 'text' &&
        block.text.includes('CLAW_TRADING_SCHEDULE_V1:')));
    if (!relevant) return decision;
    let calendar;
    try { calendar = await readCalendar(shanghaiDay(Date.now()), payload.signal); }
    catch { calendar = { rows: [] }; }
    const messages = decision.messages.filter(message => {
      const reason = reminderAdmission(message, payload.agent.id,
        manager.state?.phases ?? {}, calendar, Date.now());
      if (reason) ctx.logger.warn('Claw reminder rejected before model: ' + reason);
      return !reason;
    });
    return messages.length ? { ...decision, messages } : { kind: 'reject' };
  });
  const status = ctx.tools.register({
    name: 'claw_trading_schedule_status',
    description: 'Read rolling Claw reminder targets and bounded stored-calendar coverage. No model delivery, refresh or schedule writes.',
    parameters: { type: 'object', properties: {}, additionalProperties: false },
    output: { schema: { type: 'object' },
      render: (_, value) => [{ type: 'text', text: JSON.stringify(value) }] },
    execute: async () => ({
      version: VERSION, source_revision: RESEARCH_REPAIR_VERSION, installed: true, error: lastError,
      checked_at: manager.state?.checked_at, calendar: manager.state?.calendar,
      phases: Object.fromEntries(Object.entries(manager.state?.phases ?? {}).map(([phase, cursor]) =>
        [phase, phaseStatus(cursor)])),
    }),
  });
  ctx.effect(() => async () => {
    disposed = true; changed(); interval(); gate(); status();
    await running;
  });
  await reconcile();
  if (lastError) throw new Error(lastError);
}
