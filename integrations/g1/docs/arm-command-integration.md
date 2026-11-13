# G1 双臂命令门控与首次右臂验收

本轮实现了 [arm_command_gate.py](../scripts/arm_command_gate.py) 的纯软件门控、官方HG消息编码和显式实际输出类；**没有执行实际publisher路径，没有发运动指令或切换模式**。默认CLI和 `ArmOutputSession()` 均只预览。

当前现场：用户已选择先使用右腕 Tracker，SN **PC2310MLL9241957G**，一只Tracker数据已读通；机器人处于吊装/支撑状态，用户不确定控制模式。右腕输入的轴/单位/安装参考仍需核对，实际PICO客户端没有传感器confidence。因此现在继续右腕录制与离线IK，不能把这些条件写成实机使能已就绪。`mode_machine` 的数字不是Regular mode证明。

## 已固定的官方协议

依据 [xr_teleoperate 的 G1_29 控制器](https://github.com/unitreerobotics/xr_teleoperate/blob/817fb00c63cde15e5f24a0f8fa08e1e33ed89d3b/teleop/robot_control/robot_arm.py) 和 [SDK的7关节双臂示例](https://github.com/unitreerobotics/unitree_sdk2_python/blob/814556d15970dd2ecf1c9984e845ca02ab07e206/example/g1/high_level/g1_arm7_sdk_dds_example.py)：

| 项目 | 本项目实现 |
| --- | --- |
| Topic / IDL | 仅 `rt/arm_sdk` / `unitree_hg.msg.dds_.LowCmd_` |
| 左臂 | motor15..21，对应14维数组0..6 |
| 右臂 | motor22..28，对应14维数组7..13 |
| 首次受限关节 | **右肘 motor25 / 数组index10**；默认不允许左侧目标变化 |
| SDK混合权重 | `motor_cmd[29].q`，0..1；不是mode_pr，也不是手指DDS的mode字段 |
| `mode_pr` | 固定0，PR定义；输入feedback必须同样为0 |
| `mode_machine` | 最新有效LowState原值；改变即锁存，绝不硬编码5 |
| 命令结构 | 35个motor slots；14臂关节mode1，其他slot保持mode0/零增益/零力矩，slot29仅写weight |
| 增益 | 原XR G1默认肩/肘kp80、kd3；腕kp40、kd1.5；这是上游参数，不是Revo2负载下的已验证安全值 |
| dq / tau | 本阶段均0；不采用尚未校准Revo2惯性的IK重力补偿 |
| CRC | 全部字段设置完成后调用SDK的 `CRC.Crc(msg)`，然后 `Write(msg)` |

HG `LowCmd_`/`MotorCmd_` 字段与CRC依据固定SDK commit `814556d15970dd2ecf1c9984e845ca02ab07e206` 的 [IDL](https://github.com/unitreerobotics/unitree_sdk2_python/blob/814556d15970dd2ecf1c9984e845ca02ab07e206/unitree_sdk2py/idl/unitree_hg/msg/dds_/_LowCmd_.py)、[默认构造器](https://github.com/unitreerobotics/unitree_sdk2_python/blob/814556d15970dd2ecf1c9984e845ca02ab07e206/unitree_sdk2py/idl/default.py) 与 [CRC实现](https://github.com/unitreerobotics/unitree_sdk2_python/blob/814556d15970dd2ecf1c9984e845ca02ab07e206/unitree_sdk2py/utils/crc.py)。实际后端启动前校验这几个文件、channel实现和当前CPU的原生CRC库SHA256；不符即拒绝。

上游主程序注明 G1 `--motion` 路径要求 **Regular mode（R1+X），不支持 Running mode（R2+A）**，见 [固定主程序](https://github.com/unitreerobotics/xr_teleoperate/blob/817fb00c63cde15e5f24a0f8fa08e1e33ed89d3b/teleop/teleop_hand_and_arm.py#L141)。本项目不调用切模式RPC，不使用 `rt/lowcmd`，不调用 `ReleaseMode()`，不构造会自行发布初始零目标的上游ArmController，也不调用go-home。吊装/支撑状态下是否能进入适合此次测试的模式，仍需结合现场状态与官方操作流程确认，代码不自动尝试。

## 默认门控范围

`Config` 默认仅允许右肘index10，相对本次实测参考最大±0.03rad，命令位置变化不超过0.1rad/s，候选频率不超过250Hz。采用官方URDF关节限位并预留0.02rad；测得速度超过0.3rad/s、命令/实测差超过0.05rad、循环间隔超过50ms均锁存。输入、反馈和持续使能记录最大年龄默认100ms。

这些是**供小幅测试审阅的参数**，没有通过实机负载/碰撞验证。原 `rt/arm_sdk` 权重对双臂共享，默认单关节测试仍会给其余13个臂关节发送初始实测参考位置，不能描述为只有一台电机获得控制权。

首次 `arm` 必须同时具备新鲜反馈、有效目标与持续按住的使能，且第一帧目标严格等于14个实测关节角。先生成 `q=measured_q, weight=0`；接下来以0.5/s渐入约2秒，其间目标保持初始参考；weight达到1后才限速跟随选定关节。初始IK正则项导致的偏差不能跳过此相等检查，clutch参考建立阶段应显式使用实测q。

右肘测试验收后，下一步右腕位置IK可显式改为 `active_indices=tuple(range(7,14))`，其他限制继续生效，左臂目标仍必须等于参考。右腕旋转暂固定在参考姿态，不将尚未确认的Tracker朝向直接送入IK。

## 输入合同与时间

`step()` 接收 `now`（本机monotonic秒）、`feedback`、`target`、`enable`，以及单次手动事件 `action`。

| 对象 | 必需字段 |
| --- | --- |
| `feedback` | `sequence`、`received_t`、原始uint32 `tick`、14维 `q` / `dq`、原始 `mode_machine`、`mode_pr` |
| `target` | `sequence`、`received_t`、14维 `q`、`tracking_valid`、`calibration_id`、`reference_id` |
| `enable` | `sequence`、`received_t`、布尔 `held`、与配置匹配的 `source_id` |

反馈q/dq取LowState的motor15..28，`received_t` 在接收回调中记录，`sequence` 为本地接收序号；原始tick须前进，支持uint32正常回绕。不能在控制循环里重新给同一缓存打当前时间。同一sequence可在年龄范围内复用，但字段/时间必须完全相同；更高sequence必须有更新receipt。

目标receipt应继承**对应有效PICO源帧**的本机接收时间；不得每次重新跑IK就让旧Tracker位姿获得新时间/序号。`calibration_id` 锁定SN/坐标/安装/单位配置，`reference_id` 标识本次clutch参考，运行中变化即锁存。重新arm必须使用新的reference_id。

`tracking_valid=true` 是输入层已经完成跟踪有效性验证后的合同，门控本身不会从数值有限、SN出现或时间变化推导它。**当前PICO客户端不传confidence，raw帧满足接收条件不等于传感器跟踪置信度已验证；未知有效性不能伪造成true来绕过此门控。** 当前离线预览可以分析这些raw数据，但首次实机跟随前须补齐/验证实际输入有效性策略，并保留confidence未知这一事实。

持续使能必须来自实际按住型输入或等价已审阅物理输入，每次新的事件/心跳均有自己的源接收时间。不能由控制循环重复填写 `held=true, received_t=now` 来模拟用户一直按住，也不能用一次toggle代替。源失联/松开时锁存，恢复数据不会自动跟随。

## 纯预览与实际输出API

可立即运行的入口仅使用合成数据，没有SDK导入或网络访问：

```bash
python3 scripts/arm_command_gate.py --demo
python3 -m unittest discover -s tests -p test_arm_command_gate.py -v
```

离线回放可用 `--config config.json --input snapshots.jsonl`。JSONL每行的顶层字段是上述 `step()` 参数；config使用 `Config` 同名字段。此CLI没有发布开关，历史记录不能被CLI重放到机器人。

后续集成层的明确实际调用方式如下。这里的变量来自已验证的实时输入适配层，**本轮没有执行这段实际路径**：

```python
from scripts.arm_command_gate import Config, ArmOutputSession

cfg = Config(
    active_indices=(10,),                 # right elbow / motor25
    calibration_id=verified_calibration_id,
    enable_source=verified_deadman_source,
    regular_mode_confirmed=True,          # only after actual mode confirmation
)
session = ArmOutputSession(
    cfg,
    enable_motion=True,                  # default is False
    interface="enx6c1ff762d908",
    exclusive_control_confirmed=True,     # actual single-controller check
    motion_review_id=reviewed_test_id,
)
# Constructor still creates no publisher. First accepted arm record does so.
result = session.step(now=now_monotonic, feedback=live_lowstate,
                      target=clutch_reference_target, enable=live_deadman,
                      action="arm")
# Later fresh records: action=None; target comes from constrained IK.
```

只有 `enable_motion=True`、已确认Regular mode、单一控制权、明确网口和非空审阅标识同时存在，实际类才接受运动配置；还需首个arm帧通过门控，才初始化domain0并创建 `rt/arm_sdk` publisher。布尔确认是调用者提供的现场证据声明，不是软件自动认证。共享进程若已初始化Unitree `ChannelFactory`，上游单例会忽略再次传入的NIC/domain；集成层必须确保既有DDS上下文就是此网口/domain0，或使用独立输出进程，不能仅凭构造参数认定网口已切换。

SDK `Write()` 返回true时才记录 `published=true`，始终 `acknowledged=false`。失败/异常锁存输出会话，标记delivery未知，不自动重试，更不归零或切全身模式。状态门控与实际输出层均不是机器人侧watchdog。

## 松开、失联和退出移交

| 情况 | 软件行为 | 尚不能承诺 |
| --- | --- | --- |
| 松开使能、目标过期、反馈过期、mode变化、循环超时 | `LATCHED`，不生成新的跟随命令 | 机器人已停止/原位保持；固件可能继续使用最后指令 |
| 手动请求 `action="release"`，且反馈/模式/跟踪差仍有效 | 固定最后一个受限q，weight按0.5/s递减至0，`RELEASED` | 高层控制接手后手臂姿态不变 |
| 反馈不可用、实际位置与最后目标偏差过大 | 拒绝自动渐退帧，保持锁存 | 能安全猜测当前姿态或控制状态 |
| `close()` | 只关闭publisher，绝不额外Write/go-home/切模式 | 已完成物理移交 |

渐退调用必须区分：对纯 `ArmCommandGate.step(action="release",...)` 的调用只产生预览；对已开启运动的 `ArmOutputSession.step(action="release",...)` 会实际写出渐退帧。release事件只发一次，随后在约2秒内持续传入新鲜反馈、`action=None`，直到 `RELEASED`；逐帧重复release事件会重新开始渐退阶段，不能作为退出循环。

完成渐退后再 `session.close()`。返回的 `zero_weight_write_reported` 仅说明最后weight0写调用成功；`physical_handover_verified` 始终false。未经过渐退、写失败或中途断流会返回 `handover_incomplete=true`。发生模式变化/严重位置偏差/反馈中断时，应按已确定的现场恢复方式处理，不由软件猜测恢复动作。本轮没有验证机器人固件命令超时、急停或weight0后的物理行为。

## 首次右侧小幅测试的具体步骤

1. **先解决当前阻碍：** 核对吊装/支撑与G1当前模式，确认此现场适合Regular mode下的arm_sdk测试；只读记录当前14q/dq、mode_pr/mode_machine/tick。核对只有一个arm_sdk写入者，明确现场停止/恢复操作者。
2. 保持输出禁用，固定右腕SN，检查前后/左右/上下方向及10cm尺度；只做位置IK，左臂和右腕旋转保持参考。完成输入有效性、失联和按住使能验证；没有confidence时不虚构它。
3. 先审阅**右肘单关节**测试：从当前14q建立参考，其他13关节不变；目标只让index10增加0.01rad，0.1rad/s限速，最大参考偏移仍0.03rad。arm首帧weight0，约2秒原位渐入。尚未逐关节确认方向前，不直接接完整右臂IK。
4. 运行时逐帧观察实测q/dq、命令差、weight、deadman年龄和反馈年龄；目标到达后按已确定步骤显式渐退，验证weight0之后的实际控制权移交。首次不要用拔Ethernet来代替经审阅的停止程序。
5. 上述实机验收通过后才扩展为右臂7关节 `active_indices=7..13`、短距离腕部位置跟随；再次验证左臂不受目标输入影响、限速/限位与clutch无初始跳变。双腕输入和两臂协同另行验收。

## 验证范围

20项纯软件/fake publisher测试通过：实测q/weight0首帧、默认无publisher、显式配置与延迟构造、CRC在全部字段之后、motor索引/非臂字段、渐入限速、单关节范围、陈旧/重复/回退输入、tick回绕、mode变化、循环超时、松开锁存、手动渐退、渐退事件不突破输出频率、Write失败不重试、close不发隐藏命令。fake CRC验证调用顺序与字段，不等于已做真实SDK线格式或机器人固件验收。

本轮没有实测：Revo2负载下增益/零前馈效果、碰撞和动力学限制、吊装状态下Regular mode适用性、其他关节默认字段的固件行为、DDS单一身份认证、固件断流策略、weight0控制移交、物理停止性能。它们是明确的现场验收项，不是省略输出实现的理由，也不能由mock测试替代。
