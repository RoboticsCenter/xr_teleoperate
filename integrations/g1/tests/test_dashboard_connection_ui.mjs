import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

const source=readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
function extract(name,next){
  const a=source.indexOf(`  function ${name}(`),b=source.indexOf(`  function ${next}(`,a);
  assert.ok(a>=0&&b>a);
  return new Function(`${source.slice(a,b)}\nreturn ${name};`)();
}
const robotState=extract('robotConnectionState','armInputState');
const inputState=extract('armInputState','renderRobotConnection');
const calibrationMessage=extract('calibrationDisplayError','picoSetupFlash');
const context={supported:true,online:true,authorized:true,pending:false,settingsDirty:false};
const valid={ready:true,calibration_ready:true,checking:false,input_live:true,reader_running:true,
  receiver_running:true,head_ready:true,heading_ready:true,left_ready:true,right_ready:true,
  selected_sides:['left','right'],selected_serials:{left:'LEFT',right:'RIGHT'},missing:[],needs_reset:false};

test('legacy calibration error separates wrist side from movement direction',()=>{
  assert.equal(calibrationMessage('left forward位移不足3cm'),'左腕向前位移不足 3 厘米，请回到原点后重新测量。');
  assert.equal(calibrationMessage('left forward位移不足3cm，请回原点后重新测量。'),'左腕向前位移不足 3 厘米，请回到原点后重新测量。');
  assert.equal(calibrationMessage('right left位移不足3cm'),'右腕向左位移不足 3 厘米，请回到原点后重新测量。');
  assert.equal(calibrationMessage('left up位移不足 3 cm'),'左腕向上位移不足 3 厘米，请回到原点后重新测量。');
});
test('calibration display preserves current Chinese and unrelated errors',()=>{
  for(const message of ['左腕向前位移不足 3 厘米，请回到原点后重新测量。','reader session changed','left forward Tracker unavailable']){
    assert.equal(calibrationMessage(message),message);
  }
});

test('idle robot monitor permits connect but not disconnect',()=>{
  const s=robotState({stage:'idle',connected:false,connecting:false},context);
  assert.equal(s.canConnect,true);assert.equal(s.canDisconnect,false);assert.equal(s.label,'尚未检查');
});
test('connecting monitor can be cancelled without touching control connection',()=>{
  const robot={stage:'connecting',connected:false,connecting:true,control_connected:true};
  const s=robotState(robot,context);
  assert.equal(s.canConnect,false);assert.equal(s.canDisconnect,true);assert.equal(robot.control_connected,true);
});
test('successful readonly connection permits explicit reconnect',()=>{
  const s=robotState({connected:true,connecting:false},context);
  assert.equal(s.canConnect,true);assert.equal(s.canDisconnect,true);assert.equal(s.kind,'good');
});
test('failed monitor offers retry after backend cleanup',()=>{
  const s=robotState({stage:'error',connected:false,connecting:false,error:'network unavailable'},context);
  assert.equal(s.canConnect,true);assert.equal(s.canDisconnect,false);assert.equal(s.kind,'bad');
});
test('active control connection is not mistaken for readonly monitor success',()=>{
  const s=robotState({stage:'disconnected',connected:false,connecting:false,control_connected:true},context);
  assert.equal(s.kind,'neutral');assert.equal(s.canDisconnect,false);assert.equal(s.label,'检查连接已断开');
});
for(const [key,value] of [['supported',false],['online',false],['authorized',false],['pending',true]]){
  test(`${key} prevents monitor actions`,()=>{
    const s=robotState({connected:true}, {...context,[key]:value});
    assert.equal(s.canConnect,false);assert.equal(s.canDisconnect,false);
  });
}
test('current Head and both independent wrists are ready without G1 connection',()=>{
  const s=inputState(valid,{...context,robotConnected:false});
  assert.equal(s.ready,true);assert.equal(s.calibrationReady,true);assert.equal(s.canCheck,true);
});
test('single right arm requires only the selected wrist',()=>{
  const s=inputState({...valid,left_ready:false,selected_sides:['right'],selected_serials:{right:'RIGHT'},missing:['left']},context);
  assert.equal(s.ready,true);assert.equal(s.selection,'右腕');assert.ok(!s.missing.includes('left'));
});
test('single left arm does not wait for right input',()=>{
  const s=inputState({...valid,right_ready:false,selected_sides:['left'],selected_serials:{left:'LEFT'}},context);
  assert.equal(s.ready,true);assert.equal(s.selection,'左腕');
});
test('empty or duplicate bindings cannot become ready from cached flags',()=>{
  for(const selected_serials of [{left:'',right:'RIGHT'},{left:'SAME',right:'SAME'}]){
    const s=inputState({...valid,selected_serials},context);
    assert.equal(s.ready,false);assert.equal(s.calibrationReady,false);assert.ok(s.missing.includes('binding'));
  }
});
test('historical successful check never latches readiness after frames become stale',()=>{
  const s=inputState({...valid,input_live:false,checked_at:'2026-11-12T06:00:00Z'},context);
  assert.equal(s.ready,false);assert.equal(s.calibrationReady,false);assert.ok(s.missing.includes('stream'));
});
test('missing Head and selected wrist are reported explicitly',()=>{
  const s=inputState({...valid,ready:false,calibration_ready:false,head_ready:false,right_ready:false},context);
  assert.equal(s.ready,false);assert.match(s.missingText,/Head/);assert.match(s.missingText,/右腕/);
});
test('checking may wait indefinitely without disabling manual check',()=>{
  const s=inputState({...valid,checking:true,ready:false,head_ready:false},context);
  assert.equal(s.canCheck,true);assert.equal(s.label,'正在等待所选输入');
});
test('only HTTP pending or unsaved selection blocks an otherwise allowed check',()=>{
  assert.equal(inputState(valid,{...context,pending:true}).canCheck,false);
  assert.equal(inputState(valid,{...context,settingsDirty:true}).canCheck,false);
});
test('frozen origin heading may permit axis capture while current Head is missing',()=>{
  const s=inputState({...valid,ready:false,head_ready:false,heading_ready:false,missing:['head','heading']},context);
  assert.equal(s.ready,false);assert.equal(s.calibrationReady,true);
});
test('reader-session change requires draft reset without denying fresh live inputs',()=>{
  const s=inputState({...valid,calibration_ready:false,needs_reset:true,missing:['session']},context);
  assert.equal(s.ready,true);assert.equal(s.calibrationReady,false);
});
test('needs_reset cannot be overridden by cached calibration ready',()=>{
  assert.equal(inputState({...valid,needs_reset:true},context).calibrationReady,false);
});
test('offline page cannot claim either live-input or calibration readiness',()=>{
  const s=inputState(valid,{...context,online:false});
  assert.equal(s.ready,false);assert.equal(s.calibrationReady,false);assert.equal(s.canCheck,false);assert.equal(s.label,'状态未知');
});
