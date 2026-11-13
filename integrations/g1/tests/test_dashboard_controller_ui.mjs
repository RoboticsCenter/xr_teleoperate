import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

const source=readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
const html=readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
function extract(name,next,bindings={}){
  const a=source.indexOf(`  function ${name}(`),b=source.indexOf(`  function ${next}(`,a);
  assert.ok(a>=0&&b>a,`production ${name}`);
  return new Function(...Object.keys(bindings),`${source.slice(a,b)}\nreturn ${name};`)(...Object.values(bindings));
}
const armSource=extract('armSourceState','armSettingsRequest');
const armReference=extract('armReferenceState','armSourceState');
const dashboardNetwork=extract('dashboardNetwork','renderNetwork');
const settingsRequest=extract('armSettingsRequest','trackerOptions');
const armInput=extract('armInputState','renderRobotConnection');
const picoState=extract('picoSetupState','renderPicoSetup');
const context={supported:true,online:true,authorized:true,pending:false,settingsDirty:false};
const settings={arms_mode:'both',arm_input_source:'trackers',tracker_config:{left_sn:'WRIST-L',right_sn:'WRIST-R'},hands_enabled:{left:true,right:false}};
const input={input_source:'controllers',selected_sides:['left','right'],selected_serials:{left:'pico-controller-left',right:'pico-controller-right'},
  ready:true,calibration_ready:true,input_live:true,head_ready:true,heading_ready:true,left_ready:true,right_ready:true,reader_running:true,receiver_running:true};

test('controller option is added without changing Tracker default or old-server behavior',()=>{
  assert.equal(armSource({}).source,'trackers');assert.equal(armSource({}).supported,false);
  assert.equal(armSource(settings).source,'trackers');assert.equal(armSource(settings).supported,true);
  assert.match(html,/<select id="arm-input-source" disabled><option value="trackers">腕部 Tracker<\/option><option value="controllers">PICO 手柄<\/option>/);
});
test('controller draft changes instructions but does not mutate saved source or wrist bindings',()=>{
  const before=structuredClone(settings);const result=armSource(settings,'controllers');
  assert.equal(result.controllers,true);assert.match(result.guide,/Head \+ Controller \+ Send/);
  assert.match(result.help,/独立校准/);assert.match(result.help,/不套用腕部 Tracker 修正/);assert.match(result.help,/原始 1:1/);
  assert.deepEqual(settings,before);
});
test('Tracker and Controller connection instructions use the configured network address',()=>{
  for(const source of ['trackers','controllers']){
    const result=armSource({...settings,arm_input_source:source},undefined,'10.1.10.62');
    assert.match(result.guide,/10\.1\.10\.62/);assert.ok(!result.guide.includes('192.168.31.92'));
  }
});
test('controller save omits SN fields, preserving backend wrist settings even when hidden inputs are empty',()=>{
  const result=settingsRequest({arms_mode:'both',arm_input_source:'controllers',left_sn:'',right_sn:'',hands_enabled:{left:true,right:false}});
  assert.equal(result.error,'');assert.equal(result.body.arm_input_source,'controllers');
  assert.equal('left_sn' in result.body,false);assert.equal('right_sn' in result.body,false);
  assert.deepEqual(result.body.hands_enabled,{left:true,right:false});
});
test('controller save never replaces real wrist bindings with virtual controller IDs',()=>{
  const result=settingsRequest({arms_mode:'both',arm_input_source:'controllers',left_sn:'pico-controller-left',right_sn:'pico-controller-right'});
  assert.equal('left_sn' in result.body,false);assert.equal('right_sn' in result.body,false);
});
test('switching back to Tracker submits the preserved real serial choices',()=>{
  const result=settingsRequest({arms_mode:'both',arm_input_source:'trackers',left_sn:'WRIST-L',right_sn:'WRIST-R'});
  assert.equal(result.error,'');assert.equal(result.body.left_sn,'WRIST-L');assert.equal(result.body.right_sn,'WRIST-R');
});
test('Tracker mode retains duplicate and missing-SN validation',()=>{
  assert.match(settingsRequest({arms_mode:'both',arm_input_source:'trackers',left_sn:'SAME',right_sn:'SAME'}).error,/不同/);
  assert.match(settingsRequest({arms_mode:'both',arm_input_source:'trackers',left_sn:'L',right_sn:''}).error,/分别选择/);
});
test('fresh selected controller input requires no wrist serial binding',()=>{
  const result=armInput({...input,selected_serials:{}},context);
  assert.equal(result.ready,true);assert.equal(result.calibrationReady,true);assert.equal(result.selection,'双手柄');
  assert.ok(!result.missing.includes('binding'));assert.match(result.missingText,/手柄/);
});
test('single controller input does not wait for unselected controller',()=>{
  const result=armInput({...input,selected_sides:['right'],selected_serials:{right:'pico-controller-right'},left_ready:false},context);
  assert.equal(result.ready,true);assert.equal(result.selection,'右手柄');
});
test('missing controller data remains blocked even if old wrist serials are still configured',()=>{
  const result=armInput({...input,ready:false,right_ready:false,selected_serials:{left:'WRIST-L',right:'WRIST-R'}},context);
  assert.equal(result.ready,false);assert.match(result.missingText,/右手柄数据/);
});
test('controller input retains live stream, Head, and session validity requirements',()=>{
  assert.equal(armInput({...input,input_live:false},context).ready,false);
  assert.equal(armInput({...input,head_ready:false},context).ready,false);
  assert.equal(armInput({...input,needs_reset:true,missing:['session']},context).calibrationReady,false);
});
test('controller PICO status uses controller readiness and not real wrist serial presence',()=>{
  const setup={input_source:'controllers',ready:true,input_live:true,headset_connected:true,head_ready:true,left_ready:true,right_ready:true};
  assert.equal(picoState(setup,context).inputReady,true);assert.equal(picoState(setup,context).label,'双手柄输入已就绪');
  assert.equal(picoState({...setup,right_ready:false,left_sn:'WRIST-L',right_sn:'WRIST-R'},context).inputReady,false);
});

