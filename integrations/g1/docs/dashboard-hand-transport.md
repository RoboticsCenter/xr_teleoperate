# Dashboard 双手 Wi-Fi 输出

`scripts/dashboard_hand_transport.py` 提供电脑端 `HandTransport`，通过已固定主机密钥的 SSH 与 PC2 的 `scripts/pc2_hand_bridge.py` 通信。Wuji reader、置信度过滤、六通道映射及调用方反馈步长限制仍来自用户指定原项目；这里不做手指重定向。

```python
transport = HandTransport(password=password, max_step=.08)
transport.open(False, cancel_event=input_stop_event)  # 明确启动反馈服务，没有命令 subscriber/publisher
feedback = transport.snapshot()
transport.open(True)           # 仅 Hand START 调用：替换自己拥有的反馈服务
transport.send('right', q, speed, should_send=lambda: still_requested)
transport.hold('right')        # 停止这侧的新目标，不发送 home/默认零
transport.enable('right', True)
transport.close()              # 关闭自己的服务；不停止其他已有服务
```

构造和 import 不访问网络、DDS 或串口；`snapshot()` 在尚未 open 时返回空字典。凭据只保留在内存，并通过 SSH 认证输入及远端匿名 stdin 传递，不放入 argv、环境变量、日志或状态响应。

`snapshot()` 返回按 side 索引的字典；未收到的侧不出现。每侧包含 `q[6]`、`dq[6]`、`sequence`、`age_s`、`fresh`、`error`。年龄为 PC2 当前接收年龄加上整次请求 RTT，不相减两台机器的 monotonic 时间。原有 100ms 反馈要求保留；这不是手套中断的锁存门槛。

`send()` 成功返回 `published=true`、`side`、`dds_write_count`、`acknowledged=false`。输入/反馈被拒时返回 `published=false` 和原因，只影响这一条/这一侧。取得 transport 锁后、写入 SSH 前会再次调用可选 `should_send`，使排队命令在 STOP 后失效。`close()` 先设置取消标志，再等待 I/O 锁；协议和认证等待每 250ms 检查取消，认证总等待最长8秒。`open` 可接受输入生命周期的 `cancel_event`，防止尚未开始 open 的任务在 STOP 后重新打开连接。

PC2 在发布之前再次检查该侧本次反馈、原六通道范围 `[.1,0,0,0,0,0]` 至 `[.9,.95,.95,.95,.95,.95]`，以及相对实测位置的步长。`max_step=.08` 可保持调用方本次原设置的80/1000限制。DDS publisher 初始化之后重新检查，防止初始化等待使反馈过期。DDS 写入失败的侧被阻止继续输出并报告 delivery unknown，另一侧仍可继续；新的服务实例才清除这种硬件传输故障。手套无效帧、输入中断、反馈暂缺不使用这个硬故障状态，有效反馈/目标可恢复。

## 隔离服务

基线是已审阅和实测反馈的 `/home/unitree/g1-integration/brainco-feedback-20261111`，不是上游会主动发送默认零目标的原始服务。`patches/brainco-dashboard-hold-resume.patch` 仅应用到新的隔离构建目录：

- 删除旧200ms命令到期及无效输入的永久锁存，没有会话总时限。
- 没有收到目标时完全不写。无新目标/无效目标时保持上个目标；每侧反馈无效时该侧跳过输出，反馈恢复即可继续。
- 保留逐次验证 normalized unit、六通道硬件状态、反馈读取年龄和位置/速度范围；motor state 仅接受原 `RevoHand.state()` 的0/1/2，state3阻止该侧新输出。
- 只打开已验证 by-id 端点，并在建立工作线程之前核对 SN 和左右 SKU；不枚举新 ID/baud，也不更改设备配置。
- 正常 STOP 保留该服务已有的行为：如果此前输出过目标且退出时取得有效实测位置，尝试一次实测位置 hold。它是动作命令，不是物理停止确认。

| 侧 | 端点 | ID | SN |
| --- | --- | --- | --- |
| right | `usb-FTDI_Dual_RS232-HS-if00-port0` | 127 | `BCXTR2362J2500015` |
| left | `usb-FTDI_Dual_RS232-HS-if01-port0` | 126 | `BCXTL2204J2600021` |

启动前核对六份基线源码/库/IDL对象 SHA256，获取独占文件锁，拒绝已有 BrainCo 服务及被占用串口；不杀占用者。只对本实例的独立进程组执行有界关闭。没有 systemd 安装或系统 SDK 替换。

PC2 `--build-only` 已通过，**没有运行构建产物、DDS或实物串口**：

- 构建目录：`/home/unitree/g1-integration/brainco-dashboard-2a1b3ee5ab874168`
- 二进制 SHA256：`fc5a06cb645cf789d47c26150b01a54ff7a0cebc40c45e9c72a105218117b191`
- 只读 inventory 显示两路 by-id 存在、无 BrainCo 服务、基线哈希匹配。这是当次快照，不替代下一次 START 的检查。

11项 bridge 测试及12项 transport 测试通过，含真实 C++ gate 的离线假反馈编译运行、真实服务main.cpp的假SDK/DDS九场景（含motor state3拒绝）、1小时无命令过期、无效输入恢复、各侧独立、构造零输出、publisher初始化后重检、凭据传递、模式升级及 STOP 排队取消。未进行手部动作验证。

`dds_write_count` 只说明 DDS API 返回成功；native setter 返回 void，无法据此确认串口执行或物理运动。命令服务运行后的 `actual_write_count` 保持 null，`physical_stop_verified=false`。SSH keepalive 检测连接失效，但网络分区下远端何时收到 EOF 仍依赖 SSH/TCP，不能把电脑端关闭当作物理停止证明。

## 2026-11-11 OUTPUT ERROR 修复

PC2断电重启后，必须重建SSH手部连接；启动错误现在分类为固定的网络、认证、串口或服务提示，未知远端输出不直接回显。失败清理后可再次显式启动。

已知端点的初次身份读取失败会最多尝试3次，间隔250ms，保留150ms FTDI稳定时间及完整SN/SKU核对。成功的侧不重连，不扫描其他端点/ID，不写设备配置。取消会清理已打开连接，不继续建立worker。启动日志分别记录open_failed、identity_read_failed、最终不可用。

DDS反馈来自整数0..1000寄存器，经float32归一化后，适配层先恢复整数寄存器，再调用原项目step_limited，避免617→616.99998→536造成81格越过80格步长。Wuji读取、重定向算法和原限制未改。

已部署隔离构建`brainco-dashboard-7ddacd8ec761a4e4`，binary SHA256 `653e9999f4db6a8e9a4422de717e6e4f2dd1f08bfc692c9f8e99c8c18cd4ce02`。145项dashboard、12项bridge（含实际C++源码的假SDK）、16项UI测试通过。更新后实收左右持续新鲜反馈、手套输入有效，两侧校准文件hash不变；当前恢复的是feedback-only模式，未通过新版代码验收实物双手跟随。临时目标拒绝显示黄色保持和具体原因；真正SSH/DDS故障仍为红色。
