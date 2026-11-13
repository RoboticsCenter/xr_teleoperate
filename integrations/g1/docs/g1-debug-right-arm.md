# 吊装下 G1 29 DoF 右臂开发模式接管审查

2026-11-11。用户已确认机器人吊装/支撑，并选择先核对开发模式右臂控制。本页只记录官方源码审查、既有只读证据和待实施方案；没有切换模式、停止既有程序、创建运动 publisher 或执行动作。`rt/lowcmd` 的右臂控制需要明确全身 29 个物理关节的输出，不能直接复用当前 `rt/arm_sdk` 门控。

## 当前结论与现场下一步

官方部署流程是：机器人保持吊装，确认零力矩状态，再按遥控器 **L2+R2** 进入 Debug；官方描述此时关节处于阻尼状态。依据是宇树自己的 [RL Gym 部署说明](https://github.com/unitreerobotics/unitree_rl_gym/blob/276801e46c5d433564f24658bac64f254b7d2d4b/deploy/deploy_real/README.md#L19-L25) 与 [RL Mjlab 说明](https://github.com/unitreerobotics/unitree_rl_mjlab/blob/1425b15f73bd4095f0df53709d7c389c3eb9e790/README.md)。本次不执行其后续默认站姿、落地或策略启动步骤。

现场需要完成的具体步骤是：确认吊装装置能够承载全身、左臂与腿部在失去阻尼后仍有活动空间；确认当前确为零力矩，再由现场人员执行上述 Debug 组合键并反馈实际状态。**这仍是待用户实施的步骤，本页不表示已完成，也不授权软件代按或调用模式 setter。** 进入 Debug 后先重新只读检查模式、反馈和既有 writer，随后才能决定是否具备接管条件。

官方动态手册 [G1 quick development](https://support.unitree.com/home/en/G1_developer/quick_development) 本次未返回可读正文；上述按键结论来自两份固定版本的官方仓库，不声称已核实所有固件/遥控器版本的 UI。若现场按键或模式表现与官方流程不符，记录实际现象，不猜其他组合键继续切换。

## 模式字段与 API 分别说明什么

| 项目 | 已核对的语义 | 本项目要求 |
| --- | --- | --- |
| `CheckMode()` / API 1001 | 查询运动控制服务；当前历史响应为 code 0、`form="0", name="ai"` | `ai` 是服务标识，不表示 Regular，也不能表示已进入 Debug |
| 成功响应且 `name == ""` | 官方低层示例据此判定运动控制相关服务已停用 | 必须同时验证成功码和合法响应；超时、缺字段、`None` 都不能当空名称放行 |
| `ReleaseMode()` / API 1003 | 官方低层示例用于停用运动控制相关服务，是改变状态的调用 | 当前未授权、未调用；不放入自动重试或构造函数，不作为只读探测 |
| `lowstate.mode_machine` | 低层报文需匹配的机器类型字段；官方示例从实时状态复制 | 历史值 5 不解释为 Regular/Debug，不硬编码 5；值改变时重新核对，不能静默继续 |
| `GetFsmId()` / API 7001 | 当前历史查询 code 0、FSM 0；本版 SDK 的 `ZeroTorque()` 对应编号 0，`Damp()` 对应 1 | 这是旧时刻的服务状态，不替代现场确认和切换后复查；不调用这些 setter |

依据：[MotionSwitcher client](https://github.com/unitreerobotics/unitree_sdk2_python/blob/814556d15970dd2ecf1c9984e845ca02ab07e206/unitree_sdk2py/comm/motion_switcher/motion_switcher_client.py)、[API 定义](https://github.com/unitreerobotics/unitree_sdk2_python/blob/814556d15970dd2ecf1c9984e845ca02ab07e206/unitree_sdk2py/comm/motion_switcher/motion_switcher_api.py)、[C++ 低层示例的 queryMotionStatus/ReleaseMode](https://github.com/unitreerobotics/unitree_sdk2/blob/aaf69cb56c9d5ed63d8b284008ec3a8ab999bec1/example/g1/low_level/g1_dual_arm_example.cpp)、[G1 Loco client](https://github.com/unitreerobotics/unitree_sdk2_python/blob/814556d15970dd2ecf1c9984e845ca02ab07e206/unitree_sdk2py/g1/loco/g1_loco_client.py)。

手动 Debug 与 `ReleaseMode()` 的源码接口不同，不能未经实机确认就宣称两者完全等价。手动进入后若仍返回 `ai`，保留阻塞状态，先核对现场/固件行为；不会自动 ReleaseMode。反过来，空服务名也不证明所有外部 DDS publisher 已退出，更不证明全身已无力矩。

## 既有控制源：目前能定位到 PC1，尚不能确认独占

复用历史记录，没有新增网络扫描。17:12 PST 的发现记录已有一个 `rt/lowcmd` writer：

| 字段 | 既有证据 |
| --- | --- |
| writer GUID | `01103b75-02e7-c842-7273-d3a600001003` |
| participant GUID | `01103b75-02e7-c842-7273-d3a6000001c1` |
| 报文类型 | `unitree_hg::msg::dds_::LowCmd_` |
| 相同 RTPS GUID prefix 的网络来源 | `192.168.123.161:52860`，即 PC1 地址 |
| 存储的匹配网络元数据 | 向 PC2 `.164:39464` 的 9 包；向组播 `239.255.0.1:7400` 的 2 包 |
| 进程/PID/服务名 | 未知；记录没有该 participant 的进程属性 |

关联结果与输入文件 SHA-256 在 [g1-debug-lowcmd-origin-2026-11-11.json](../diagnostics/g1-debug-lowcmd-origin-2026-11-11.json)。这是 participant 网络归属的证据，包数包含 discovery 等通信，**不等同已解析到 11 条 lowcmd 运动目标**。

另一个 `rt/arm_sdk` participant 自报 `python3 / PID 1241`，但 GUID 不同，不能把这个 PID 归给 `rt/lowcmd`。先前 5 秒 arm_sdk 订阅无样本，也不能推出 lowcmd 无控制者；此前没有订阅解析 lowcmd 目标。PC1 SSH 22 的一次连接被拒，未能进一步读取进程。后续先通过切换后的只读发现或现场已知的控制程序核对归属，不停未知服务，也不根据名称猜 `master_service` 就是来源。

## 右臂低层帧的待实现约定

官方 [G1 Python 低层示例](https://github.com/unitreerobotics/unitree_sdk2_python/blob/814556d15970dd2ecf1c9984e845ca02ab07e206/example/g1/low_level/g1_low_level_example.py) 用 `rt/lowcmd`、PR 模式、29 个物理关节以及 CRC；其自动 ReleaseMode、全身归零和后续摆动是动作，不能直接运行该 demo 做本次验证。以下是基于接口提出的本项目约束，还不是已实现/实机验证的控制器。

| 范围/字段 | 拟发送内容 | 审阅含义 |
| --- | --- | --- |
| `mode_pr` | `0 / PR` | 保持 pitch/roll 表达，不切 AB |
| `mode_machine` | 首次使能前最新有效 lowstate 的实读值 | 后续监测变化，禁止用旧快照填值 |
| 右臂 motor 22–28 的 `mode` | `1` | 官方示例定义为 Enable；`0` Disable 不是零力矩字段组合的替代品 |
| 右臂第一帧 `q` | 同一新鲜反馈快照的七个实测关节角 | 不用全零、历史 17:12 姿态或 IK 预览目标作为起始角度 |
| 右臂 `dq / tau` | 初期明确设 `0 / 0` | 不悄悄加入未核对的重力前馈；需要前馈时另审负载模型 |
| 右臂 `kp / kd` | 明确配置、上限校验与渐增；数值尚未核定 | 示例增益不是装 BrainCo 后已验证值；零增益阶段也不能承诺右臂保持不动 |
| 非右臂物理 motor 0–21 | `mode=1, q=0, dq=0, kp=0, kd=0, tau=0` | 拟定零力矩策略；会撤掉 Debug 的阻尼，不维持腿、腰或左臂位置 |
| 保留 motor slot 29–34 | 保持定义好的零默认值 | HG IDL 有 35 槽，物理 G1 为 29；不套用 arm_sdk 的 `motor[29].q` 混合权重 |
| `crc` | 全字段填完之后按固定 SDK 计算 | 每帧完成序列化前计算，不复用旧 CRC |

右臂顺序为：22 肩 pitch、23 肩 roll、24 肩 yaw、25 肘、26 腕 roll、27 腕 pitch、28 腕 yaw。字段与槽数另可核对固定官方 [MotorCmd HG 定义](https://github.com/unitreerobotics/unitree_sdk2/blob/aaf69cb56c9d5ed63d8b284008ec3a8ab999bec1/include/unitree/idl/hg/MotorCmd_.hpp) 和 [LowCmd HG 定义](https://github.com/unitreerobotics/unitree_sdk2/blob/aaf69cb56c9d5ed63d8b284008ec3a8ab999bec1/include/unitree/idl/hg/LowCmd_.hpp)。

官方 RL Gym 的 [command_helper.py](https://github.com/unitreerobotics/unitree_rl_gym/blob/276801e46c5d433564f24658bac64f254b7d2d4b/deploy/deploy_real/common/command_helper.py) 展示零力矩时令 `kp/kd/tau` 为零、阻尼时保留 `kd`，但该版本将速度属性写成 `qd`，与 HG IDL 的序列化字段 **`dq`** 不一致。集成时必须显式写 `dq`；不能直接复制这个 helper 并认为速度字段已经清零。

非右臂零力矩是本页提出、待现场和代码审阅确认的策略。若要保留阻尼，应改成另一套明确的 `kd>0` 策略并单独验收；不能一面填写零力矩、一面承诺其余身体保持不动。仅有吊带而未确认肢体活动空间，还不满足此项。

## 实施顺序与可检查验收

1. **完成现场 Debug 步骤。** 用户确认悬挂、零力矩和可达的现场停止操作后，再执行官方按键步骤；不要求落地或进入站立 Regular。
2. **只读重新确认接管条件。** 用现有查询入口记录成功的 CheckMode、低层模式字段、新鲜的 29 关节状态、故障字段和 DDS writer。当前历史 `ai` 与已有 PC1 lowcmd writer 均未解决。即使短时未看到 writer，也需要知道原控制程序如何退出、谁负责恢复，不能据未观察到推导独占。Debug 以后 GetFsmId 可能因服务退出而不再响应，不能把这种超时当正常模式证明。
3. **先实现纯帧生成和离线门控。** 默认无 publisher；用存储/合成反馈验证首帧等于新鲜实测 q、只右臂目标可变化、0–21 全部符合选定非右策略、保留槽无权重、所有数值有限、限位/步长/增益上限及 CRC 有效。模式缺失、反馈过期、计时回退、异常帧、控制源未确认均拒绝使能；恢复输入不能自动解锁。现有 arm_sdk 测试通过不覆盖这些条件。
4. **审阅具体有限动作再获得实机授权。** 先固定右臂当前姿态，再考虑单个右肘的小幅有限时长动作；PICO 首次姿态应标为相对原点，不能让初始绝对位姿直接跳入 IK 目标。实际增益、幅度、持续时间、反馈误差阈值和退出帧尚待确定，本页没有给出可直接运行的动作命令。
5. **异常/退出策略必须先定义再验收。** 输入/反馈/发布周期超时要锁存故障；由哪个线程在何时发何种最后帧，以及发布线程阻塞时怎么办，都需验证。保持当前角度、阻尼、零力矩都是不同的主动输出，均需审阅。停止发包不等于物理停止；不能依赖未知固件的最后目标行为。退出后不能自动 SelectMode 或恢复原高层服务。

本轮完成的是官方入口/字段审查与历史 writer 关联。10 份固定官方源码的路径、commit、SHA-256，以及按键/速度字段/35 槽静态核对结果保存于 [g1-debug-official-sources-2026-11-11.json](../diagnostics/g1-debug-official-sources-2026-11-11.json)；历史证据文件校验通过。这些检查没有执行 SDK 示例。

现场 Debug、服务退出/控制归属、低层专用门控、具体增益/退出策略和任何右臂动作验收均未完成。
