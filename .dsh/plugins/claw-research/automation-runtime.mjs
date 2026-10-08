import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import { ensureReadingFiles } from './report-reading.mjs';
import { messagePhase, denyReason } from './automation-policy.mjs?native-schedule-v2';

export const name = '@local/claw-readonly-research';
export const inject = ['tools','sessions'];
// This project-local bundle is deliberately bound to one verified workspace.
export const ROOT = '/Users/youzix/WorkBuddy/Claw';
const REPORT_ROOT = path.join(ROOT,'outputs','dsh_reviews');
const VERSION = 'claw_two_phase_v1';
const phaseSchema = {type:'string',enum:['postmarket','premarket']};
const dateSchema = {type:'string',description:'YYYY-MM-DD explicit target A-share trading day'};
const output = {schema:{type:'object'},render:(_,value)=>[{type:'text',text:JSON.stringify(value)}]};
const hash = value => crypto.createHash('sha256').update(value).digest('hex');
const dayValid = day => typeof day==='string' && /^\d{4}-\d{2}-\d{2}$/.test(day) &&
  !Number.isNaN(Date.parse(day+'T12:00:00Z')) && new Date(day+'T12:00:00Z').toISOString().slice(0,10)===day;
const filename = (phase,day,id) => phase+'-'+day+'-'+id+'.json';
function canonical(value) {
  if(Array.isArray(value)) return value.map(canonical);
  if(value && typeof value==='object') return Object.fromEntries(Object.keys(value).sort()
    .filter(key=>!['read_at','generated_at'].includes(key)).map(key=>[key,canonical(value[key])]));
  return value;
}

async function safeDirectory(root=REPORT_ROOT) {
  // Never follow an output-directory symlink into source, profile or trading storage.
  const outputs = path.join(ROOT,'outputs');
  await fs.mkdir(outputs,{recursive:true});
  if ((await fs.realpath(outputs)) !== outputs) throw new Error('unsafe outputs directory');
  await fs.mkdir(root,{recursive:true});
  if ((await fs.realpath(root)) !== root) throw new Error('unsafe report directory');
  return root;
}
async function savedRecord(destination, id) {
  const info=await fs.lstat(destination);
  if(!info.isFile() || info.size>400*1024) throw new Error('unsafe canonical report');
  const value=JSON.parse(await fs.readFile(destination,'utf8'));
  if(value.report_id!==id || value.schema!==VERSION || typeof value.markdown!=='string' ||
      Buffer.byteLength(value.markdown)>128*1024) throw new Error('canonical report identity mismatch');
  return value;
}
function checked(args) {
  if (!args || !['premarket','postmarket'].includes(args.phase) || !dayValid(args.trade_date))
    throw new Error('explicit valid phase/trade_date required');
}

