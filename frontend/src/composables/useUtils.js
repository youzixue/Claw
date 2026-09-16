import dayjs from 'dayjs'

/** 格式化涨跌幅 — 中国A股: 红涨绿跌 */
export function formatChange(pct) {
  if (pct == null || isNaN(pct)) return '--'
  const val = pct.toFixed(2)
  return pct >= 0 ? `+${val}%` : `${val}%`
}

/** 涨跌幅颜色类名 */
export function changeColorClass(pct) {
  if (pct == null || isNaN(pct)) return ''
  return pct > 0 ? 'text-up' : pct < 0 ? 'text-down' : 'text-flat'
}

/** 格式化金额(亿) */
export function formatAmount(val) {
  if (val == null || isNaN(val)) return '--'
  if (Math.abs(val) >= 1e8) return (val / 1e8).toFixed(2) + '亿'
  if (Math.abs(val) >= 1e4) return (val / 1e4).toFixed(2) + '万'
  return val.toFixed(2)
}

/** 格式化日期 */
export function formatDate(d, fmt = 'YYYY-MM-DD') {
  if (!d) return '--'
  return dayjs(d).format(fmt)
}

/** 股票代码标记 */
export function stockTagLabel(tag) {
  const map = {
    '✅': '可交易',
    tradeable: '可交易',
    '👁️': '仅观察',
    observe_only: '仅观察',
    '🚫': '不推',
    blocked: '不推',
  }
  return map[tag] || tag || ''
}

/** 情绪周期标签 */
export function sentimentCycleLabel(cycle) {
  const map = {
    climax: '亢奋',
    divergence: '分歧',
    freezing: '冰点',
    recovery: '修复',
    pending: '待定',
  }
  return map[cycle] || cycle
}

/** 情绪周期颜色 */
export function sentimentCycleColor(cycle) {
  const map = {
    climax: '#f56c6c',
    divergence: '#e6a23c',
    freezing: '#909399',
    recovery: '#67c23a',
    pending: '#c0c4cc',
  }
  return map[cycle] || '#c0c4cc'
}

/** 信号等级颜色 */
export function levelColor(level) {
  const map = { T: '#f56c6c', S: '#f56c6c', A: '#e6a23c', B: '#409eff', C: '#909399', D: '#c0c4cc' }
  return map[level] || '#909399'
}

/** 封板质量标签 */
export function sealQualityLabel(q) {
  const map = { solid: '一字', good: '优质', medium: '一般', loose: '偏弱', broken: '炸板' }
  return map[q] || q
}

/** 突破质量标签 */
export function breakthroughQualityLabel(q) {
  const map = { '🟢高质量': '高质量', '🟡待确认': '待确认', '🔴存疑': '存疑' }
  return map[q] || q
}

/** 限制输入为数字 */
export function toNumber(val) {
  const n = Number(val)
  return isNaN(n) ? 0 : n
}
