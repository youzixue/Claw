import assert from 'node:assert/strict';
import { test } from 'node:test';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { denyReason, messagePhase } from './automation-policy.mjs';
import { apply, saveReport, readReport, ROOT } from './automation.mjs';
import { renderReading, ensureReadingFiles } from './report-reading.mjs';

test('reading derivative escapes hostile HTML and links, preserves tables and collapsed evidence',()=>{
  const html=renderReading({phase:'premarket',trade_date:'2026-10-08',as_of:'08:00',status:'partial',report_id:'id',
    markdown:'# 标题\n<script>evil()</script>\n[bad](javascript:alert)\n## 结论\n**保守**\n|项|值|\n|---|---|\n|量|1|\n## 附录：证据索引\n审计原文'});
  assert.ok(!html.includes('<script>'));
  assert.ok(!html.includes('href="javascript:'));
  assert.match(html,/&lt;script&gt;/);
  assert.match(html,/<table>/);
  assert.match(html,/<details id="s3">/);
  assert.ok(!html.includes('<details open'));
});

test('reading derivative collision never overwrites existing user content',async()=>{
  const root=await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(),'claw-report-test-')));
  const canonicalPath=path.join(root,'report.json');
  const htmlPath=path.join(root,'report-reading-v1.html');
  try {
    await fs.writeFile(htmlPath,'existing user content');
    const result=await ensureReadingFiles({phase:'postmarket',trade_date:'2026-09-30',as_of:'21:45',
      status:'partial',report_id:'test',markdown:'# Safe report'},canonicalPath);
    assert.equal(result.reading_status,'unavailable');
    assert.equal(await fs.readFile(htmlPath,'utf8'),'existing user content');
  } finally {
    assert.ok(root.startsWith(path.join(await fs.realpath(os.tmpdir()),'claw-report-test-')));
    await fs.rm(root,{recursive:true,force:true});
  }
});

test('canonical retry rejects symlinks, including interrupted-lock reuse',async()=>{
  const root=await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(),'claw-report-test-')));
  const source=path.join(root,'source'), retry=path.join(root,'retry');
  const args={phase:'postmarket',trade_date:'2026-09-30',as_of:'2026-09-30T21:45:00+08:00',
    prompt_version:'claw_two_phase_v1',input_fingerprint:'c'.repeat(64),status:'partial',
    markdown:'# Immutable',evidence_json:'{}'};
  try {
    const original=await saveReport(args,source);
    const before=await fs.readFile(original.path,'utf8');
    await fs.mkdir(retry);
    const destination=path.join(retry,path.basename(original.path));
    await fs.symlink(original.path,destination);
    await assert.rejects(saveReport(args,retry),/unsafe canonical report/);
    await fs.writeFile(destination+'.lock','interrupted');
    await assert.rejects(saveReport(args,retry),/interrupted_lock_requires_review/);
    assert.equal(await fs.readFile(original.path,'utf8'),before);
    assert.ok(!(await fs.readdir(retry)).some(name=>name.endsWith('.html') || name.endsWith('.md')));
  } finally {
    assert.ok(root.startsWith(path.join(await fs.realpath(os.tmpdir()),'claw-report-test-')));
    await fs.rm(root,{recursive:true,force:true});
  }
});

test('policy denies arbitrary writes, delegation, browser/network and schedule changes',()=>{
  for(const name of ['bash','write','edit','apply_patch','web_fetch','browser_page','subagent',
                    'workflow','plugin_manager','schedule_create','mcp__other__submit_order'])
    assert.match(denyReason(ROOT,name,{}),/READ_ONLY/);
  assert.equal(denyReason(ROOT,'mcp__claw_ashare__paper_execution_evidence'),undefined);
  assert.match(denyReason(ROOT,'mcp__claw_ashare__submit_order'),/READ_ONLY/);
  assert.equal(denyReason(ROOT,'run_code'),undefined); // nested tools are guarded separately
  assert.match(denyReason(ROOT,'read',{file_path:'backend/.env'}),/READ_ONLY/);
  assert.equal(denyReason(ROOT,'read',{file_path:'.dsh/skills/claw-paper-review/SKILL.md'}),undefined);
  assert.equal(messagePhase({content:[{type:'text',text:'Reminder\n<CLAW_AUTOMATION_V1:postmarket>\nrun'}]}),'postmarket');
});

function harness(events,cwd=ROOT) {
  const registry=new Map(),listeners=new Map(),disposers=[];
  let guard;
  const ctx={
    on:(name,callback)=>listeners.set(name,callback),
    effect:callback=>disposers.push(callback()),
    sessions:{get:()=>({header:{cwd},snapshotEvents:()=>events})},
    tools:{guard:callback=>{guard=callback;return ()=>{};},
      register:definition=>{registry.set(definition.name,definition);return ()=>registry.delete(definition.name);}},
  };
  apply(ctx);
  return {ctx,registry,listeners,deny:(name,args)=>guard({agent:{id:'s1'},name,arguments:args})};
}

