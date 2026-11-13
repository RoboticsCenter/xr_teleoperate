# PICO 双腕数据验证

当前里程碑是两只腕部 Motion Tracker 控制 G1 双臂；手套和手指链路并行推进。

## 当前实测

用户选择先用右腕，在 **Object** 模式收到唯一有效Tracker **`PC2310MLL9241957G`**：1221包/13.60秒，约89.68Hz，位姿连续变化。`Motion.len=1`、数组长度2，旧尾部 `PC2310MLLA050051G`被排除。此条目不能被认作已验证的左腕输入。记录：`diagnostics/pico-right-validation-2026-11-11.json`。

右腕接收已通；当前发布版没有传confidence/传感器时间戳，单位、坐标方向和安装偏移未完成现场标定。用户后续选择吊装下的开发模式右臂路线，当前机器人模式和输出进展以 `PLAN.md` 与 `docs/teleop-g1-readiness.md` 为准；本文件的录制与纯输入测试不代表已验收硬件运动。

### 先前的连接恢复记录

2026-11-11 头显重新连接 `192.168.31.92` 后，已确认 `.31.46` 到 PCService `63901` 的 TCP 连接。10秒内收到854个完整跟踪报文，解析失败和接收队列丢弃均为0；报文包含 Head、Controller、Hand、Input、appState，**没有 Motion，也没有 Body**。因此网络已恢复，独立 Tracker 流尚未接通。配对完成不等于应用已选择并成功启动独立跟踪。

证据：`diagnostics/pico-raw-reconnected-2026-11-11.jsonl` 与 `diagnostics/pico-raw-packets-2026-11-11.jsonl`。后续已确认官方实际UI是 **None / Full-body / Object**，Object对应源码Motion枚举；用户先选Full-body后改为Object并读通右腕。

## 完整报文接收器

`scripts/pico_raw_probe.py` 只绑定官方原生SDK的 `PXREAInit/PXREADeinit`，注册接收回调，不调用设备控制API或任何机器人SDK。每个JSON样本对应一个完整收到的报文；没有把多个缓存getter拼成同一帧。回调复制数据后交有界队列，退出汇报丢弃数；独立工作进程保证native初始化/退出有时间上限。

```bash
cd /home/jack-xiang/unitree-g1
python3 scripts/pico_raw_probe.py --seconds 10 \
  --expected-trackers 1 \
  --output diagnostics/pico-packets.jsonl \
  --raw-output diagnostics/pico-packets-raw.jsonl
```

与 `pico_probe.py` **只能选一个运行**；PCService版本同时只保留一个SDK接收者。此入口按固定官方native库SHA256核对，库来自 Pybind commit `8941e1dbbf4313fe83846f82984eaa8c992690bf` 的vendor。

`--expected-trackers`默认2；当前单腕测试显式设为1。初次右腕记录仍使用默认2，因此程序退出1，但报文的有效一腕数据已独立统计；不把退出码1误解为没有接收到右腕。

解析时以 `Motion.len` 为有效条目数，不读取被客户端复用的旧数组尾部；检查SN唯一、7维位姿有限且四元数合理、应用时间戳前进。静止Tracker不因位姿相同被误判失联。未传入的confidence和传感器时间戳保持未知；网络回调接收时间不能被称为传感器采样时间。`motion_ready=false` 始终保留，不能把诊断成功当成运动授权或全部控制条件已满足。

19项纯解析测试覆盖零/减少len、旧尾部、身份重复、非有限位姿、时戳回退、设备会话、缺少Motion、未知confidence，以及下述同包控制器使能字段：

```bash
python3 -m unittest discover -s tests -p test_pico_raw_probe.py -v
```

## 下一步验收

1. 收到两只独立Tracker的不同SN与位姿；逐只移动确认左右侧，不用数组顺序绑定。
2. 核对方向、长度单位和坐标约定；记录佩戴安装偏移与相对初始姿态。
3. 验证Send关闭、失去焦点、断连与Tracker遮挡时收到什么；发布版没有发送confidence时明确保留限制，不伪造valid标记。
4. 用真实录制包与G1当前实测14关节姿态进行官方IK预览，检查单轴移动、左右侧与工作范围。
5. 在确认所选机器人模式、姿态和控制权后，进行明确持续使能、限幅的小范围单臂验证，再扩到双臂。开发模式与Regular模式使用不同输出路径；输入诊断入口不发布命令。

