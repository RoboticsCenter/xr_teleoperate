import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const html = readFileSync(new URL('../web/index.html', import.meta.url), 'utf8');
function extract(name, next, bindings = {}) {
  const start = source.indexOf(`  function ${name}(`), end = source.indexOf(`  function ${next}(`, start);
  assert.ok(start >= 0 && end > start);
  return new Function(...Object.keys(bindings), `${source.slice(start, end)}\nreturn ${name};`)(...Object.values(bindings));
}
const reference = extract('armReferenceState', 'armSourceState');
const armSource = extract('armSourceState', 'armSettingsRequest');
const request = extract('armSettingsRequest', 'trackerOptions');
const network = extract('dashboardNetwork', 'renderNetwork');
const capabilities = {arm_reference_frames: ['world', 'headset']};
const settings = {arms_mode: 'both', arm_input_source: 'trackers', arm_reference_frame: 'world',
  tracker_config: {left_sn: 'LEFT', right_sn: 'RIGHT'}, hands_enabled: {left: true, right: false}};

test('reference option defaults to fixed START and requires explicit backend support', () => {
  assert.equal(reference({}).frame, 'world');
  for (const caps of [{}, {arm_reference_frames: ['world']}, {arm_reference_frames: 'world,headset'}]) {
    assert.equal(reference(settings, caps).supported, false);
  }
  assert.equal(reference({}, capabilities).supported, false);
  assert.equal(reference(settings, capabilities).supported, true);
  assert.match(html, /<select id="arm-reference-frame"[^>]*disabled><option value="world">固定方向（START）<\/option><option value="headset">跟随头显（位置与朝向）/);
});

test('headset help explains position origin, horizontal forward, gravity up and head-only effects', () => {
  const before = structuredClone(settings);
  const state = reference(settings, capabilities, 'headset');
  assert.equal(state.headset, true);
  assert.equal(state.label, '跟随头显（位置与朝向）');
  assert.match(state.help, /头显当前位置为原点/);
  assert.match(state.help, /水平朝向对应机器人前方/);
  assert.match(state.help, /上方始终竖直向上（与重力反向）/);
  assert.match(state.help, /共同平移或水平转向会抵消/);
  assert.match(state.help, /点头和侧倾不旋转参考坐标/);
  assert.match(state.help, /单独平移头显或左右转头/);
  assert.match(state.help, /手臂相对目标与腕部相对朝向/);
  assert.match(state.help, /START 仍记录中立姿态/);
  assert.match(state.help, /分别保存校准/);
  assert.deepEqual(settings, before);
});

test('save carries selected reference and preserves side choices and real wrist bindings', () => {
  const result = request({...settings, arm_reference_frame: 'headset', left_sn: 'LEFT', right_sn: 'RIGHT'});
  assert.equal(result.error, '');
  assert.equal(result.body.arm_reference_frame, 'headset');
  assert.deepEqual(result.body.hands_enabled, {left: true, right: false});
  assert.equal(result.body.left_sn, 'LEFT');
  assert.equal(result.body.right_sn, 'RIGHT');
  const controller = request({...settings, arm_input_source: 'controllers', arm_reference_frame: 'headset'});
  assert.equal(controller.error, '');
  assert.equal(controller.body.arm_reference_frame, 'headset');
  assert.equal('left_sn' in controller.body, false);
});

test('legacy saves omit unsupported reference and invalid values cannot be submitted', () => {
  assert.equal('arm_reference_frame' in request({arm_input_source: 'controllers'}).body, false);
  assert.match(request({...settings, arm_reference_frame: 'unknown'}).error, /参考坐标/);
});

function fixture() {
  const nodes = new Map();
  const $ = id => {
    if (!nodes.has(id)) nodes.set(id, {value: '', checked: false, disabled: false, textContent: '', option: {},
      querySelector() { return this.option; }, classList: {toggle() {}},
      addEventListener(name, callback) { this[name] = callback; }});
    return nodes.get(id);
  };
  const render = dirty => extract('renderSettings', 'renderHands', {
    $, settingsDirty: dirty, pending: new Set(), armReferenceState: reference, armSourceState: armSource,
    dashboardNetwork: network, trackerOptions: (id, selected) => { $(id).value = selected; },
    armNames: {both: '双臂'}, write: (id, value) => { $(id).textContent = value; }
  });
  return {$, render};
}

test('saved selection loads, draft survives polling, and clean state resets to accepted backend value', () => {
  const f = fixture(), data = {settings, capabilities, services: {tracker: {}}};
  f.render(false)(data);
  assert.equal(f.$('arm-reference-frame').value, 'world');
  f.$('arm-reference-frame').value = 'headset';
  f.render(true)(data);
  assert.equal(f.$('arm-reference-frame').value, 'headset');
  assert.match(f.$('arm-reference-help').textContent, /共同平移或水平转向会抵消/);
  assert.equal(f.$('left-tracker').value, 'LEFT');
  assert.equal(f.$('left-hand-enabled').checked, true);
  assert.match(f.$('settings-detail').textContent, /尚未保存/);
  f.render(false)({...data, settings: {...settings, arm_reference_frame: 'headset'}});
  assert.equal(f.$('arm-reference-frame').value, 'headset');
  f.render(false)(data);
  assert.equal(f.$('arm-reference-frame').value, 'world');
});

