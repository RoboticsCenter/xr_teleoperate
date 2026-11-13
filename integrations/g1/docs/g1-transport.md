# G1 29DoF / BrainCo Revo2 通信接入核对

核对日期：2026-11-11。本文主体是固定版本源码审计。后续已登录 PC2、验证本体反馈并识别 FT2232H 双串口；最新现场证据见 [PLAN.md](../PLAN.md)。源码审计与现场验证分别记录，尚未启动手部控制服务或发布运动指令。

## 1. 已确定的接口选择

复用 `~/branco-wuji-teleop/brainco-wuji-teleop` 的 Wuji 输入、标定、重定向和本地限制。这里仅解决其六通道结果怎样到达 G1 上的 Revo2，不重新计算手指姿态。

建议的 RS-485 路径：

```text
现有 Wuji→六通道目标（0..1000）
    → 新 DDS 输出适配器（0..1）
    → G1 PC2 上唯一一个 brainco_hand_server
    → 两路 USB/RS-485 → 左右 Revo2
```

这条路径的前提是实物使用官方 RS-485 接法。若实际上仍使用 DaMiao CAN-FD，优先保留原驱动，在适配器所在计算机运行现有程序；不要仅因 G1 可通过 Ethernet 访问就假定手也使用 Ethernet 或 RS-485。

最初网络现场为 UGREEN `enx6c1ff762d908=10.42.0.1/24`，与 G1 子网不同。后续已临时增加 `192.168.123.10/24` 并登录 PC2 `.164`；本体反馈有效，手部反馈尚待验证。`.120` 身份仍未知。

## 2. 固定核对版本

以下是本次读取的上游版本，不表示项目必须永久使用这些版本；更新时需重做启动行为和字段语义检查。

