(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const pending = new Set();
  let latest = null;
  let connected = false;
  let controlToken = '';
  let possiblyActive = false;
  let pollBusy = false;
  let cameraBusy = false;
  let cameraRequestedAt = 0;
  let imageFailed = false;
  let lastHistory = '';
  let statusReceivedAt = 0;
  let settingsDirty = false;
  const possiblyHandActive = {left: false, right: false};
  const possiblyModelActive = {left: false, right: false};
  const armNames = {left: '左臂', right: '右臂', both: '双臂'};
  const armCaptureStages = ['origin', 'forward', 'left', 'up'];
  const armStageNames = {origin: '原点', forward: '向前', left: '向左', up: '向上'};
  const handNames = {left: '左手', right: '右手'};
  const modelStageLabels = {idle: '待开始', starting: '正在连接手套', preparing: '准备校准',
    backing_up: '正在备份原模型', backup: '正在备份原模型', calibrating: '按提示完成手型',
    collecting: '正在采集手型', progress: '按提示完成手型', saving: '正在保存模型',
    completed: '个人模型已保存', done: '个人模型已保存', success: '个人模型已保存',
    cancelled: '模型校准已取消', cancelling: '正在取消', error: '模型校准异常', failed: '模型校准异常'};
  const previewChannelNames = {thumb_flex: '拇指弯曲', thumb_opposition: '拇指对掌',
    index: '食指', middle: '中指', ring: '无名指', little: '小指'};
  const previewMessages = {'Calibration capture in progress': '正在采集开合姿态，完成后恢复预览。',
    'Waiting for live glove input': '等待有效手套输入，请启动该侧读取并查看输入状态。',
    'Complete open/fist calibration': '完成张开与握拳采集后显示映射预览。'};
  const stageLabels = {
    idle: '等待准备', starting: '正在准备', launching: '正在准备', launch: '正在准备',
    preparing: '正在准备', initializing: '初始化设备', prepared: '准备完成', ready: '准备完成',
    waiting: '等待开始', waiting_for_start: '等待开始', preview: '正在预演', previewing: '正在预演',
    release: '正在准备控制', mode_probe: '正在检查控制状态', motion: '正在跟随',
    following: '正在跟随', running: '正在跟随', tracker_live: 'Tracker 实时跟随',
    holding: '保持当前目标', paused_input: '等待输入恢复', reconnecting: '输入重新连接中',
    preview_completed: '预演完成', motion_session_completed: '会话已结束', completed: '会话已结束',
    stopped: '会话已结束', finished: '会话已结束', stopping: '正在停止', error: '会话异常', failed: '会话异常'
  };
  const liveStages = new Set(['motion', 'following', 'running', 'tracker_live']);
  const holdStages = new Set(['holding', 'paused_input', 'reconnecting']);
  const readyStages = new Set(['prepared', 'ready', 'waiting', 'waiting_for_start']);
  const endStages = new Set(['preview_completed', 'motion_session_completed', 'completed', 'stopped', 'finished', 'error', 'failed']);
  const asText = value => typeof value === 'string' ? value : value == null ? '' : JSON.stringify(value);
  const write = (id, value) => { $(id).textContent = asText(value); };
  const stageOf = session => String(session.stage || 'idle').toLowerCase();
  const numeric = value => typeof value === 'number' && Number.isFinite(value);
  const count = value => numeric(value) ? Math.max(0, Math.floor(value)).toLocaleString('zh-CN') : '未知';
  function compactError(value, limit = 360) {
    const text = typeof value === 'string' ? value : value == null ? '' : JSON.stringify(value);
    if (/No route to host|Network is unreachable/i.test(text)) return '机器人网络不可达。请确认 G1 已开机并恢复网络连接后，再准备会话。';
    if (/Host key verification failed|REMOTE HOST IDENTIFICATION HAS CHANGED|No .* host key is known/i.test(text)) return 'SSH 主机身份校验失败。请核对机器人身份和已保存的主机密钥。';
    if (/Permission denied|Authentication failed|Incorrect password/i.test(text)) return '机器人 SSH 认证失败。请检查登录凭据。';
    if (/Connection refused/i.test(text)) return '机器人拒绝 SSH 连接。请确认 G1 已启动且 SSH 服务可用。';
    if (/Connection timed out|connect to host[^\n]*timed out|Timeout exceeded[\s\S]*\/usr\/bin\/ssh/i.test(text)) return '连接机器人超时。请检查 G1 的网络与 SSH 服务后重试。';
    const cleaned = text.split('\n').filter(line => !/^\s*(?:<pexpect\.|command:|args:|buffer \(|before \(|after:|searcher:)/.test(line)).join('\n').trim();
    return cleaned.length > limit ? `${cleaned.slice(0, limit)} …（完整信息见本机会话日志）` : cleaned;
  }
  function clock(seconds) {
    const n = Math.max(0, Math.floor(numeric(seconds) ? seconds : 0));
    const parts = [Math.floor(n / 3600), Math.floor(n / 60) % 60, n % 60];
    return (parts[0] ? parts : parts.slice(1)).map(v => String(v).padStart(2, '0')).join(':');
  }
  function badge(id, label, kind = 'neutral') {
    write(id, label);
    $(id).className = `badge ${kind}`;
  }
  function flash(message, error = false) {
    write('action-message', message);
    $('action-message').hidden = !message;
    $('action-message').className = error ? 'error' : '';
  }
  function handFlash(side, message, error = false) {
    if (!handNames[side]) return;
    const node = $(`${side}-hand-action-message`);
    node.textContent = message;
    node.hidden = !message;
    node.classList.toggle('error', error);
  }
  function armCalibrationFlash(message, error = false) {
    const node = $('arm-calibration-action-message');
    node.textContent = calibrationDisplayError(message);
    node.hidden = !message;
    node.classList.toggle('error', error);
  }
  function calibrationDisplayError(value) {
    const text = typeof value === 'string' ? value : value == null ? '' : JSON.stringify(value);
    return text.replace(/^(left|right)\s+(forward|left|up)位移不足\s*3\s*cm(?:[，,]\s*请回原点后重新测量)?[。.]?\s*$/i,
      (_, side, axis) => `${{left: '左腕', right: '右腕'}[side.toLowerCase()]}${{forward: '向前', left: '向左', up: '向上'}[axis.toLowerCase()]}位移不足 3 厘米，请回到原点后重新测量。`);
  }
  function picoSetupFlash(message, error = false) {
    const node = $('pico-setup-action-message');
    node.textContent = message;
    node.hidden = !message;
    node.classList.toggle('error', error);
  }
  function connectionFlash(message, error = false) {
    const node = $('robot-connection-action-message');
    node.textContent = message;
    node.hidden = !message;
    node.classList.toggle('error', error);
  }
  function flag(data, key, fallback) {
    for (const object of [data.session, data, data.capabilities]) {
      if (object && typeof object[key] === 'boolean') return object[key];
    }
    return fallback;
  }
  function activeSession(session) {
    return session.running === true || session.prepared === true || session.following === true ||
      readyStages.has(stageOf(session)) || liveStages.has(stageOf(session)) || holdStages.has(stageOf(session));
  }
  function modelActive(model) {
    return model?.active === true || model?.running === true;
  }
  function calibrationBusy(hands = {}) {
    return ['left', 'right'].some(side => modelActive(hands[side]?.model_calibration) ||
      possiblyModelActive[side] || pending.has(`/api/gloves/model/start:${side}`));
  }
  function handPresentation(hand) {
    const mappingReset = 'Personal model recalibrated; capture open and fist again';
    const legacyPending = !Object.prototype.hasOwnProperty.call(hand, 'calibration_pending') &&
      hand.model_ready === true && hand.calibration_ready === false &&
      (hand.calibration_error === mappingReset || hand.error === mappingReset);
    const calibrationPending = hand.calibration_pending === true || legacyPending;
    const captureError = hand.capture_error || (hand.capture_result?.state === 'error' ? hand.capture_result.message : '');
    const holding = hand.running === true && String(hand.stage || '').toLowerCase() === 'holding';
    const transientRejections = [
      'target exceeds original measured-position step bound',
      'feedback changed during publisher setup; hold previous target',
      'left feedback unavailable; hold previous target',
      'right feedback unavailable; hold previous target'
    ];
    const recoverable = reason => holding && hand.delivery_unknown !== true && transientRejections.includes(reason);
    const holdReason = [hand.motion_error, hand.error].find(recoverable);
    const motionError = recoverable(hand.motion_error) ? '' : hand.motion_error;
    const combinedError = hand.error_kind === 'readiness' || legacyPending && hand.error === mappingReset || recoverable(hand.error) ? '' : hand.error;
    const calibrationError = hand.calibration_invalidation_error || (!calibrationPending ? hand.calibration_error : '');
    const fault = [['读取异常', hand.reader_error], ['手部通信异常', hand.transport_error],
      ['手部输出异常', motionError], ['联动启动异常', hand.auto_start_error], ['采集异常', captureError],
      ['模型校准异常', hand.model_calibration?.error], ['校准状态异常', calibrationError],
      ['手部异常', combinedError]].find(([, error]) => !!error);
    if (fault) return {label: fault[0], detail: compactError(fault[1]), kind: 'bad', error: true, calibrationPending};
    if (modelActive(hand.model_calibration)) return {label: '个人模型校准中', detail: hand.model_calibration.instruction || hand.detail, kind: 'caution'};
    if (hand.capture) return {label: '开合采集中', detail: '保持当前手型，等待本次采集完成。', kind: 'caution'};
    if (holding) {
      const waitingFeedback = hand.feedback?.fresh !== true;
      const waitingInput = hand.input_live !== true;
      const label = waitingFeedback ? '等待手部反馈' : waitingInput ? '等待输入' : '保持当前目标';
      const fallback = waitingFeedback ? '手部反馈缺失或过期，保持上一目标，等待反馈恢复。'
        : waitingInput ? '手套输入暂不可用，保持上一目标，等待有效输入恢复。' : '本次目标未发送，保持上一目标。';
      return {label, kind: 'caution', detail: holdReason || hand.hold_reason || hand.detail || fallback};
    }
    if (hand.running === true && hand.stage === 'preparing') return {
      label: hand.input_live === false ? '等待手套输入' : '正在准备手部输出', kind: 'caution',
      detail: hand.auto_start_detail || hand.detail || (hand.input_live === false ? '等待该侧手套的有效输入。' : '正在准备机械手输出，请等待服务就绪。')};
    if (hand.running) return {label: hand.input_live === false ? '等待输入' : '正在跟随',
      detail: hand.detail, kind: hand.input_live === false ? 'caution' : 'good'};
    if (calibrationPending) {
      const hasOpen = numeric(hand.captured_poses?.open) && hand.captured_poses.open > 0;
      const inputHint = hand.input_live !== true ? '请先启动该侧读取并等待有效输入，再' : '请';
      return {label: '需开合校准', kind: 'caution', calibrationPending: true,
        detail: `个人模型已就绪。${inputHint}${hasOpen ? '握拳保持，采集握拳以完成开合映射。' : '自然张开手指，先采集张开，再采集握拳。'}`};
    }
    if (hand.auto_start_pending === true) {
      const labels = {preparing_input: '正在启动手套读取', waiting_input: '等待手套输入',
        waiting_arm: '等待手臂跟随', waiting_calibration: '等待校准完成', starting: '正在启动手部输出'};
      return {label: labels[hand.auto_start_stage] || '等待手部启动', kind: 'caution',
        detail: hand.auto_start_detail || hand.detail || '该侧已排队，等待输入和会话就绪；可点击该侧停止取消。'};
    }
    const ready = hand.input_live === true && hand.model_ready === true && hand.calibration_ready === true;
    return {label: ready ? '可开始跟随' : '尚未就绪', detail: hand.detail || '等待只读输入检查。', kind: ready ? 'good' : 'neutral'};
  }
  function handStopAvailable(hand, context) {
    return context.authorized && !context.stopping &&
      (hand.running === true || hand.auto_start_pending === true || context.possiblyActive === true);
  }
  function renderControls() {
    const data = latest || {};
    const session = data.session || {};
    const caps = data.capabilities || {};
    const services = data.services || {};
    const active = activeSession(session);
    const hands = services.hands || {};
    const handActive = ['left', 'right'].some(side => hands[side]?.running === true || possiblyHandActive[side]);
    const modelBusy = calibrationBusy(hands);
    const captureBusy = ['left', 'right'].some(side => !!hands[side]?.capture || pending.has(`/api/hands/calibrate:${side}`));
    const selectedHands = ['left', 'right'].some(side => data.settings?.hands_enabled?.[side] === true);
    const supported = caps.teleop !== false && !!(caps.right_arm || caps.dual_arm || caps.arms || caps.hands);
    const available = connected && !!controlToken;
    const calibrationAvailable = available && caps.glove_calibration === true;
    const operationPending = [...pending].some(path => ['/api/launch', '/api/start', '/api/stop', '/api/preview'].includes(path));
    const robotOffline = services.robot?.connected === false;
    $('launch').disabled = !available || settingsDirty || pending.has('/api/settings') || operationPending || !flag(data, 'can_launch', supported && !active && !robotOffline);
    $('start').disabled = !available || settingsDirty || operationPending || selectedHands && (modelBusy || captureBusy) || !flag(data, 'can_start', readyStages.has(stageOf(session)) && !robotOffline);
    const commonBlock = !available ? '等待本机控制服务连接。' : settingsDirty ? '先保存顶部选择（包含手指勾选）。'
      : pending.has('/api/settings') ? '正在保存选择。' : operationPending ? '正在处理会话操作，请等待状态更新。' : '';
    const launchReason = commonBlock || (active ? '会话已准备或运行，无需重复 LAUNCH。'
      : robotOffline ? '机器人网络未连接，请查看机器人状态。' : '当前会话暂不能准备，请查看会话状态。');
    const startReason = commonBlock || (selectedHands && (modelBusy || captureBusy) ? '所选手指正在校准，请等待完成。'
      : liveStages.has(stageOf(session)) || holdStages.has(stageOf(session)) ? '跟随会话已启动。'
      : readyStages.has(stageOf(session)) ? '接收端尚未读到开始跟随所需的有效数据，请查看下方 PICO 与设备状态。'
      : stageOf(session) === 'error' || stageOf(session) === 'failed' ? 'LAUNCH 尚未完成，请先处理会话错误并重新准备。'
      : '先完成 LAUNCH，等待准备完成后再 START。');
    $('launch-disabled-reason').hidden = !$('launch').disabled;
    $('start-disabled-reason').hidden = !$('start').disabled;
    write('launch-disabled-reason', `LAUNCH：${launchReason}`);
    write('start-disabled-reason', `START：${startReason}`);
    $('stop').disabled = !controlToken || pending.has('/api/stop') || !(flag(data, 'can_stop', active) || possiblyActive || handActive);
    $('stop').classList.toggle('stop-pending', pending.has('/api/stop') || session.stop_requested === true);
    $('preview').hidden = caps.preview !== true;
    $('preview').disabled = !available || operationPending || active;
    const picoState = picoSetupState(services.pico_setup || {}, {supported: caps.pico_setup === true,
      online: connected, authorized: !!controlToken, armActive: active || possiblyActive,
      pending: pending.has('/api/pico/start') || pending.has('/api/pico/stop')});
    for (const id of ['pico-start', 'pico-setup-start']) $(id).disabled = !picoState.canStart;
    for (const id of ['pico-stop', 'pico-setup-stop']) $(id).disabled = !picoState.canStop;
    const connection = robotConnectionState(services.robot || {}, {supported: caps.robot_connection === true,
      online: connected, authorized: !!controlToken,
      pending: pending.has('/api/robot/connect') || pending.has('/api/robot/disconnect')});
    $('robot-connect').disabled = !connection.canConnect;
    $('robot-disconnect').disabled = !connection.canDisconnect;
    const armInput = armInputState(services.arm_input || {}, {supported: caps.arm_input_check === true,
      online: connected, authorized: !!controlToken, settingsDirty, inputSource: data.settings?.arm_input_source,
      pending: pending.has('/api/arms/input/check') || pending.has('/api/pico/start') || pending.has('/api/pico/stop')});
    for (const id of ['pico-arm-input-check', 'calibration-arm-input-check']) $(id).disabled = !armInput.canCheck;
    const camera = services.camera || {};
    $('camera-start').disabled = !available || camera.running === true || pending.has('/api/camera/start');
    $('camera-stop').disabled = !available || camera.running !== true || pending.has('/api/camera/stop');
    $('pico-video').disabled = !available || camera.running === true || pending.has('/api/camera/start');
    const settingsAvailable = available && !!data.settings && !active && !handActive && !modelBusy && !operationPending && !pending.has('/api/settings');
    ['arms-mode', 'left-hand-enabled', 'right-hand-enabled'].forEach(id => { $(id).disabled = !settingsAvailable; });
    const source = armSourceState(data.settings || {}, $('arm-input-source').value);
    $('arm-input-source').disabled = !settingsAvailable || !source.supported;
    const reference = armReferenceState(data.settings || {}, caps, $('arm-reference-frame').value);
    $('arm-reference-frame').disabled = !settingsAvailable || !reference.supported;
    for (const side of ['left', 'right']) $(`${side}-tracker`).disabled = !settingsAvailable || source.controllers;
    $('save-settings').disabled = !settingsAvailable || !settingsDirty;
    const checking = services.tracker?.running === true || hands.input_running === true;
    $('inputs-start').disabled = !available || checking || active || handActive || modelBusy || pending.has('/api/inputs/start');
    $('inputs-stop').disabled = !available || !checking || active || handActive || modelBusy || captureBusy || pending.has('/api/inputs/stop');
    $('gloves-refresh').disabled = !calibrationAvailable || modelBusy || captureBusy || pending.has('/api/gloves/refresh');
    for (const side of ['left', 'right']) {
      const hand = hands[side] || {};
      const model = hand.model_calibration || {};
      const ready = hand.can_start ?? (hand.input_live === true && hand.calibration_ready === true && hand.model_ready === true);
      const starting = pending.has(`/api/hands/start:${side}`);
      const stopping = pending.has(`/api/hands/stop:${side}`) || pending.has('/api/stop');
      const calibrating = !!hand.capture || pending.has(`/api/hands/calibrate:${side}`) || pending.has(`/api/hands/calibrate/cancel:${side}`);
      const readerRunning = hand.input_running === true;
      $(`${side}-hand-start`).disabled = !available || !ready || hand.running === true || starting || stopping || calibrating || modelBusy || settingsDirty;
      $(`${side}-hand-stop`).disabled = !handStopAvailable(hand, {authorized: !!controlToken, stopping, possiblyActive: possiblyHandActive[side]});
      for (const pose of ['open', 'closed']) {
        const missingOpen = pose === 'closed' && !(numeric(hand.captured_poses?.open) && hand.captured_poses.open > 0);
        $(`${side}-hand-${pose}`).disabled = !calibrationAvailable || hand.input_live !== true || hand.model_ready !== true || handActive || starting || stopping || calibrating || modelBusy || missingOpen;
      }
      $(`${side}-model-start`).disabled = !calibrationAvailable || modelBusy || handActive || captureBusy || model.can_start === false;
      $(`${side}-model-continue`).hidden = model.awaiting_continue !== true;
      $(`${side}-model-continue`).disabled = !calibrationAvailable || !modelActive(model) || model.awaiting_continue !== true || pending.has(`/api/gloves/model/continue:${side}`);
      $(`${side}-model-cancel`).disabled = !controlToken || caps.glove_calibration !== true || !(modelActive(model) || possiblyModelActive[side]) || pending.has(`/api/gloves/model/cancel:${side}`);
      $(`${side}-glove-input-start`).disabled = !calibrationAvailable || modelBusy || hand.model_ready !== true || readerRunning || hand.running === true || pending.has(`/api/gloves/input/start:${side}`);
      $(`${side}-glove-input-stop`).disabled = !calibrationAvailable || modelBusy || !readerRunning || hand.running === true || calibrating || pending.has(`/api/gloves/input/stop:${side}`);
      $(`${side}-capture-cancel`).disabled = !controlToken || caps.glove_calibration !== true || !(hand.capture_cancel_ready === true || !!hand.capture) || pending.has(`/api/hands/calibrate/cancel:${side}`);
    }
    renderArmCalibrationControls();
  }
  function armCalibrationState(calibration, context) {
    const stages = ['origin', 'forward', 'left', 'up'];
    const captured = stages.filter(stage => calibration.captured_stages?.includes(stage) || stage === 'origin' && calibration.origin_captured === true);
    const nextStage = stages.find(stage => !captured.includes(stage)) || null;
    const distance = Number(context.distance);
    const validDistance = String(context.distance).trim() !== '' && Number.isFinite(distance) && distance >= 3 && distance <= 100;
    const available = context.supported === true && context.online === true && context.authorized === true &&
      !context.outputsActive && !context.modelBusy && !context.settingsDirty && !context.pending;
    const candidate = calibration.candidate;
    const saved = calibration.saved;
    const savedMatches = calibration.candidate_saved ?? !!(candidate && saved &&
      Object.keys(candidate).every(key => JSON.stringify(candidate[key]) === JSON.stringify(saved[key])));
    return {captured, nextStage, distance, validDistance, savedMatches,
      canCapture: available && calibration.can_capture === true && calibration.input_ready === true && validDistance,
      canSave: available && calibration.can_save === true && !!candidate,
      canReset: available && calibration.can_reset === true,
      distanceDisabled: !available || captured.includes('origin')};
  }
  function armCalibrationContext() {
    const data = latest || {};
    const hands = data.services?.hands || {};
    const outputsActive = activeSession(data.session || {}) || possiblyActive ||
      ['left', 'right'].some(side => hands[side]?.running === true || possiblyHandActive[side]);
    return {supported: data.capabilities?.arm_calibration === true, online: connected, authorized: !!controlToken,
      outputsActive, modelBusy: calibrationBusy(hands), settingsDirty,
      pending: [...pending].some(path => path.startsWith('/api/arms/calibration/') ||
        ['/api/launch', '/api/start', '/api/stop', '/api/preview', '/api/settings'].includes(path) || path.startsWith('/api/hands/start:') || path.startsWith('/api/hands/stop:')),
      distance: $('arm-calibration-distance').value};
  }
  function renderArmCalibrationControls() {
    const calibration = latest?.arm_calibration || {};
    const context = armCalibrationContext();
    const state = armCalibrationState(calibration, context);
    for (const stage of armCaptureStages) {
      $(`arm-capture-${stage}`).disabled = !state.canCapture || state.nextStage !== stage;
    }
    $('arm-calibration-distance').disabled = state.distanceDisabled;
    $('arm-calibration-save').disabled = !state.canSave;
    $('arm-calibration-reset').disabled = !state.canReset;
  }
  function renderArmCalibration(data) {
    const calibration = data.arm_calibration || {};
    const controllers = (calibration.input_source || data.settings?.arm_input_source) === 'controllers';
    const reference = armReferenceState({arm_reference_frame: calibration.reference_frame || data.settings?.arm_reference_frame}, data.capabilities || {});
    const inputName = controllers ? '手柄' : '手腕';
    if (calibration.origin_captured === true && numeric(calibration.distance_cm)) $('arm-calibration-distance').value = String(calibration.distance_cm);
    const context = armCalibrationContext();
    const state = armCalibrationState(calibration, context);
    const selected = Array.isArray(calibration.selected_sides) ? calibration.selected_sides.filter(side => handNames[side]) : [];
    write('arm-calibration-sides', `${reference.label} · ${controllers ? '手柄独立校准' : '腕部 Tracker 校准'} · ` + (selected.length
      ? `采集范围：${selected.map(side => controllers ? side === 'left' ? '左手柄' : '右手柄' : side === 'left' ? '左腕' : '右腕').join(' + ')}` : '等待当前输入选择'));
    write('arm-calibration-reference-note', reference.headset
      ? '本轮校准与跟随都以头显位置为原点、水平朝向为机器人前方，上方始终竖直向上；点头和侧倾不旋转参考坐标。测量时保持头显稳定，只移动所选手臂输入。校准单独保存，不套用固定方向模式的修正。'
      : '本轮校准使用 START 时的固定参考方向；校准单独保存，不套用跟随头显模式的修正。');
    const savedCompatible = !!calibration.saved && calibration.saved_compatible !== false;
    const savedComplete = savedCompatible && calibration.saved_complete !== false;
    const badgeLabel = !context.supported ? '服务待就绪' : calibration.error ? '校准未完成'
      : calibration.candidate ? state.savedMatches ? '已保存' : '结果待保存'
      : state.captured.length ? '正在采集' : savedComplete ? '已有保存配置' : savedCompatible ? '部分腕部已保存' : '待采集';
    badge('arm-calibration-badge', badgeLabel, calibration.error ? 'caution'
      : state.savedMatches || savedComplete && !state.captured.length ? 'good' : calibration.candidate || savedCompatible && !savedComplete ? 'caution' : 'neutral');
    const blocked = !context.online ? '本机服务连接中断，采集状态未知。恢复连接后自动更新。'
      : !context.supported ? '手臂方向校准服务尚未就绪，功能就绪后会自动启用。'
      : context.outputsActive ? '请先停止全部手臂和机械手跟随，等待会话结束后再采集。'
      : context.modelBusy ? '请等待手套个人模型校准结束后，再采集腕部方向。'
      : context.settingsDirty ? '顶部选择尚未保存，请先保存手臂与输入来源选择。'
      : calibration.input_ready !== true ? calibration.input_detail || '等待所选腕部和头部的有效输入。点击「检查 Head / Tracker 输入」查看缺少的数据。' : '';
    write('arm-calibration-instruction', blocked || calibration.instruction || (state.savedMatches
      ? '方向和距离配置已保存，将在下一次 START 使用。'
      : calibration.candidate ? '采集完成。检查下方所选输入的结果，再点击保存。'
      : state.nextStage === 'origin' ? `面朝正前方，所选${inputName}保持舒适中立姿态，然后采集原点。`
      : state.nextStage ? `先将所选${inputName}返回原点，再按你指定的${armStageNames[state.nextStage]}方向实际移动 ${state.distance} cm 并保持朝向，点击对应采集按钮；无需匹配 PICO 读数。`
      : '四步采集已记录，请查看校准结果或错误提示；需要重采可重置本轮。'));
    $('arm-calibration-instruction').classList.toggle('caution', !!blocked);
    for (const stage of armCaptureStages) {
      const done = state.captured.includes(stage);
      write(`arm-stage-${stage}-status`, done ? '已采集' : state.nextStage === stage ? '当前步骤' : '未采集');
      const element = document.querySelector(`[data-arm-stage="${stage}"]`);
      element.classList.toggle('done', done);
      element.classList.toggle('current', state.nextStage === stage);
      if (state.nextStage === stage) element.setAttribute('aria-current', 'step'); else element.removeAttribute('aria-current');
    }
    const error = calibration.error || calibration.saved_error || '';
    $('arm-calibration-error').hidden = !error;
    write('arm-calibration-error', calibrationDisplayError(error));
    $('arm-calibration-candidate').hidden = !calibration.candidate;
    badge('arm-candidate-badge', state.savedMatches ? '已保存 · 下次 START 生效' : '尚未保存', state.savedMatches ? 'good' : 'caution');
    renderArmCandidate(calibration.candidate, controllers);
    $('arm-calibration-saved').hidden = !calibration.saved;
    write('arm-calibration-saved', !calibration.saved ? '' : calibration.saved_compatible === false
      ? controllers ? '此手柄校准与当前选择不一致；未匹配侧使用原始 1:1 跟随，也可重新采集保存。' : '已有配置与当前输入选择不一致，请完成本轮采集并保存。'
      : calibration.saved_complete === false ? controllers ? '未保存手柄校准的侧使用原始 1:1 跟随；可按需采集并保存。' : '当前所选输入尚未全部保存校准，请完成本轮采集并保存。'
      : state.savedMatches ? '已保存；下一次 START 会重新捕获中立姿态。'
      : '已有保存配置；本轮新结果在点击保存前不会替换它。');
  }
  function armCandidatePresentation(value, matrix = value.translation_correction ?? null) {
    const finite = number => typeof number === 'number' && Number.isFinite(number);
    const validMatrix = Array.isArray(matrix) && matrix.length === 3 && matrix.every(row => Array.isArray(row) && row.length === 3 && row.every(finite));
    const axisText = values => ['forward', 'left', 'up'].filter(axis => finite(values?.[axis])).map(axis =>
      `${{forward: '向前', left: '向左', up: '向上'}[axis]} ${values[axis].toFixed(1)} cm`).join(' · ');
    const diagnostics = value.diagnostics || {};
    return {matrix: validMatrix ? matrix : null,
      title: validMatrix ? '三轴平移修正' : '旧版统一比例修正',
      description: validMatrix ? '按你指定的三个方向与实际距离分别修正平移；不改变腕部旋转。'
        : finite(value.translation_scale) ? `旧版统一平移倍率 ${value.translation_scale.toFixed(3)}×；各方向共用该倍率。` : '此结果未提供可显示的平移修正。',
      actual: axisText(diagnostics.desired_distance_cm),
      raw: axisText(diagnostics.measured_distance_cm)};
  }
  function renderArmCandidate(candidate, controllers = false) {
    const container = $('arm-candidate-sides');
    const sides = candidate?.sides || {};
    const signature = JSON.stringify([sides, controllers]);
    if (container.dataset.signature === signature) return;
    container.dataset.signature = signature;
    container.replaceChildren(...['left', 'right'].filter(side => sides[side]).map(side => {
      const value = sides[side];
      const presentation = armCandidatePresentation(value);
      const card = document.createElement('article'); card.className = 'arm-candidate-side';
      const title = document.createElement('h4'); title.textContent = controllers ? side === 'left' ? '左手柄' : '右手柄' : side === 'left' ? '左腕' : '右腕';
      const heading = document.createElement('p'); heading.textContent = presentation.title;
      card.append(title, heading);
      const rows = [presentation.description,
        presentation.actual ? `你填写的实际距离（校准依据）：${presentation.actual}` : '',
        presentation.raw ? `PICO 原始位移（未校准，仅供参考）：${presentation.raw}` : '',
        value.serial ? `${controllers ? '手柄输入标识' : 'Tracker'}：${value.serial}` : ''];
      for (const text of rows.filter(Boolean)) { const row = document.createElement('p'); row.className = 'detail'; row.textContent = text; card.append(row); }
      if (presentation.matrix) {
        const details = document.createElement('details'); details.className = 'model-log';
        const summary = document.createElement('summary'); summary.textContent = '查看三轴平移修正矩阵';
        const matrix = document.createElement('pre'); matrix.tabIndex = 0;
        matrix.textContent = presentation.matrix.map(row => `[ ${row.map(number => number.toFixed(4)).join('  ')} ]`).join('\n');
        details.append(summary, matrix); card.append(details);
      }
      return card;
    }));
  }
  function picoSetupState(setup, context) {
    const controllers = (setup.input_source || context.inputSource) === 'controllers';
    const download = setup.download || {};
    const allRunning = download.running === true && setup.receiver_running === true && setup.reader_running === true;
    const leftSN = controllers ? 'pico-controller-left' : typeof setup.left_sn === 'string' ? setup.left_sn.trim() : '';
    const rightSN = controllers ? 'pico-controller-right' : typeof setup.right_sn === 'string' ? setup.right_sn.trim() : '';
    const distinctSN = !!leftSN && !!rightSN && leftSN !== rightSN;
    const conflictingSN = !!leftSN && !!rightSN && leftSN === rightSN;
    const inputReady = context.online === true && context.supported === true && setup.ready === true && setup.headset_connected === true && setup.input_live === true && setup.head_ready === true &&
      setup.left_ready === true && setup.right_ready === true && distinctSN;
    const available = context.supported === true && context.online === true && context.authorized === true && !context.pending;
    const label = !context.supported ? '功能待就绪' : !context.online ? '状态未知' : inputReady ? controllers ? '双手柄输入已就绪' : '双腕输入已就绪'
      : conflictingSN ? '左右腕 SN 冲突' : setup.reader_running === true && setup.headset_connected !== true ? '等待头显发送'
      : setup.headset_connected === true ? controllers ? '等待 Head 与手柄数据' : '等待 Head 与双腕数据' : allRunning ? '等待头显发送'
      : setup.receiver_running === true || setup.reader_running === true ? '部分服务运行中'
      : download.running === true ? '下载页面已就绪' : 'PICO 尚未启动';
    return {allRunning, inputReady, conflictingSN, leftSN, rightSN, label,
      kind: !context.online ? 'caution' : !context.supported ? 'neutral' : inputReady ? 'good'
        : setup.receiver_running === true || setup.reader_running === true || conflictingSN ? 'caution' : 'neutral',
      canStart: available && !allRunning,
      canStop: available && !context.armActive && (setup.receiver_running === true || setup.reader_running === true)};
  }
  function renderPicoSetup(data) {
    const controllers = data.settings?.arm_input_source === 'controllers';
    const tracker = data.services?.tracker || {};
    const setup = {...(data.services?.pico_setup || {}), input_source: controllers ? 'controllers' : 'trackers'};
    if (controllers) for (const side of ['left', 'right']) setup[`${side}_ready`] = tracker.controllers?.[side]?.ready === true;
    const supported = data.capabilities?.pico_setup === true;
    const state = picoSetupState(setup, {supported, online: connected});
    for (const side of ['left', 'right']) write(`pico-${side}-label`, controllers ? side === 'left' ? '左手柄' : '右手柄' : side === 'left' ? '左腕' : '右腕');
    badge('pico-setup-badge', state.label, state.kind);
    const badges = ['pico-download-badge', 'pico-receiver-badge', 'pico-broadcast-badge', 'pico-reader-badge',
      'pico-headset-badge', 'pico-stream-badge', 'pico-head-badge', 'pico-left-badge', 'pico-right-badge'];
    if (!connected || !supported) {
      for (const id of badges) badge(id, connected ? '待服务更新' : '状态未知', connected ? 'neutral' : 'caution');
      write('pico-setup-detail', connected ? 'PICO 整合服务尚未就绪，功能就绪后会自动启用。安装页链接继续可用。' : '本机控制台连接中断，PICO 服务与输入状态未知。');
      for (const id of ['pico-download-detail', 'pico-receiver-detail', 'pico-broadcast-detail', 'pico-reader-detail']) write(id, connected ? '等待整合服务状态。' : '连接中断，等待重新读取状态。');
      return;
    }
    const download = setup.download || {};
    const broadcast = setup.broadcast || {};
    badge('pico-download-badge', download.error ? '下载页异常' : download.running === true ? download.available === false ? 'APK 不可用' : '可访问' : '未启动',
      download.error || download.available === false ? 'bad' : download.running === true ? 'good' : 'neutral');
    const network = dashboardNetwork(data);
    write('pico-download-detail', download.error || (download.available === false ? '安装包暂不可用。' : network.pageURL || '等待读取电脑地址。'));
    badge('pico-receiver-badge', setup.receiver_running === true ? '运行中' : '未启动', setup.receiver_running === true ? 'good' : 'neutral');
    write('pico-receiver-detail', setup.receiver_running === true ? '电脑接收服务运行中，头显连接单独显示。' : '点击一键启动 PICO 准备接收。');
    const observed = broadcast.observed === true;
    const wrongIP = observed && !!network.laptop && broadcast.ip !== network.laptop;
    badge('pico-broadcast-badge', broadcast.error ? '观测异常' : wrongIP ? '广播 IP 不符'
      : observed ? setup.receiver_running ? '已实收广播' : '曾观测到广播' : setup.receiver_running ? '等待广播' : '尚未观测',
      broadcast.error ? 'bad' : wrongIP || !observed && setup.receiver_running ? 'caution' : observed && setup.receiver_running ? 'good' : 'neutral');
    const age = numeric(broadcast.age_s) ? `${Math.max(0, broadcast.age_s).toFixed(1)} 秒前 · ` : '';
    write('pico-broadcast-detail', broadcast.error || (observed ? `${age}${broadcast.ip || 'IP 未提供'}` : `等待实际收到${network.laptop ? ` ${network.laptop} ` : '已配置电脑地址的'}发现广播。`));
    badge('pico-reader-badge', setup.reader_running === true ? '运行中' : '未启动', setup.reader_running === true ? 'good' : 'neutral');
    write('pico-reader-detail', setup.reader_running === true ? '读取进程运行中，有效输入单独显示。' : '一键启动同时准备 PICO 输入读取。');
    badge('pico-headset-badge', setup.headset_connected === true ? '已连接' : '等待 Send', setup.headset_connected === true ? 'good' : 'caution');
    badge('pico-stream-badge', setup.input_live === true ? '正在接收' : '等待新帧', setup.input_live === true ? 'good' : 'caution');
    badge('pico-head-badge', setup.head_ready === true ? '有效' : '等待姿态', setup.head_ready === true ? 'good' : 'caution');
    for (const side of ['left', 'right']) {
      const sn = side === 'left' ? state.leftSN : state.rightSN;
      const ready = setup[`${side}_ready`] === true && !!sn && !state.conflictingSN;
      badge(`pico-${side}-badge`, state.conflictingSN ? 'SN 冲突' : ready ? '输入有效' : '等待输入', ready ? 'good' : 'caution');
      write(`pico-${side}-sn`, controllers ? `${side === 'left' ? '左' : '右'}手柄 · 无需 Tracker SN` : sn || '等待 SN · 在顶部选择对应腕部');
    }
    const detail = state.inputReady ? `Head 与两个独立${controllers ? '手柄' : '腕部'}输入已就绪，可查看下方方向和距离校准。`
      : state.conflictingSN ? '左右腕需要绑定不同的 Tracker SN，请检查顶部腕部选择。'
      : setup.detail || armSourceState(data.settings || {}, undefined, network.laptop).guide;
    write('pico-setup-detail', detail);
    badge('pico-badge', state.label, state.kind);
    write('pico-detail', detail);
  }
  function robotConnectionState(robot, context) {
    const available = context.supported === true && context.online === true && context.authorized === true && !context.pending;
    const label = !context.supported ? '功能待就绪' : !context.online ? '状态未知'
      : robot.connecting === true ? '正在检查连接' : robot.connected === true ? '只读连接正常'
      : robot.error || robot.stage === 'error' ? '连接检查失败' : robot.stage === 'disconnected' ? '检查连接已断开' : '尚未检查';
    return {label, kind: !context.online ? 'caution' : !context.supported ? 'neutral' : robot.connecting ? 'caution'
      : robot.connected === true ? 'good' : robot.error || robot.stage === 'error' ? 'bad' : 'neutral',
      canConnect: available && robot.connecting !== true,
      canDisconnect: available && (robot.connected === true || robot.connecting === true)};
  }
  function armInputState(input, context) {
    const controllers = (input.input_source || context.inputSource) === 'controllers';
    const selected = Array.isArray(input.selected_sides) ? [...new Set(input.selected_sides.filter(side => ['left', 'right'].includes(side)))] : [];
    const serials = input.selected_serials && typeof input.selected_serials === 'object' ? input.selected_serials : {};
    const known = ['head', 'left', 'right', 'stream', 'heading', 'binding', 'session'];
    const missing = new Set((Array.isArray(input.missing) ? input.missing : []).filter(code => known.includes(code) &&
      (!['left', 'right'].includes(code) || selected.includes(code))));
    if (input.input_live !== true || input.reader_running !== true || input.receiver_running !== true) missing.add('stream');
    if (input.head_ready !== true) missing.add('head');
    if (input.heading_ready === false) missing.add('heading');
    if (!selected.length) missing.add('binding');
    for (const side of selected) {
      if (input[`${side}_ready`] !== true) missing.add(side);
      if (!controllers && (typeof serials[side] !== 'string' || !serials[side].trim())) missing.add('binding');
    }
    if (!controllers && selected.length === 2 && serials.left === serials.right) missing.add('binding');
    const ready = context.supported === true && context.online === true && input.ready === true &&
      !['stream', 'head', 'heading', 'binding', ...selected].some(code => missing.has(code));
    const calibrationReady = context.supported === true && context.online === true && input.calibration_ready === true && input.needs_reset !== true &&
      !['stream', 'binding', 'session', ...selected].some(code => missing.has(code));
    const labels = {head: 'Head 姿态', left: controllers ? '左手柄数据' : '左腕数据', right: controllers ? '右手柄数据' : '右腕数据', stream: '实时数据流', heading: '头部水平朝向', binding: controllers ? '手柄输入配置' : '正确的 Tracker 绑定', session: '与原点一致的读取会话'};
    const missingLabels = [...missing].map(code => labels[code]);
    const selection = controllers ? selected.length === 2 ? '双手柄' : selected[0] === 'left' ? '左手柄' : selected[0] === 'right' ? '右手柄' : '所选手柄'
      : selected.length === 2 ? '双腕' : selected[0] === 'left' ? '左腕' : selected[0] === 'right' ? '右腕' : '所选腕部';
    return {ready, calibrationReady, selected, serials, missing: [...missing], selection, controllers,
      label: !context.supported ? '功能待就绪' : !context.online ? '状态未知' : ready ? `${selection}输入已就绪`
        : input.checking ? '正在等待所选输入' : '输入尚未就绪',
      missingText: ready ? `Head 与当前所选${controllers ? '手柄' : '腕部'}数据有效。`
        : missingLabels.length ? `还缺：${missingLabels.join('、')}。` : '等待后台确认当前输入。',
      kind: ready ? 'good' : context.supported ? 'caution' : 'neutral',
      canCheck: context.supported === true && context.online === true && context.authorized === true && !context.pending && !context.settingsDirty};
  }
  function renderRobotConnection(data) {
    const robot = data.services?.robot || {};
    const supported = data.capabilities?.robot_connection === true;
    const state = robotConnectionState(robot, {supported, online: connected});
    badge('robot-connection-badge', state.label, state.kind);
    write('robot-connect', robot.connected === true || ['error', 'disconnected'].includes(robot.stage) ? '重新连接 G1' : '连接 G1');
    write('robot-connection-detail', !connected ? '本机服务连接中断，机器人检查状态未知。' : !supported ? 'G1 只读连接功能尚未就绪。'
      : `${robot.simulation ? '模拟连接 · ' : ''}${robot.detail || '仅检查 SSH 连接和设备身份，不启动控制输出。'}`);
    write('robot-connection-hostname', robot.hostname || '—');
    write('robot-connection-last-seen', !connected ? '状态未知' : numeric(robot.last_seen_age_s) ? `${Math.max(0, robot.last_seen_age_s).toFixed(1)} 秒前` : '尚未收到响应');
    write('robot-control-connected', !connected ? '状态未知' : robot.control_connected === true ? '已建立（独立于此检查）' : robot.control_connected === false ? '未建立' : '等待会话状态');
    const addresses = Array.isArray(robot.addresses) ? robot.addresses.filter(value => typeof value === 'string').join(' · ') : '';
    $('robot-connection-addresses').hidden = !addresses;
    write('robot-connection-addresses', addresses ? `设备地址：${addresses}` : '');
    $('robot-connection-error').hidden = !connected || !robot.error;
    write('robot-connection-error', compactError(robot.error));
    if (supported && connected && robot.control_connected === true && robot.connected !== true) {
      badge('robot-badge', '会话已连接', 'good');
      write('robot-detail', '会话控制连接已建立；上方只读连接检查可单独启停。');
    }
  }
  function renderArmInput(data) {
    const input = data.services?.arm_input || {};
    const supported = data.capabilities?.arm_input_check === true;
    const state = armInputState(input, {supported, online: connected, inputSource: data.settings?.arm_input_source});
    const sideName = side => state.controllers ? side === 'left' ? '左手柄' : '右手柄' : side === 'left' ? '左腕' : '右腕';
    const scope = state.selected.length ? `当前检查：Head + ${state.selected.map(sideName).join(' + ')}` : '请先保存顶部手臂与输入来源选择。';
    const checkLabel = state.controllers ? '检查 Head / Controller 输入' : '检查 Head / Tracker 输入';
    write('pico-input-heading', state.controllers ? 'Head / Controller 输入检查' : 'Head / Tracker 输入检查');
    write('pico-arm-input-check', checkLabel); write('calibration-arm-input-check', checkLabel);
    let detail = !connected ? '连接中断，输入状态未知。恢复后继续按实时数据更新。' : !supported ? '只读输入检查功能尚未就绪。'
      : settingsDirty ? '顶部选择尚未保存，请先保存后检查对应输入。'
      : input.detail || `检查仅准备 PICO 接收与${state.controllers ? '手柄' : 'Tracker'}读取；输入就绪后校准按钮自动更新，无需连接 G1 或 LAUNCH。`;
    if (connected && supported && input.checked_at) {
      const date = new Date(typeof input.checked_at === 'number' ? input.checked_at * 1000 : input.checked_at);
      if (!Number.isNaN(date.getTime())) detail += ` 最近检查请求：${date.toLocaleTimeString('zh-CN', {hour12: false})}；就绪状态持续按新帧更新。`;
    }
    for (const prefix of ['pico', 'calibration']) {
      const calibration = prefix === 'calibration';
      const reset = calibration && input.needs_reset === true && connected && supported;
      const calibrationReady = calibration && state.calibrationReady;
      write(`${prefix}-arm-input-scope`, scope);
      badge(`${prefix}-arm-input-badge`, reset ? '需要重置本轮采集' : calibrationReady ? '本轮校准输入就绪' : state.label,
        reset ? 'caution' : calibrationReady ? 'good' : state.kind);
      write(`${prefix}-arm-input-missing`, !connected ? '当前数据是否有效尚未确认。' : !supported ? '等待服务状态。'
        : reset ? '读取会话或输入配置已变化，请重置本轮采集后重新记录原点。'
        : calibrationReady ? data.arm_calibration?.input_detail || '当前所选输入满足本轮采集要求。' : state.missingText);
      write(`${prefix}-arm-input-detail`, calibration && supported && connected && data.arm_calibration?.input_detail
        ? data.arm_calibration.input_detail : detail);
    }
    if (supported && connected && input.reader_running === true && data.capabilities?.pico_setup === true) {
      badge('pico-setup-badge', state.label, state.kind);
      badge('pico-badge', state.label, state.kind);
      write('pico-setup-detail', state.ready ? `Head 与当前所选${state.selection}数据有效，可查看下方方向和距离校准。` : detail);
      write('pico-detail', state.ready ? `当前所选${state.selection}输入已就绪。` : detail);
      for (const side of ['left', 'right']) if (!state.selected.includes(side)) badge(`pico-${side}-badge`, '未选择', 'neutral');
    }
  }
  function dashboardNetwork(data = {}) {
    const ipv4 = value => typeof value === 'string' && /^(?:\d{1,3}\.){3}\d{1,3}$/.test(value) && value.split('.').every(part => Number(part) <= 255);
    const setup = data.services?.pico_setup || {};
    const laptop = [data.laptop_ip, setup.host, data.network?.laptop_ip, data.services?.pico?.host].find(ipv4) || '';
    const robot = [data.services?.robot?.host, data.network?.robot_ip, data.robot_ip].find(ipv4) || '';
    const pageURL = laptop ? `http://${laptop}:8027/` : '';
    return {laptop, robot, pageURL, apkURL: pageURL ? `${pageURL}XRoboToolkit-G1-Wrists-TwoObject.apk` : '', distanceURL: pageURL ? `${pageURL}distance` : ''};
  }
  function renderNetwork(data) {
    const network = dashboardNetwork(data);
    write('robot-connection-robot-ip', network.robot || '等待读取');
    write('robot-connection-laptop-ip', network.laptop || '等待读取');
    write('pico-host-ip', network.laptop || '等待读取');
    write('pico-install-url', network.pageURL || '等待读取安装页地址');
    $('pico-copy-ip').disabled = !network.laptop;
    for (const [id, url] of [['pico-install-link', network.pageURL], ['pico-apk-link', network.apkURL], ['pico-distance-link', network.distanceURL]]) {
      if (url) { $(id).href = url; $(id).removeAttribute('aria-disabled'); }
      else { $(id).removeAttribute('href'); $(id).setAttribute('aria-disabled', 'true'); }
    }
  }
  function armReferenceState(settings, capabilities = {}, draftReference) {
    const frame = (draftReference ?? settings.arm_reference_frame) === 'headset' ? 'headset' : 'world';
    const headset = frame === 'headset';
    const supported = Object.prototype.hasOwnProperty.call(settings, 'arm_reference_frame') && Array.isArray(capabilities.arm_reference_frames) &&
      ['world', 'headset'].every(value => capabilities.arm_reference_frames?.includes(value));
    return {frame, headset, supported, label: headset ? '跟随头显（位置与朝向）' : '固定方向（START）',
      help: headset
        ? '以头显当前位置为原点，水平朝向对应机器人前方，上方始终竖直向上（与重力反向）。身体、头显与手柄／Tracker 的共同平移或水平转向会抵消；点头和侧倾不旋转参考坐标。单独平移头显或左右转头仍会改变手臂相对目标与腕部相对朝向。START 仍记录中立姿态，两种参考模式分别保存校准。'
        : '使用 START 时的固定参考方向，身体整体移动仍会进入手臂目标。两种参考模式分别保存校准；切换后 START 会重新记录中立姿态。'};
  }
  function armSourceState(settings, draftSource, laptop = '') {
    const source = (draftSource ?? settings.arm_input_source) === 'controllers' ? 'controllers' : 'trackers';
    const controllers = source === 'controllers';
    const destination = laptop ? `电脑 ${laptop}` : '本页显示的电脑 IP';
    return {source, controllers, supported: Object.prototype.hasOwnProperty.call(settings, 'arm_input_source'),
      label: controllers ? 'PICO 手柄' : '腕部 Tracker',
      guide: controllers ? `在 XRoboToolkit 启用 Head + Controller + Send，连接${destination}。左、右手柄直接对应所选手臂，无需绑定 Tracker SN。`
        : `在 XRoboToolkit 启用 Head + Object + Send，连接${destination}，并在顶部绑定所选腕部 Tracker。`,
      help: controllers ? '手柄使用独立校准，不套用腕部 Tracker 修正；没有手柄校准时使用原始 1:1 跟随。START 仍捕获中立姿态，手指仍使用 Wuji 输入。'
        : '腕部 Tracker 使用当前保存的绑定与校准；切换手柄会保留这些设置。'};
  }
  function armSettingsRequest(values) {
    const source = values.arm_input_source === 'controllers' ? 'controllers' : 'trackers';
    const body = {arms_mode: values.arms_mode, arm_input_source: source,
      hands_enabled: {left: values.hands_enabled?.left === true, right: values.hands_enabled?.right === true}};
    if (values.arm_reference_frame !== undefined) {
      if (!['world', 'headset'].includes(values.arm_reference_frame)) return {body, error: '请选择有效的参考坐标。'};
      body.arm_reference_frame = values.arm_reference_frame;
    }
    if (source === 'controllers') return {body, error: ''};
    body.left_sn = values.left_sn || ''; body.right_sn = values.right_sn || '';
    const error = body.left_sn && body.left_sn === body.right_sn ? '左右腕需要绑定不同的 Tracker SN。'
      : body.arms_mode === 'both' && (!body.left_sn || !body.right_sn) ? '双臂跟随需要分别选择左右腕 Tracker。' : '';
    return {body, error};
  }
  function trackerOptions(id, selected, available) {
    const select = $(id);
    const serials = [...new Set([...available, selected].filter(sn => typeof sn === 'string' && sn))];
    const signature = JSON.stringify([serials, selected, available]);
    if (select.dataset.options !== signature && document.activeElement !== select) {
      const blank = document.createElement('option');
      blank.value = '';
      blank.textContent = '尚未选择';
      select.replaceChildren(blank, ...serials.map(serial => {
        const option = document.createElement('option');
        option.value = serial;
        option.textContent = serial + (available.includes(serial) ? '' : '（未检测到）');
        return option;
      }));
      select.dataset.options = signature;
      select.value = selected || '';
    }
  }
  function renderSettings(data) {
    const settings = data.settings || {};
    const tracker = data.services?.tracker || {};
    const config = settings.tracker_config || tracker;
    const draft = settingsDirty || pending.has('/api/settings');
    const serials = Array.isArray(tracker.serials) ? tracker.serials.filter(sn => typeof sn === 'string' && !sn.startsWith('pico-controller-')) : [];
    if (!draft) $('arms-mode').value = settings.arms_mode || 'right';
    const source = armSourceState(settings, draft ? $('arm-input-source').value : undefined, dashboardNetwork(data).laptop);
    if (!draft) $('arm-input-source').value = source.source;
    $('arm-input-source').querySelector('option[value="controllers"]').disabled = !source.supported;
    const reference = armReferenceState(settings, data.capabilities || {}, draft ? $('arm-reference-frame').value : undefined);
    if (!draft) $('arm-reference-frame').value = reference.frame;
    $('arm-reference-frame').querySelector('option[value="headset"]').disabled = !reference.supported;
    write('arm-reference-help', reference.supported ? reference.help : '当前使用 START 时的固定参考方向；跟随头显选项等待后台更新。');
    for (const side of ['left', 'right']) {
      const selected = draft ? $(`${side}-tracker`).value : config[`${side}_sn`] || '';
      trackerOptions(`${side}-tracker`, selected, serials);
      $(`${side}-tracker-field`).hidden = source.controllers;
      if (!draft) $(`${side}-hand-enabled`).checked = settings.hands_enabled?.[side] === true;
    }
    write('arm-source-help', source.supported ? source.help : '当前使用腕部 Tracker；PICO 手柄选项等待后台更新。');
    write('pico-source-guide', source.guide);
    const arm = armNames[$('arms-mode').value] || '手臂';
    write('launch-label', `准备${arm}跟随`);
    write('start-label', `开始${arm}跟随`);
    const detail = settingsDirty ? '选择尚未保存。保存后再开始遥操作。' : tracker.running
      ? source.controllers ? `手柄输入检查运行中 · 左${tracker.controllers?.left?.ready === true ? '有效' : '等待输入'} / 右${tracker.controllers?.right?.ready === true ? '有效' : '等待输入'} · Head ${tracker.head_ready === true ? '已就绪' : '未就绪'}`
        : `只读输入检查运行中 · ${serials.length} 个 Tracker · 头部方向${tracker.head_ready === true ? '已就绪' : '未就绪'}`
      : source.controllers ? '保存手柄选择后检查 Head / Controller 输入；无需腕部 Tracker SN。'
        : '先检查输入，分别绑定左右腕；保存后的选择用于下次 LAUNCH。';
    write('settings-detail', detail);
    $('settings-detail').classList.toggle('caution-text', settingsDirty);
  }
  function renderHands(data) {
    const hands = data.services?.hands || {};
    for (const side of ['left', 'right']) {
      possiblyModelActive[side] = modelActive(hands[side]?.model_calibration) || pending.has(`/api/gloves/model/start:${side}`);
    }
    const modelBusy = calibrationBusy(hands);
    const handActive = ['left', 'right'].some(side => hands[side]?.running === true);
    const modelSide = ['left', 'right'].find(side => modelActive(hands[side]?.model_calibration));
    const calibrationSupported = data.capabilities?.glove_calibration === true;
    $('calibration-busy-note').hidden = calibrationSupported && !modelBusy && !handActive;
    write('calibration-busy-note', !calibrationSupported ? '手套校准服务尚未就绪，功能就绪后会自动启用。' : modelBusy
      ? `${handNames[modelSide] || '手套'}个人模型校准中。两个手套读取暂时暂停；SDK 会自动识别手型并进入下一步。`
      : '机械手正在跟随。停止机械手跟随后，即可开始个人模型或开合校准。');
    for (const side of ['left', 'right']) {
      const hand = hands[side] || {};
      const model = hand.model_calibration || {};
      possiblyHandActive[side] = hand.running === true || hand.auto_start_pending === true || pending.has(`/api/hands/start:${side}`);
      const presentation = handPresentation(hand);
      badge(`${side}-hand-badge`, presentation.label, presentation.kind);
      write(`${side}-hand-input`, hand.input_live === true ? '实时有效' : hand.input_live === false ? '暂无有效输入' : '未检查');
      write(`${side}-hand-model`, hand.model_ready === true ? '已就绪' : hand.model_ready === false ? '未就绪' : '未检查');
      write(`${side}-hand-calibration`, hand.calibration_ready === true ? '已就绪' : hand.calibration_ready === false ? '尚未完成' : '未检查');
      write(`${side}-hand-detail`, presentation.detail);
      $(`${side}-hand-detail`).classList.toggle('caution-text', presentation.kind === 'caution');
      const sn = hand.sn || hand.serial || hand.glove_sn || model.sn;
      write(`${side}-glove-identity`, sn || `${handNames[side]}手套 · 按设备侧别绑定`);
      renderModelCalibration(side, hand);
      renderCapture(side, hand);
      const preview = hand.preview || {};
      renderFingerPreview(side, presentation.calibrationPending && !presentation.error
        ? {...preview, error: presentation.detail} : preview);
    }
  }
  function setProgress(id, fraction) {
    if (numeric(fraction)) $(id).value = Math.max(0, Math.min(1, fraction)) * 100;
    else $(id).removeAttribute('value');
  }
  function renderModelCalibration(side, hand) {
    const model = hand.model_calibration || {};
    const active = modelActive(model);
    const terminal = ['completed', 'done', 'success', 'cancelled', 'error', 'failed'].includes(model.state);
    const stage = String(terminal ? model.state : model.stage || model.phase || model.state || 'idle');
    const succeeded = model.success === true || model.success == null && model.completed === true && !model.error && !['cancelled', 'error', 'failed'].includes(stage);
    const visible = active || model.completed === true || !!model.error || stage !== 'idle';
    $(`${side}-model-session`).hidden = !visible;
    write(`${side}-model-start`, `${hand.model_ready === true ? '重新校准' : '创建'}${handNames[side]}模型`);
    badge(`${side}-model-badge`, active ? '正在校准' : hand.model_ready === true ? '已有模型' : '需要创建', active ? 'caution' : hand.model_ready === true ? 'good' : 'neutral');
    write(`${side}-model-phase`, modelStageLabels[stage] || model.phase || '个人手型校准');
    const fraction = model.progress_fraction;
    setProgress(`${side}-model-progress`, fraction);
    write(`${side}-model-progress-text`, numeric(fraction) ? `${Math.round(Math.max(0, Math.min(1, fraction)) * 100)}%` : active ? '等待 SDK 进度' : '');
    const instructions = Array.isArray(model.instruction) ? model.instruction.join('\n') : model.instruction;
    const instructionText = instructions || (active
      ? '正在等待手套校准提示。按提示保持手型，SDK 识别成功后会自动进入下一步。'
      : succeeded ? '个人模型已保存，正在准备所选手套的只读输入。继续进行下方开合采集。' : '校准尚未完成，可重新开始。');
    const diagnostics = Array.isArray(model.diagnostics) ? model.diagnostics.filter(value => typeof value === 'string' && value.trim()) : [];
    write(`${side}-model-instruction`, [instructionText, ...diagnostics].join('\n'));
    $(`${side}-model-error`).hidden = !model.error;
    write(`${side}-model-error`, model.error || '');
    const lines = Array.isArray(model.log_tail) ? model.log_tail.map(asText) : asText(model.log_tail).split('\n').filter(Boolean);
    const content = lines.length ? lines.join('\n').slice(-30000) : '等待校准程序输出。';
    const log = $(`${side}-model-log`);
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 35;
    if (log.textContent !== content) { log.textContent = content; if (atBottom) log.scrollTop = log.scrollHeight; }
    const result = model.backup_path ? `已有模型已备份：${model.backup_path}` : succeeded ? '个人模型已保存。' : '';
    $(`${side}-model-result`).hidden = !result;
    write(`${side}-model-result`, result);
  }
  function renderCapture(side, hand) {
    const capture = hand.capture;
    const poses = hand.captured_poses || {};
    for (const [name, key] of [['open', 'open'], ['closed', 'fist']]) {
      const samples = poses[key];
      write(`${side}-${name}-state`, numeric(samples) && samples > 0 ? `已采集 ${count(samples)} 帧` : '本轮未采集');
      $(`${side}-${name}-state`).classList.toggle('ready-text', numeric(samples) && samples > 0);
    }
    $(`${side}-capture-session`).hidden = !capture;
    if (capture) {
      setProgress(`${side}-capture-progress`, capture.progress);
      const pose = capture.pose === 'open' ? '张开' : '握拳';
      const remaining = numeric(capture.remaining_s) ? ` · 剩余 ${Math.max(0, capture.remaining_s).toFixed(1)} 秒` : '';
      write(`${side}-capture-status`, `请保持${pose} · ${count(capture.samples)} 帧${remaining}`);
    }
    const result = hand.capture_result || {};
    const error = hand.capture_error || (result.state === 'error' ? result.message : '');
    const resultText = error || result.message || (hand.calibration_ready === true ? '已有保存的开合校准，可查看下方映射预览。' : '');
    $(`${side}-capture-result`).hidden = !resultText || !!capture;
    write(`${side}-capture-result`, resultText);
    $(`${side}-capture-result`).classList.toggle('caution-text', !!error);
  }
  function renderFingerPreview(side, preview = {}) {
    const channels = Array.isArray(preview.channels) ? preview.channels : [];
    const available = connected && preview.ready === true && preview.input_live === true &&
      channels.length > 0 && channels.length === preview.closure?.length && channels.length === preview.goals?.length &&
      preview.closure.every(numeric) && preview.goals.every(numeric);
    const container = $(`${side}-preview-channels`);
    container.hidden = !available;
    write(`${side}-preview-detail`, !connected ? '连接中断，映射状态未知。' : available
      ? '原项目映射目标（0–1000），仅计算、未发送；数值不是机械手实测位置。'
      : previewMessages[preview.error] || preview.error || '模型、开合校准与有效输入就绪后显示映射，不向机械手发送目标。');
    if (!available) return;
    const signature = JSON.stringify(channels);
    if (container.dataset.channels !== signature) {
      container.replaceChildren(...channels.map(channel => {
        const row = document.createElement('div');
        row.className = 'preview-channel';
        const label = document.createElement('span');
        label.textContent = previewChannelNames[channel] || channel;
        const meter = document.createElement('meter');
        meter.min = 0; meter.max = 1;
        meter.setAttribute('aria-label', `${handNames[side]}${label.textContent}弯曲比例`);
        const value = document.createElement('output');
        row.append(label, meter, value);
        return row;
      }));
      container.dataset.channels = signature;
    }
    [...container.children].forEach((row, index) => {
      row.querySelector('meter').value = Math.max(0, Math.min(1, preview.closure[index]));
      row.querySelector('output').textContent = String(Math.round(preview.goals[index]));
    });
  }
  function teleopControlPresentation(data) {
    const caps = data.capabilities || {};
    const session = data.session || {};
    const active = session.running === true || session.prepared === true || session.following === true ||
      ['starting','launching','preparing','prepared','ready','waiting','waiting_for_start','following','running','holding','stopping'].includes(session.stage);
    const sessionMode = active && typeof session.control_mode === 'string' && session.control_mode ? session.control_mode : null;
    const sessionNote = active && typeof session.control_note === 'string' ? session.control_note : '';
    const motion = caps.control_mode === 'normal_walking' && (!sessionMode || sessionMode === 'normal_walking');
    const legacyNote = 'LAUNCH 会驱动手臂至初始目标；START 捕获当前姿态；STOP 回位退出。';
    return motion ? {
      motion: true, modeLabel: '常规行走 · 手臂接口',
      launch: 'LAUNCH · 连接待命，权重 0', start: 'START · 捕获姿态并接入跟随', stop: 'STOP · 释放手臂控制',
      prepareStep: '准备连接',
      note: sessionNote || caps.control_note || 'LAUNCH 仅准备连接，手臂接管权重保持 0；START 捕获当前姿态并逐步接入手臂跟随；STOP 停止手指跟随，将手臂控制交回原控制器。',
      locomotion: '保留 G1 3 自由度腰 Regular Mode；腿部由原厂控制器控制，腰部保持每次 START 捕获的姿态，不提供独立的动态腰部平衡。腰部保持修正的实机效果待验证。行走使用原控制器／遥控器，PICO 跟随手臂，Wuji 跟随手指。官方按键表：1 自由度腰模式为 R1+X，3 自由度腰模式为 R1+Y，均长按 2 秒；以本机固件说明为准。'
    } : {
      motion: false, modeLabel: '原有手臂控制接口',
      launch: 'LAUNCH · 接管并初始化双臂', start: 'START · 持续至停止', stop: 'STOP · 手臂回位并结束',
      prepareStep: '接管并初始化',
      note: sessionMode && sessionMode !== caps.control_mode ? sessionNote || legacyNote : caps.control_note || legacyNote,
      locomotion: ''
    };
  }
  function renderSession(data) {
    const session = data.session;
    const stage = stageOf(session);
    const active = activeSession(session);
    possiblyActive = active || pending.has('/api/launch') || pending.has('/api/start') || pending.has('/api/preview');
    $('simulation-banner').hidden = !(session.simulation === true || data.simulation === true);
    write('session-title', stageLabels[stage] || asText(session.stage) || '等待准备');
    const description = session.error ? compactError(session.error) : session.warning || session.detail || (holdStages.has(stage)
      ? '输入暂不可用，按当前会话策略保持目标；输入恢复后继续跟随。'
      : readyStages.has(stage) ? '准备完成。保持起始姿态，点击 START 开始跟随。'
      : liveStages.has(stage) ? '会话正在跟随输入，点击 STOP 结束。'
      : endStages.has(stage) ? '查看本次结果和日志；最新本体状态以设备反馈为准。'
      : active ? '正在加载输入和机器人控制，请留意设备状态。'
      : '点击 LAUNCH 准备输入与控制，再点击 START 开始跟随。');
    write('session-detail', description);
    $('session-detail').classList.toggle('caution-text', holdStages.has(stage) || !!session.warning);
    write('session-clock', clock(session.elapsed_s));
    const hasRun = !!(session.session_id || session.started_at || session.output_dir || active);
    write('elapsed', hasRun ? clock(session.elapsed_s) : '—');
    write('accepted', hasRun ? count(session.accepted_targets) : '—');
    write('writes', hasRun ? count(session.actual_write_count) : '—');
    const preview = String(session.mode || '').includes('preview');
    const mode = preview ? '预演' : session.simulation ? '界面模拟' : hasRun ? '遥操作' : '待启动';
    badge('session-mode', mode, session.error ? 'bad' : holdStages.has(stage) || session.warning ? 'caution' : active ? 'good' : 'neutral');
    $('session-error').hidden = !session.error;
    write('session-error', compactError(session.error));
    const path = session.output_dir || session.recording_dir || '';
    $('run-path').hidden = !path;
    write('output-path', path);
    const lines = Array.isArray(session.log_tail) ? session.log_tail.map(asText) : asText(session.log_tail).split('\n').filter(Boolean);
    const log = $('log');
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 35;
    const content = lines.length ? lines.map(line => compactError(line, 1200)).join('\n').slice(-50000) : '启动会话后显示日志。';
    if (log.textContent !== content) { log.textContent = content; if (atBottom) log.scrollTop = log.scrollHeight; }
    write('log-count', `${lines.length} 条`);
    let step = active ? 'prepare' : endStages.has(stage) ? 'finish' : '';
    if (readyStages.has(stage)) step = 'ready';
    if (liveStages.has(stage) || holdStages.has(stage)) step = 'motion';
    document.querySelectorAll('[data-step]').forEach(node => {
      node.classList.toggle('current', node.dataset.step === step);
      if (node.dataset.step === step) node.setAttribute('aria-current', 'step'); else node.removeAttribute('aria-current');
    });
  }
  function renderServices(data) {
    const services = data.services || {};
    const caps = data.capabilities || {};
    const robot = services.robot || {};
    const pico = services.pico || {};
    const tracker = services.tracker || {};
    const hands = services.hands || {};
    write('header-host', robot.host || 'G1');
    write('robot-address', robot.host || '地址未提供');
    badge('robot-badge', robot.connected === true ? '已连接' : robot.connected === false ? '未连接' : '待检查', robot.connected === true ? 'good' : robot.connected === false ? 'bad' : 'neutral');
    write('robot-detail', robot.detail || '等待机器人连接信息。');
    write('body-detail', robot.body_detail || robot.feedback_detail || '网络连接状态不代表本体控制已准备完成。');
    badge('pico-badge', pico.running === true ? '服务运行中' : pico.running === false ? '未启动' : '待检查', pico.running === true ? 'good' : 'neutral');
    write('pico-detail', pico.detail || '输入服务状态不代表 Tracker 已发送有效姿态。');
    write('laptop-ip', data.laptop_ip || pico.host || pico.address || '请使用本电脑的无线网卡 IP');
    const trackerReady = tracker.ready ?? tracker.valid ?? tracker.connected;
    const holding = holdStages.has(stageOf(data.session));
    badge('tracker-badge', holding ? '等待输入' : trackerReady === true ? '输入有效' : trackerReady === false ? '输入未就绪' : '会话内检查', holding || trackerReady === false ? 'caution' : trackerReady === true ? 'good' : 'neutral');
    const armSource = armSourceState(data.settings || {}, undefined, dashboardNetwork(data).laptop);
    write('tracker-title', armSource.label);
    write('tracker-scope', armSource.controllers ? '左、右手柄独立对应手臂' : tracker.scope || '独立腕部跟踪');
    const serial = tracker.serials || tracker.sns || tracker.sn || tracker.serial || caps.tracker_sn;
    write('tracker-sn', armSource.controllers ? '左手柄 / 右手柄 · 无需 Tracker SN' : Array.isArray(serial) ? serial.join(' · ') : serial || '等待设备信息');
    write('tracker-detail', tracker.detail || armSource.guide);
    const waitingHands = ['left', 'right'].some(side => hands[side]?.auto_start_pending === true);
    badge('hands-badge', waitingHands ? '等待手部启动' : hands.command_ready === true ? '跟随可用' : hands.command_ready === false ? '输出未就绪' : '待检查', waitingHands ? 'caution' : hands.command_ready === true ? 'good' : hands.command_ready === false ? 'caution' : 'neutral');
    write('hands-detail', [hands.detail || '继续使用原 Wuji 读取与 BrainCo 手指映射，等待链路状态。',
      hands.coordination_error ? `联动提示：${compactError(hands.coordination_error)}` : ''].filter(Boolean).join(' '));
    $('hands-detail').classList.toggle('caution-text', waitingHands || !!hands.coordination_error);
    const handRows = ['left', 'right'].filter(side => hands[side] && typeof hands[side] === 'object').map(side => {
      const hand = hands[side];
      const presentation = handPresentation(hand);
      const row = document.createElement('p');
      row.className = 'detail';
      const ready = hand.command_ready ?? hand.ready;
      const name = side === 'left' ? '左手' : '右手';
      row.textContent = `${name}：${presentation.detail || hand.state || (ready === true ? '跟随可用' : ready === false ? '输出未就绪' : '等待状态')}`;
      if (presentation.kind === 'caution') row.classList.add('caution-text');
      return row;
    });
    $('hands-sides').hidden = !handRows.length;
    $('hands-sides').replaceChildren(...handRows);
    write('scope-label', armSource.controllers ? 'PICO 手柄 + Wuji → G1 / BrainCo' : caps.scope || 'PICO Tracker + Wuji → G1 / BrainCo');
    const control = teleopControlPresentation(data);
    write('control-mode-label', control.modeLabel);
    write('launch-subtitle', control.launch);
    write('start-subtitle', control.start);
    write('stop-subtitle', control.stop);
    write('control-note', control.note);
    write('locomotion-note', control.locomotion);
    $('locomotion-note').hidden = !control.motion;
    $('locomotion-reference').hidden = !control.motion;
    document.querySelector('[data-step="prepare"]').lastChild.nodeValue = control.prepareStep;
    const scope = [];
    if (armNames[data.settings?.arms_mode]) scope.push([`${armNames[data.settings.arms_mode]}跟随`, true]);
    else if (caps.dual_arm === true || caps.arms === 'both') scope.push(['双臂跟随', true]);
    else if (caps.right_arm === true || caps.arms === 'right') scope.push(['右臂跟随', true]);
    else if (caps.left_arm === true || caps.arms === 'left') scope.push(['左臂跟随', true]);
    else if (caps.arms === true) scope.push(['手臂跟随', true]);
    else scope.push(['手臂：等待可用功能信息', false]);
    if (caps.hands === true) scope.push(['Wuji → BrainCo 手指跟随', hands.command_ready === true]);
    else scope.push(['手部输出当前不可用', false]);
    if (control.motion) scope.push(['行走：机器人原控制器／遥控器', true]);
    scope.push(['LAUNCH 准备 · START 跟随 · STOP 结束', true]);
    const list = $('scope-list');
    list.replaceChildren(...scope.map(([label, enabled]) => {
      const item = document.createElement('li');
      item.textContent = label;
      item.className = enabled ? '' : 'muted';
      return item;
    }));
    renderCamera();
  }
  function renderCamera() {
    const camera = latest?.services?.camera || {};
    const live = connected && camera.live === true && !imageFailed;
    $('camera-image').hidden = !live;
    $('camera-placeholder').hidden = live;
    $('camera-live-label').hidden = !live;
    const error = camera.error || (imageFailed ? '暂未收到相机画面，正在重试。' : '');
    badge('camera-badge', !connected ? '状态未知' : live ? '实时' : error ? '画面不可用' : camera.running ? '正在连接' : '未启动', live ? 'good' : error ? 'caution' : 'neutral');
    write('camera-empty-title', !connected ? '相机状态未知' : error ? '画面暂不可用' : camera.running ? '等待第一帧画面' : '画面尚未启动');
    write('camera-empty-detail', !connected ? '恢复本机服务连接后自动更新。' : error || (camera.running ? '相机服务运行中，等待机器人传回图像。' : '启动 G1 相机后，在这里查看画面。'));
    write('camera-detail', camera.detail || error || '仅查看相机画面，不控制头部或双臂。');
    if (camera.running && typeof camera.pico_video === 'boolean') $('pico-video').checked = camera.pico_video;
    if (!connected || camera.live !== true) cameraBusy = false;
  }
  function renderHistory(history) {
    const rows = Array.isArray(history) ? history.slice(0, 10) : [];
    const signature = JSON.stringify(rows);
    if (signature === lastHistory) return;
    lastHistory = signature;
    $('history-empty').hidden = rows.length > 0;
    $('history-table-wrap').hidden = !rows.length;
    $('history').replaceChildren(...rows.map(row => {
      const summary = row.summary || row;
      const time = row.started_at || row.created_at || row.timestamp;
      const date = time ? new Date(typeof time === 'number' ? time * 1000 : time) : null;
      const timeText = date && !Number.isNaN(date.getTime()) ? date.toLocaleString('zh-CN', {hour12: false}) : row.session_id || row.name || '—';
      const mode = String(row.mode || summary.mode || '').includes('preview') ? '预演' : row.simulation ? '界面模拟' : '遥操作';
      const stage = String(row.stage || summary.stage || row.status || '').toLowerCase();
      const result = row.error || summary.error ? '异常 · 查看日志' : row.completed || summary.completed ? '已结束' : stageLabels[stage] || row.status || '已记录';
      const values = [timeText, mode, result, count(summary.actual_write_count ?? summary.commands_published), row.output_dir || row.recording_dir || row.path || '—'];
      const tr = document.createElement('tr');
      for (const [index, value] of values.entries()) {
        const td = document.createElement('td');
        td.textContent = asText(value);
        if (index === 4) { td.className = 'history-path mono'; td.title = asText(value); }
        tr.append(td);
      }
      return tr;
    }));
  }
  function render(data) {
    latest = data;
    connected = true;
    statusReceivedAt = Date.now();
    if (typeof data.control_token === 'string' && data.control_token) controlToken = data.control_token;
    $('connection-dot').className = 'status-dot online';
    write('connection-status', '本机服务已连接');
    $('offline-banner').hidden = true;
    write('last-update', `更新于 ${new Date().toLocaleTimeString('zh-CN', {hour12: false})}`);
    renderNetwork(data);
    renderSettings(data);
    renderSession(data);
    renderServices(data);
    renderRobotConnection(data);
    renderPicoSetup(data);
    renderArmInput(data);
    renderHands(data);
    renderArmCalibration(data);
    renderHistory(data.history);
    renderControls();
  }
  function disconnected() {
    connected = false;
    $('connection-dot').className = 'status-dot offline';
    write('connection-status', '本机服务连接中断');
    $('offline-banner').hidden = false;
    write('session-title', '会话状态未知');
    write('session-detail', '正在重新连接本机服务。页面无法确认机器人当前状态。');
    write('last-update', statusReceivedAt ? `最后更新 ${new Date(statusReceivedAt).toLocaleTimeString('zh-CN', {hour12: false})}` : '尚未连接到本机服务');
    for (const side of ['left', 'right']) {
      badge(`${side}-hand-badge`, '状态未知', 'caution');
      write(`${side}-hand-input`, '状态未知');
      badge(`${side}-model-badge`, '状态未知', 'caution');
      write(`${side}-model-phase`, '校准状态未知');
      write(`${side}-model-instruction`, '本机连接中断，无法确认当前校准进度。恢复连接后自动更新。');
      write(`${side}-model-progress-text`, '');
      setProgress(`${side}-model-progress`, null);
      write(`${side}-capture-status`, '连接中断，采集状态未知。');
      renderFingerPreview(side);
    }
    badge('arm-calibration-badge', '状态未知', 'caution');
    write('arm-calibration-instruction', '本机服务连接中断，采集与保存状态未知。恢复连接后自动更新。');
    $('arm-calibration-instruction').classList.add('caution');
    renderPicoSetup(latest || {});
    renderRobotConnection(latest || {});
    renderArmInput(latest || {});
    renderCamera();
    renderControls();
  }
  async function request(path, options = {}, timeoutMs = 4500) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(path, {cache: 'no-store', credentials: 'same-origin', ...options, signal: controller.signal});
      let value;
      try { value = await response.json(); } catch { throw new Error('服务未返回有效状态。'); }
      if (!response.ok || value.ok === false) throw new Error(value.message || asText(value.error) || `请求失败 (${response.status})`);
      return value;
    } finally { clearTimeout(timer); }
  }
  async function poll() {
    if (pollBusy) return;
    pollBusy = true;
    try {
      const data = await request('/api/status');
      if (!data.session || typeof data.session !== 'object') throw new Error('状态格式不完整。');
      render(data);
    } catch { disconnected(); }
    finally { pollBusy = false; }
  }
  async function action(path, body = {}, operationKey = path) {
    if (pending.has(operationKey)) return;
    if (!controlToken) { flash('尚未获取本机控制凭据，请等待服务连接。', true); return; }
    pending.add(operationKey);
    if (['/api/launch', '/api/start', '/api/preview'].includes(path)) possiblyActive = true;
    if (path === '/api/hands/start' && body.side in possiblyHandActive) possiblyHandActive[body.side] = true;
    if (path === '/api/gloves/model/start' && body.side in possiblyModelActive) possiblyModelActive[body.side] = true;
    renderControls();
    flash(path === '/api/stop' ? '正在请求停止，请等待会话状态更新。' : '正在发送请求…');
    handFlash(body.side, '正在发送请求…');
    if (path.startsWith('/api/arms/calibration/')) armCalibrationFlash('正在发送请求…');
    if (path.startsWith('/api/pico/')) picoSetupFlash(path.endsWith('/stop') ? '正在停止 PICO 接收，下载页保持运行…' : '正在准备下载页、接收、广播观测和 Tracker 读取…');
    if (path.startsWith('/api/robot/')) connectionFlash(path.endsWith('/disconnect') ? '正在断开只读检查连接，控制会话不受影响…' : '正在检查 G1 连接与设备身份…');
    if (path === '/api/arms/input/check') {
      picoSetupFlash(`正在准备接收与读取，检查当前所选 Head / ${latest?.settings?.arm_input_source === 'controllers' ? 'Controller' : 'Tracker'} 数据…`);
      armCalibrationFlash('正在检查当前所选输入；有效数据到达后，采集按钮自动更新。');
    }
    try {
      const result = await request(path, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Control-Token': controlToken}, body: JSON.stringify(body)}, 15000);
      if (path === '/api/settings') settingsDirty = false;
      flash(result.message || '请求已接收，正在更新会话状态。');
      handFlash(body.side, result.message || '请求已接收，正在更新状态。');
      if (path.startsWith('/api/arms/calibration/')) armCalibrationFlash(result.message || '请求已接收，正在更新采集状态。');
      if (path.startsWith('/api/pico/')) picoSetupFlash(result.message || '请求已接收，请查看各项服务和输入状态。');
      if (path.startsWith('/api/robot/')) connectionFlash(result.message || '请求已接收，请查看只读连接状态。');
      if (path === '/api/arms/input/check') {
        picoSetupFlash(result.message || '检查已启动，请查看实时输入状态。');
        armCalibrationFlash(result.message || '检查已启动，就绪状态持续按新帧更新。');
      }
      if (result.status?.session) render(result.status);
    } catch (error) {
      const message = error.name === 'AbortError' ? '请求结果尚未确认，请查看最新会话状态。' : compactError(error.message);
      flash(message, true);
      handFlash(body.side, message, true);
      if (path.startsWith('/api/arms/calibration/')) armCalibrationFlash(message, true);
      if (path.startsWith('/api/pico/')) picoSetupFlash(message, true);
      if (path.startsWith('/api/robot/')) connectionFlash(message, true);
      if (path === '/api/arms/input/check') { picoSetupFlash(message, true); armCalibrationFlash(message, true); }
    } finally {
      pending.delete(operationKey);
      renderControls();
      poll();
    }
  }
  const routes = {
    launch: '/api/launch', start: '/api/start', stop: '/api/stop', preview: '/api/preview',
    'pico-start': '/api/pico/start', 'pico-stop': '/api/pico/stop',
    'pico-setup-start': '/api/pico/start', 'pico-setup-stop': '/api/pico/stop',
    'robot-connect': '/api/robot/connect', 'robot-disconnect': '/api/robot/disconnect',
    'pico-arm-input-check': '/api/arms/input/check', 'calibration-arm-input-check': '/api/arms/input/check',
    'camera-stop': '/api/camera/stop', 'inputs-start': '/api/inputs/start', 'inputs-stop': '/api/inputs/stop'
  };
  Object.entries(routes).forEach(([id, path]) => $(id).addEventListener('click', () => action(path)));
  $('camera-start').addEventListener('click', () => action('/api/camera/start', {pico_video: $('pico-video').checked}));
  $('gloves-refresh').addEventListener('click', () => action('/api/gloves/refresh'));
  $('pico-copy-ip').addEventListener('click', async () => {
    const host = dashboardNetwork(latest || {}).laptop;
    if (!host) { picoSetupFlash('尚未读取电脑地址，请等待服务状态。', true); return; }
    try { await navigator.clipboard.writeText(host); picoSetupFlash(`电脑 IP ${host} 已复制。`); }
    catch { picoSetupFlash(`请手动复制电脑 IP：${host}`, true); }
  });
  $('arm-calibration-distance').addEventListener('input', renderArmCalibrationControls);
  for (const stage of armCaptureStages) {
    $(`arm-capture-${stage}`).addEventListener('click', () => {
      const state = armCalibrationState(latest?.arm_calibration || {}, armCalibrationContext());
      if (!state.validDistance) { armCalibrationFlash('请输入你确认的实际距离（3–100 cm），无需按 PICO 读数填写。', true); return; }
      if (!state.canCapture || state.nextStage !== stage) return;
      action('/api/arms/calibration/capture', {stage, distance_cm: state.distance});
    });
  }
  for (const command of ['save', 'reset']) {
    $(`arm-calibration-${command}`).addEventListener('click', () => {
      const state = armCalibrationState(latest?.arm_calibration || {}, armCalibrationContext());
      if (!(command === 'save' ? state.canSave : state.canReset)) return;
      action(`/api/arms/calibration/${command}`);
    });
  }
  for (const id of ['arms-mode', 'arm-input-source', 'arm-reference-frame', 'left-tracker', 'right-tracker', 'left-hand-enabled', 'right-hand-enabled']) {
    $(id).addEventListener('change', () => {
      settingsDirty = true;
      renderSettings(latest || {});
      renderControls();
    });
  }
  $('save-settings').addEventListener('click', () => {
    const request = armSettingsRequest({arms_mode: $('arms-mode').value, arm_input_source: $('arm-input-source').value,
      arm_reference_frame: armReferenceState(latest?.settings || {}, latest?.capabilities || {}).supported ? $('arm-reference-frame').value : undefined,
      left_sn: $('left-tracker').value, right_sn: $('right-tracker').value,
      hands_enabled: {left: $('left-hand-enabled').checked, right: $('right-hand-enabled').checked}});
    if (request.error) { flash(request.error, true); return; }
    action('/api/settings', request.body);
  });
  for (const side of ['left', 'right']) {
    for (const command of ['start', 'continue', 'cancel']) {
      $(`${side}-model-${command}`).addEventListener('click', () => {
        const body = {side};
        const promptId = latest?.services?.hands?.[side]?.model_calibration?.prompt_id;
        if (command === 'continue' && promptId != null) body.prompt_id = promptId;
        action(`/api/gloves/model/${command}`, body, `/api/gloves/model/${command}:${side}`);
      });
    }
    for (const command of ['start', 'stop']) {
      $(`${side}-glove-input-${command}`).addEventListener('click', () => action(`/api/gloves/input/${command}`, {side}, `/api/gloves/input/${command}:${side}`));
    }
    $(`${side}-capture-cancel`).addEventListener('click', () => action('/api/hands/calibrate/cancel', {side}, `/api/hands/calibrate/cancel:${side}`));
    for (const command of ['start', 'stop']) {
      $(`${side}-hand-${command}`).addEventListener('click', () => action(`/api/hands/${command}`, {side}, `/api/hands/${command}:${side}`));
    }
    for (const pose of ['open', 'closed']) {
      $(`${side}-hand-${pose}`).addEventListener('click', () => action('/api/hands/calibrate', {side, pose}, `/api/hands/calibrate:${side}`));
    }
  }
  $('copy-path').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText($('output-path').textContent);
      flash('记录路径已复制。');
    } catch { flash('无法自动复制，请选中记录路径手动复制。', true); }
  });
  $('camera-image').addEventListener('load', () => { cameraBusy = false; imageFailed = false; renderCamera(); });
  $('camera-image').addEventListener('error', () => { cameraBusy = false; imageFailed = true; renderCamera(); });
  setInterval(() => {
    if (!connected || latest?.services?.camera?.live !== true || document.hidden) return;
    if (cameraBusy && Date.now() - cameraRequestedAt < 4000) return;
    cameraBusy = true;
    cameraRequestedAt = Date.now();
    $('camera-image').src = `/api/camera.jpg?t=${cameraRequestedAt}`;
  }, 350);
  setInterval(() => {
    const session = latest?.session;
    if (!connected || !session || session.running !== true || !numeric(session.elapsed_s)) return;
    const elapsed = session.elapsed_s + (Date.now() - statusReceivedAt) / 1000;
    write('session-clock', clock(elapsed));
    write('elapsed', clock(elapsed));
  }, 500);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
  poll();
  setInterval(poll, 1000);
})();
