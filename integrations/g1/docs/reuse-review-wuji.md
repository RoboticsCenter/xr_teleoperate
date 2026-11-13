# H2 分支 Wuji、手部协调与录制复用审查

审查对象：`kevin-roboticscenter/xr_teleoperate` 的 `h2/pico-wuji-head-camera`，固定提交 **`053ba99af55c1d8ddc6b87918cb0fa234625886e`**。本次仅源码检查和纯软件测试，没有连接机器人、手套、相机或启动控制服务，没有修改上游及指定的原 Wuji 项目。

结论：可抽取双手启停协调、SSH 输入转发和录制生命周期管理。该分支的手指计算和输出针对 **Unitree Dex5 的 20 个关节**，不能替代当前 **BrainCo Revo2 六通道** 的读取、映射和反馈限步。

## 可以抽取的模块

以下上游路径均相对 `integrations/h2/`，链接固定到本次提交。

| 模块 | 可复用程度 | 本项目接入点与必要变更 |
|---|---|---|
| [`quest_auto_record.py`](https://github.com/kevin-roboticscenter/xr_teleoperate/blob/053ba99af55c1d8ddc6b87918cb0fa234625886e/integrations/h2/quest_auto_record.py#L9) 的 `AutoRecording` | **核心类可原样抽取**；stdlib、注入 `recorder` 和 `state_reader` | 给当前运行器提供 session identity/running/started 状态，接入实际录制后端；改界面中的 Quest/腕相机文字即可。START 后开始录制、进程退出保存；手动录制所有权分离、异常不无限重启。当前项目没有相应的统一录制生命周期层。 |
| [`quest_wuji.py:220–281`](https://github.com/kevin-roboticscenter/xr_teleoperate/blob/053ba99af55c1d8ddc6b87918cb0fa234625886e/integrations/h2/quest_wuji.py#L220) 的 `WujiHands` | **协调逻辑可抽取**，不依赖机器人型号；后端接口须适配 | 为左右 `TeleopHandSession` 提供 `status/start/stop/conflict/close` 包装。先检查双侧输入/反馈/占用，再依次启动；一侧失败调用双方 stop。仅单侧测试和左侧旧模式也有明确分支。它是应用层回滚，不能撤销第一只手在第二只启动失败前已经发生的动作。 |
| [`wuji_glove/send_input.py:12–21`](https://github.com/kevin-roboticscenter/xr_teleoperate/blob/053ba99af55c1d8ddc6b87918cb0fa234625886e/integrations/h2/wuji_glove/send_input.py#L12) | **小转发器几乎可原样抽取** | 标准输入 JSONL 转到本机左右 Unix datagram socket，不创建 DDS。需要接到本项目接收端和路径；该文件自身不创建 SSH，SSH 由上层启动。它不验证 payload、新鲜度或会话身份，不能代替现有时效/绑定检查。 |
| [`wuji_glove/dashboard_gateway.py:40–79`](https://github.com/kevin-roboticscenter/xr_teleoperate/blob/053ba99af55c1d8ddc6b87918cb0fa234625886e/integrations/h2/wuji_glove/dashboard_gateway.py#L40) | **只复用进程编排结构** | 具有按左右 SN 配置、校准前置检查、输入进程与 SSH relay 生命周期分离的结构。必须替换 `h2_link`、H2 ControlPath/HostKeyAlias `.124.162`、远程 Python/目录，并将 source 改接指定原 `wuji_bridge.py`；不可直接使用其 tip-poses reader。当前 G1 无线地址 `.31.65` 与主机密钥别名 `.123.164` 已验证。 |
| [`h2_record.py`](https://github.com/kevin-roboticscenter/xr_teleoperate/blob/053ba99af55c1d8ddc6b87918cb0fa234625886e/integrations/h2/h2_record.py#L130) | **录制框架可复用，数据后端须适配** | 保留 `meta.json/status.json`、视频逐帧索引、measured/commanded 分列、过期状态写空值的结构。换成 BrainCo 两侧六通道 IDL/topic/单位；本次 G1 Debug 输出记录应订阅 `rt/lowcmd`，不能保留只记 `rt/arm_sdk`。配置实际摄像头或 TeleImager 源。 |

`quest_wuji.py` 与 `deployment/robot/quest_wuji.py` 经 `cmp` 确认为相同副本。接入时只维护一份来源，不增加两套同步副本。

## 不能直接拿来替换的手指链路

1. **不同的输入和映射。** [`stream_latest_tips.py:22–47`](https://github.com/kevin-roboticscenter/xr_teleoperate/blob/053ba99af55c1d8ddc6b87918cb0fa234625886e/integrations/h2/wuji_glove/stream_latest_tips.py#L22) 订阅官方 `tip_poses()`，送五个指尖位置；本项目原 `wuji_bridge.py:40–50` 使用 `joint_angles`，检查每指 confidence>=0.5 后生成 flexion 读数。两者不是可直接交换的消息格式。
2. **不同的机器人手。** [`left_dex5_adapter.py:98–136`](https://github.com/kevin-roboticscenter/xr_teleoperate/blob/053ba99af55c1d8ddc6b87918cb0fa234625886e/integrations/h2/wuji_glove/left_dex5_adapter.py#L98) 调用 `HandRetargeting(HandType.UNITREE_DEX5)` 和 dex-retargeting，输出20关节；176–186建立 `rt/dex5/{side}/state`、`rt/dex5/{side}/cmd`，使用 HG `HandState_/HandCmd_`。本项目使用 `rt/brainco/{side}/*` 和六通道，0–1000到0–1转换保留原映射。因此该适配器、Dex5增益/模型/重定向profile都不应迁入作为本项目手指算法。
3. **低质量输入没有同等过滤。** `stream_latest_tips.py:30–31` 发送 confidence，但 `quest_wuji.py:99–112` 只检查侧别、五个点与有限坐标；`left_dex5_adapter.landmarks()` 也没有 confidence 门限。它不能作为解决当前“原过滤器接受0帧”的替代读取器。
4. **连续输出的失联策略不足。** [`left_dex5_adapter.py:201–269`](https://github.com/kevin-roboticscenter/xr_teleoperate/blob/053ba99af55c1d8ddc6b87918cb0fa234625886e/integrations/h2/wuji_glove/left_dex5_adapter.py#L201) 对 input/feedback age 只生成遥测，运行后没有持续过期或 driver-error 拒绝；序号不变仍可继续重复目标，约100Hz。它的1秒初始插值也不能替代本项目每帧基于新鲜 measured state 的限步。`quest_wuji.py:197–208` 的 stop 是停止适配器，文字明确说明手保持最后姿态，不能当作经验证的设备停止策略。
5. **Dex5拟合不适用。** [`build_left_profile.py:15–51`](https://github.com/kevin-roboticscenter/xr_teleoperate/blob/053ba99af55c1d8ddc6b87918cb0fa234625886e/integrations/h2/wuji_glove/build_left_profile.py#L15) 修改 Dex5 左手 URDF 的四个 distal coupling，来源是 H2/Dex5反馈拟合，并标记 `physically_validated=False`。不能用作 Revo2 左手校准。

当前项目应保持 `scripts/run_wuji_hand_session.py:145–157` → 指定原 `wuji_bridge.py` 的路径，以及 `scripts/preview_hand_dds.py:242–254` 对原 `retarget` 的调用。`scripts/teleop_hand_session.py:129–139` 写前再次检查输入/反馈年龄和真实 Write 返回值，不能因采用新编排而绕过。

## 录制后端的具体差异

- `h2_record.py:27–40` 硬编码两路 H2 UVC相机的 PCI/USB by-path 和 Dex5关节名字；G1的 D435i 或其他实际相机应单独配置。
- `ARM_INDEX=15..28` 恰好与当前 G1_29双臂索引相同，不需要因为文件名含H2就重写索引；仍要核对字段/关节名字。手部20关节结构和单位不能沿用。
- `h2_record.py:138–144` 只订阅 `rt/arm_sdk` 命令与 Dex5手topic；本项目Debug路线是 `rt/lowcmd`，手状态为 `MotorStates_` 六通道。
- `h2_record.py:152–197` 用PC2接收时的 `time.time()` 对齐快照。它不是硬件同步时戳；不应宣传为 PICO/Wuji/相机曝光严格同步。保留本项目跨机RTT时效逻辑，用 monotonic 判断新鲜度更符合当前约定。
- 该录制器保存腕相机和关节；README:103明确头相机录制未启用。不能将它计作已经提供头相机录制。

## 验证与来源约束

- 实跑 `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s integrations/h2/tests -p test_auto_recording.py -v`：**12项通过**，全部使用fake recorder/临时文件，没有硬件连接。
- 原 `test_dual_wuji.py` 测试尝试在导入阶段因当前Python环境缺 `zmq` 失败；未计为通过，也未为审查安装/升级机器人端依赖。测试源码覆盖双手预检、失败回滚、单侧测试、侧别socket隔离与未校准拒绝，但不等于实机停止/失联验证。
- 完整运行时另需 `upstream/dex5-pr321` 固定submodule和 `runtime.patch` 中的 `dex5_gains.py`，不能只拷贝dashboard后期望直接运行。见 `sources.json` 固定commit `39b79bca34b7547b21edad3fe3e642bb5cac7147`。
- 仓库顶层 `LICENSE` 标示 Apache-2.0 并列出依赖的独立许可证；抽取代码时保留许可证、版权和固定提交来源。此审查没有把外部子模块许可推断为完全相同。

建议优先级：**首先保留当前Revo2链路，按需要抽取 `WujiHands` 的协调接口；需要统一录制时引入 `AutoRecording`；跨电脑/PC2输入转发可借鉴小型SSH/Unix socket结构，但复用本项目既有nonce/时效协议。** 不因该分支名含Wuji而切换到其Dex5重定向。
