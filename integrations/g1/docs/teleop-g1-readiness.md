# 整套 teleop 的 G1 接管条件与反馈会话

2026-11-11。用户现已要求运行遥操作，并确认 G1 仍吊装、现场有人持遥控器；旧记录中的“没有运动授权”不是当前阻碍。本子任务执行了只读模式检查及明确获准的 BrainCo 默认反馈服务启动，没有发 G1/手部动作，也没有 ReleaseMode、SelectMode 或停止未知进程。

## 当前实机状态

| 时刻（PST） | 实读结果 | 接管含义 |
| --- | --- | --- |
| 18:14 | CheckMode code 0，`form=0,name=ai`；GetFsmId code 0，FSM 0 | 当前高层服务有名称，不能当作已释放低层控制 |
| 18:14 | 4762 个有效 lowstate，0 invalid；mode_machine 5、PR 0 | 本体反馈可用；5 不表示 Regular/Debug |
| 18:14 | 各观察到一个 arm_sdk 与 lowcmd writer | GUID 与旧时刻不同，不能继承旧 PID/进程归属 |
| 用户随后反馈 | 已按 L2+R2，进入 Debug/阻尼 | 现场模式切换已确认，无需反复要求重按 |
| 18:17:50 复查 | CheckMode 仍 code 0、`name=ai`；GetFsmId code 3102、无 FSM 值 | 如实保留差异，不把失败码翻译成某个 FSM |
| 18:17:50 复查 | 5025 个有效 lowstate，0 invalid；仍 mode_machine 5/PR 0 | 反馈持续；不因 RPC 差异推断本体掉线 |
| 18:17:50 复查 | 4 秒内未观察到 lowcmd writer；arm_sdk 仍有一个 | 发现结果有所改变，但未观察到不证明控制独占 |

证据：[初次检查](../diagnostics/teleop-readiness-g1-current-2026-11-11.json)、[Debug 后复查](../diagnostics/teleop-readiness-g1-after-debug-2026-11-11.json)。最新 arm_sdk GUID 为 `01100fd4-ba86-d846-1406-03a300000403`；Debug 前 lowcmd GUID 为 `01104c02-399c-194a-c805-d9de00001003`。没有把它们归给旧的 PC1 PID 1241。

管理连接为 `unitree@192.168.31.65`，严格使用现有 `diagnostics/g1-known_hosts` 和 `HostKeyAlias=192.168.123.164` 校验同一 PC2。机器人侧 DDS 在 PC2 `eth0` 读取；这不等于已验收电脑通过 Wi-Fi 直接收发 DDS。两个时刻的完整 29 关节 q/dq、14 臂关节故障字段及 IMU 原始值均保存在 JSON，不能再用 17:12 的旧姿态作为控制首帧。

