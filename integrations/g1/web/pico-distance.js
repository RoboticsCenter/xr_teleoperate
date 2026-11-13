(() => {
  'use strict';
  function distanceState(data = {}, context = {}) {
    const number = value => typeof value === 'number' && Number.isFinite(value);
    const fresh = context.online === true && number(context.ageMs) && context.ageMs >= 0 && context.ageMs <= 1000;
    const selected = Array.isArray(data.selected_sides) ? ['left', 'right'].filter(side => data.selected_sides.includes(side)) : [];
    const originValid = data.has_origin === true && data.needs_zero !== true && data.zero_pending !== true;
    const sides = {};
    for (const side of ['left', 'right']) {
      const raw = data.sides?.[side] || {};
      const included = selected.includes(side);
      const numeric = ['displacement_cm', 'forward_cm', 'left_cm', 'up_cm'].every(key => number(raw[key])) && raw.displacement_cm >= 0;
      const visible = fresh && included && originValid && raw.live === true && numeric;
      const label = !included ? '未选择' : !fresh ? '等待新数据' : !originValid ? '请设置起点'
        : raw.live !== true ? '等待有效输入' : !numeric ? '暂无参考值' : '输入实时';
      sides[side] = {selected: included, visible, label, kind: !included ? 'neutral' : visible ? 'good' : 'caution',
        sn: typeof raw.sn === 'string' ? raw.sn : '',
        displacement: visible ? raw.displacement_cm : null, forward: visible ? raw.forward_cm : null,
        left: visible ? raw.left_cm : null, up: visible ? raw.up_cm : null};
    }
    const allLive = selected.length > 0 && selected.every(side => sides[side].visible);
    const anyLive = selected.some(side => sides[side].visible);
    const ready = fresh && originValid && allLive && data.ready === true;
    const label = !fresh ? context.received ? '连接中断 / 数据过期' : '连接中'
      : !selected.length ? '请先选择手腕' : data.zero_pending === true ? '已排队 · 返回跟踪应用' : data.needs_zero === true ? '需要重新设置起点'
      : !originValid ? '等待设置起点' : ready ? '原始输入实时' : anyLive ? '部分输入缺失' : '等待有效输入';
    const detail = !fresh ? '实时数据尚未更新，距离已隐藏。请保持头显与电脑连接，并确认 XRoboToolkit 持续发送数据。'
      : !selected.length ? '请先在电脑控制页选择左臂、右臂或双臂，并保存 Tracker 绑定。'
      : data.zero_pending === true ? '请返回 XRoboToolkit，启用 Head / Object / Send，并保持测量起始姿势；收到有效输入后自动归零。'
      : data.detail || (data.needs_zero ? '输入会话、腕部选择或电脑校准已变化，请回到同一原点后重新设置测量起点。'
      : !originValid ? '保持电脑端采集原点时的姿势，点击「设置测量起点」。'
      : ready ? '正在显示未校准的 PICO 原始输入，仅供参考。校准依据是你填写的实际距离和指定方向，无需匹配这里的数值。' : '等待所选腕部的有效输入。');
    return {fresh, sides, ready, label, detail, kind: ready ? 'good' : 'caution',
      canZero: fresh && selected.length > 0 && data.zero_ready === true && data.zero_pending !== true && !!context.token && !context.pending,
      canArmZero: fresh && selected.length > 0 && data.zero_pending !== true && !!context.token && !context.pending,
      canCancelZero: fresh && data.zero_pending === true && !!context.token && !context.pending,
      originValid, actualDistance: number(data.target_cm) && data.target_cm > 0 ? data.target_cm : null,
      stage: data.current_stage === 'complete' && data.calibration_error ? '查看电脑提示'
        : ({origin: '原点', forward: '向前', left: '向左', up: '向上', complete: '采集完成'})[data.current_stage] || '—'};
  }
  function formatDistance(value, signed = false) {
    if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
    const rounded = Math.round(value * 10) / 10;
    return `${signed && rounded >= 0 ? '+' : ''}${rounded.toFixed(1)}`;
  }
  function distanceHost(data = {}, pageHost = '') {
    return [data.laptop_ip, data.host, pageHost].find(value => typeof value === 'string' &&
      /^(?:\d{1,3}\.){3}\d{1,3}$/.test(value) && value.split('.').every(part => Number(part) <= 255) && !value.startsWith('127.')) || '';
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = {distanceState, formatDistance, distanceHost};
  if (typeof document === 'undefined') return;

  const $ = id => document.getElementById(id);
  let latest = {}, token = '', online = false, receivedAt = null, zeroPending = false;
  let pollTimer, currentPoll, generation = 0;
  const setText = (id, value) => { $(id).textContent = value; };
  const text = value => typeof value === 'string' ? value.slice(0, 500) : '';
  function render() {
    const laptop = distanceHost(latest, typeof location !== 'undefined' ? location.hostname : '');
    setText('distance-laptop-ip', laptop || '等待读取');
    setText('distance-footer-ip', laptop || '等待读取');
    const state = distanceState(latest, {online, token, pending: zeroPending,
      received: receivedAt !== null, ageMs: receivedAt === null ? Infinity : performance.now() - receivedAt});
    setText('status-badge', zeroPending ? '正在提交请求' : state.label); $('status-badge').className = `badge ${state.kind}`;
    setText('status-detail', zeroPending ? '正在更新独立测量起点，等待服务确认。' : text(state.detail));
    setText('target-cm', formatDistance(state.actualDistance));
    setText('current-stage', state.stage);
    $('calibration-error').hidden = !state.fresh || !latest.calibration_error;
    setText('calibration-error', latest.calibration_error ? `电脑校准提示：${text(latest.calibration_error)}` : '');
    $('zero').disabled = !state.canZero;
    $('arm-zero').disabled = !state.canArmZero;
    $('cancel-zero').disabled = !state.canCancelZero;
    $('cancel-zero').hidden = latest.zero_pending !== true;
    setText('origin-label', !state.fresh ? '等待服务确认测量起点'
      : latest.zero_pending ? '等待有效输入后自动设置测量起点'
      : latest.needs_zero ? '原测量起点已失效，请重新设置'
      : state.originValid ? `${text(latest.origin_label) || '本页测量起点'} · 独立于电脑校准原点` : '尚未设置独立的测量起点');
    for (const side of ['left', 'right']) {
      const value = state.sides[side];
      $(`${side}-card`).className = `wrist-card ${value.visible ? 'live' : value.selected ? 'waiting' : 'unselected'}`;
      setText(`${side}-status`, value.label); $(`${side}-status`).className = `badge ${value.kind}`;
      setText(`${side}-sn`, `Tracker：${value.sn || '—'}`);
      for (const axis of ['displacement', 'forward', 'left', 'up']) setText(`${side}-${axis}`, formatDistance(value[axis], axis !== 'displacement'));
    }
    setText('remote-vision-detail', !state.fresh ? '正在读取应用内距离画面状态。'
      : text(latest.remote_vision?.error) || text(latest.remote_vision?.detail) || (latest.remote_vision?.enabled ? '距离画面服务已启用；请在 XRoboToolkit 中连接 RemoteVision。' : '距离画面服务状态尚未确认。'));
  }
  function message(value, error = false) {
    setText('action-message', text(value));
    $('action-message').hidden = !value;
    $('action-message').className = error ? 'error' : '';
  }
  function receive(data) {
    latest = data;
    token = typeof data.token === 'string' ? data.token : '';
    online = true;
    receivedAt = performance.now();
    render();
  }
  async function poll() {
    if (zeroPending) return;
    const ownGeneration = generation;
    const controller = new AbortController();
    currentPoll = controller;
    const timeout = setTimeout(() => controller.abort(), 1200);
    try {
      const response = await fetch('/api/distance', {cache: 'no-store', signal: controller.signal});
      if (!response.ok) throw new Error(`距离服务返回 ${response.status}`);
      const data = await response.json();
      if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('距离服务响应无效');
      if (ownGeneration === generation) receive(data);
    } catch (_) {
      if (ownGeneration === generation) { online = false; render(); }
    } finally {
      clearTimeout(timeout);
      if (currentPoll === controller) currentPoll = null;
      if (ownGeneration === generation && !zeroPending) pollTimer = setTimeout(poll, 200);
    }
  }
  async function originAction(action) {
    const state = distanceState(latest, {online, token, pending: zeroPending,
      received: receivedAt !== null, ageMs: receivedAt === null ? Infinity : performance.now() - receivedAt});
    if (!({zero: state.canZero, 'arm-zero': state.canArmZero, 'cancel-zero': state.canCancelZero})[action]) return;
    zeroPending = true;
    online = false;
    generation += 1;
    clearTimeout(pollTimer);
    currentPoll?.abort();
    message(''); render();
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch(`/api/distance/${action}`, {method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-Control-Token': token},
        body: '{}', signal: controller.signal});
      const result = await response.json();
      if (!response.ok || result.ok !== true) throw new Error(text(result.message) || text(result.error) || '设置起点失败，请等待新数据后重试。');
      // Confirm the actual origin with a fresh GET, rather than showing pre-request distances.
      online = false;
      message(text(result.message) || ({zero: '测量起点已设置，正在确认最新距离。', 'arm-zero': '归零已排队，请保持起始姿势；下一帧有效输入即归零。', 'cancel-zero': '已取消等待归零。'})[action]);
    } catch (error) {
      online = false;
      message(error.name === 'AbortError' ? '设置起点请求超时，正在重新确认状态。' : error.message, true);
    } finally {
      clearTimeout(timeout); zeroPending = false; render(); poll();
    }
  }
  for (const action of ['zero', 'arm-zero', 'cancel-zero']) $(action).addEventListener('click', () => originAction(action));
  if (document.documentElement.requestFullscreen) {
    $('fullscreen').hidden = false;
    $('fullscreen').addEventListener('click', async () => {
      try {
        if (document.fullscreenElement) await document.exitFullscreen();
        else await document.documentElement.requestFullscreen();
      } catch (_) { message('浏览器暂不支持全屏，请使用当前页面查看。', true); }
    });
    document.addEventListener('fullscreenchange', () => setText('fullscreen', document.fullscreenElement ? '退出全屏' : '全屏查看'));
  }
  document.addEventListener('visibilitychange', () => {
    // Fetch again on resume; never present pre-suspension measurements as live.
    if (!document.hidden && !zeroPending) {
      generation += 1; currentPoll?.abort(); clearTimeout(pollTimer); online = false; render(); poll();
    }
  });
  setInterval(render, 100);
  render(); poll();
})();
