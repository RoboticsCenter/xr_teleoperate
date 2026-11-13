# Revo2 RS-485 只读身份与状态探测

2026-11-11。入口：[`scripts/brainco_serial_probe.py`](../scripts/brainco_serial_probe.py)。本工具使用 Linux Python 3.8+ 标准库，通过 Modbus RTU 读取固定寄存器；不依赖 PySerial、BrainCo SDK 或预编译 `.so`。

## 为什么不直接运行官方示例

BrainCo 官方 `bc_stark_sdk` 提供 `modbus_open`、`get_device_info`、`get_finger_unit_mode` 和 `get_motor_status` 等 API，但核心实现由预编译库提供，仅阅读 Python 示例不足以保证所有底层调用都没有副作用。`python/revo2/revo2_utils.py` 的 `open_modbus_revo2` 还包括自动发现和 `set_hardware_type`，不适合用作这次最小、逐帧可审计的探测入口。

宇树 `brainco_hand_server` 的初始化会写 unit mode，随后循环发送目标，因此不能为了读取身份而启动它。本探测脚本直接使用官方寄存器定义，仅能生成以下读请求。

| 查询 | Function | 十进制地址 | Register 数量 | 解码 |
|---|---|---|---|---|
| 固件 | `04` Read Input | `3000` | 10 | 大端双字节拼接为最多 20 字节 ASCII |
| 序列号 | `04` Read Input | `3010` | 10 | 同上，保留完整字符串 |
| 左右手 | `03` Read Holding | `901` | 1 | `1=right`，`2=left`；未知值不作推断 |
| 当前单位模式 | `03` Read Holding | `937` | 1 | `0=normalized`，`1=physical`；只读，不更改 |
| 六通道状态 | `04` Read Input | `2000` | 24 | 6 位置、6 速度、6 电流、6 电机状态 |

这里“只读”是指没有设备寄存器写入或动作目标；仍需向串口发送 FC03/04 查询帧，因此文件描述符使用读写方式打开。主机串口临时设置为指定波特率、8N1、无流控；正常退出、查询异常和 Ctrl-C 时恢复原 termios 设置并关闭端口。不设置低延迟标志，不调用 DTR/RTS 控制，不改手的 ID、波特率、单位模式、电流或校准。硬杀进程、断电等情况无法由 Python finally 保证恢复。

## 使用前先确认端点和占用

本会话已出现：

```text
/dev/serial/by-id/usb-FTDI_Dual_RS232-HS-if00-port0 → /dev/ttyUSB0
/dev/serial/by-id/usb-FTDI_Dual_RS232-HS-if01-port0 → /dev/ttyUSB1
```

USB 枚举本身只证明 USB 通道对应关系。**不能把 if00/USB0 自动认作左手：本次后续实读已确认 if00/slave127 是右手，见下方实测记录。** 默认 slave ID 为左 126、右 127，但地址也可被修改；以实读 `901`、SN 和现场接线联合确认。本脚本每次必须显式指定一个 port 和一个 slave，不扫描其他端口、不遍历 ID、不在超时后自动改变波特率。

先检查 root 可见的端口占用、现有服务和 ModemManager 是否会访问端口。本脚本的 `flock` 是协作锁，`TIOCEXCL` 阻止后续普通打开，但不会驱逐既有使用者，也不能阻止特权进程。不得把获得锁等同于排除了全部竞争。

`unitree` 若不在 `dialout` 且节点是 `root:dialout 0660`，直接执行会报告 PermissionError；这与设备是否响应是不同层次。本轮可由主代理在已授权范围内临时以适当权限执行，脚本本身不修改用户组、权限或服务。

## 命令

串口实际位于 PC2，实物查询必须在 PC2 执行并能访问脚本；本仓库位于工作站，不能在工作站直接打开远端串口。本轮通过 SSH `python -c` 传入已审查源码执行，没有把脚本安装到 PC2。下面 `--plan` 示例可直接在工作站运行；去掉 `--plan` 的示例需在提供脚本的 PC2 上运行。

先检查即将发送的五个读帧，此模式不打开串口：

```bash
python3 scripts/brainco_serial_probe.py \
  --port /dev/serial/by-id/usb-FTDI_Dual_RS232-HS-if00-port0 \
  --slave 127 --plan
```

明确端点、占用和权限后，去掉 `--plan` 做一次查询：

```bash
python3 scripts/brainco_serial_probe.py \
  --port /dev/serial/by-id/usb-FTDI_Dual_RS232-HS-if00-port0 \
  --slave 127 --baud 460800 --timeout 1
```

以上参数对应本次已读成功的右手端点。若首个固件请求超时，程序立即结束该端点；不要据此宣称此端口就是另一只手。后续是否针对另一精确端点查询，由主代理依据当前结果决定。

输出为单份 JSON，包含每次请求／应答 hex、寄存器原值、交易耗时、完整身份、单位模式及六通道原始反馈。退出码 `0` 表示五次读取完整且 side/unit 可识别；`1` 表示读取、解码或关闭恢复失败；`2` 表示参数错误。失败报告保留已经成功的读事务，但不会生成不完整的最终设备摘要。

所有速度和电流使用二补码 `int16` 解码；位置仍保留 `uint16` 原值。工具不自动转成度、rad、mA 或归一化比例，以免不同单位模式／固件精度被误解释。五次请求并非原子快照；该报告只验证这次读取，不证明连续反馈频率、失联行为、保护配置或动作控制已验证。

## 失败隔离与测试