export async function saveReport(args, root=REPORT_ROOT) {
  checked(args);
  if (args.prompt_version !== VERSION || !/^[a-f0-9]{64}$/.test(args.input_fingerprint ?? ''))
    throw new Error('known prompt version and evidence fingerprint required');
  if (typeof args.markdown !== 'string' || !args.markdown.trim() || Buffer.byteLength(args.markdown)>128*1024)
    throw new Error('nonempty report within 128 KiB required');
  if (typeof args.evidence_json !== 'string' || Buffer.byteLength(args.evidence_json)>128*1024)
    throw new Error('bounded JSON evidence manifest required');
  const evidence = JSON.parse(args.evidence_json);
  if (!evidence || typeof evidence !== 'object' || Array.isArray(evidence))
    throw new Error('evidence manifest must be an object');
  const missing = JSON.parse(args.missing_json ?? '[]');
  const workitems = JSON.parse(args.workitems_json ?? '[]');
  if (!Array.isArray(missing) || !Array.isArray(workitems) || workitems.length>100 ||
      Buffer.byteLength(JSON.stringify({missing,workitems}))>64*1024)
    throw new Error('bounded missing/workitem lists required');
  const body = {schema:VERSION,phase:args.phase,trade_date:args.trade_date,
    as_of:args.as_of,prompt_version:args.prompt_version,
    readiness_fingerprint:args.input_fingerprint,
    input_fingerprint:hash(JSON.stringify(canonical({readiness_fingerprint:args.input_fingerprint,evidence}))),
    status:args.status,previous_report_id:args.previous_report_id ?? null,
    evidence,missing,workitems,markdown:args.markdown};
  if (!['complete','partial','failed','skipped','late_research'].includes(body.status))
    throw new Error('valid report status required');
  const at = Date.parse(args.as_of);
  if (!Number.isFinite(at) || !/[+-]\d{2}:\d{2}$|Z$/.test(args.as_of) || at>Date.now())
    throw new Error('non-future as_of with timezone required');
  if (body.phase === 'premarket' && body.status === 'complete') {
    const cutoff = Date.parse(body.trade_date+'T09:15:00+08:00');
    if (at>=cutoff || Date.now()>=cutoff) throw new Error('no complete premarket plan after deadline');
    if (!body.previous_report_id) throw new Error('complete premarket requires prior postmarket report');
  }
  const localDay = new Date(at+8*3600*1000).toISOString().slice(0,10);
  if(localDay!==body.trade_date) throw new Error('as_of must be on explicit target Shanghai date');
  if(body.previous_report_id && !/^[a-f0-9]{64}$/.test(body.previous_report_id))
    throw new Error('previous report ID must be a frozen report identifier');
  if(workitems.some(item=>!item || item.status!=='pending_review' ||
      !['evidence_repair','engineering_equivalence','strategy_hypothesis'].includes(item.kind)))
    throw new Error('workitems must be pending_review with a known non-executing kind');
  if(body.status==='complete' && (missing.length || evidence.readiness?.status!=='ready' ||
      evidence.complete_coverage!==true))
    throw new Error('complete requires ready inputs, explicitly complete coverage and no missing evidence');
  if(body.phase==='premarket' && body.status==='complete' &&
      evidence.premarket_context?.coverage?.complete_overnight_coverage!==true)
    throw new Error('complete premarket requires certified overnight coverage');
  await safeDirectory(root);
  // Same task/day/input/protocol key is idempotent despite paraphrase or crash retry.
  const id = hash([VERSION,body.phase,body.trade_date,body.input_fingerprint].join(':'));
  const destination = path.join(root,filename(body.phase,body.trade_date,id));
  const payload = JSON.stringify({...body,report_id:id,generated_at:new Date().toISOString()},null,2)+'\n';
  const lockPath = destination+'.lock';
  let lock;
  try {lock=await fs.open(lockPath,'wx',0o600);} catch(e) {
    if(e.code!=='EEXIST') throw e;
    // A crashed lock is not bypassed blindly; materialized report is safe to reuse.
    try {
      const old = await savedRecord(destination,id);
      return {status:'already_saved',report_id:old.report_id,path:destination,report_status:old.status,
        ...await ensureReadingFiles(old,destination)};
    } catch {throw new Error('report_in_progress_or_interrupted_lock_requires_review');}
  }
  try {
    try {
      const old = await savedRecord(destination,id);
      return {status:'already_saved',report_id:old.report_id,path:destination,report_status:old.status,
        ...await ensureReadingFiles(old,destination)};
    } catch(e) {if(e.code!=='ENOENT') throw e;}
    // Fully write and fsync before atomic, no-overwrite publication.
    const temporary = path.join(root,'.'+id+'-'+crypto.randomUUID()+'.tmp');
    const handle = await fs.open(temporary,'wx',0o600);
    try {await handle.writeFile(payload);await handle.sync();} finally {await handle.close();}
    try {await fs.link(temporary,destination);} finally {await fs.unlink(temporary);}
    return {status:'saved',report_id:id,path:destination,sha256:hash(payload),report_status:body.status,
            ...await ensureReadingFiles(JSON.parse(payload),destination)};
  } finally {await lock.close();await fs.unlink(lockPath);}
}

export async function readReport(args, root=REPORT_ROOT) {
  checked(args);
  let entries;
  try {entries=await fs.readdir(root,{withFileTypes:true});} catch(e) {
    if(e.code==='ENOENT') return {status:'unavailable',reason:'no_prior_report'};
    throw e;
  }
  if ((await fs.realpath(root)) !== root) throw new Error('unsafe report directory');
  const prefix=args.phase+'-'+args.trade_date+'-';
  const files=entries.filter(e=>e.isFile()&&e.name.startsWith(prefix)&&/^\w+-\d{4}-\d{2}-\d{2}-[a-f0-9]{64}\.json$/.test(e.name));
  if(files.length>200) throw new Error('report catalog budget exceeded');
  const records=[];
  for(const file of files) {
    const full=path.join(root,file.name);
    if((await fs.stat(full)).size>400*1024) throw new Error('report byte budget exceeded');
    const data=JSON.parse(await fs.readFile(full,'utf8'));
    if(args.report_id&&data.report_id!==args.report_id) continue;
    records.push({...data,path:full});
  }
  records.sort((a,b)=>b.generated_at.localeCompare(a.generated_at));
  const report=records[0];
  return report?{status:'ok',revision_count:records.length,report}: {status:'unavailable',reason:'no_matching_report'};
}