官方低层示例先通过 CheckMode 查询，再通过 ReleaseMode 停用运动控制相关服务；这会改变服务状态，**不是查询，也不保证外部 writer 全部消失或物理停止**。用户手动 Debug 后仍有 `ai`，由主代理据现场与官方流程决定是否执行一次明确的 ReleaseMode，并在之后复查结果；本模块不会自动调用、循环重试或自动恢复高层服务。参考 [固定 Python 低层示例](https://github.com/unitreerobotics/unitree_sdk2_python/blob/814556d15970dd2ecf1c9984e845ca02ab07e206/example/g1/low_level/g1_low_level_example.py) 和 [MotionSwitcher 实现](https://github.com/unitreerobotics/unitree_sdk2_python/blob/814556d15970dd2ecf1c9984e845ca02ab07e206/unitree_sdk2py/comm/motion_switcher/motion_switcher_client.py)。

## BrainCo 反馈服务已经运行

启动前二进制 SHA-256 匹配已验收版 `b0df0937ea32ac6ddf80c5d851fbf7405434fbf3d8aeb59003f5d456e12ffb8b`。只有 `/dev/ttyUSB0`、`/dev/ttyUSB1` 两个候选端点，root fuser 无占用。无额外 YAML 配置，默认 `--enable-commands=false`。两手 SN 均再次绑定成功：左 `BCXTL2204J2600021 / USB1 / ID126`，右 `BCXTR2362J2500015 / USB0 / ID127`。

18:15:16 PST 开始的服务由 timeout 进程组 **3836** 管理，实际 server PID **3837**，约 **19:15:16 PST** 自动结束，超时后最多再等 3 秒。此后 Debug 复查确认两个串口仍仅由已知服务 PID3837 占用。默认服务无命令订阅或目标写入，不能把“服务运行”称为手指已跟随。

| 5 秒独立订阅 | 有效帧 | 频率 | 结束样本年龄 |
| --- | --- | --- | --- |
| 左手 state | 480 | 96.10 Hz | 6.6 ms |
| 右手 state | 478 | 95.84 Hz | 15.1 ms |
| 本体 lowstate | 5075 | 1012.69 Hz | 1.1 ms |

三路 fresh，invalid=0，probe exit 0；stderr 保留一条官方 Reader take sample error，未据此隐藏数据。证据：[运行与订阅报告](../diagnostics/teleop-readiness-g1-feedback-running-2026-11-11.json)。PC2 日志：`/home/unitree/g1-integration/brainco-feedback-20261111/teleop-feedback-20261112T021516Z.log`。

等价启动命令如下，**已有本次会话运行时不要再启动第二份**：

```bash
sudo env LD_LIBRARY_PATH=/home/unitree/g1-integration/brainco-feedback-20261111/lib/aarch64:/usr/local/lib \
  timeout --signal=TERM --kill-after=3s 3600s \
  /home/unitree/g1-integration/brainco-feedback-20261111/bin/brainco_hand_server -n eth0
```

提前停止时先核对 `/proc/3836/cmdline`、进程组与上述 timeout 身份仍一致，再在 PC2 执行 `sudo kill -TERM -- -3836`；不要仅凭旧 PID 停进程。状态服务当前只读，结束它也不等于对手执行物理停止。若后续切换手指输出服务，必须先有明确控制源和退出策略，不能同占串口。

## 新增低层右臂会话接口

[g1_lowcmd_session.py](../scripts/g1_lowcmd_session.py) 提供 `Config`、`LowCmdSession`、`decode_lowstate`、`encode_idl`，默认 disabled，导入/构造不创建网络实体。`enable_motion=True` 也仅允许后续显式使能，只有 `step(action="enable", readiness=...)` 通过全部检查才惰性创建 `rt/lowcmd` publisher。没有构造即运动、ReleaseMode、SelectMode 或自动恢复路径。

当前已经实现并离线验证的行为：

- 右臂 motor22–28 为唯一允许位置目标变化的范围；首次输出 q 来自新鲜实测反馈、kp=0，此后至少 1 秒渐增 kp。目标窗口最多 ±0.03 rad，默认目标限速 0.05 rad/s。
- 非右臂 motor0–21 统一 `mode=1,q=dq=tau=kp=0`，使用**必须显式给定且非零的 22 个 kd**，保留阻尼而非默认撤掉阻尼；阻尼也不维持身体姿态，吊装条件仍必要。
- 物理 29 槽 mode=1，HG 保留槽29–34与 reserve 全零，不使用 arm_sdk 的 slot29 权重；PR0、实时 mode_machine，最后计算 CRC。
- 反馈必须包含 29 个关节 q/dq/motorstate、CRC 验证结果、原始 tick 和本机接收序号/时刻。任何关节故障、速度异常、右臂限位或跟踪误差、模式变化均锁存。
- 目标/反馈/deadman 各自校验新鲜度；旧 sequence 不允许改接收时间冒充新帧。循环或写入时限超限不会被后来的新输入自动恢复；每个会话最长 60 秒，需新建会话重新核对。
- 建立 transport 与编码后、真正 Write 前重新核对本机时钟和所有输入年龄。Write 阻塞超过期限则返回后锁存；阻塞期间软件无法保证按时停机。
- `damp_and_latch(now=..., feedback=...)` 是**显式动作**：最多尝试一次全身阻尼帧，要求此前已使能、现有 transport 和新鲜有效状态。`close()` 不隐式发最后一帧或恢复模式。停止发包没有被描述为物理停止。

参数依据：[官方 G1 Python 低层示例](https://github.com/unitreerobotics/unitree_sdk2_python/blob/814556d15970dd2ecf1c9984e845ca02ab07e206/example/g1/low_level/g1_low_level_example.py) 的右臂增益为 kp40/kd1；[官方 RL Gym 阻尼 helper](https://github.com/unitreerobotics/unitree_rl_gym/blob/276801e46c5d433564f24658bac64f254b7d2d4b/deploy/deploy_real/common/command_helper.py) 使用 kd8。**这些是示例参数，不是厂家公布的硬件最大值**；未找到可靠官方最大 kp/kd 规格。代码将 40/1/8 用作本项目更窄的配置上界，实际增益仍必须逐项显式传入并带 `gain_review_id`。不得称其为经过 BrainCo 负载验证的安全上限。helper 的 `qd` 拼写与 HG 字段不同，新增编码实际写 `dq`。

调用方应在 PC2 上订阅状态，接收 callback 内用 `decode_lowstate(message, received_t=time.monotonic(), sequence=..., crc=CRC())` 生成反馈；缓存读取不能重打时戳。session 的 `now`、callback 时间、目标到达时间和 deadman 时间必须在同一主机 monotonic 域，不能把电脑时钟直接塞进 PC2 会话。纯 Python 会话没有独立发布/故障线程，调用方负责稳定循环、互斥访问与进程监督。

`step` 输入结构可直接参考 [测试的 record()](../tests/test_g1_lowcmd_session.py)：target 为七个右关节角、校准 ID、reference ID、严格布尔 tracking_valid；enable 为已按住状态和来源 ID；首次 readiness 包含本机最近 1 秒内的成功空名称 CheckMode、吊装、现场操作员、控制独占确认。不能把未知 PICO 有效性硬填 True，也不能把短时未观察到 writer 自动填为独占。

测试命令：

```bash
python3 -m unittest discover -s tests -p test_g1_lowcmd_session.py -v
```

20 项离线测试通过，包括完整 35 槽、首帧、限速/渐增、序号缓存、状态异常、模式变化、超时后新包、建 transport 延迟、同步 Write 阻塞、一次阻尼和无隐式退出动作。这组使用 fake publisher/CRC 验证行为和字段顺序。

18:21 PST 另在 PC2 的 tv Python 环境完成真实 **aarch64 IDL/CRC 无输出验证**：合成 LowCmd 序列化为 1004 字节，CRC 重算一致；35 槽中的保留槽全零；合成 LowState 通过真实 CRC 验证并解出 29 个物理状态。退出 0、stderr 空。没有 ChannelFactoryInitialize、subscriber、publisher 或 RPC，没有调用真实低层 transport。证据：[ARM64 编码检查](../diagnostics/teleop-readiness-g1-lowcmd-native-codec-2026-11-11.json)。它核实编码可用，仍不是物理动作验收。

## 距离实际运行还差什么

低层帧生成与门控类已存在，ARM64 无输出序列化核对已通过；仍需主运行器把 PC2 本机状态订阅、经过标定/有效性检查的右腕目标、持续 deadman 和明确退出策略接入同一时钟域。当前刚进入 Debug，但服务查询仍为 ai；由主代理完成控制释放/归属决策，不能在代码里绕过 gate。

现场已完成吊装、有人持遥控器、手动 Debug 三项，不再要求重复这些步骤。下一次真实动作应使用刚捕获的姿态作为参考，先有限时长右臂小幅验证，再延长会话；异常阻尼策略本身也须纳入该次动作审阅。手指路径仍要由原映射产生有效帧，用户选择暂不调 glove 质量不会让无效帧自动变有效。
