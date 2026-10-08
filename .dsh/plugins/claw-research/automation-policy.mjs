/** Deterministic policy for unattended research turns; ordinary user work unchanged. */
import path from 'node:path';
import { realpathSync } from 'node:fs';

export const MARKER = /(?:^|\n)<CLAW_AUTOMATION_V1:(postmarket|premarket)>/;
export const READ_TOOLS = new Set([
  'run_code', 'skill', 'todo_write', 'clock__curr_time',
  'clock.curr_time', 'claw_review_report_save', 'claw_review_report_read',
  'claw_review_guard_status',
]);
export const SAFE_MCP = new Set([
  'ashare_review_history','ashare_review_snapshot','ashare_review_attributions',
  'ashare_market_regimes','ashare_prediction_runs','ashare_training_runs',
  'ashare_shadow_runs','ashare_shadow_evaluations','ashare_deployments',
  'ashare_prediction_quality','paper_experiment_report','paper_daily_outcomes',
  'paper_candidate_shadow','paper_c3_events','ashare_review_readiness',
  'paper_research_artifact','paper_execution_evidence','paper_decision_trace',
  'paper_notification_ledger','ashare_market_review_universe','ashare_price_evidence',
  'ashare_premarket_context',
]);

export function messagePhase(message) {
  const text = (Array.isArray(message?.content) ? message.content : [])
    .filter(b => b?.type === 'text' && typeof b.text === 'string').map(b => b.text).join('\n');
  const direct = MARKER.exec(text)?.[1];
  if (direct) return direct;
  // Native Schedule JSON-escapes its prompt. Decode only recognized framing,
  // never recursively parse arbitrary quoted user data or dump the full payload.
  const batch = text.startsWith('[SCHEDULE REMINDER BATCH]\n');
  const single = text.startsWith('[SCHEDULE REMINDER]\n');
  if ((!batch && !single) || !text.includes('<CLAW_AUTOMATION_V1:')) return undefined;
  // Malformed/oversized review envelopes stay fail-closed, not human work.
  const fallback = /<CLAW_AUTOMATION_V1:(postmarket|premarket)>/.exec(text)?.[1] ?? 'postmarket';
  if (Buffer.byteLength(text) > 1024 * 1024) return fallback;
  const prefix = batch ? 'reminders_json: ' : 'reminder_prompt_json: ';
  const line = text.split('\n').find(value => value.startsWith(prefix));
  try {
    const payload = JSON.parse(line?.slice(prefix.length) ?? '');
    const prompts = batch && Array.isArray(payload) && payload.length <= 32
      ? payload.map(item => item?.reminder_prompt) : single ? [payload] : [];
    for (const prompt of prompts) {
      if (typeof prompt !== 'string') continue;
      const phase = MARKER.exec(prompt)?.[1];
      if (phase) return phase;
    }
  } catch { /* Fail closed below for an explicitly marked review envelope. */ }
  return fallback;
}

export function safeReadPath(root, raw) {
  if (typeof raw !== 'string' || !raw) return false;
  let absolute;
  try { absolute = realpathSync(path.resolve(root, raw)); } catch { return false; }
  const bases = [path.join(root,'.dsh','skills'), path.join(root,'outputs','dsh_reviews')];
  return absolute === path.join(root,'AGENTS.md') ||
    absolute === path.join(root,'.workbuddy','memory','MEMORY.md') ||
    bases.some(base => absolute.startsWith(base+path.sep));
}

export function denyReason(root, name, args={}) {
  if (READ_TOOLS.has(name)) return undefined;
  if (name === 'present' && Array.isArray(args.files) && args.files.length<=4 &&
      args.files.every(file=>safeReadPath(root,file.path) && path.resolve(root,file.path).startsWith(path.join(root,'outputs','dsh_reviews')+path.sep))) return undefined;
  if (name.startsWith('mcp__claw_ashare__') && SAFE_MCP.has(name.slice('mcp__claw_ashare__'.length))) return undefined;
  if (name === 'read' && safeReadPath(root, args.file_path)) return undefined;
  if (['read_mcp_resource','list_mcp_resources','list_mcp_resource_templates'].includes(name) &&
      args.server === 'claw_ashare') return undefined;
  return 'CLAW_AUTOMATION_READ_ONLY: unattended review permits audited evidence and report output only; shell, code edits, browser, network refresh, schedule changes, delegation and trading actions are denied';
}
