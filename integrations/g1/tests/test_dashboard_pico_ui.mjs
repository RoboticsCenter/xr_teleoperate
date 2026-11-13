import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

const source=readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
function extract(name,next){
  const start=source.indexOf(`  function ${name}(`),end=source.indexOf(`  function ${next}(`,start);
  assert.ok(start>=0 && end>start);
  return new Function(`${source.slice(start,end)}\nreturn ${name};`)();
}
const state=extract('picoSetupState','renderPicoSetup');
const compactError=extract('compactError','clock');
const network=extract('dashboardNetwork','renderNetwork');
const context={supported:true,online:true,authorized:true,pending:false,armActive:false};
const running={download:{running:true,available:true},receiver_running:true,reader_running:true,
  broadcast:{observed:true,ip:'192.168.31.92'},headset_connected:false,input_live:false,
  head_ready:false,left_ready:false,right_ready:false,left_sn:'LEFT',right_sn:'RIGHT',ready:false};

test('configured laptop address drives every PICO link without old-subnet fallback',()=>{
  const value=network({laptop_ip:'10.1.10.62',services:{pico_setup:{host:'10.1.10.62'},robot:{host:'10.1.10.80'}}});
  assert.equal(value.laptop,'10.1.10.62');assert.equal(value.robot,'10.1.10.80');
  assert.equal(value.pageURL,'http://10.1.10.62:8027/');
  assert.equal(value.apkURL,'http://10.1.10.62:8027/XRoboToolkit-G1-Wrists-TwoObject.apk');
  assert.equal(value.distanceURL,'http://10.1.10.62:8027/distance');
});
test('network metadata accepts setup fallback but missing or malformed host leaves links unavailable',()=>{
  assert.equal(network({services:{pico_setup:{host:'10.1.10.62'}}}).laptop,'10.1.10.62');
  assert.equal(network({network:{laptop_ip:'10.1.10.62',robot_ip:'10.1.10.80'}}).robot,'10.1.10.80');
  assert.equal(network({}).pageURL,'');assert.equal(network({laptop_ip:'evil.example/path'}).pageURL,'');
  assert.equal(network({laptop_ip:'999.1.1.1'}).pageURL,'');
});
test('new central address takes precedence over stale previous PICO endpoint',()=>{
  const value=network({laptop_ip:'10.1.10.62',services:{pico_setup:{host:'192.168.31.92',download:{url:'http://192.168.31.92:8027/'}}}});
  assert.equal(value.pageURL,'http://10.1.10.62:8027/');
});
test('actual network renderer updates page addresses and hyperlink destinations together',()=>{
  const nodes=new Map();
  for(const id of ['robot-connection-robot-ip','robot-connection-laptop-ip','pico-host-ip','pico-install-url','pico-copy-ip','pico-install-link','pico-apk-link','pico-distance-link']){
    nodes.set(id,{textContent:'',attrs:{},removeAttribute(key){delete this.attrs[key];if(key==='href')delete this.href;},setAttribute(key,value){this.attrs[key]=value;}});
  }
  const a=source.indexOf('  function renderNetwork('),b=source.indexOf('  function armSourceState(',a);
  const render=new Function('dashboardNetwork','$','write',`${source.slice(a,b)}\nreturn renderNetwork;`)(network,id=>nodes.get(id),(id,value)=>{nodes.get(id).textContent=value;});
  render({laptop_ip:'10.1.10.62',services:{robot:{host:'10.1.10.80'}}});
  assert.equal(nodes.get('pico-host-ip').textContent,'10.1.10.62');assert.equal(nodes.get('robot-connection-robot-ip').textContent,'10.1.10.80');
  assert.equal(nodes.get('pico-distance-link').href,'http://10.1.10.62:8027/distance');assert.equal(nodes.get('pico-copy-ip').disabled,false);
  render({});assert.equal(nodes.get('pico-distance-link').href,undefined);assert.equal(nodes.get('pico-copy-ip').disabled,true);
});

