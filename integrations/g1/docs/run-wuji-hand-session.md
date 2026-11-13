# 原 Wuji reader → 原映射 → BrainCo 手部会话

[run_wuji_hand_session.py](../scripts/run_wuji_hand_session.py) 已将现有读取和映射链路接到 `BrainCoHandSession`，不是另一个预检包装器。真实模式默认预演：读取手套与实测手状态、计算 JSON 候选，不创建命令 publisher。新增显式 `--feedback-transport wifi-ssh`，复用现有 `WifiFeedbackClient` 从 PC2 获取左右手反馈，**不要求电脑 DDS 直连，也不在电脑导入 Unitree SDK**。Wi-Fi SSH 当前仅支持反馈预演，任何 `--enable-motion` 组合均在联网前拒绝；本地 DDS 模式保留此前的显式输出入口。程序不会启动手部服务、改网络或改校准。

原离线预演已执行；本轮新增 Wi-Fi 反馈适配后 **21 项 fake/离线测试通过**。**本轮没有访问 SSH、扫描手套、启动 SDK 读取、采样实物、启动命令服务或发送运动命令**。反馈适配已接入此前单独实测通过的 SSH 反馈桥，但这条手部组合入口尚未实机运行；本次 binding、左侧工件及原 reader 无合格输出的问题仍在，不将离线结果称为整套遥操作已经运行。

## 当前可以直接执行的预演

```bash
uv run --offline --no-project --python 3.12 --with numpy \
  python -B scripts/run_wuji_hand_session.py --demo
```

这个命令已运行成功：校验并加载原 bridge 的准确字节，调用其真实 `joint_readings()`，再调用原 retarget 和现有 `HandDDSPreview`，最后交给默认禁用输出的 `BrainCoHandSession`。两帧 0.9 置信度合成数据生成候选；一帧 0.49 数据被**原过滤器**拒绝。全部输出标记 `synthetic=true`、`published=false`。示例候选按 measured=500/1000 限步到 420/1000，重复相同 measured 时不会沿上次候选继续累加。

这验证代码实际运行及拒绝路径，不是实物动作，也没有拿真实右手模型伪装左手校准。

## 真实模式如何接入

复用链路如下：

```text
锁定原 wuji_bridge.py（默认confidence=0.5，JSON角度为degrees）
  → live_hand_preview 的身份/时戳验证
  + 本机DDS回调，或 Wi-Fi SSH 的真实手状态及保守时间界
  → 原 HandDDSPreview（原映射、滤波、实测限步）
  → BrainCoHandSession（默认预演；显式启用才发布）
```

原 reader 作为同一 Python 的子进程运行，未传 `--min-confidence`，未改源文件。SDK 原生日志走 stderr，原 reader 的 JSON 走独立管道。仅真实原 reader 输出且通过身份/时戳检查的帧，由采集代码内部设置 `original_filter_passed=True`；CLI 不提供这个标记，也不允许输入网络包通过标记绕过质量规则。

真实模式继续要求现有 `operator_calibrated` binding，包括实际侧别、SN/user、原校准及个人模型 hash、捕获主机 boot clock、反馈源与速度/年龄/步长设置。当前右手可以复用已存在的 right `calibration.json` 与 `right_hand.urdf`，但尚未生成本次真实 binding；左手缺对应工件，不复制右侧来满足 schema。

在实际 binding 和 Python 环境具备后，当前已拔掉 G1 外部网线的布局使用下面的 **Wi-Fi SSH 预演**命令。变量必须指向本次真实工件，不能用示例值填过检查：

```bash
python -B scripts/run_wuji_hand_session.py \
  --feedback-transport wifi-ssh \
  --side right --sn WG1KA06260821549 --user brainco-teleop \
  --binding "${HAND_SESSION_BINDING:?set actual binding path}" \
  --seconds 5 --idle-timeout 2 --rate-hz 20
```