test('unsupported backend disables headset option and explains pending service update', () => {
  const f = fixture();
  f.render(false)({settings: {...settings, arm_reference_frame: undefined}, services: {tracker: {}}});
  assert.equal(f.$('arm-reference-frame').option.disabled, true);
  assert.match(f.$('arm-reference-help').textContent, /等待后台更新/);
});

test('reference selector shares active arm, hand, model, pending, and offline setting locks', () => {
  const start = source.indexOf('    const settingsAvailable =');
  const end = source.indexOf('    const checking =', start);
  const f = fixture(); f.$('arm-input-source').value = 'trackers'; f.$('arm-reference-frame').value = 'world';
  const base = {available: true, data: {settings}, caps: capabilities, active: false, handActive: false,
    modelBusy: false, operationPending: false, pending: new Set(), settingsDirty: true, $: f.$,
    armReferenceState: reference, armSourceState: armSource};
  const run = overrides => {
    const values = {...base, ...overrides};
    new Function(...Object.keys(values), source.slice(start, end))(...Object.values(values));
    return f.$('arm-reference-frame').disabled;
  };
  assert.equal(run({}), false);
  for (const flags of [{active: true}, {handActive: true}, {modelBusy: true}, {operationPending: true},
    {available: false}, {pending: new Set(['/api/settings'])}, {caps: {}}]) assert.equal(run(flags), true);
});

test('real change and save handlers mark draft dirty and send selected reference only when supported', () => {
  const f = fixture(); f.render(false)({settings, capabilities, services: {tracker: {}}});
  const start = source.indexOf("  for (const id of ['arms-mode', 'arm-input-source', 'arm-reference-frame'");
  const end = source.indexOf("  for (const side of ['left', 'right']) {", start);
  assert.ok(start > 0 && end > start);
  const calls = [], context = {$: f.$, latest: {settings, capabilities}, settingsDirty: false,
    renderSettings() {}, renderControls() {}, armSettingsRequest: request, armReferenceState: reference,
    action: (path, body) => calls.push({path, body}), flash: message => assert.fail(message)};
  vm.createContext(context); vm.runInContext(source.slice(start, end), context);
  f.$('arm-reference-frame').value = 'headset';
  f.$('arm-reference-frame').change();
  assert.equal(context.settingsDirty, true);
  assert.equal(calls.length, 0, 'selection does not trigger motion or save automatically');
  f.$('save-settings').click();
  assert.equal(calls[0].path, '/api/settings');
  assert.equal(calls[0].body.arm_reference_frame, 'headset');
  context.latest.capabilities = {};
  f.$('save-settings').click();
  assert.equal('arm_reference_frame' in calls[1].body, false);
});

test('calibration description reads its actual reference frame instead of an unsaved selector draft', () => {
  const start = source.indexOf('  function renderArmCalibration(data)');
  const end = source.indexOf('    const savedCompatible =', start);
  assert.ok(start > 0 && end > start);
  const f = fixture(); f.$('arm-reference-frame').value = 'world';
  const bindings = {$: f.$, armReferenceState: reference, numeric: Number.isFinite,
    armCalibrationContext: () => ({}), armCalibrationState: () => ({}),
    handNames: {left: '左手', right: '右手'}, write: (id, text) => { f.$(id).textContent = text; }};
  const render = new Function(...Object.keys(bindings), `${source.slice(start, end)}}\nreturn renderArmCalibration;`)(...Object.values(bindings));
  const data = {settings, capabilities, arm_calibration: {reference_frame: 'headset', selected_sides: ['right']}};
  render(data);
  assert.match(f.$('arm-calibration-sides').textContent, /跟随头显/);
  assert.match(f.$('arm-calibration-reference-note').textContent, /头显位置为原点、水平朝向为机器人前方/);
  assert.match(f.$('arm-calibration-reference-note').textContent, /上方始终竖直向上/);
  assert.match(f.$('arm-calibration-reference-note').textContent, /点头和侧倾不旋转参考坐标/);
  assert.match(f.$('arm-calibration-reference-note').textContent, /测量时保持头显稳定/);
  render({...data, settings: {...settings, arm_reference_frame: 'headset'}, arm_calibration: {reference_frame: 'world'}});
  assert.match(f.$('arm-calibration-sides').textContent, /固定方向（START）/);
  assert.match(f.$('arm-calibration-reference-note').textContent, /不套用跟随头显模式/);
});
