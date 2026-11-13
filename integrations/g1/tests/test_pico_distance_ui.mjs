import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source=readFileSync(new URL('../web/pico-distance.js',import.meta.url),'utf8');
const html=readFileSync(new URL('../web/pico-distance.html',import.meta.url),'utf8');
const scope={module:{exports:{}}};vm.runInNewContext(source,scope);
const {distanceState:state,formatDistance:format,distanceHost}=scope.module.exports;
const context={online:true,ageMs:50,token:'measurement-only',pending:false,received:true};
const wrist={sn:'LEFT',live:true,displacement_cm:21,forward_cm:20,left_cm:-6,up_cm:1};
const valid={zero_ready:true,has_origin:true,needs_zero:false,ready:true,selected_sides:['left','right'],
  sides:{left:wrist,right:{...wrist,sn:'RIGHT',forward_cm:19}},target_cm:20,current_stage:'forward'};

test('independent left/right positions and signed directions are preserved',()=>{
  const s=state(valid,context);assert.equal(s.ready,true);assert.equal(s.canZero,true);
  assert.equal(s.sides.left.forward,20);assert.equal(s.sides.right.forward,19);
  assert.equal(s.sides.left.left,-6);assert.equal(s.sides.left.displacement,21);
});
for(const side of ['left','right']) test(`single ${side} does not wait for other wrist`,()=>{
  const s=state({...valid,selected_sides:[side],sides:{[side]:valid.sides[side]}},context);
  assert.equal(s.ready,true);assert.equal(s.sides[side].visible,true);
  assert.equal(s.sides[side==='left'?'right':'left'].selected,false);
});
test('missing right input hides only right values without claiming overall ready',()=>{
  const s=state({...valid,ready:false,sides:{left:wrist,right:{...valid.sides.right,live:false}}},context);
  assert.equal(s.ready,false);assert.equal(s.kind,'caution');assert.equal(s.label,'部分输入缺失');
  assert.equal(s.sides.left.visible,true);assert.equal(s.sides.right.displacement,null);assert.equal(s.sides.right.kind,'caution');
});
for(const patch of [{has_origin:false},{needs_zero:true}]) test(`invalid origin suppresses all measurements: ${JSON.stringify(patch)}`,()=>{
  const s=state({...valid,...patch},context);assert.equal(s.ready,false);
  for(const side of ['left','right']) assert.equal(s.sides[side].displacement,null);
  assert.equal(s.canZero,true);
});
for(const patch of [{online:false},{ageMs:1001},{ageMs:Infinity},{ageMs:-1}]) test(`local stale/unknown response suppresses all values: ${JSON.stringify(patch)}`,()=>{
  const s=state(valid,{...context,...patch});assert.equal(s.ready,false);assert.equal(s.canZero,false);
  assert.equal(s.sides.left.displacement,null);assert.equal(s.sides.left.kind,'caution');
});
test('zero only available with fresh authorized backend readiness',()=>{
  assert.equal(state({...valid,zero_ready:false},context).canZero,false);
  assert.equal(state(valid,{...context,token:''}).canZero,false);
  assert.equal(state(valid,{...context,pending:true}).canZero,false);
  assert.equal(state({...valid,selected_sides:[]},context).canZero,false);
});
test('deferred zero is available without pose input while service is reachable',()=>{
  const s=state({...valid,zero_ready:false,ready:false,has_origin:false,sides:{}},context);
  assert.equal(s.canArmZero,true);assert.equal(s.canZero,false);assert.equal(s.canCancelZero,false);
  assert.equal(state(valid,{...context,online:false}).canArmZero,false);
});
test('pending deferred zero suppresses old values and permits explicit cancel only',()=>{
  const s=state({...valid,zero_pending:true},context);
  assert.equal(s.canArmZero,false);assert.equal(s.canZero,false);assert.equal(s.canCancelZero,true);
  assert.equal(s.sides.left.displacement,null);assert.match(s.detail,/保持测量起始姿势/);
});
test('malformed or missing numbers cannot display a valid zero or green card',()=>{
  for(const bad of [null,undefined,NaN,Infinity,'20']){
    const s=state({...valid,sides:{left:{...wrist,forward_cm:bad}}},context);
    assert.equal(s.sides.left.visible,false);assert.equal(s.sides.left.forward,null);
  }
  assert.equal(state({...valid,sides:{left:{...wrist,displacement_cm:-1}}},context).sides.left.visible,false);
});
test('user actual distance and stage come from computer calibration',()=>{
  assert.equal(state({...valid,target_cm:35,current_stage:'left'},context).stage,'向左');
  assert.equal(state({...valid,target_cm:35},context).actualDistance,35);
  assert.equal(state({...valid,current_stage:'complete'},context).stage,'采集完成');
});
test('raw PICO displacement is never compared with user actual distance for readiness',()=>{
  for(const displacement_cm of [0,0.1,1,300]){
    const s=state({...valid,target_cm:20,sides:{left:{...wrist,displacement_cm},right:{...wrist,displacement_cm}}},context);
    assert.equal(s.ready,true);assert.equal(s.canZero,true);assert.equal(s.actualDistance,20);
    assert.equal(s.sides.left.displacement,displacement_cm);assert.equal(s.label,'原始输入实时');
  }
});
test('raw PICO axes do not validate the users intended movement direction',()=>{
  const s=state({...valid,current_stage:'forward',sides:{left:{...wrist,forward_cm:-5,left_cm:19},right:wrist}},context);
  assert.equal(s.ready,true);assert.equal(s.stage,'向前');assert.equal(s.sides.left.forward,-5);
});
test('formatting uses centimeters, signs, and no negative zero',()=>{
  assert.equal(format(20),'20.0');assert.equal(format(-2.4,true),'-2.4');assert.equal(format(2.4,true),'+2.4');
  assert.equal(format(-0.01,true),'+0.0');assert.equal(format(null),'—');assert.equal(format(NaN),'—');
});

