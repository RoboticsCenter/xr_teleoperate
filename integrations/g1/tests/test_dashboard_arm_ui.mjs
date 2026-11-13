import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

// Run production browser decisions without HTTP requests or hardware.
const source = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const start = source.indexOf('  function armCalibrationState(calibration, context) {');
const end = source.indexOf('  function armCalibrationContext()', start);
assert.ok(start >= 0 && end > start);
const state = new Function(`${source.slice(start, end)}\nreturn armCalibrationState;`)();
const presentationStart=source.indexOf('  function armCandidatePresentation(');
const presentationEnd=source.indexOf('  function renderArmCandidate(',presentationStart);
assert.ok(presentationStart>=0&&presentationEnd>presentationStart);
const presentation=new Function(`${source.slice(presentationStart,presentationEnd)}\nreturn armCandidatePresentation;`)();
const controlStart=source.indexOf('  function teleopControlPresentation(');
const controlEnd=source.indexOf('  function renderSession(',controlStart);
assert.ok(controlStart>=0&&controlEnd>controlStart);
const controlPresentation=new Function(`${source.slice(controlStart,controlEnd)}\nreturn teleopControlPresentation;`)();
const idle = {supported:true,online:true,authorized:true,outputsActive:false,modelBusy:false,
  settingsDirty:false,pending:false,distance:'20'};
const allowed = {origin_captured:false,captured_stages:[],input_ready:true,
  can_capture:true,can_reset:true,can_save:false,candidate:null,saved:null};

test('ordered capture locks distance after origin', () => {
  for (const [captured, expected] of [
    [[], 'origin'], [['origin'], 'forward'], [['origin','forward'], 'left'],
    [['origin','forward','left'], 'up'], [['origin','forward','left','up'], null]
  ]) {
    const result = state({...allowed,captured_stages:captured},idle);
    assert.equal(result.nextStage,expected);
    assert.equal(result.distanceDisabled,captured.includes('origin'));
  }
});

test('restored origin locks the stored distance', () => {
  const result=state({...allowed,origin_captured:true},{...idle,distance:'30'});
  assert.equal(result.nextStage,'forward');
  assert.equal(result.distance,30);
  assert.equal(result.distanceDisabled,true);
});

for(const [field,value] of [['supported',false],['online',false],['authorized',false],
  ['outputsActive',true],['modelBusy',true],['settingsDirty',true],['pending',true]]) {
  test(`${field} gate disables calibration mutations`,()=>{
    const result=state({...allowed,candidate:{calibration_id:'draft'},can_save:true},{...idle,[field]:value});
    assert.equal(result.canCapture,false);
    assert.equal(result.canSave,false);
    assert.equal(result.canReset,false);
  });
}

test('missing input blocks capture but permits allowed reset',()=>{
  const result=state({...allowed,input_ready:false},idle);
  assert.equal(result.canCapture,false);
  assert.equal(result.canReset,true);
});

test('server denial is authoritative',()=>{
  assert.equal(state({...allowed,can_capture:false},idle).canCapture,false);
});

test('distance respects core 3–100 cm limits',()=>{
  for(const distance of ['', ' ', 'NaN', 'Infinity', '-20', '0', '2.99', '100.01']) {
    const result=state(allowed,{...idle,distance});
    assert.equal(result.validDistance,false,`invalid ${distance}`);
    assert.equal(result.canCapture,false);
  }
  for(const distance of ['3','20','100']) assert.equal(state(allowed,{...idle,distance}).canCapture,true);
});

test('save requires a candidate and backend permission',()=>{
  assert.equal(state({...allowed,can_save:true},idle).canSave,false);
  assert.equal(state({...allowed,candidate:{calibration_id:'draft'}},idle).canSave,false);
  assert.equal(state({...allowed,candidate:{calibration_id:'draft'},can_save:true},idle).canSave,true);
});

test('old saved profile never marks the new candidate saved',()=>{
  const result=state({...allowed,candidate:{calibration_id:'new'},saved:{calibration_id:'old',saved_at:'yesterday'}},idle);
  assert.equal(result.savedMatches,false);
});

test('explicit unsaved draft stays unsaved with equal artifact values',()=>{
  const artifact={calibration_id:'same',sides:{left:{translation_scale:1}}};
  assert.equal(state({...allowed,candidate:artifact,saved:artifact,candidate_saved:false},idle).savedMatches,false);
});

test('save confirmation includes timestamp without changing candidate identity',()=>{
  const candidate={calibration_id:'new',sides:{left:{translation_scale:1},right:{translation_scale:0.9}}};
  const result=state({...allowed,candidate,saved:{...candidate,saved_at:'now'},candidate_saved:true},idle);
  assert.equal(result.savedMatches,true);
});

test('resetting a draft does not mark an existing saved profile as a new save',()=>{
  const result=state({...allowed,saved:{calibration_id:'existing'}},idle);
  assert.equal(result.nextStage,'origin');
  assert.equal(result.savedMatches,false);
  assert.equal(result.distanceDisabled,false);
});

