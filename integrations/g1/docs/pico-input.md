# PICO 输入接入 G1 29DoF

核对日期：2026-11-11。本文是源码审计和实施设计，未安装 PICO 驱动、未连接 PICO 实机、未发送机器人运动指令。本文审查未修改网络，主机后续诊断配置见 PLAN.md。手指继续使用现有 Wuji→BrainCo 项目，本文只处理手臂位姿输入。

用户已确认左右手腕各一个 Motion Tracker，Wuji 手套负责手指。因此正式腕部输入采用独立 Tracker 流，按 SN 固定左右侧；不以 full-body 模式替代。控制器位姿仅作为可选调试基线。

## 已确定的接入路线

```text
PICO 头显上的 XRoboToolkit Unity Client
  → 电脑上的 XRoboToolkit PC Service
  → Python xrobotoolkit_sdk
  → 逐设备有效性检查 / 左右手绑定 / 坐标与安装偏移转换 / clutch
  → 宇树 G1_29_ArmIK.solve_ik
  → 独立的 G1 控制输出层

Wuji 手指输入 → 现有手指映射 → BrainCo 输出（由另一条链负责）
```

PICO 官方入口明确指向 XRoboToolkit。安装 Python 包并不能替代 PC Service 和头显端 App。具体 PICO 型号、系统版本和两只 Tracker 的 SN 尚未确认，不应默认所有 PICO 头显都具有同样支持。见 [PICO 官方入口](https://github.com/Pico-Developer/XRoboToolkit/blob/8aa00cc86a5e029d04275bf794ed9a68a5b902c0/README.md)、[Python 组件依赖](https://github.com/XR-Robotics/XRoboToolkit-PC-Service-Pybind/blob/8941e1dbbf4313fe83846f82984eaa8c992690bf/README.md)。

## 三种输入不能混用

| 输入 | 实际输出 | 本项目用途 |
| --- | --- | --- |
| 左右控制器 | 各一个 6DoF 位姿，另有 grip/trigger/button | 可选调试基线；正式腕部输入已选手腕 Tracker，grip 是否作 clutch 待定 |
| 独立 Tracker / Object Tracking | 以序列号标识的各 Tracker 6DoF 位姿 | Tracker 固定在手背/手腕时可替代控制器位姿；固定在肘部时只能提供肘部辅助信息 |
| Full Body Tracking | 24 个人体骨架节点 | 是另一套人体骨架估计；不能按独立 Tracker 的 SN/数组接口读取 |

Unity 当前源码以 `TrackingType.Motion` 和 `TrackingType.Body` 分支分别生成 `Motion`、`Body`。Full Body 的左右 wrist 是 20、21，左右 hand 是 22、23；这不意味着已经测得两个独立腕部 Tracker。官方 G1 **仿真**示例采用控制器控制末端、两个 Tracker 辅助肘部，不能宣称它已经实现本项目的真机控制。见 [Unity 数据分支](https://github.com/XR-Robotics/XRoboToolkit-Unity-Client/blob/cdc53166b0bf412efae71046c6a225eb5091605f/Assets/Scripts/TrackingData.cs#L110)、[人体骨架示例](https://github.com/XR-Robotics/XRoboToolkit-PC-Service-Pybind/blob/8941e1dbbf4313fe83846f82984eaa8c992690bf/examples/example_body_tracking.py)、[G1 仿真示例](https://github.com/XR-Robotics/XRoboToolkit-Teleop-Sample-Python/blob/79e5cb8a56e3455515ce1b476e993c764ec58739/scripts/simulation/teleop_unitree_g1_placo.py)。

## 已核对的 Python API

导入：`import xrobotoolkit_sdk as xrt`；生命周期：`xrt.init()` / `xrt.close()`。

| 数据 | API | 返回形态/注意事项 |
| --- | --- | --- |
| 控制器/头显位姿 | `get_left_controller_pose()`、`get_right_controller_pose()`、`get_headset_pose()` | 每个为 `[x,y,z,qx,qy,qz,qw]` |
| 控制器按键 | `get_left_grip()`、`get_right_grip()`、`get_A_button()`、`get_X_button()` 等 | 不能把读取到的缓存按键当作新帧证据 |
| 顶层时间 | `get_time_stamp_ns()` | 来自 Unity 顶层 `timeStampNs` |
| 独立 Tracker 数量 | `num_motion_data_available()` | 当前绑定最多返回 3 个；计数有下述缓存缺口 |
| 独立 Tracker 位姿 | `get_motion_tracker_pose()` | `NumMotionDataAvailable` × 7，最多 3 项；计数与缓存不保证设备仍在线 |
| 独立 Tracker SN | `get_motion_tracker_serial_numbers()` | 长度为 `NumMotionDataAvailable`，最多 3 项；必须按 SN 绑定角色，不按数组顺序绑定左右手 |
| 独立 Tracker 时间 | `get_motion_timestamp_ns()` | 只读取 `Motion.timeStampNs`，与当前 Unity 主分支存在兼容缺口 |
| Full Body | `is_body_data_available()`、`get_body_joints_pose()` | 24×7；available 不等于当前帧仍有效 |
| Full Body 时间 | `get_body_timestamp_ns()`、`get_body_joints_timestamp()` | 前者也存在子对象时戳缺口；后者是源 joint 时间，时钟语义另行确认 |

以上由 [Python 绑定实现](https://github.com/XR-Robotics/XRoboToolkit-PC-Service-Pybind/blob/8941e1dbbf4313fe83846f82984eaa8c992690bf/bindings/py_bindings.cpp) 和 [独立 Tracker 示例](https://github.com/XR-Robotics/XRoboToolkit-PC-Service-Pybind/blob/8941e1dbbf4313fe83846f82984eaa8c992690bf/examples/example_motion_tracker.py) 核对。

## 必须先解决的数据有效性缺口

这些是所列版本的静态源码发现，尚未证明用户当前安装的 APK/SDK 具有相同行为。

1. **旧数据会留在缓存。** Python 绑定在 server/device disconnect 回调中只打印信息，没有清空 pose、Tracker 数量或 `BodyDataAvailable`。
2. **缺少逐设备跟踪有效性。** 公开 Python API 没有 controller/tracker 的 tracking-valid getter。Unity 获取了 `MotionTrackerConfidence`，但没有将它写入 `Motion` 数据。`get_left_hand_is_active()` 是光学手部追踪状态，不能代替 Wuji、控制器或 Tracker 的状态。
3. **独立 Tracker 列表可能保留尾部旧值。** Unity 复用 `joints` 数组并更新 `len`，没有截短数组；Python 忽略 `len`，按 `joints.size()` 计算数量。这会影响 Tracker 掉线后识别。
4. **逐流时间戳可能始终为 0。** 所核对 Unity 代码只赋值顶层 `timeStampNs`；`Motion`/`Body` 未赋值各自的 `timeStampNs`。Python 的逐流 getter 只读子对象字段。
5. **读取不是原子快照。** pose、SN、count、timestamp 分别加锁读取，连续 getter 可能跨帧。对于机器人控制，应提供一次读取的完整快照。

最低改动建议：在输入层的实际使用版本补齐逐源序列号/采样时间、tracking validity/confidence、有效数组长度；在 Python 绑定暴露原子快照并在断连清零。PC 每收到有效的新源帧记录本机 `monotonic_ns()`；超时必须停止更新目标。仅检测顶层时戳变化不能证明某个 Tracker 仍更新；静止时 pose 不变也不能据此判定掉线。

出处：[Unity Tracker 数据生成](https://github.com/XR-Robotics/XRoboToolkit-Unity-Client/blob/cdc53166b0bf412efae71046c6a225eb5091605f/Assets/Scripts/TrackingData.cs#L164)、[Python 回调解析](https://github.com/XR-Robotics/XRoboToolkit-PC-Service-Pybind/blob/8941e1dbbf4313fe83846f82984eaa8c992690bf/bindings/py_bindings.cpp#L98)。

## 坐标、单位和时间

- **四元数顺序已确认：** 网络/Python pose 为 `qx,qy,qz,qw`。转换到需要 `wxyz` 的库时显式重排，禁止隐式猜测。
- **时间戳已确认：** Unity 顶层时间来自 `DateTime.UtcNow - Unix epoch`，转换为纳秒数值；它是设备 UTC 时钟，不是 PC 单调时钟，也不自动等于传感器采样时间。不能直接减 PC `monotonic_ns()` 算延迟。见 [Utils.GetCurrentTimestamp](https://github.com/XR-Robotics/XRoboToolkit-Unity-Client/blob/cdc53166b0bf412efae71046c6a225eb5091605f/Assets/Scripts/Utils.cs#L12)。
- **坐标转换有官方参考：** XRoboToolkit 示例的 `R_HEADSET_TO_WORLD = [[0,0,-1],[-1,0,0],[0,1,0]]`。它将 `[x,y,z]` 映射为 `[-z,-x,y]`，适合作为机器人“前、左、上”的候选转换；仍需在本机逐轴测试。见 [geometry.py](https://github.com/XR-Robotics/XRoboToolkit-Teleop-Sample-Python/blob/79e5cb8a56e3455515ce1b476e993c764ec58739/xrobotoolkit_teleop/utils/geometry.py)。
- **不同源要分别验证：** PICO 官方说明 Tracker `localLocation` 使用右手坐标；Unity Full Body 分支显式改了 z 和四元数符号，独立 Tracker 分支直接传原值。因此不能把某个源已验证的变换无条件复用到所有源。见 [PICO Object Tracking](https://developer.picoxr.com/document/unity/object-tracking/)、[Unity Full Body 转换](https://github.com/XR-Robotics/XRoboToolkit-Unity-Client/blob/cdc53166b0bf412efae71046c6a225eb5091605f/Assets/Scripts/TrackingData.cs#L278)。
- **长度单位：** 接入内部统一为米。此轮所读 XRoboToolkit 序列化层没有单位字段；上线前用实物移动 10 cm 核对是否得到约 0.1 的差值，并固化 SDK 版本和单位转换。不能把“预期为米”写成当前实测事实。
- 重定位、头显 recenter 或切换跟踪模式会改变参考坐标；检测到后失效本轮参考姿态，重新 clutch，不继续沿用旧偏移。

## 如何连接宇树 G1 IK

可复用的接口是：

```python
from teleop.robot_control.robot_arm_ik import G1_29_ArmIK

# 输入为机器人模型基坐标中的两个 4×4 齐次变换；q/dq 为当前双臂 14 关节。
sol_q, sol_tauff = arm_ik.solve_ik(T_left_target, T_right_target, current_q, current_dq)
```

`G1_29_ArmIK` 的 `L_ee/R_ee` 在 wrist-yaw joint 上额外平移 `[0.05,0,0]`。目标必须对准这个 IK frame，或明确修改模型末端；不能把 Tracker 位置直接当成掌心或 grasp TCP。现有 G1 IK 中 `scale_arms()` 调用被注释，因此人体/机器人缩放需要在输入适配层明确配置。其默认 URDF 是带宇树手的模型，Revo2 的末端几何与负载模型还需与 G1 输出负责人核对。

实际主循环另行调用 `arm_ctrl.ctrl_dual_arm(sol_q, sol_tauff)`，这是运动输出，不属于输入验证。不要为了检查 PICO 数据而构造实机 arm controller 或直接启动完整 `teleop_hand_and_arm.py`。参见 [G1 IK 实现](https://github.com/unitreerobotics/xr_teleoperate/blob/817fb00c63cde15e5f24a0f8fa08e1e33ed89d3b/teleop/robot_control/robot_arm_ik.py)、[主控制循环](https://github.com/unitreerobotics/xr_teleoperate/blob/817fb00c63cde15e5f24a0f8fa08e1e33ed89d3b/teleop/teleop_hand_and_arm.py#L362)。

建议初期使用“按住跟随，松开保持”的增量位姿模式。每次启用时保存有效的操作者参考位姿和机器人当前末端位姿，让第一帧目标等于机器人当前位置。

设原始 Tracker 到操作者腕部的固定安装变换为 `T_tracker_wrist`：先计算 `T_xr_wrist = T_xr_tracker @ T_tracker_wrist`。该偏移必须包含旋转，才能正确补偿 Tracker 绕手腕转动造成的位置变化。之后转到机器人参考轴，使用 `p_target = p_robot_ref + scale * (p_input - p_input_ref)`、`R_target = (R_input @ R_input_ref.T) @ R_robot_ref`。机器人 wrist→IK-frame 的固定偏移另算，不与 Tracker 安装偏移混在一起。左右侧各自标定。

## 最小实现与验收顺序

- [ ] 记录 PICO 型号/OS/APK 版本、PC Service/Python 版本、Tracker 数量/SN/佩戴位置。
- [ ] 新建只读 `pico_probe`：列出控制器、Tracker、原始位姿、时戳、有效性；保存短 JSONL，完全不导入 G1 控制器。
- [ ] 对照上述缺口验证用户实际版本；需要时修补输入序列化/绑定，先测掉线和重新连接。
- [ ] 定义统一 `PoseSample`：`source_type`、`source_id`、`side`、`position_m`、`quaternion_xyzw`、源 seq/time、PC 接收单调时间、valid/confidence、参考坐标标识。
- [ ] 做左右手、三个平移轴、三个旋转轴、10 cm 尺度、四元数归一化和 recenter 检查。
- [ ] 标定两侧 `T_tracker_wrist`、机器人末端偏移、初始姿态和缩放；保存 JSON/YAML，注明源 SN 和版本。
- [ ] 仅用 IK/可视化验证目标和 14 关节结果；加入 clutch、目标速度限制、工作空间限制和失联保持。
- [ ] 以实机反馈的当前末端作为启用参考，验证首次启用/再次启用无跳变后，交给 G1 输出阶段。

只有一个 Tracker 时，不能宣称它能独立提供左右两个手腕的 6DoF；可继续以双控制器作腕部输入，把 Tracker 用于腰/肘等辅助用途，或补足所需追踪设备。

## 审计版本

以下是 GitHub `commits/main` 在核对时返回的提交，不是用户当前安装版本：

| 仓库 | Commit |
| --- | --- |
| Pico-Developer/XRoboToolkit | `8aa00cc86a5e029d04275bf794ed9a68a5b902c0` |
| XR-Robotics/XRoboToolkit-PC-Service | `85bac4dbc1fd5cef42c74a160d9c30aa3491f122` |
| XR-Robotics/XRoboToolkit-PC-Service-Pybind | `8941e1dbbf4313fe83846f82984eaa8c992690bf` |
| XR-Robotics/XRoboToolkit-Unity-Client | `cdc53166b0bf412efae71046c6a225eb5091605f` |
| XR-Robotics/XRoboToolkit-Teleop-Sample-Python | `79e5cb8a56e3455515ce1b476e993c764ec58739` |
| unitreerobotics/xr_teleoperate | `817fb00c63cde15e5f24a0f8fa08e1e33ed89d3b` |
