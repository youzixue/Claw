import assert from 'node:assert/strict';
import { test } from 'node:test';
import { phaseStatus } from './runtime.mjs';
import { RESEARCH_REPAIR_VERSION } from './policy.mjs';

test('status exposes old prompt without claiming the repaired template is loaded',()=>{
  const result=phaseStatus({status:'scheduled',sessionId:'original',id:'bound',record:{
    scheduledAt:'2026-10-09T00:00:00.000Z',kind:'at',prompt:'old readonly prompt'}});
  assert.equal(result.prompt_has_repair_marker,false);
  assert.equal(result.sessionId,'original');
  assert.equal(result.id,'bound');
  assert.equal(result.prompt_scope,'last_manager_observed_record_not_task_execution_receipt');
});

test('repair marker describes observed prompt, not natural execution acceptance',()=>{
  const result=phaseStatus({status:'scheduled',record:{prompt:RESEARCH_REPAIR_VERSION}});
  assert.equal(result.prompt_has_repair_marker,true);
  assert.equal(result.execution_accepted,undefined);
  assert.equal(phaseStatus({status:'blocked_calendar_unknown'}).prompt_has_repair_marker,false);
});