test('three-axis correction is displayed as a matrix, not one misleading scale',()=>{
  const matrix=[[2,0.2,0],[0,0.5,0],[0,0,4]];
  const value={translation_scale:99,diagnostics:{desired_distance_cm:{forward:20,left:20,up:20},measured_distance_cm:{forward:10,left:40,up:5}}};
  const result=presentation(value,matrix);
  assert.equal(result.title,'三轴平移修正');assert.deepEqual(result.matrix,matrix);
  assert.match(result.description,/不改变腕部旋转/);assert.ok(!result.description.includes('99'));
  assert.equal(result.actual,'向前 20.0 cm · 向左 20.0 cm · 向上 20.0 cm');
  assert.equal(result.raw,'向前 10.0 cm · 向左 40.0 cm · 向上 5.0 cm');
  const artifact=presentation({...value,translation_correction:matrix});
  assert.deepEqual(artifact.matrix,matrix);assert.equal(artifact.title,'三轴平移修正');
  assert.ok(!artifact.description.includes('99'));
});
test('UI does not reintroduce raw-distance, scale, angle or fit-error rejection',()=>{
  const candidate={sides:{left:{translation_scale:50,diagnostics:{measured_distance_cm:{forward:0.1,left:90,up:4},max_axis_error_deg:80,max_relative_residual:2}}}};
  const result=state({...allowed,candidate,can_save:true},idle);
  assert.equal(result.canSave,true);assert.equal(result.canCapture,true);
  assert.equal(state({...allowed,candidate,can_save:false},idle).canSave,false);
});
test('old artifacts keep an explicitly legacy scalar representation',()=>{
  const result=presentation({translation_scale:1.25});
  assert.equal(result.matrix,null);assert.match(result.title,/旧版/);assert.match(result.description,/1.250×/);
});
test('malformed matrix is not shown as valid three-axis correction',()=>{
  for(const matrix of [[],[[1,0,0]],[[1,0,0],[0,NaN,0],[0,0,1]]]){
    assert.equal(presentation({},matrix).matrix,null);
  }
});

test('normal-walking capability describes zero-weight preparation and release instead of go-home',()=>{
  const result=controlPresentation({capabilities:{control_mode:'normal_walking',control_topic:'rt/arm_sdk'},session:{stage:'idle'}});
  assert.equal(result.motion,true);assert.match(result.launch,/权重 0/);assert.match(result.start,/接入跟随/);
  assert.match(result.stop,/释放手臂控制/);assert.ok(!result.stop.includes('回位'));assert.ok(!result.note.includes('回位'));
  assert.match(result.locomotion,/Regular Mode/);assert.match(result.locomotion,/原控制器／遥控器/);
  assert.match(result.locomotion,/1 自由度腰模式为 R1\+X/);
  assert.match(result.locomotion,/3 自由度腰模式为 R1\+Y/);
  assert.match(result.locomotion,/长按 2 秒/);assert.match(result.locomotion,/固件/);
  assert.match(result.locomotion,/保留 G1 3 自由度腰 Regular Mode/);
  assert.match(result.locomotion,/腿部由原厂控制器控制/);
  assert.match(result.locomotion,/腰部保持每次 START 捕获的姿态/);
  assert.match(result.locomotion,/不提供独立的动态腰部平衡/);
  assert.match(result.locomotion,/腰部保持修正的实机效果待验证/);
  assert.ok(!result.locomotion.includes('Running Mode'));
});
test('old backend keeps its legacy preparation and stop semantics',()=>{
  const result=controlPresentation({capabilities:{},session:{stage:'idle'}});
  assert.equal(result.motion,false);assert.match(result.launch,/接管并初始化/);assert.match(result.stop,/回位/);assert.equal(result.locomotion,'');
});
test('capability is required even when a session happens to name normal walking',()=>{
  const result=controlPresentation({capabilities:{},session:{running:true,control_mode:'normal_walking'}});
  assert.equal(result.motion,false);
});
test('stopped historical debug session cannot override next-launch normal-walking capability',()=>{
  const result=controlPresentation({capabilities:{control_mode:'normal_walking'},session:{running:false,stage:'stopped',control_mode:'debug',control_note:'旧会话回位'}});
  assert.equal(result.motion,true);assert.ok(!result.note.includes('旧会话回位'));
});
test('active debug session preserves its actual behavior despite different next-launch capability',()=>{
  const result=controlPresentation({capabilities:{control_mode:'normal_walking',control_note:'下一次使用手臂接口'},session:{running:true,stage:'ready',control_mode:'debug'}});
  assert.equal(result.motion,false);assert.match(result.stop,/回位/);assert.ok(!result.note.includes('下一次使用手臂接口'));
});
test('active compatible session can supply its exact lifecycle description',()=>{
  const result=controlPresentation({capabilities:{control_mode:'normal_walking',control_note:'默认说明'},session:{running:true,stage:'ready',control_mode:'normal_walking',control_note:'当前会话说明'}});
  assert.equal(result.motion,true);assert.equal(result.note,'当前会话说明');
});
test('legacy backend control note remains authoritative',()=>{
  assert.equal(controlPresentation({capabilities:{control_note:'当前旧服务说明'}}).note,'当前旧服务说明');
});