已审查的客户端独立模式请求数量为ONE，两只Tracker可能需要本项目 `patches/pico-two-trackers.patch`；这是已验证适用的源码补丁，尚未Unity构建，不能宣称已安装生效。先以实际Mode/TrackerNum和原始包核实原因。

用户已实测确认Object只允许一个。完整双腕工程和有效性补丁已准备，见[双腕APK构建说明](pico-two-tracker-build.md)；当前缺Unity/Android构建与激活环境，尚无新APK。

## 右腕录制到受限右臂 IK 预览

```bash
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
~/.cache/unitree-g1-arms/env/bin/python scripts/preview_pico_single_arm.py \
  --pico-jsonl diagnostics/pico-right-object-2026-11-11.jsonl \
  --robot-state diagnostics/pc2-feedback-service-verified-2026-11-11.json \
  --side right --tracker-sn PC2310MLL9241957G --position-scale 0.02 \
  --output diagnostics/pico-right-ik-small-trial-2026-11-11.json
```

入口复用官方模型与IK误差项，左臂和右腕朝向固定。此小幅试验将姿态正则中心从零位改为录制实测姿态，并加每关节±0.03rad窗口；不修改上游代码。默认10%位置比例的6.1mm目标未通过严格2mm残差验收；2%比例的1.22mm目标残差0.857mm，最大关节差0.00812rad。不同结果均保留，不能用优化器success替代验收。5项预览测试通过，另有20项输出门控测试通过。

XR→机器人轴旋转和米单位目前是明确标记的预览假设，不是假装已完成的物理标定。使用的是保存的G1反馈，不是实时反馈；两个阶段均无机器人访问或输出。实际控制还需[输出门控与控制权验收](arm-command-integration.md)。

