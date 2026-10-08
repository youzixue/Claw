import { test } from 'node:test'
import assert from 'node:assert/strict'
import { familyRoutes, normalizeReport, resultText, priceText, delayText, receiptCounts, labelFor, markText, markClass } from '../src/views/paper/candidateShadowModel.js'

test('future reference labels preserve numeric zero, percentage units and red-up green-down', () => {
  for (const [pct, shown, color] of [[2.5, '+2.50%', 'shadow-up'], [-1.2, '-1.20%', 'shadow-down'], [0, '0.00%', '']]) {
    const label = { status: 'observed', reference_markout_pct: pct, anchor_at: '2026-09-24T09:30:00' }
    assert.equal(markText(label), shown)
    assert.equal(markClass(label), color)
  }
  for (const value of [null, undefined, '', '0', false, NaN, Infinity]) {
    assert.match(markText({ status: 'observed', reference_markout_pct: value, anchor_at: '2026-09-24T09:30:00' }), /unknown/)
  }
  assert.match(markText({ status: 'unknown', reference_markout_pct: 0 }), /unknown/)
})

test('future labels are joined only to explicit same-session first-state anchors', () => {
  const label = { kind: 'future_label', frame_id: 'anchor', session_id: 'session', route: 'A', horizon_minutes: 5, status: 'observed', reference_markout_pct: 3, anchor_at: '2026-09-24T09:30:00' }
  const row = { frame_id: 'latest-frame', session_id: 'session', route: 'A', label_sampling: { anchor_frame_id: 'anchor', selected: false }, future_labels: [label] }
  assert.equal(markText(labelFor(row, 5)), '+3.00%')
  assert.match(markText(labelFor(row, 15)), /unknown/)
  for (const change of [{ frame_id: 'wrong' }, { session_id: 'wrong' }, { route: 'B' }]) {
    assert.match(markText(labelFor({ ...row, future_labels: [{ ...label, ...change }] }, 5)), /unknown/)
  }
  assert.match(labelFor({ ...row, future_labels: [label, label] }, 5).reason, /冲突/)
  assert.match(markText(labelFor({ ...row, label_sampling: null }, 5)), /unknown/)
  assert.match(markText(labelFor({ ...row, future_labels: [label, { ...label, kind: 'label_censored', remaining_horizons: ['5'], reason: 'gap' }] }, 5)), /unknown/)
})

test('six family scopes never include another account or a shared portfolio', () => {
  assert.deepEqual(['default', 'promotion', 'mainline', 'auction', 'tenbagger', 'reversal'].map(familyRoutes),
    [['A', 'A2'], ['B', 'B2'], ['C', 'C2', 'C3'], ['D', 'D2'], ['E', 'E2'], ['F', 'F2']])
  assert.deepEqual(familyRoutes('shared'), [])
})

test('unknown and malformed leaves are not coerced into prices or confirmations', () => {
  for (const value of [null, undefined, '', false, {}, [], '0', NaN, Infinity, -1, 0]) {
    assert.equal(priceText(value), '未知 / 未接收')
  }
  assert.equal(priceText(10.2), '10.200')
  assert.match(resultText({ value: true, status: 'observed' }), /观察/)
  assert.doesNotMatch(resultText({ value: true, status: 'observed' }), /确认/)
  assert.match(resultText(null), /unknown/)
})

test('independent result roles never turn experiment observation into a formal buy point', () => {
  const yes = { value: true, status: 'observed' }
  const no = { value: false, status: 'control' }
  assert.equal(resultText(yes, 'baseline'), '原确认已观察（不代表成交）')
  assert.equal(resultText(yes, 'candidate'), '实验条件满足（不下单）')
  assert.equal(resultText(yes, 'early_observation'), '提前观察（非买点）')
  assert.equal(resultText(no, 'baseline'), '原确认未满足')
  assert.match(resultText(no, 'candidate'), /过滤/)
  for (const value of [null, {}, { value: 'true', status: 'observed' }, { value: true, status: 'unknown' }, { value: false, status: 'observed' }]) {
    assert.match(resultText(value, 'candidate'), /unknown/)
  }
})

test('v2 and freshness never convert missing evidence or elapsed confirmations into buy permission', () => {
  assert.equal(resultText({ value: true, status: 'observed' }, 'candidate_v2'), 'v2研究条件满足（不下单）')
  assert.equal(resultText({ value: true, status: 'observed' }, 'confirmation_freshness'), '原确认时效通过（非交易许可）')
  assert.equal(resultText({ value: false, status: 'control' }, 'confirmation_freshness'), '原确认时效未通过')
  for (const role of ['candidate_v2', 'confirmation_freshness']) {
    for (const value of [undefined, null, { value: null, status: 'unknown' }, { value: true, status: 'unknown' }]) {
      assert.match(resultText(value, role), /unknown/)
      assert.doesNotMatch(resultText(value, role), /条件满足|已过滤/)
    }
  }
})

test('research delay uses predicate-to-record clocks, never source-to-send latency', () => {
  assert.equal(delayText({ source_quote_at: '2026-09-24T09:30:00', observed_at: '2026-09-24T09:30:03.100', recorded_at: '2026-09-24T09:30:05.600' }), '2.500 秒')
  assert.equal(delayText({ observed_at: '2026-09-24T09:30:03+08:00', recorded_at: '2026-09-24T01:30:05.5Z' }), '2.500 秒')
  assert.equal(delayText({ observed_at: '2026-09-24T09:30:03', recorded_at: '2026-09-24T09:30:03' }), '0.000 秒')
  assert.match(delayText({ observed_at: '2026-09-24T09:30:03', recorded_at: '2026-09-24T09:30:02' }), /时钟异常/)
  assert.match(delayText({ observed_at: '2026-09-24T09:30:03', recorded_at: '2026-09-24T09:30:05Z' }), /口径不一致/)
  assert.match(delayText({}), /未知/)
})

test('bounded coverage receipts preserve unknown counts and route scope', () => {
  assert.equal(receiptCounts(null), '来源未提供')
  assert.equal(receiptCounts({ eligible: null, scanned: 0, invalid: false }), 'eligible：未知 · scanned：0 · invalid：未知')
  const report = normalizeReport({ rows: [], coverage: { recent_receipts: [
    null, { route: 'B', reason: 'wrong-route' }, ...Array.from({ length: 25 }, () => ({ route: 'D', reason: 'auction_source_missing' })),
  ] } }, 'D')
  assert.equal(report.receipts.length, 20)
  assert.equal(report.receipts[0].reason, 'auction_source_missing')
  assert.deepEqual(normalizeReport({ rows: [] }, 'D').receipts, [])
})

test('malformed envelope, date mismatch and cross-route records fail closed', () => {
  assert.ok(normalizeReport(null, 'A').error)
  assert.ok(normalizeReport({ rows: {} }, 'A').error)
  assert.ok(normalizeReport({ rows: [], route: 'B' }, 'A').error)
  assert.equal(normalizeReport({ rows: Array.from({ length: 201 }, () => ({ route: 'A' })) }, 'A').rows.length, 200)
  assert.equal(normalizeReport({ rows: Array.from({ length: 201 }, () => ({ route: 'A' })) }, 'A').truncated, true)
  assert.ok(normalizeReport({ rows: [], trade_date: '2026-09-23' }, 'A', '2026-09-24').error)
  const result = normalizeReport({ rows: [null, [], { route: 'B' }, { route: 'A', code: '000001' }] }, 'A')
  assert.equal(result.rows.length, 1)
  assert.match(result.warnings[0], /覆盖不完整/)
  assert.deepEqual(result.status, {})
})