此模式的 Python 环境需要 NumPy、Wuji SDK 和 pexpect；PC2 继续使用现有 tv 环境内的 Unitree SDK/CycloneDDS。脚本会在本地工件检查成功后提示 SSH 密码；密码只通过匿名管道传给自己的 worker，不写 argv、环境变量或日志。`--check-binding` 和 `--show-clock-id` 不提示密码、不联网。`--help` 不需要硬件 SDK。

右手 Wi-Fi binding 的 `feedback_source_id` 必须是：

```text
wifi-ssh:unitree@192.168.31.65;interface=eth0;domain=0;topic=rt/brainco/right/state
```

左侧将末尾改为 `left/state`，同时仍需真实左侧模型/校准。binding 的 `clock_id` 继续是**电脑本次 boot**，因为 Wuji reader 与 mapper 都在电脑上运行；远端 PC2 clock 另记在 measured 元数据里。不能直接复用旧 DDS `feedback_source_id`。Wi-Fi 模式的远端主机、接口和 domain 是现有已审查客户端固定的 `.31.65 / eth0 / 0`，不接受本地 `--interface` 或非零 `--domain`，也不切换网络。

若以后使用本地 DDS，显式选择 `--feedback-transport dds --interface "$HAND_DDS_INTERFACE"`，其 Python 环境还需要 Unitree SDK/CycloneDDS，并使用相应 DDS binding。正式输出还要求 `--enable-motion` 和此前三项控制/反馈/服务确认，且实际网络与 binding 检查通过。**Wi-Fi SSH 分支没有手指命令跨机传输，所以即使三项确认都给出也拒绝 motion；不会退回电脑 DDS 发送。**

## Wi-Fi 反馈时间如何保留

适配器消费现有 `WifiFeedbackClient` 的 `conservative_received_monotonic_ns`、`age_upper_bound_s` 和远端样本 `sequence`。后者直接作为 measured `sample_id`，不是 RPC 请求计数。位置信息来自目标侧 `payload.q`，同样要求六个有限归一化值；`payload.dq` 也检查六项有限数，不用另侧反馈兜底。

本地 measured `received_t` 使用客户端给出的**保守时间界**，不直接减两机 monotonic，也不改成收到 JSON 的时刻。同一个远端 sequence 被重复查询时，q、sample_id、received_t 和源样本必须保持原值；远端 boot 改变、序号回退、缓存值或时间被改写均拒绝。

完整 RTT 加远端样本年龄得到的 `age_upper_bound_s` 还会随电脑本机经过时间增长。即使同一缓存保留的 received_t 看起来仍新，较强的 RTT 年龄界一旦超过 binding 的 `max_feedback_age_s`，也立即拒绝。这防止波动延迟下只检查一个旧估计时间而漏掉过期。新样本的保守时间界若没有推进，也不会强行刷新时间来通过 mapper。

反馈单线程按不高于约 20 Hz 拉取，每次 SSH 查询后重新取本地时间；没有额外 pexpect 读取线程。查询失败、过期或非法反馈锁存错误并结束本次预演；初始尚无目标侧样本允许等到原 idle deadline，收到过有效样本后再缺失则拒绝。该频率是请求上限，不是承诺的遥操作频率。订阅与查询仍只有读取，不启动 PC2 手部服务或命令订阅。

## 无帧、失联与退出

首次得到原 reader 合格帧与真实 measured state 后，才创建输出会话，避免把 SDK 初始化耗时误算为运行中输入中断。启动期没有合格帧或反馈超过 `--idle-timeout` 时退出，不创建 publisher，不补虚构 measured state。收到无效身份/时戳、EOF、坏反馈时立即结束；每条拒绝均不会重复上次目标。

会话建立后，采集循环即使没有新帧也持续调用 `check_timeout()`，原输入空窗达到默认 200 ms 即锁存。每个目标仍相对于当前新鲜 measured state 限步；默认最大 80/1000。DDS 初始化后再次验证年龄，过期不写入。