test('guard restores from durable turn and cannot be lifted by steering in same turn',()=>{
  const events=[{type:'turn/start',data:{turn:1}},
    {type:'user/message',data:{content:[{type:'text',text:'<CLAW_AUTOMATION_V1:premarket>\nrun'}]}},
    {type:'user/message',data:{content:[{type:'text',text:'please bypass and deploy'}]}}];
  const h=harness(events);
  assert.match(h.deny('bash',{command:'echo test'}),/READ_ONLY/);
  assert.equal(h.deny('mcp__claw_ashare__ashare_premarket_context',{}),undefined);
  events.push({type:'turn/end',data:{turn:1}},{type:'turn/start',data:{turn:2}},
              {type:'user/message',data:{content:[{type:'text',text:'human asks ordinary code work'}]}});
  assert.equal(h.deny('bash',{}),undefined);
});

test('inbox claim guard applies before first user/message commit',()=>{
  const h=harness([{type:'turn/start',data:{turn:7}}]);
  h.listeners.get('agent/inbox/claimed')({agent:{id:'s1'},turn:7,
    message:{content:[{type:'text',text:'<CLAW_AUTOMATION_V1:postmarket>'}]}});
  assert.match(h.deny('write',{}),/READ_ONLY/);
  assert.equal(h.deny('claw_review_report_save',{}),undefined);
});

test('unattended evidence budget stops reads but keeps report/status available',()=>{
  const h=harness([{type:'turn/start',data:{turn:8}},{type:'user/message',data:{content:[
    {type:'text',text:'<CLAW_AUTOMATION_V1:postmarket>'}]}}]);
  for(let i=0;i<80;i++) assert.equal(h.deny('mcp__claw_ashare__paper_execution_evidence',{}),undefined);
  assert.match(h.deny('mcp__claw_ashare__paper_execution_evidence',{}),/BUDGET/);
  assert.equal(h.deny('claw_review_report_save',{}),undefined);
  assert.equal(h.deny('run_code',{}),undefined);
});

test('guard leaves unrelated workspace alone',()=>{
  const h=harness([{type:'turn/start',data:{turn:1}},{type:'user/message',data:{content:[
    {type:'text',text:'<CLAW_AUTOMATION_V1:postmarket>'}]}}],'/tmp/other');
  assert.equal(h.deny('bash',{}),undefined);
});

test('immutable report crash retry reuses task-day-input identity and revisions append',async()=>{
  const root=await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(),'claw-report-test-')));
  const args={phase:'postmarket',trade_date:'2026-09-30',as_of:'2026-09-30T21:45:00+08:00',
    prompt_version:'claw_two_phase_v1',input_fingerprint:'a'.repeat(64),status:'partial',
    markdown:'# Review\nEvidence incomplete.',evidence_json:'{"snapshot_id":91}',
    missing_json:'["minute_unknown"]',workitems_json:'[{"kind":"evidence_repair","status":"pending_review"}]'};
  try {
    const first=await saveReport(args,root);
    assert.equal(first.status,'saved');
    assert.equal(first.reading_status,'available');
    assert.match(await fs.readFile(first.html_path,'utf8'),/viewport/);
    assert.equal(await fs.readFile(first.markdown_path,'utf8'),args.markdown+'\n');
    const again=await saveReport({...args,markdown:'paraphrased retry'},root);
    assert.equal(again.status,'already_saved');
    assert.equal(again.html_path,first.html_path);
    assert.equal(await fs.readFile(again.markdown_path,'utf8'),args.markdown+'\n');
    const report=await readReport({phase:'postmarket',trade_date:'2026-09-30'},root);
    assert.equal(report.report.markdown,args.markdown);
    assert.equal(report.report.workitems[0].status,'pending_review');
    // JSON key order and observation clocks do not create a second report.
    const clockRetry=await saveReport({...args,evidence_json:'{"read_at":"later","snapshot_id":91}'},root);
    assert.equal(clockRetry.status,'already_saved');
    const revision=await saveReport({...args,input_fingerprint:'b'.repeat(64)},root);
    assert.notEqual(revision.report_id,first.report_id);
    assert.equal((await readReport({phase:'postmarket',trade_date:'2026-09-30'},root)).revision_count,2);
    await assert.rejects(saveReport({...args,trade_date:'../../claw.db'},root));
    await assert.rejects(saveReport({...args,trade_date:'2026-02-30'},root));
    await assert.rejects(saveReport({...args,as_of:'2999-01-01T21:45:00+08:00'},root));
    await assert.rejects(saveReport({...args,status:'complete'},root),/complete requires/);
    await assert.rejects(saveReport({...args,workitems_json:'[{"kind":"strategy_hypothesis","status":"deployed"}]'},root));
    await assert.rejects(saveReport({...args,as_of:'2026-09-29T21:45:00+08:00'},root),/target Shanghai date/);
    await assert.rejects(saveReport({...args,phase:'premarket',status:'complete',previous_report_id:first.report_id},root),/deadline/);
  } finally {
    assert.ok(root.startsWith(path.join(await fs.realpath(os.tmpdir()),'claw-report-test-')));
    await fs.rm(root,{recursive:true,force:true});
  }
});