const flush=()=>new Promise(resolve=>setImmediate(resolve));
function browser(){
  const nodes=new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(match=>[match[1],{
    textContent:'',className:'',disabled:false,hidden:false,listeners:{},
    addEventListener(name,fn){this.listeners[name]=fn;}
  }]));
  const timers=new Map(),intervals=[],events={},requests=[];
  let now=0,nextTimer=0,current=structuredClone({...valid,token:'measurement-only'}),handler=null;
  const document={documentElement:{},hidden:false,getElementById(id){assert.ok(nodes.has(id),`missing #${id}`);return nodes.get(id);},
    addEventListener(name,fn){events[name]=fn;}};
  vm.runInNewContext(source,{document,performance:{now:()=>now},AbortController,
    setTimeout(fn,delay){timers.set(++nextTimer,{fn,delay});return nextTimer;},clearTimeout(id){timers.delete(id);},setInterval(fn){intervals.push(fn);},
    async fetch(url,options={}){requests.push({url,options});if(handler)return handler(url,options);
      return {ok:true,status:200,json:async()=>options.method==='POST'?{ok:true,result:current}:structuredClone(current)};}});
  return {nodes,requests,timers,events,intervals,setNow(value){now=value;},setData(value){current=structuredClone(value);},setHandler(fn){handler=fn;},
    poll(){const [id,timer]=[...timers].find(([,v])=>v.delay===200);timers.delete(id);timer.fn();}};
}
test('actual browser renderer hides stale numbers within watchdog without waiting for fetch timeout',async()=>{
  const b=browser();await flush();assert.equal(b.nodes.get('left-displacement').textContent,'21.0');
  b.setNow(1001);b.intervals[0]();assert.equal(b.nodes.get('left-displacement').textContent,'—');
  assert.equal(b.nodes.get('zero').disabled,true);assert.ok(!b.nodes.get('left-card').className.includes('live'));
});
test('actual POST uses only measurement zero endpoint/token and confirms via fresh GET',async()=>{
  const b=browser();await flush();await b.nodes.get('zero').listeners.click();await flush();
  const posts=b.requests.filter(r=>r.options.method==='POST');assert.equal(posts.length,1);
  assert.equal(posts[0].url,'/api/distance/zero');assert.equal(posts[0].options.headers['X-Control-Token'],'measurement-only');
  assert.equal(posts[0].options.body,'{}');assert.equal(b.requests.at(-1).url,'/api/distance');
  assert.ok(b.requests.every(r=>['/api/distance','/api/distance/zero'].includes(r.url)));
});
test('deferred action sends only arm-zero with measurement token even while poses are absent',async()=>{
  const b=browser();await flush();b.setData({...valid,token:'measurement-only',zero_ready:false,ready:false,has_origin:false,sides:{}});
  b.poll();await flush();assert.equal(b.nodes.get('zero').disabled,true);assert.equal(b.nodes.get('arm-zero').disabled,false);
  await b.nodes.get('arm-zero').listeners.click();
  const post=b.requests.find(r=>r.options.method==='POST');assert.equal(post.url,'/api/distance/arm-zero');
  assert.equal(post.options.headers['X-Control-Token'],'measurement-only');
});
test('cancel deferred zero does not issue immediate zero or robot API commands',async()=>{
  const b=browser();await flush();b.setData({...valid,token:'measurement-only',zero_pending:true});b.poll();await flush();
  assert.equal(b.nodes.get('cancel-zero').hidden,false);assert.equal(b.nodes.get('left-displacement').textContent,'—');
  await b.nodes.get('cancel-zero').listeners.click();
  const posts=b.requests.filter(r=>r.options.method==='POST');assert.equal(posts.length,1);assert.equal(posts[0].url,'/api/distance/cancel-zero');
});
test('fresh poll with lost input removes previous valid per-side values',async()=>{
  const b=browser();await flush();b.setData({...valid,token:'measurement-only',ready:false,sides:{left:wrist,right:{...wrist,live:false}}});
  b.poll();await flush();assert.equal(b.nodes.get('right-displacement').textContent,'—');assert.equal(b.nodes.get('left-displacement').textContent,'21.0');
});
test('browser resume clears old values before a new response arrives',async()=>{
  const b=browser();await flush();b.setHandler(()=>new Promise(()=>{}));b.events.visibilitychange();
  assert.equal(b.nodes.get('left-displacement').textContent,'—');assert.equal(b.nodes.get('zero').disabled,true);
});
test('a rejected origin request reports backend error and does not claim success',async()=>{
  const b=browser();await flush();b.setHandler(async(url,options)=>options.method==='POST'
    ?{ok:false,status:409,json:async()=>({ok:false,error:'请启用 Head'})}
    :{ok:true,status:200,json:async()=>({...valid,token:'measurement-only',has_origin:false,needs_zero:true})});
  await b.nodes.get('zero').listeners.click();await flush();assert.equal(b.nodes.get('left-displacement').textContent,'—');
  assert.equal(b.nodes.get('action-message').textContent,'请启用 Head');assert.equal(b.nodes.get('action-message').className,'error');
});
test('HTML declares same-origin assets, independent origin and physical ruler instructions',()=>{
  assert.match(html,/href="\/distance.css"/);assert.match(html,/src="\/distance.js"/);
  assert.match(html,/两处起点分别记录/);assert.match(html,/不能替代实体尺/);assert.match(html,/ZEDMINI \/ H.264 \/ 2560 × 720/);
  assert.match(readFileSync(new URL('../web/index.html',import.meta.url),'utf8'),/id="pico-distance-link"/);
});
test('distance page follows configured laptop address and never advertises loopback or old hardcoded IP',()=>{
  assert.equal(distanceHost({laptop_ip:'10.1.10.62'},'192.168.31.92'),'10.1.10.62');
  assert.equal(distanceHost({},'10.1.10.62'),'10.1.10.62');
  assert.equal(distanceHost({},'127.0.0.1'),'');
  assert.ok(!html.includes('192.168.31.92'));
});
test('distance renderer updates RemoteVision and footer addresses from its API',async()=>{
  const b=browser();await flush();b.setData({...valid,token:'measurement-only',laptop_ip:'10.1.10.62'});b.poll();await flush();
  assert.equal(b.nodes.get('distance-laptop-ip').textContent,'10.1.10.62');
  assert.equal(b.nodes.get('distance-footer-ip').textContent,'10.1.10.62');
});
test('page identifies PICO readings as uncalibrated reference and actual input as authoritative',()=>{
  assert.match(html,/你填写的实际距离/);assert.match(html,/PICO 原始直线位移 · 未校准/);
  assert.match(html,/不是动作目标或合格标准/);assert.match(html,/无需让读数与它们一致/);
  assert.ok(!html.includes('尺量目标'));
  const dashboard=readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
  assert.match(dashboard,/以你填写的实际移动距离和指定的前、左、上方向为校准依据/);
  assert.match(dashboard,/无需按 PICO 数值追加移动/);
});