function settingsRenderer(){
  const nodes=new Map();
  for(const id of ['arms-mode','arm-input-source','arm-reference-frame','arm-reference-help','left-tracker','right-tracker','left-tracker-field','right-tracker-field',
    'left-hand-enabled','right-hand-enabled','arm-source-help','pico-source-guide','launch-label','start-label','settings-detail']){
    nodes.set(id,{value:id==='arm-input-source'?'trackers':'',hidden:false,checked:false,textContent:'',
      option:{disabled:true},querySelector(){return this.option;},classList:{toggle(){}}});
  }
  const calls=[];
  const create=dirty=>extract('renderSettings','renderHands',{$:id=>nodes.get(id),settingsDirty:dirty,pending:new Set(),
    armSourceState:armSource,armReferenceState:armReference,dashboardNetwork,trackerOptions:(id,selected,available)=>{nodes.get(id).value=selected;calls.push({id,selected,available});},
    armNames:{left:'左臂',right:'右臂',both:'双臂'},write:(id,value)=>{nodes.get(id).textContent=value;}});
  return {nodes,calls,create};
}
test('actual settings renderer hides SN controls for controller draft and preserves choices when toggled back',()=>{
  const r=settingsRenderer(),data={settings,services:{tracker:{running:true,serials:['WRIST-L','WRIST-R']}}};
  r.create(false)(data);assert.equal(r.nodes.get('left-tracker').value,'WRIST-L');
  r.nodes.get('arm-input-source').value='controllers';r.create(true)(data);
  assert.equal(r.nodes.get('left-tracker-field').hidden,true);assert.equal(r.nodes.get('left-tracker').value,'WRIST-L');
  assert.match(r.nodes.get('pico-source-guide').textContent,/Controller/);
  r.nodes.get('arm-input-source').value='trackers';r.create(true)(data);
  assert.equal(r.nodes.get('left-tracker-field').hidden,false);assert.equal(r.nodes.get('left-tracker').value,'WRIST-L');
  assert.equal(r.nodes.get('right-tracker').value,'WRIST-R');assert.equal(r.nodes.get('left-hand-enabled').checked,true);
});
test('saved controller mode loads original wrist configuration in hidden selectors, not virtual IDs',()=>{
  const r=settingsRenderer();r.create(false)({settings:{...settings,arm_input_source:'controllers'},services:{tracker:{
    serials:['WRIST-L','WRIST-R'],selected_serials:{left:'pico-controller-left',right:'pico-controller-right'},controllers:{left:{ready:true},right:{ready:false}}}}});
  assert.equal(r.nodes.get('arm-input-source').value,'controllers');assert.equal(r.nodes.get('left-tracker').value,'WRIST-L');
  assert.equal(r.nodes.get('right-tracker').value,'WRIST-R');assert.equal(r.nodes.get('left-tracker-field').hidden,true);
});