test('running services do not imply headset or live-pose readiness',()=>{
  const result=state(running,context);
  assert.equal(result.label,'等待头显发送');assert.equal(result.kind,'caution');assert.equal(result.inputReady,false);
});
test('TCP-connected headset with stale poses remains waiting for data',()=>{
  const result=state({...running,headset_connected:true},context);
  assert.equal(result.label,'等待 Head 与双腕数据');assert.equal(result.inputReady,false);
});
test('live Head and two distinct wrists are required for green input ready',()=>{
  const result=state({...running,headset_connected:true,input_live:true,head_ready:true,left_ready:true,right_ready:true,ready:true},context);
  assert.equal(result.inputReady,true);assert.equal(result.kind,'good');assert.equal(result.label,'双腕输入已就绪');
});
test('duplicate serial numbers never appear as valid dual wrists',()=>{
  const result=state({...running,headset_connected:true,input_live:true,head_ready:true,left_ready:true,right_ready:true,ready:true,right_sn:'LEFT'},context);
  assert.equal(result.inputReady,false);assert.equal(result.conflictingSN,true);assert.equal(result.label,'左右腕 SN 冲突');
});
test('stale input cannot become ready from other cached booleans',()=>{
  const result=state({...running,headset_connected:true,input_live:false,head_ready:true,left_ready:true,right_ready:true,ready:true},context);
  assert.equal(result.inputReady,false);assert.equal(result.kind,'caution');
});
test('one-click start remains available when any required service is missing',()=>{
  for(const partial of [{...running,download:{running:false}},{...running,receiver_running:false},{...running,reader_running:false}]) assert.equal(state(partial,context).canStart,true);
  assert.equal(state(running,context).canStart,false);
});
test('active arm session prevents stopping PICO but allows idempotent recovery',()=>{
  const result=state({...running,reader_running:false},{...context,armActive:true});
  assert.equal(result.canStop,false);assert.equal(result.canStart,true);
});
test('download-only service cannot be stopped through receiver stop',()=>{
  const result=state({download:{running:true},receiver_running:false,reader_running:false},context);
  assert.equal(result.canStop,false);assert.equal(result.canStart,true);
});
for(const [key,value] of [['supported',false],['online',false],['authorized',false],['pending',true]]){
  test(`${key} gate prevents PICO API actions`,()=>{
    const result=state({...running,reader_running:false},{...context,[key]:value});
    assert.equal(result.canStart,false);assert.equal(result.canStop,false);
  });
}
test('offline page does not display old valid input as current',()=>{
  const result=state({...running,headset_connected:true,input_live:true,head_ready:true,left_ready:true,right_ready:true,ready:true},{...context,online:false});
  assert.equal(result.inputReady,false);assert.equal(result.label,'状态未知');assert.equal(result.kind,'caution');
});
test('SSH no-route summary finds the actual cause after a large pexpect argument dump',()=>{
  const value='EOF: End Of File\nargs: '+ 'BASE64'.repeat(5000)+'\nbefore: ssh: connect to host 192.168.31.65 port 22: No route to host';
  const result=compactError(value);assert.match(result,/网络不可达/);assert.ok(result.length<100);assert.ok(!result.includes('BASE64'));
});
for(const [raw,expected] of [['Connection timed out','超时'],['Connection refused','拒绝'],['Permission denied (publickey,password).','认证失败'],['Host key verification failed.','身份校验失败']]){
  test(`SSH failure has a compact actionable summary: ${raw}`,()=>assert.ok(compactError(raw).includes(expected)));
}
test('unknown error display is bounded without mutating the original stored string',()=>{
  const original='Unknown backend failure: '+ 'x'.repeat(10000);
  const result=compactError(original);
  assert.ok(result.length<420);assert.match(result,/完整信息/);assert.equal(original.length,10025);
});
test('known transient target rejection keeps its exact original reason',()=>{
  const reason='target exceeds original measured-position step bound';assert.equal(compactError(reason),reason);
});