结束时先关闭输出会话，再清理本次原 reader 与状态订阅器；父进程只回收自己创建的进程组，并对整个 worker 设置 `seconds + 12` 秒硬等待上限。客户端 close 不发送额外 hold，也不声称物理停止；PC2 服务的超时保持行为及其 native 阻塞边界见 [手部输出类说明](teleop-hand-session.md)。

输出包含 `session`、`hand_result`、`waiting_for_feedback`、`hand_output_closed`、`summary`。summary 的 `accepted_original_frames` 与 `input_quality_passed` 表示实际观察到的原 reader 合格输出，不反推未输出的 SDK 原始帧数量。`published_count` 只统计 `Write()` 报告成功，始终不等于硬件执行确认。

退出码：0 为预演/运行到期且至少生成一个合法结果，或明确 synthetic demo 成功；2 为绑定、输入、反馈或初始化/关闭失败；3 为 worker 超时；130 为用户中断。

## 原 reader 为何没有输出：本次只做源码审查

原 `joint_readings()` 要求五指、每指五个槽位、所有角度和置信度有限，并且**每指**置信度均在 `[0.5,1]`。任一指不满足就返回 `None`；main 不输出该帧。它在数据质量通过后才把 flexion 槽位相加并转换为 degrees，没有角度幅值门槛。[原 reader](../../branco-wuji-teleop/brainco-wuji-teleop/wuji_bridge.py)

SDK 的 `FingerJointAngles.angles` 确实为弧度、五指各固定五槽；非拇指第五槽是占位。`confidence` 是 IK 求解置信度，原始 EMF 置信度属于另一层。原 reader 将选定 flexion 的弧度转换为 degrees，原开合标定也来自该 reader 输出；没有发现需要再乘/除单位的依据。[官方手部跟踪参考](https://docs.wuji.tech/docs/zh/wuji-glove/latest/sdk-data-reference/hand-tracking.md)

已有 18:04–18:05 记录足以定位当时的直接拒绝原因，无需重扫：

| 检查 | 左手 | 右手 |
| --- | --- | --- |
| 原过滤通过 | 0/361 | 0/361 |
| 每帧低于 0.5 的指 | 中、无名、小 | 食、中、无名、小 |
| 形状错误 / 非有限角度 / 过滤器异常 | 全部 0 | 全部 0 |
| 实际模型 | 缺个人 LEFT，使用内置模型 | 正确加载 `brainco-teleop/right_hand.urdf` |

右侧模型 SHA 为 `db5844c24c72b427fecfc5051666d5568aa8bbfb9187f836dfa4a70aea88ec99`，与报告/日志中实际加载文件一致。这证明当时使用了预期模型文件，不能独立证明它仍适合当前佩戴者。左侧缺模型与两侧源 EMF 质量低均已有记录，但不据此推断单一物理原因。**当时 0 帧输出由置信度条件直接解释，不是角度单位或六通道映射阶段拒绝造成。** [左报告](../diagnostics/wuji-after-rewire-left-quality-20261111-180428.json)、[右报告](../diagnostics/wuji-after-rewire-right-quality-20261111-180522.json)

当前遵照要求不做质量调优；原 reader 仍按原规则工作，低质量时自然没有跟随目标，这不妨碍其他链路继续启动/集成。程序不降阈值、不篡改置信度、不用合成数据冒充手套输入。

## 已完成的验证

```bash
uv run --offline --no-project --python 3.12 --with numpy \
  python -m unittest discover -s tests -p test_run_wuji_hand_session.py -v
```

21 项测试通过：保留此前 12 项 collector/mapper/session、过滤、超时与清理测试，新增左右手 Wi-Fi 时间/序号保持、完整 RTT 年龄界过期、缓存篡改/回退/远端 boot 变化拒绝、Wi-Fi 专属 binding、联网前拒绝 motion、无本机 Unitree SDK 的 worker 及 SSH 清理、匿名管道凭据、坏凭据不回显，以及 Wi-Fi 反馈到现有 mapper 的 fake 合流。默认预演与 fake 验证没有创建硬件 publisher。