官方依据：[客户端界面与操作](https://github.com/XR-Robotics/XRoboToolkit-Unity-Client)、[固定版Tracker发送源码](https://github.com/XR-Robotics/XRoboToolkit-Unity-Client/blob/v1.1.1/Assets/Scripts/TrackingData.cs)、[SDK回调ABI](https://github.com/XR-Robotics/XRoboToolkit-PC-Service-Pybind/blob/8941e1dbbf4313fe83846f82984eaa8c992690bf/vendor/include/PXREARobotSDK.h)。

## 同一原始包的右控制器使能

2026-11-11 核对未打补丁的官方 Unity commit `cdc53166b0bf412efae71046c6a225eb5091605f`：右控制器来自 `XRNode.RightHand`，写入 `Controller.right`；`trigger` / `grip` 是模拟量，读取函数的成功返回值没有被发送，设备连接有效标志也未发送。`appState.focus` 来自应用焦点，顶层时间戳是应用生成时间。[控制器与焦点源码](https://github.com/XR-Robotics/XRoboToolkit-Unity-Client/blob/cdc53166b0bf412efae71046c6a225eb5091605f/Assets/Scripts/TrackingData.cs#L286)

`Input=1` 表示 ControllerActive；0 表示头显，2 表示手部追踪。这个模式枚举不是右控制器已连接证明。[官方枚举定义](https://github.com/XR-Robotics/XRoboToolkit-Unity-Client/blob/cdc53166b0bf412efae71046c6a225eb5091605f/PICO%20Unity%20Integration%20SDK/Runtime/Scripts/Features/PXR_HandTracking.cs#L35)

原始记录 `diagnostics/teleop-pico-current-raw-20261111-1810.jsonl` 离线重解码854包：焦点均true、Input均1；右trigger范围0–1，其中39包达到0.8，815包低于0.8；右grip均0。每包有效Tracker数为1，SN `PC2310MLL9241957G`；数组第二项仍排除。文件SHA256：`0b4f7987eb05ea93dde12fae839668b1ed9522b45b4c0debfba4b72128f14378`。

`pico_raw_probe.py` 默认 `--enable-control trigger --enable-threshold 0.8`。每个样本新增 `controller_enable`：

| 字段 | 约定 |
| --- | --- |
| source_id / source | `pico_right_trigger` / `same_native_packet.Controller.right` |
| sequence / local_monotonic_ns / app_timestamp_ns | 原始整包的序号、本机接收时间、应用时间；与Tracker共用，不重新生成 |
| trigger / grip | 严格0–1有限数值；布尔、字符串、缺值、NaN均变为null |
| threshold / control | 默认0.8 / trigger；选择grip时不与trigger做OR |
| held / status | 同包焦点true、Input1、选定模拟量达到阈值才held；正常松开false/released；失焦、非控制器模式或包错误false/inhibited；缺字段null/unavailable |
| policy_origin | `receiver_defined_not_vendor_deadman`，接收端定义的按钮规则 |
| vendor_controller_valid / feature_query_success | null，原APK未发送 |
| receiver_age_checked | false；解析阶段不把录制文件当作当前新鲜输入 |

顶层同时提供 `app_focused` / `input_mode`。不跨帧保留按钮值，不用手部姿态或grip猜测trigger。Tracker的 `tracking_valid` 与confidence保持原值；缺失仍为null。

## 显式的10秒监督位置测试策略

`scripts/pico_supervised_input.py` 是纯函数式状态检查类，不连接硬件，不发布命令。只有运行器显式选择 `supervised_position_only_v1` 才使用它；厂商有效性策略仍应作为其他场景默认。调用合同：

```python
policy = SupervisedInputPolicy(sn='PC2310MLL9241957G')
policy.set_reference(reference)  # 与实时IK完全相同的新鲜14关节reference
decision = policy.evaluate(packet, ik_result, now_ns=time.monotonic_ns())
# 转发 supervised_input_accepted；不得把 tracking_valid 从 None 改为 True。
```

`set_reference` 校验live参考、14个有限关节值、右SN、唯一reference_id、本机时钟域、明确的米单位和正交右手轴矩阵。参考实时年龄由状态桥与IK验证；此类首次Tracker包不得早于参考、不得比参考晚超过200ms。现场轴标定尚未验证时原样报告 `axes_physically_verified=false`，不伪造标定完成。

每次决策同时核对：

- 包来自原生原子回调，结构无错误，固定头显与右Tracker SN，Motion.len严格为1，位置不为近零哨兵且四元数合理；任何显式tracking invalid立即拒绝。已有有效厂商元数据时应切回厂商策略，不走此未知质量分支。
- 原始本机接收年龄≤100ms，序号、本机接收时间和应用时间严格递增；帧间隔≤200ms，位移跳变≤0.1m，旋转跳变≤0.5rad。静止时位姿可重复。
- 同包右trigger、焦点true、Input1，严格模拟量与held一致；trigger达到至少0.8才使能。控制器与IK的源序号、原始接收时间、应用时间必须逐项等于当前包。
- IK使用同一reference/SN/轴配置；首帧必须是原点且等于14关节参考。之后左臂固定、朝向固定，比例严格0.02，按配置轴重算位移并径向限1cm，关节相对参考限±0.03rad；左右位置残差≤2mm、旋转残差≤0.03rad。

状态行为：有效但松开trigger的初始及后续包更新origin/previous，输出 `state=WAIT`、`reason=waiting_for_trigger`、`supervised_input_accepted=false`，不启动10秒计时。首次按住以该包原始接收时间开始窗口；`ACTIVE` 内必须持续按住。开始后松开、输入错误或10秒到期输出false并锁存；重启需要新的唯一参考。普通输入拒绝返回字典，不抛异常；参考配置错误抛 `ValueError`。

输出始终保留 `tracking_valid=null`、`vendor_tracking_valid=null`、`tracker_sample_freshness_verified=false`、`controller_connected_verified=false`、`motion_ready=false`。即使 `supervised_input_accepted=true`，也只表示满足这个有限监督规则；机器人新鲜反馈、模式、控制归属、nonce时效、限速、增益和停止行为仍由独立输出层验证。

已知限制：原客户端不发送Tracker confidence或每Tracker采样时间；当选定Motion但底层模式改变时，还存在保留旧Motion字段的路径。因此整包时间前进、focus、len和SN不能证明传感器样本仍有效。数值静止同时应用时间前进可能是正常静止，也可能是缓存冻结，本策略无法区分，不能宣称遮挡/断连已可靠检测。按钮持续监督与10秒小幅边界是本次显式测试约束，不补齐上述厂商信息。[原Motion发送及保留路径](https://github.com/XR-Robotics/XRoboToolkit-Unity-Client/blob/cdc53166b0bf412efae71046c6a225eb5091605f/Assets/Scripts/TrackingData.cs#L128)

18项策略测试通过，包括WAIT超过10秒仍不消耗ACTIVE窗口、ACTIVE松开锁存、同包三种身份匹配、保留未知/拒绝显式invalid、重放/跳变/过期、定量映射与关节/残差边界。19项解析测试包括上述854包离线统计；这些均为软件验证，没有新增SDK接收器或机器人输出。

```bash
python3 -m unittest discover -s tests -p test_pico_raw_probe.py
python3 -m unittest discover -s tests -p test_pico_supervised_input.py
```
