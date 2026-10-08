// Rendering only: never reconstruct a trading signal or fill absent research values.
const families = {
  default: ['A', 'A2'], promotion: ['B', 'B2'], mainline: ['C', 'C2', 'C3'],
  auction: ['D', 'D2'], tenbagger: ['E', 'E2'], reversal: ['F', 'F2'],
}
export const familyRoutes = account => families[account] || []
export const object = value => value !== null && typeof value === 'object' && !Array.isArray(value) ? value : {}
export const text = (value, fallback = '未知 / 未接收') => typeof value === 'string' && value.trim() ? value : typeof value === 'number' && Number.isFinite(value) ? String(value) : fallback
export const flag = (value, yes, no) => value === true ? yes : value === false ? no : '未知'
const number = value => typeof value === 'number' && Number.isFinite(value) ? value : null
export const stateText = value => ({
  observed: '观察', observation: '观察', confirmed: '确认（研究）', filtered: '过滤',
  control: '对照 / 条件未满足', unknown: 'unknown（证据不足）',
  not_open: '未开盘', not_received: '未接收', missing: '缺失',
  pending: '尚未到期', disabled: '未启用', partial: '部分覆盖（不完整）',
  not_started: '尚未启动 / 未接收', unavailable: '未提供',
})[value] || text(value, 'unknown（未接收）')
export function resultText(value, role) {
  const result = object(value)
  const positive = {
    baseline: '原确认已观察（不代表成交）',
    candidate: '实验条件满足（不下单）',
    candidate_v2: 'v2研究条件满足（不下单）',
    confirmation_freshness: '原确认时效通过（非交易许可）',
    early_observation: '提前观察（非买点）',
  }
  const negative = {
    baseline: '原确认未满足',
    candidate: '实验过滤 / 条件未满足',
    candidate_v2: 'v2条件未满足 / 已失效',
    confirmation_freshness: '原确认时效未通过',
    early_observation: '提前观察条件未满足',
  }
  if (result.value === true && result.status === 'observed') return positive[role] || '观察（非买点）'
  if (result.value === false && result.status === 'control') return negative[role] || '条件未满足'
  return 'unknown（证据不足 / 状态未确认）'
}
export const priceText = value => number(value) !== null && value > 0 ? value.toFixed(3) : '未知 / 未接收'
export const markColumns = [
  { key: 5, label: '5分钟锚点参考涨跌' }, { key: 15, label: '15分钟锚点参考涨跌' },
  { key: 30, label: '30分钟锚点参考涨跌' },
]
// Contract: future_labels carry percentage points (not fractions). They belong
// to the explicitly linked first-state anchor, never to a repeated row's price.
export function labelFor(row, horizon) {
  const anchor = row?.label_sampling?.anchor_frame_id
  const session = row?.session_id
  if (typeof anchor !== 'string' || !anchor || typeof session !== 'string' || !session) return { status: 'unknown', reason: '标签锚点或会话未提供' }
  const labels = Array.isArray(row.future_labels) ? row.future_labels : []
  const matched = labels.filter(label => object(label) === label && label.frame_id === anchor && label.session_id === session && label.route === row.route)
  const outcomes = matched.filter(label => label.kind === 'future_label' && label.horizon_minutes === horizon)
  const censored = matched.find(label => label.kind === 'label_censored' && Array.isArray(label.remaining_horizons) && label.remaining_horizons.includes(String(horizon)))
  if (censored) return { status: 'unknown', reason: text(censored.reason, '标签右删失 / 覆盖不足') }
  if (outcomes.length > 1) return { status: 'unknown', reason: '同锚点标签冲突，未采用' }
  return outcomes[0] || { status: 'unknown', reason: '未到期 / 未接收有效标签，不推断零涨跌' }
}
export function markText(value) {
  const mark = object(value)
  const pct = number(mark.reference_markout_pct)
  return mark.status === 'observed' && pct !== null && typeof mark.anchor_at === 'string' && mark.anchor_at
    ? (pct > 0 ? '+' : '') + pct.toFixed(2) + '%'
    : 'unknown（未到期 / 断档 / 未提供）'
}
export function markClass(value) {
  if (markText(value).startsWith('unknown')) return ''
  return value.reference_markout_pct > 0 ? 'shadow-up' : value.reference_markout_pct < 0 ? 'shadow-down' : ''
}
export function delayText(row) {
  const observed = row?.observed_at
  const recorded = row?.recorded_at
  // Backend naive timestamps are exchange-local wall clocks. Treat both in the
  // same neutral zone for subtraction, never let the browser timezone shift one.
  const naive = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?$/
  const zoned = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/
  if (typeof observed !== 'string' || typeof recorded !== 'string') return '未知 / 未接收'
  const bothNaive = naive.test(observed) && naive.test(recorded)
  if (!bothNaive && !(zoned.test(observed) && zoned.test(recorded))) return '未知 / 时钟口径不一致'
  const milliseconds = Date.parse(recorded + (bothNaive ? 'Z' : '')) - Date.parse(observed + (bothNaive ? 'Z' : ''))
  return Number.isFinite(milliseconds) && milliseconds >= 0 ? (milliseconds / 1000).toFixed(3) + ' 秒' : '未知 / 时钟异常'
}
export function coverageText(value) {
  const coverage = object(value)
  return [stateText(coverage.status), text(coverage.reason, '覆盖分母未提供，不能据记录数推断完整覆盖')].join(' · ')
}
export function receiptCounts(value) {
  const fields = Object.entries(object(value)).slice(0, 20)
  return fields.length ? fields.map(([key, count]) => key + '：' + (number(count) !== null && count >= 0 ? String(count) : '未知')).join(' · ') : '来源未提供'
}
export function normalizeReport(payload, route, requestedDate = '') {
  const data = object(payload)
  if (!Array.isArray(data.rows)) return { route, error: '研究响应结构异常：未接收 rows 数组；不推断零命中。' }
  if (requestedDate && data.trade_date !== requestedDate) return { route, error: '研究响应交易日不匹配，已隐藏结果。' }
  if (data.route != null && data.route !== route) return { route, error: '研究响应路线不匹配，已隐藏结果。' }
  const valid = data.rows.filter(row => object(row) === row && row.route === route)
  const warnings = Array.isArray(data.warnings) ? data.warnings.map(value => text(value, '')).filter(Boolean) : []
  if (Array.isArray(data.errors)) data.errors.slice(0, 20).forEach(error => warnings.push('研究读取异常：' + text(error?.error, text(error)) + ' · ' + text(error?.file, '文件未提供')))
  if (data.raw_evidence_omitted === true) warnings.push('接口省略原始大证据；完整证据保留在独立不可变文件。')
  if (valid.length !== data.rows.length) warnings.push('响应含无效或其他路线记录，已隐藏；当前覆盖不完整。')
  const receipts = Array.isArray(data.coverage?.recent_receipts)
    ? data.coverage.recent_receipts.filter(row => object(row) === row && (row.route == null || row.route === route)).slice(0, 20) : []
  return {
    receipts, route, trade_date: text(data.trade_date, ''), status: object(data.status),
    coverage: object(data.coverage), rows: valid.slice(0, 200), warnings, truncated: data.truncated === true || valid.length > 200,
  }
}
