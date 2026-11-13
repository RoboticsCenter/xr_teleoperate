import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

// Exercise the production status presenter without a browser or hardware API.
const source = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const start = source.indexOf('  function handPresentation(hand) {');
const end = source.indexOf('  function renderControls()', start);
assert.ok(start >= 0 && end > start, 'production hand presenter is present');
const errorStart = source.indexOf('  function compactError(value, limit = 360) {');
const errorEnd = source.indexOf('  function clock(seconds)', errorStart);
const compactError = new Function(`${source.slice(errorStart, errorEnd)}\nreturn compactError;`)();
const present = new Function('numeric', 'modelActive', 'compactError', `${source.slice(start, end)}\nreturn handPresentation;`)(
  value => typeof value === 'number' && Number.isFinite(value),
  model => model?.active === true || model?.running === true,
  compactError
);
const stopAvailable=new Function(`${source.slice(start,end)}\nreturn handStopAvailable;`)();
const holding = {
  running: true, stage: 'holding', input_live: true, model_ready: true,
  calibration_ready: true, mapping_ready: true, feedback: {fresh: true},
  error: null, motion_error: null, reader_error: null
};

test('active hand with no feedback is amber, not following', () => {
  const result = present({...holding, feedback: null});
  assert.equal(result.label, '等待手部反馈');
  assert.equal(result.kind, 'caution');
});

test('stale feedback is visibly held', () => {
  const result = present({...holding, feedback: {fresh: false, age_s: 0.2}});
  assert.equal(result.label, '等待手部反馈');
  assert.equal(result.kind, 'caution');
});

test('fresh feedback with unavailable input waits for input', () => {
  const result = present({...holding, input_live: false});
  assert.equal(result.label, '等待输入');
  assert.equal(result.kind, 'caution');
});

for (const reason of [
  'target exceeds original measured-position step bound',
  'feedback changed during publisher setup; hold previous target',
  'left feedback unavailable; hold previous target',
  'right feedback unavailable; hold previous target'
]) {
  test(`known target rejection is amber and keeps its reason: ${reason}`, () => {
    const result = present({...holding, error_kind: 'motion', motion_error: reason, error: reason});
    assert.equal(result.kind, 'caution');
    assert.equal(result.label, '保持当前目标');
    assert.equal(result.detail, reason);
  });
}

for (const reason of [
  'hand bridge initialization failed (EOF)',
  'hand bridge response timed out',
  'DDS hand write did not report success; delivery unknown',
  'target outside original six-channel envelope'
]) {
  test(`hard or unknown output error remains red even while holding: ${reason}`, () => {
    const result = present({...holding, feedback: null, error_kind: 'motion', motion_error: reason, error: reason});
    assert.equal(result.kind, 'bad');
    assert.equal(result.label, '手部输出异常');
    assert.equal(result.detail, reason);
  });
}

test('explicit unknown delivery never becomes a recoverable target rejection', () => {
  const reason = 'target exceeds original measured-position step bound';
  const result = present({...holding, error: reason, motion_error: reason, delivery_unknown: true});
  assert.equal(result.kind, 'bad');
  assert.equal(result.detail, reason);
});

test('real reader error keeps priority over a transient output rejection', () => {
  const reason = 'target exceeds original measured-position step bound';
  const result = present({...holding, motion_error: reason, error: reason, reader_error: 'No fresh valid glove data'});
  assert.equal(result.kind, 'bad');
  assert.equal(result.label, '读取异常');
  assert.equal(result.detail, 'No fresh valid glove data');
});

test('bridge transport failure keeps priority over a transient output rejection', () => {
  const reason = 'target exceeds original measured-position step bound';
  const result = present({...holding, motion_error: reason, error: reason, transport_error: 'hand bridge connection closed'});
  assert.equal(result.kind, 'bad');
  assert.equal(result.detail, 'hand bridge connection closed');
});

test('successful following returns green after the rejection clears', () => {
  const result = present({...holding, stage: 'following', detail: '手指跟随中'});
  assert.equal(result.kind, 'good');
  assert.equal(result.label, '正在跟随');
});
test('output preparing is amber until actual following begins',()=>{
  const result=present({...holding,stage:'preparing'});
  assert.equal(result.kind,'caution');assert.equal(result.label,'正在准备手部输出');
});
test('preparing with missing glove data explicitly waits for glove input',()=>{
  const result=present({...holding,stage:'preparing',input_live:false});
  assert.equal(result.kind,'caution');assert.equal(result.label,'等待手套输入');
});
test('real output error remains red while preparing',()=>{
  const result=present({...holding,stage:'preparing',motion_error:'hand bridge initialization failed (EOF)'});
  assert.equal(result.kind,'bad');assert.equal(result.label,'手部输出异常');
});
for(const [stage,label] of [['preparing_input','正在启动手套读取'],['waiting_input','等待手套输入'],
  ['waiting_arm','等待手臂跟随'],['waiting_calibration','等待校准完成'],['starting','正在启动手部输出']]){
  test(`auto-start ${stage} is amber and preserves its actual waiting reason`,()=>{
    const result=present({...holding,running:false,stage:'idle',input_live:false,auto_start_pending:true,
      auto_start_stage:stage,auto_start_detail:'该侧等待原因'});
    assert.equal(result.kind,'caution');assert.equal(result.label,label);assert.equal(result.detail,'该侧等待原因');
  });
}
test('one waiting hand does not alter the other hands following state',()=>{
  const left=present({...holding,running:false,input_live:false,auto_start_pending:true,auto_start_stage:'waiting_input'});
  const right=present({...holding,stage:'following'});
  assert.equal(left.label,'等待手套输入');assert.equal(right.label,'正在跟随');assert.equal(right.kind,'good');
});
test('real reader and motion failures take priority over queued auto-start status',()=>{
  for(const fault of [{reader_error:'reader exited'},{motion_error:'DDS delivery unknown'}]){
    const result=present({...holding,...fault,running:false,input_live:false,auto_start_pending:true,auto_start_stage:'waiting_input'});
    assert.equal(result.kind,'bad');
  }
});
test('terminal auto-start failure is visible and is not disguised as waiting',()=>{
  const result=present({...holding,running:false,auto_start_pending:false,auto_start_stage:'error',auto_start_error:'glove reader failed to start'});
  assert.equal(result.kind,'bad');assert.equal(result.label,'联动启动异常');assert.equal(result.detail,'glove reader failed to start');
});
test('STOP can cancel a queued hand start before output begins',()=>{
  assert.equal(stopAvailable({running:false,auto_start_pending:true},{authorized:true,stopping:false,possiblyActive:false}),true);
  assert.equal(stopAvailable({running:false,auto_start_pending:false},{authorized:true,stopping:false,possiblyActive:false}),false);
});
test('STOP remains available for previously observed pending start during connection loss',()=>{
  assert.equal(stopAvailable({}, {authorized:true,stopping:false,possiblyActive:true,online:false}),true);
  assert.equal(stopAvailable({auto_start_pending:true},{authorized:false,stopping:false}),false);
  assert.equal(stopAvailable({auto_start_pending:true},{authorized:true,stopping:true}),false);
});

test('a stopped hand is not disguised as recoverable holding by matching error text', () => {
  const reason = 'target exceeds original measured-position step bound';
  const result = present({...holding, running: false, error: reason, motion_error: reason});
  assert.equal(result.kind, 'bad');
});
