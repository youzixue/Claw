import { PHASES, VERSION, recordOf, equal, metadata, markedPrompt,
  nextOccurrence, expectedAt, shanghaiDay } from './policy.mjs?repair-20261008-v1';

export class TradingScheduleManager {
  constructor({ schedule, load, save, readCalendar, now = Date.now }) {
    Object.assign(this, { schedule, load, save, readCalendar, now });
    this.state = undefined;
  }
  async persist() { await this.save(this.state); }
  async reconcile() {
    this.state ??= await this.load();
    if (this.state.version !== VERSION || !this.state.phases) throw new Error('invalid cursor state');
    const now = this.now();
    const calendar = await this.readCalendar(shanghaiDay(now));
    this.state.calendar = { source: calendar.source, sha256: calendar.sha256,
      row_count: calendar.row_count, last_stored_date: calendar.last_stored_date };
    const catalog = await this.schedule.catalog();
    if (new Set(catalog.map(row => row.id)).size !== catalog.length)
      throw new Error('ambiguous native schedule catalog');
    for (const phase of Object.keys(PHASES)) {
      await this.reconcilePhase(phase, catalog, calendar, now);
    }
    this.state.checked_at = new Date(now).toISOString();
    await this.persist();
    return this.state;
  }
  async reconcilePhase(phase, catalog, calendar, now) {
    let cursor = this.state.phases[phase];
    if (!cursor) {
      const root = catalog.find(row => row.id === PHASES[phase].rootId);
      if (!root || root.status !== 'active' ||
          !root.prompt.startsWith('<CLAW_AUTOMATION_V1:' + phase + '>')) {
        this.state.phases[phase] = { status: 'stopped_root_missing_or_invalid' };
        return;
      }
      cursor = this.state.phases[phase] = { sessionId: root.sessionId, id: root.id,
        record: recordOf(root), status: 'adopted' };
      // Persist the exact original binding before making any native management write.
      await this.persist();
    }
    if (!cursor.id || cursor.status.startsWith('stopped_')) return;
    let row = catalog.find(value => value.id === cursor.id && value.sessionId === cursor.sessionId);
    // Recover a committed create after a crash between native storage and our cursor.
    if (cursor.pendingCreate) {
      const p = cursor.pendingCreate;
      const children = catalog.filter(value => value.sessionId === cursor.sessionId &&
        metadata(value.prompt)?.parent === cursor.id &&
        metadata(value.prompt)?.rootId === PHASES[phase].rootId &&
        value.title === p.title && value.prompt === p.prompt && value.scheduledAt === p.scheduledAt);
      if (children.length === 1) {
        row = children[0];
        Object.assign(cursor, { id: row.id, record: recordOf(row), status: 'recovered_create' });
        delete cursor.pendingCreate;
        await this.persist();
      } else {
        cursor.status = children.length ? 'stopped_ambiguous_children' : 'stopped_uncertain_create';
        return; // Never blindly retry a cross-store create or resurrect a deleted child.
      }
    }
    if (!row) { cursor.status = 'stopped_deleted'; return; }
    if (cursor.pendingUpdate && equal(recordOf(row), expectedAt(cursor.record, cursor.pendingUpdate))) {
      cursor.record = recordOf(row);
      delete cursor.pendingUpdate;
      await this.persist();
    }
    if (!equal(recordOf(row), cursor.record)) {
      cursor.status = 'stopped_manual_edit';
      return; // A human timing/title/prompt edit takes precedence over auto-rolling.
    }
    const deliveredDay = row.status === 'inactive' && row.lastDelivery
      ? shanghaiDay(Date.parse(row.lastDelivery.scheduledAt)) : undefined;
    if (row.status === 'inactive' && !row.lastDelivery) {
      cursor.status = 'stopped_inactive_without_receipt';
      return;
    }
    // Do not move a currently due one-shot out from under the native delivery FIFO.
    if (row.status === 'active' && row.kind === 'at' && Date.parse(row.scheduledAt) <= now) {
      cursor.status = 'awaiting_native_delivery';
      return;
    }
    const plan = nextOccurrence(calendar, phase, now, deliveredDay);
    if (plan.status !== 'scheduled') {
      cursor.status = plan.status;
      cursor.missing_date = plan.missing_date ?? null;
      // An existing one-shot is not silently deleted (and its saved history is retained).
      // The pre-step gate also checks a fresh calendar before any model request.
      return;
    }
    delete cursor.missing_date;
    if (row.status === 'active') {
      const repaired = markedPrompt(row.prompt, phase,
        metadata(row.prompt)?.parent ?? row.id, plan.trade_date);
      if (row.kind === 'at' && row.scheduledAt === plan.scheduledAt &&
          metadata(row.prompt)?.version === VERSION && row.prompt === repaired) {
        cursor.status = 'scheduled';
        return;
      }
      const pending = { scheduledAt: plan.scheduledAt, prompt: markedPrompt(row.prompt, phase,
        metadata(row.prompt)?.parent ?? row.id, plan.trade_date) };
      cursor.pendingUpdate = pending;
      await this.persist();
      const result = await this.schedule.update({ sessionId: cursor.sessionId, id: row.id,
        expected: cursor.record, prompt: pending.prompt,
        change: { kind: 'at', at: plan.at } });
      if (!result.record) {
        cursor.status = 'stopped_update_' + (result.code ?? 'failed');
        return;
      }
      cursor.record = result.record;
      delete cursor.pendingUpdate;
      cursor.status = 'scheduled';
      await this.persist();
      return;
    }
    const pending = { title: row.title,
      prompt: markedPrompt(row.prompt, phase, row.id, plan.trade_date),
      scheduledAt: plan.scheduledAt };
    cursor.pendingCreate = pending;
    await this.persist();
    // Public create preserves the original Session binding, without activating its Agent.
    const record = await this.schedule.create(cursor.sessionId, {
      title: pending.title, prompt: pending.prompt, at: plan.at });
    Object.assign(cursor, { id: record.id, record, status: 'scheduled' });
    delete cursor.pendingCreate;
    await this.persist();
  }
}