export function apply(ctx) {
  const automatic=new Map();
  ctx.on('agent/inbox/claimed',({agent,message,turn})=>{
    const phase=messagePhase(message);
    if(phase) automatic.set(agent.id,{turn,phase,startedAt:Date.now(),calls:0});
  });
  function mode(agent) {
    const session=agent&&ctx.sessions.get(agent.id);
    if(!session || session.header.cwd!==ROOT) return undefined;
    const events=session.snapshotEvents();
    let currentTurn;
    for(let i=events.length-1;i>=0;i--) if(events[i].type==='turn/start') {
      currentTurn=events[i].data.turn;break;
    }
    const cached=automatic.get(agent.id);
    if(cached?.turn===currentTurn) return cached;
    // Restore/HMR: do not require the inbox listener to have seen delivery.
    for(let i=events.length-1;i>=0;i--) {
      const event=events[i];
      if(event.type==='turn/start') break;
      if(event.type==='user/message') {
        const phase=messagePhase(event.data);
        if(phase) {
          const state={turn:currentTurn,phase,startedAt:Date.now(),calls:0};
          automatic.set(agent.id,state);
          return state;
        }
      }
    }
    return undefined;
  }
  const unguard=ctx.tools.guard(exec=>{
    const state=mode(exec.agent);
    if(!state) return undefined;
    const reason=denyReason(ROOT,exec.name,exec.arguments);
    if(reason) return reason;
    // Always retain bounded report/status access to record a truthful stop.
    if(!['run_code','claw_review_report_save','claw_review_guard_status'].includes(exec.name)) {
      state.calls++;
      if(state.calls>80 || Date.now()-state.startedAt>20*60*1000)
        return 'CLAW_AUTOMATION_BUDGET: stop research and save partial coverage; max 80 evidence/read calls or 20 minutes';
    }
    return undefined;
  });
  const save=ctx.tools.register({name:'claw_review_report_save',
    description:'Save bounded immutable Claw research Markdown, evidence refs, missing coverage and pending workitems. Only writes outputs/dsh_reviews. No business DB, source edits or external pushes.',
    parameters:{type:'object',properties:{phase:phaseSchema,trade_date:dateSchema,
      as_of:{type:'string'},prompt_version:{type:'string',enum:[VERSION]},
      input_fingerprint:{type:'string'},status:{type:'string',enum:['complete','partial','failed','skipped','late_research']},
      previous_report_id:{type:'string'},markdown:{type:'string'},evidence_json:{type:'string'},
      missing_json:{type:'string'},workitems_json:{type:'string'}},
      required:['phase','trade_date','as_of','prompt_version','input_fingerprint','status','markdown','evidence_json'],
      additionalProperties:false}, output,
    execute:args=>saveReport(args), timeoutMs:15000});
  const read=ctx.tools.register({name:'claw_review_report_read',
    description:'Read prior immutable Claw postmarket/premarket research by explicit target date and optional exact report ID. Does not generate, overwrite or send reports.',
    parameters:{type:'object',properties:{phase:phaseSchema,trade_date:dateSchema,report_id:{type:'string'}},
      required:['phase','trade_date'],additionalProperties:false}, output,execute:args=>readReport(args)});
  const status=ctx.tools.register({name:'claw_review_guard_status',
    description:'Read effective unattended Claw tool guard status for this agent; ordinary human turns stay unrestricted.',
    parameters:{type:'object',properties:{},additionalProperties:false},output,
    execute:(_,exec)=>({guard_installed:true,active:!!mode(exec.agent),
      mode:mode(exec.agent)??'human_turn',max_evidence_calls:80,max_minutes:20,allowed_scope:'audited_evidence_and_outputs_only',prompt_version:VERSION})});
  ctx.effect(()=>()=>{save();read();status();unguard();automatic.clear();});
}