| 仓库 | 核对 commit |
|---|---|
| [unitreerobotics/brainco_hand_service](https://github.com/unitreerobotics/brainco_hand_service/tree/d71996b6999edb2f838a3dca3d9621429a2ef966) | `d71996b6999edb2f838a3dca3d9621429a2ef966` |
| [unitreerobotics/xr_teleoperate](https://github.com/unitreerobotics/xr_teleoperate/tree/817fb00c63cde15e5f24a0f8fa08e1e33ed89d3b) | `817fb00c63cde15e5f24a0f8fa08e1e33ed89d3b` |
| [unitreerobotics/unitree_sdk2_python](https://github.com/unitreerobotics/unitree_sdk2_python/tree/814556d15970dd2ecf1c9984e845ca02ab07e206) | `814556d15970dd2ecf1c9984e845ca02ab07e206`，默认分支是 `master` |
| [BrainCoTech/unitree-g1-brainco-hand](https://github.com/BrainCoTech/unitree-g1-brainco-hand/tree/e93b3693e3dad5dd1bedb7f2a5999b5990aa0b3a) | `e93b3693e3dad5dd1bedb7f2a5999b5990aa0b3a` |

## 3. DDS 合同

实机使用 domain `0`，宇树 XR 仿真使用 domain `1`。主机初始化应显式指定 `enx6c1ff762d908`；机器人端服务需要指定机器人自己的网络接口，不能照抄主机接口名。

| 内容 | Topic | Python IDL |
|---|---|---|
| 左手指令 | `rt/brainco/left/cmd` | `unitree_sdk2py.idl.unitree_go.msg.dds_.MotorCmds_` |
| 左手反馈 | `rt/brainco/left/state` | `unitree_sdk2py.idl.unitree_go.msg.dds_.MotorStates_` |
| 右手指令 | `rt/brainco/right/cmd` | `MotorCmds_` |
| 右手反馈 | `rt/brainco/right/state` | `MotorStates_` |
| G1 全身反馈 | `rt/lowstate` | `unitree_sdk2py.idl.unitree_hg.msg.dds_.LowState_` |
| G1 常规运动模式中的上肢指令 | `rt/arm_sdk` | `unitree_sdk2py.idl.unitree_hg.msg.dds_.LowCmd_` |
| G1 调试／低层指令 | `rt/lowcmd` | `unitree_hg.msg.dds_.LowCmd_` |

手部采用 **unitree_go** 消息，G1 本体采用 **unitree_hg** 消息，不可混用。`MotorCmds_.cmds` / `MotorStates_.states` 都是可变长 sequence；应用应要求长度恰好为 6，且所有使用的数值有限。消息本身不附带手的序列号、固件版本或应用层时间戳。

手部的服务端解释如下：

| DDS 字段 | 服务行为 | 适配器要求 |
|---|---|---|
| `cmds[i].q` | 限制至 `[0,1]`，乘 `1000` 后转整数，作为位置 | 将现有 `0..1000` 目标除以 `1000`；继续使用原有更窄的目标范围 |
| `cmds[i].dq` | 限制至 `[0,1]`，乘 `1000`，作为速度参数 | 是归一化手指速度参数，不是关节 rad/s，也不是运动持续时间 |
| `states[i].q` | 原始位置除以 `1000` | 转回原项目单位后做 measured-position 步长约束 |
| `states[i].dq` | 原始速度除以 `1000` | 保留归一化语义 |
| `states[i].tau_est` | 原始电流除以 `1000` | 此服务填的是缩放电流，不要当成 N·m 力矩 |

序号合同为 `[Thumb, Thumb_aux, Index, Middle, Ring, Pinky]`。宇树控制器源码部分注释与 thumb/aux 的名称对应存在不一致，因此应保持原项目的已验证六通道顺序，并在实机逐通道验收中核对 0/1 通道；不能根据英文变量名自行交换。

直接来源：[服务端 main.cpp](https://github.com/unitreerobotics/brainco_hand_service/blob/d71996b6999edb2f838a3dca3d9621429a2ef966/main.cpp)、[Python 手控制器](https://github.com/unitreerobotics/xr_teleoperate/blob/817fb00c63cde15e5f24a0f8fa08e1e33ed89d3b/teleop/robot_control/robot_hand_brainco.py)、[SDK MotorCmds IDL](https://github.com/unitreerobotics/unitree_sdk2_python/blob/814556d15970dd2ecf1c9984e845ca02ab07e206/unitree_sdk2py/idl/unitree_go/msg/dds_/_MotorCmds_.py)、[SDK MotorStates IDL](https://github.com/unitreerobotics/unitree_sdk2_python/blob/814556d15970dd2ecf1c9984e845ca02ab07e206/unitree_sdk2py/idl/unitree_go/msg/dds_/_MotorStates_.py)。

## 4. 官方服务的启动与失联行为

`brainco_hand_server` 扫描 `/dev/ttyUSB*`、`/dev/ttyHAND*`、`/dev/ttyUN*`，使用 `460800` baud、左手 ID `0x7e`、右手 ID `0x7f`，并按 SKU 判断左右。其每只手运行独立工作线程，目标周期为 10 ms；串口调用耗时可能降低实际频率。

**启动该服务不是只读探测：** 连接阶段会写入 normalized unit mode；工作线程随即循环发送位置／速度，再读取反馈。DDS 初始指令默认零位置，速度设为 `1`。因此只启动服务而不发布手指令也可能造成手部动作。

本次审阅的 `main.cpp` 未见命令过期检测：发布者退出后，服务仍可能反复发送最后一个目标。软件正常停机可显式发送最新测量位置作为保持目标，但主机崩溃／断网时该消息无法送达。正式实机闭环需核对设备端现有行为，并为服务端增加可验证的命令超时处理，或证明部署版本已具备等价机制。不能把停止 DDS 发布等同于手部立即停止。

此外，DDS 接口未暴露原项目可能依赖的序列号／电流保护／turbo 等配置校验，也没有逐条硬件执行确认。`Write()` 返回成功仅能说明发布调用的结果，不能记录为“手部已执行并确认”。RS-485 服务的 `positions_and_speeds` 与原驱动的 duration 语义也需要单独验收，不能机械替换。

来源：[服务实现](https://github.com/unitreerobotics/brainco_hand_service/blob/d71996b6999edb2f838a3dca3d9621429a2ef966/main.cpp)、[SDK ChannelPublisher](https://github.com/unitreerobotics/unitree_sdk2_python/blob/814556d15970dd2ecf1c9984e845ca02ab07e206/unitree_sdk2py/core/channel.py)。

## 5. G1 arm / standby 语义

推荐先以固定站姿完成双臂和双手遥操作。是否采用 `rt/arm_sdk` 路径，需与机器人当前常规运动模式及现有控制服务匹配；本阶段不添加行走输入。

在所核对的官方实现中：

- `--motion` 使 G1_29 控制器发布 `rt/arm_sdk`；它用于与本体常规运动控制共存。代码注释要求 G1 Regular mode（R1+X），不支持 Running mode（R2+A）。启用该选项不代表必须发出行走指令，但官方 controller 模式会接入 locomotion 输入，集成时需显式禁用。
- 不使用 `--motion` 时主程序调用 `Enter_Debug_Mode()`，内部会 `ReleaseMode()`，再用 `rt/lowcmd`。这属于控制模式改变，不是网络测试。
- `G1_29_ArmController` 构造函数收到反馈后立即启动约 250 Hz 发布线程；初始 `q_target` 是 14 个零。主程序随后才等待用户按 `r`。因此 `READY` / 等待 `r` 只表示尚未开始跟随 XR，不能视作“未发运动指令”。
- `ctrl_dual_arm_go_home()` 会主动归零；主程序 `finally` 调用它。退出程序不等价于原位保持。
- 左臂使用 motor index `15..21`，右臂 `22..28`。实际运动前需读取 `mode_machine` 并保持 CRC 等官方协议细节；优先复用控制实现，但修改其启动／停机接口，使网络订阅、预览、持位和开始跟随分离。

所以只读工具只创建 `ChannelSubscriber`，不能通过构造 `G1_29_ArmController` / `Brainco_Controller_*` 来读取状态，也不能运行完整遥操作脚本作为 dry-run。

来源：[robot_arm.py](https://github.com/unitreerobotics/xr_teleoperate/blob/817fb00c63cde15e5f24a0f8fa08e1e33ed89d3b/teleop/robot_control/robot_arm.py)、[主程序](https://github.com/unitreerobotics/xr_teleoperate/blob/817fb00c63cde15e5f24a0f8fa08e1e33ed89d3b/teleop/teleop_hand_and_arm.py)、[motion_switcher.py](https://github.com/unitreerobotics/xr_teleoperate/blob/817fb00c63cde15e5f24a0f8fa08e1e33ed89d3b/teleop/utils/motion_switcher.py)。

## 6. 只读检查及单一控制者

以下是查看服务、进程和设备节点的命令示例。后续现场检查已完成其中的服务/进程/设备盘点，结果见 PLAN；不能假定示例中的服务已经安装：

```bash
systemctl status brainco_hand.service --no-pager
systemctl cat brainco_hand.service
journalctl -u brainco_hand.service -n 80 --no-pager
pgrep -af 'brainco|stark|teleop_hand|teleop_hand_and_arm|arm_sdk'
ls -l /dev/serial/by-id
```

`brainco_hand.service` 来自上游 [setup_autostart.sh](https://github.com/unitreerobotics/brainco_hand_service/blob/d71996b6999edb2f838a3dca3d9621429a2ef966/setup_autostart.sh)。不能假设这台机器人已采用此服务名：若不存在，继续查看进程和部署目录。不要执行 `setup_autostart.sh` 作为检查，它会安装并启动服务。

只读 DDS 检查应以独立短时进程初始化 domain 0、指定网卡，并订阅 `rt/lowstate` 及两路手 `state`。记录每路首包时间、接收计数、到达频率、最后包距现在的时间、左右手各六个位置；以本机 monotonic 接收时间判断新鲜度。没有手反馈时报告缺失，不自动启动驱动。

DDS 不自动保证指令 Topic 只有一个 publisher。保留现有 Wuji 映射后，不能同时运行宇树 `--ee brainco` 控制器，否则双方会写同一组手指令。每个手硬件只保留一个底层总线驱动；现有 CAN 驱动、ROS 2 驱动和 DDS bridge 不应同时占有同一设备。原项目的本地适配器锁无法约束另一台电脑上的 DDS publisher，正式启动还需要记录唯一发布者及运行实例。

BrainCo 的 G1 ROS 2 工程是可替代的集成路线，不应与宇树 XR 全套控制服务未经核对地同时启动。其 README 特别说明上肢控制启动前会禁用内置 Unitree arm control service；说明部署时必须先识别现有服务所有权。[BrainCo G1 README](https://github.com/BrainCoTech/unitree-g1-brainco-hand/blob/e93b3693e3dad5dd1bedb7f2a5999b5990aa0b3a/README.md)

## 7. 下一步与验收

| 顺序 | 工作 | 完成证据 |
|---|---|---|
| 1 | 对 UGREEN 接口配置同子网地址；保持 Wi-Fi 默认路由 | G1 PC2 单播可达、SSH 身份明确；记录实际机器人型号和服务版本 |
| 2 | 识别手接线、现有驱动和左右手序列号／固件 | 明确走原 DaMiao CAN-FD 还是官方 RS-485/DDS；不靠 IP 猜手身份 |
| 3 | 建立独立只读 DDS 诊断 | 三路状态分别统计；零 command publisher；数据缺失可正常超时退出 |
| 4 | 做现有六通道输出的 DDS 编码适配 | 离线验证顺序、0..1000↔0..1、六元素与非有限数拒绝、左右手不串线；不引入第二套手指算法 |
| 5 | 补足驱动能力差异 | 硬件配置验证、反馈过期、命令超时、正常停机和断网行为均有明确实现与验证方式 |
| 6 | 合并 G1 双臂与 PICO 输入 | 预览阶段无发布者；启动从实测姿态接管；单一发布者；不把原脚本的 READY/退出归零当作所需行为 |
| 7 | 分阶段实机验收 | 单手逐通道→双手→单臂→双臂合并；记录实际更新率、起停跳变、失联恢复是否需要重新启用 |

截至后续现场复查，同子网通信、PC2 软件盘点和 FT2232H 双串口枚举已完成；手部身份、有效手状态、DDS 输出适配和运动验证仍未完成。源码审计不能替代这些现场证据。
