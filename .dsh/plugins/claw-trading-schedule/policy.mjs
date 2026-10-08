/** Stored calendar only: a workday is never used as a trading-day fallback. */
export const ROOT = '/Users/youzix/WorkBuddy/Claw';
export const VERSION = 'claw_trading_schedule_v1';
export const PHASES = {
  premarket: { time: '08:00:00', rootId: 'schedule-a9739a9f-4a09-483f-aaf6-0352ee1dfa80' },
  postmarket: { time: '15:30:00', rootId: 'schedule-dc7d5659-6fe2-4656-b9ac-71cf633fea10' },
};
const PREFIX = '\nCLAW_TRADING_SCHEDULE_V1: ';
export const shanghaiDay = now => new Date(now + 8 * 3600000).toISOString().slice(0, 10);
export const recordOf = ({ sessionId, status, lastDelivery, ...record }) => record;
const canonical = value => Array.isArray(value) ? value.map(canonical) :
  value && typeof value === 'object' ? Object.fromEntries(Object.keys(value).sort()
    .map(key => [key, canonical(value[key])])) : value;
export const equal = (a, b) => JSON.stringify(canonical(a)) === JSON.stringify(canonical(b));
export function metadata(prompt) {
  const pos = prompt.lastIndexOf(PREFIX);
  if (pos < 0) return undefined;
  try { return JSON.parse(prompt.slice(pos + PREFIX.length)); } catch { return undefined; }
}
export function basePrompt(prompt) {
  const pos = prompt.lastIndexOf(PREFIX);
  return pos < 0 ? prompt : prompt.slice(0, pos);
}
export const RESEARCH_REPAIR_VERSION = 'claw_research_repair_20261008_v1';
export function repairedResearchPrompt(prompt, phase) {
  let base=basePrompt(prompt);
  if(base.includes(RESEARCH_REPAIR_VERSION)) return base;
  base=base.replace('本版complete_overnight_coverage=false，交易日只允许partial；09:15后用late_research。',
    '状态由真实覆盖门禁决定：缺失或未认证时partial，不人为强制交易日partial，更不能删除警告假装complete；09:15后用late_research。');
  const shared='\n\n'+RESEARCH_REPAIR_VERSION+'（本次修复指令优先于旧报告版式和读取口径）：\n'+
    '正文先给一句结论、3—5条关键事实和对判断的影响，再给情景与失效条件；技术诊断、长ID、版本哈希和逐笔证据放“## 附录：证据索引”，不塞进摘要。使用短段落和小表，不贴工具JSON。专用保存器返回html_path/markdown_path时优先链接可读HTML，其次Markdown，最后才是原始JSON；写明证据截止、生成时点与partial的具体原因。“任务结束/已保存”不等于采集、AI或覆盖均成功。\n'+
    '账户summary/current positions只按唯一active物理ID读取；closed同名账户是历史记录，不计为活动歧义、不合并资金；trades/trace保留历史物理ID。真正多个active或无active仍缺失/拦截，当前投影不倒填历史估值。\n'+
    '盘后区分官方源与腾讯/新浪供应商观察，后者可供研究但不是官方认证；保留采集selected、stored、valid、reader returned、truncated和失败分母，不把有限样本中位数当全市场。上线前未采证、尚未到自然窗口、漏跑、源失败分开归因；不得回填伪造旧截止可见性。\n';
  const news=phase==='premarket'?
    '采集与研究是两阶段：后端在08:00前主动采集原文并尝试NLP，本08:00任务只读已固化材料并主动完成本次研究分析。不得等后端付费AI恢复才分析已有原文，也不得在08:00之后爬取再倒填08:00。若采集未跑/版本未发布，明确缺口，不能把读缓存称“已爬取”。\n'+
    'ashare_premarket_context先context小样本，再section=news_page、limit=20按next_cursor读同日期同as_of的长假窗口；不得因一次响应截断把25条统计/5条详读当全窗口。记录候选总量、已扫页/验证通过/拒绝/正文截断/剩余未评估；预算不足保存partial，不能谎称全量。旧MCP不支持分页时标“接口版本待发布”，不反复扩大limit。原文采集收到比发布时间晚是正常轮询延迟，只有首次可用晚于截止才不得入该截止事实。\n'+
    '对截止前原文独立做“本次研究模型研判”：事实、关联机制、受益假设/风险分别列；后端analyzed、mixed、keyword/fallback/未分析与本次DSH研判分列，不把关键词当大模型成功，不写业务NLP版本或交易催化门禁。HTTP402只称payment_required，余额原因未核实，不自动换供应商/密钥或付费。\n'+
    'external.forward_observations是本地何时看到指数响应的可追溯原文，不是regular/after-hours会话认证；源站本地行情时间未知时区不换算成上海钟。小波动/零涨幅也应进入目标指数采证，不当新闻条数。美股期货/完整美股盘后未接入仍明确unavailable。\n':'';
  return base+shared+news;
}
export function markedPrompt(prompt, phase, parent, day) {
  return repairedResearchPrompt(prompt,phase) + PREFIX + JSON.stringify({
    version: VERSION, phase, rootId: PHASES[phase].rootId, parent, trade_date: day,
    instruction: '由Host按已存交易日历滚动安排。occurrence_at的上海日期必须等于当前上海自然日；跨日迟到停止，不补做旧任务或冒充今日自然成功。保留原只读门禁。',
  });
}
function nextDay(day) {
  return new Date(Date.parse(day + 'T12:00:00Z') + 86400000).toISOString().slice(0, 10);
}
export function nextOccurrence(calendar, phase, now, afterDay) {
  if (!PHASES[phase] || !Number.isFinite(now)) throw new Error('invalid phase/clock');
  const days = new Map(calendar.rows);
  if (days.size !== calendar.rows.length) throw new Error('duplicate calendar dates');
  let day = shanghaiDay(now);
  // Full natural-date continuity is required; a missing weekend is not inferred closed.
  for (let count = 0; count <= 370; count++, day = nextDay(day)) {
    if (afterDay && day <= afterDay) continue;
    if (!days.has(day) || typeof days.get(day) !== 'boolean')
      return { status: 'blocked_calendar_unknown', missing_date: day };
    const at = day + 'T' + PHASES[phase].time + '+08:00';
    if (days.get(day) && Date.parse(at) > now)
      return { status: 'scheduled', trade_date: day, at,
        scheduledAt: new Date(at).toISOString() };
  }
  return { status: 'blocked_calendar_horizon' };
}
export function expectedAt(record, pending) {
  return { id: record.id, kind: 'at', title: record.title,
    prompt: pending.prompt, scheduledAt: pending.scheduledAt };
}
/** Inspect only our actual native one-shot envelopes; ordinary human text is untouched. */
export function reminderAdmission(message, sessionId, phaseStates, calendar, now) {
  if (message?.source?.kind !== 'schedule') return undefined;
  const text = (message.content ?? []).filter(x => x.type === 'text').map(x => x.text).join('\n');
  if (!text.startsWith('[SCHEDULE REMINDER]\n')) return undefined;
  const lines = text.split('\n');
  const get = key => lines.find(x => x.startsWith(key))?.slice(key.length);
  let prompt, id;
  try {
    prompt = JSON.parse(get('reminder_prompt_json: '));
    id = JSON.parse(get('schedule_id_json: '));
  } catch { return 'invalid_native_envelope'; }
  const meta = metadata(prompt ?? '');
  if (!meta || meta.version !== VERSION || !PHASES[meta.phase] ||
      phaseStates[meta.phase]?.sessionId !== sessionId) return undefined;
  const occurrence = get('occurrence_at: ');
  if (!occurrence || !Number.isFinite(Date.parse(occurrence))) return 'invalid_occurrence';
  const day = shanghaiDay(Date.parse(occurrence));
  if (day !== meta.trade_date || day !== shanghaiDay(now)) return 'cross_day_delivery';
  const flag = new Map(calendar.rows).get(day);
  if (flag !== true) return flag === false ? 'non_trading_day' : 'calendar_unknown';
  if (!id) return 'missing_schedule_id';
  return undefined;
}