- 出站构造器只允许上表五个 `(function,address,count)`；不允许 FC06/10、广播地址或任意读寄存器。
- 入站逐项校验 slave、function、字节数、完整帧长度、CRC；支持完整 Modbus exception 报告。
- 对部分读／写使用共同的每事务 deadline；任意超时、CRC 错误、异常或不匹配立即结束整个端点，不重试、不继续读下一个寄存器。
- 这是必要的隔离：固件和 SN 都是 FC04/10 registers，RTU 响应没有地址或 transaction ID；超时后若继续下一请求，迟到应答可能被误认。程序不会这样继续。
- 不静默 flush 意外串口数据；发送前若发现可读数据则拒绝查询，避免掩盖竞争。此检查仍不能替代占用核对。
- 按[官方发现示例](https://github.com/BrainCoTech/brainco-hand-sdk/blob/1e04359e93ad082fc1478bfdfac615c38a2761e4/c/demo/debug_detect.cpp#L403)，打开串口后等待 150 ms 再检查是否空闲并发送首帧，以减少 FTDI 首次打开漏包。此改动后 9 项测试再次通过；等待不写入手部配置。

纯软件验证：

```bash
python3 -m unittest discover -s tests -p test_brainco_serial_probe.py -v
```

9 项测试通过，包括官方 CRC 请求示例、写请求拒绝、CRC/地址/功能码/长度/异常拒绝、原单位保留、无依赖预览，以及 PTY 模拟串口分片响应、错误／超时立即停止、恢复 termios 和释放 exclusive。另以 Python 3.8 语法解析确认入口兼容性。本代理没有打开实物串口；主代理后续实机只读结果如下。

## 2026-11-11 实机只读结果

主代理在 PC2 核对端口占用和权限后，使用本脚本进行了精确端点查询。成功报告时间为 **2026-11-12 00:00:13 UTC / 2026-11-11 16:00:13 PST**，证据：[brainco-if00-id127-2026-11-11.json](../diagnostics/brainco-if00-id127-2026-11-11.json)。

| 字段 | 实读值 |
|---|---|
| 主机 | PC2 `ubuntu` |
| 串口 | `usb-FTDI_Dual_RS232-HS-if00-port0` → `/dev/ttyUSB0` |
| 通信 | `460800`，slave `127 / 0x7f` |
| 左右手 | 寄存器 `901=1` → **right** |
| SN | `BCXTR2362J2500015` |
| 固件 | `1.0.22.U` |
| 当前单位模式 | `937=0` → normalized，探测没有修改 |
| 六通道位置 | `[749, 56, 1000, 1000, 1000, 1000]` |
| 六通道速度 | 全部 `0` |
| 六通道电机状态 | 全部 `0` |
| 查询情况 | 五个事务完整通过 CRC/ID/FC/长度检查，单次约 10.9–12.6 ms |

其余精确组合 `if00/126`、`if01/126`、`if01/127` 均在第一条固件请求等待 1 秒后超时，没有收到响应字节。对应证据分别是 [if00/126](../diagnostics/brainco-if00-id126-2026-11-11.json)、[if01/126](../diagnostics/brainco-if01-id126-2026-11-11.json)、[if01/127](../diagnostics/brainco-if01-id127-2026-11-11.json)。**左手仍未读通，不能把 if01 自动认定为已验证的左手端点。**

右手在同一 FTDI/纯 termios/460800 实现下成功，说明无需为当前只读路径臆测或修改 DTR/RTS、半双工方向或板卡波特率。左手的后续定位应从已知接线与具体端点证据继续；本次没有扩扫未知 ID／波特率，也没有改配置或启动动作服务。单次读取成功不等于手部 DDS 服务已接通、连续反馈能力已验证或动作已验证。

## 官方依据

- 后续有限补查见 [if01 候选结果](../diagnostics/brainco-if01-candidates-2026-11-11.json)：加入 150 ms 打开等待后，`460800 × (126,127,1,2,10)`、`115200 × (126,127)`、`1000000 × (126,127)` 均无回应。只覆盖这些组合，不证明其他配置也不可用。每个组合单独关闭恢复串口，没有更改手部配置。候选来自下方官方发现示例。
- [官方发现候选与 FTDI 打开等待](https://github.com/BrainCoTech/brainco-hand-sdk/blob/1e04359e93ad082fc1478bfdfac615c38a2761e4/c/demo/debug_detect.cpp#L356)。
- [Revo2 Basic Modbus 协议](https://www.brainco-hz.com/docs/revolimb-hand/en/revo2/modbus_foundation.html)：上述身份、单位模式和状态寄存器定义。
- [Revo2 Pro Modbus 协议](https://www.brainco-hz.com/docs/revolimb-hand/en/revo2/modbus_pro.html)：同一基础寄存器合同，左右手枚举、单位模式和六通道语义。
- [官方 raw serial benchmark](https://github.com/BrainCoTech/brainco-hand-sdk/blob/1e04359e93ad082fc1478bfdfac615c38a2761e4/tools/serial-benchmark/serial_test_read_speed.py)：不用 SDK 的 FC04 `2000/24`，以及参考 CRC 实现。
- [benchmark README](https://github.com/BrainCoTech/brainco-hand-sdk/blob/1e04359e93ad082fc1478bfdfac615c38a2761e4/tools/serial-benchmark/README.md)：460800、8N1 和官方请求 `7E 04 07 D0 00 18 FB 42` / `7F 04 07 D0 00 18 FA 93`。
- [SDK Python Revo2 初始化辅助函数](https://github.com/BrainCoTech/brainco-hand-sdk/blob/1e04359e93ad082fc1478bfdfac615c38a2761e4/python/revo2/revo2_utils.py)：用于区分 SDK 自动初始化与本工具的固定读帧。

审查官方 SDK 示例 commit：`1e04359e93ad082fc1478bfdfac615c38a2761e4`。协议网页核对日期：2026-11-11；网页本身没有提供可固定的源码 commit。
