/** Conservative, deterministic first-task routing. No LLM, tools or disk writes. */
import { messagePhase } from '../claw-research/automation-policy.mjs?native-schedule-v2';

export const ROOT = '/Users/youzix/WorkBuddy/Claw';
export const VERSION = 'claw_task_router_v1';
export const MAX_TASK_BYTES = 32 * 1024;
export const MODES = ['standard', 'ptc', 'cordis'];

export function textOf(message) {
  if (!Array.isArray(message?.content)) return '';
  return message.content.filter(part => part?.type === 'text' && typeof part.text === 'string')
    .map(part => part.text).join('\n');
}

export function isFresh(session) {
  if (!session || session.header?.cwd !== ROOT || session.header.parentSession ||
      session.header.origin === 'subagent' || session.header.isSeeded) return false;
  return !session.snapshotEvents().some(event =>
    ['turn/start', 'user/message', 'assistant/message', 'assistant/attempt', 'tool/call'].includes(event.type) ||
    (event.type === 'agent/inbox/spliced' && event.data?.inserted?.length > 0));
}

const PLATFORM = /\b(?:DSH|Cordis|DeepSeek Harness)\b|这个Harness|本Harness/i;
const PLUGIN = /插件|\bplugin\b|\bbundle\b|\bMCP\b|skill/i;
const PLUGIN_ACTION = /开发|安装|接入|实现|新增|创建|修复|调试|修改|配置|启用|禁用|卸载|\b(?:develop|install|implement|create|fix|debug|configure|enable|disable)\b/i;
const BATCH = /批量|批处理|全市场|全存储宇宙|逐页|分页|游标|数百|几千|上千|\b(?:batch|paginate|pagination|bulk)\b/i;
const EVIDENCE = /证据|行情|分钟|统计|研究数据|\b(?:MCP|evidence|dataset|records)\b/i;
const READ = /只读|仅读|读取|筛选|汇总|聚合|统计|\b(?:read.only|read|filter|summari[sz]e|aggregate)\b/i;
const MUTATION = /改代码|改策略|调参|修改|修复|重构|实现|开发|删除|清理|部署|重启|下单|交易执行|补采|刷新|结算|出版|写入|写库|写文件|导出|保存|备份|复制|安装|\b(?:edit|modify|fix|implement|delete|remove|deploy|restart|write|export|backup|copy|install)\b/i;

function affirmativeText(text) {
  // Negated prohibitions are not requests to perform those operations.
  return text.replace(/(?:禁止|不得|不要|无需|不允许|不需要|不做|不)(?:修改(?:代码|策略|参数|配置|文件)?|改代码|改策略|调参|删除|清理|部署|重启|下单|补采|刷新|结算|出版|写入|写库|写文件|导出|保存|备份|复制|安装)/g, '');
}

export function classify(message) {
  const phase = messagePhase(message);
  if (phase) return { mode: 'standard', reason: 'scheduled_review_readonly', readonly: true, phase };
  const text = textOf(message).trim();
  if (!text) return { mode: 'standard', reason: 'empty_or_attachment_only', readonly: false };
  if (Buffer.byteLength(text) > MAX_TASK_BYTES)
    return { mode: 'standard', reason: 'task_over_classification_budget', readonly: false };
  // Do not route on examples, quoted instructions or pasted programs.
  if (/\x60\x60\x60|<untrusted|<environment_context>|Earlier turns of this conversation/i.test(text))
    return { mode: 'standard', reason: 'quoted_or_mixed_context', readonly: false };
  const task = affirmativeText(text);
  const plugin = PLATFORM.test(task) && PLUGIN.test(task) && PLUGIN_ACTION.test(task);
  const batch = BATCH.test(task) && EVIDENCE.test(task) && READ.test(task);
  const businessMutation = /(?:修复|修改|重构|开发|实现)[^。；;\n]{0,24}(?:交易|风控|订单|策略参数|买卖算法|Vue|FastAPI|前端页面|后端服务)/i.test(task);
  if (plugin && (batch || businessMutation)) return { mode: 'standard', reason: 'mixed_task', readonly: false };
  if (plugin) return { mode: 'cordis', reason: 'explicit_dsh_plugin_work', readonly: false };
  if (batch && !MUTATION.test(task))
    return { mode: 'ptc', reason: 'bounded_batch_evidence', readonly: false };
  return { mode: 'standard', reason: 'general_or_uncertain', readonly: false };
}
