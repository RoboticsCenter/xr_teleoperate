# BrainCo 官方服务的默认只读补丁

更新时间：2026-11-11。此文档记录软件补丁、离线验证、PC2 隔离构建及用户授权的限定时长默认反馈实测。左右手 DDS 新鲜反馈已验证；临时服务已退出、串口已释放。没有安装自启动服务，没有启用或发送运动命令。

## 基线与交付文件

- 官方仓库：[unitreerobotics/brainco_hand_service](https://github.com/unitreerobotics/brainco_hand_service/tree/d71996b6999edb2f838a3dca3d9621429a2ef966)，固定 commit `d71996b6999edb2f838a3dca3d9621429a2ef966`。
- 补丁：[brainco-service-feedback-first.patch](../patches/brainco-service-feedback-first.patch)，修改 `main.cpp`、`include/param.h`，新增 `include/command_gate.h`、`test/command_gate_test.cpp`。
- 本会话完整、已应用补丁的隔离源码树：`/tmp/brainco-hand-service-pinned`；不修改原 Wuji 项目。
- 官方库版本见 [lib/VERSION](https://github.com/unitreerobotics/brainco_hand_service/blob/d71996b6999edb2f838a3dca3d9621429a2ef966/lib/VERSION)：`bc-stark-sdk v1.1.9`。仓库自带 `lib/aarch64/libbc_stark_sdk.so` 与 `lib/x86_64/libbc_stark_sdk.so`。本次已核实前者是 ARM aarch64 ELF，并导出补丁使用的两个 raw-read API。

原服务连接后会设置单位模式，再从默认缓存构造目标并周期发送；补丁移除这些默认行为。SKU 不匹配分支改成先打印再释放身份结构，修复释放后访问。DDS 改用官方直接 Publisher/Subscriber 接口，避免旧 helper 的未加锁共享缓存与 `unlockAndPublish()` 使用问题。[上游 main.cpp](https://github.com/unitreerobotics/brainco_hand_service/blob/d71996b6999edb2f838a3dca3d9621429a2ef966/main.cpp)

## 默认启动语义

默认只有身份读取、反馈读取和状态发布，没有 command subscriber。初始化、正常循环、异常、SIGINT/SIGTERM 退出均不会调用任何 `stark_set_*`，也不会发送 hold。

发现沿用上游固定范围：`/dev/ttyUSB*`、`/dev/ttyHAND*`、`/dev/ttyUN*`，460800 baud，只试左 ID126、右 ID127，按返回 SKU 绑定左右；没有扩大 ID/baud 扫描，也没有自动重连循环。实际身份以序列号日志核对，不能仅凭 tty 编号断定左右。运行前仍需核对进程/串口占用；此补丁不提供跨任意第三方程序的串口排他保障。

每轮重新调用 `stark_read_holding_registers(937, 1)` 验证 unit=0，再调用 `stark_read_input_registers(2000, 24)`。任一步失败、读取超时龄、unit 非0、位置不在0..1000、速度/电流不在−1000..1000、motor state不在0..3，均不发布该帧；不拿旧缓存代替。初始读取失败则该手线程退出，完全不建立 command subscriber。unit 非0不会被自动改写。[官方寄存器定义](https://www.brainco-hz.com/docs/revolimb-hand/en/revo2/modbus_foundation.html)、[官方 C ABI](https://github.com/unitreerobotics/brainco_hand_service/blob/d71996b6999edb2f838a3dca3d9621429a2ef966/include/stark-sdk.h)

状态 DDS 为 `rt/brainco/{left,right}/state`，类型 `unitree_go::msg::dds_::MotorStates_`，严格6通道。`q=position/1000`、`dq=speed/1000`、`tau_est=current/1000`；最后一项是归一化电流，不是 N·m。排列沿用官方 SDK 的 thumb、thumb auxiliary、index、middle、ring、pinky。Wuji 的既有映射应由本项目适配器转换成同一通道契约，不另写手指重定向。

## 显式命令开关与过期策略

`--enable-commands` 才会创建 `rt/brainco/{left,right}/cmd` 的 `MotorCmds_` subscriber，并且先要求该手初始反馈有效。这个软件参数不是实机运动授权。

| 条件 | 行为 |
| --- | --- |
| 尚未收到真实 DDS callback | 没有默认目标，也不从默认 `msg_` 缓存发命令 |
| 有效 callback | 必须恰好6项；每项 q 有限且0..1，dq 有限且0.001..1；转换为整数0..1000位置、1..1000速度 |
| 无效 callback | 锁存 `invalid-command`，以后新包不恢复 |
| 接收年龄达到 `--command-timeout-ms`，默认200ms | 锁存 `command-expired`，以后新包不恢复 |
| 漏 tick/SDK阻塞后才收到新包，距上次接收已达deadline | 在 submit 内直接锁存过期，不能覆盖旧时间掩盖中断 |
| 反馈失败、非归一化或超龄 | 锁存 `feedback-unavailable`，以后新反馈不恢复命令 |
| 正常退出 | 锁存 `shutdown`；不再发送正常目标 |

锁存后的唯一恢复方法是重新启动服务并重新满足前置检查。`--feedback-max-age-ms` 默认100ms，从本轮读取开始计时；两个参数允许10..5000ms，不能视为越大越好。

如果此前确实尝试过正常目标输出，第一次处理锁存/退出时又有**本轮重新读取且仍新鲜有效**的状态，则尝试一次“反馈位置 + 速度100/1000”的 hold。**hold 本身就是动作命令**，可能因采样延迟、机构变化或控制器内部状态而产生运动。没有新鲜反馈就不发 hold；之后反馈恢复也不补发迟到的 hold。此前从未尝试目标输出，退出也不发 hold。SDK setter 是 void，没有写入确认。

这不是硬实时看门狗：同步 native SDK 的读写若阻塞，门控检查只能在调用返回后执行；SIGTERM 也不能打断该阻塞。停止发送包不等于物理停止，hold 尝试也不是急停或制动确认。单手故障锁存目前按手独立，另一手可继续；双手/双臂共同停机策略应在上层协调。DDS 消息没有源端生成时间或会话号，本补丁检查的是本地真实 callback 接收时间，不能证明发送方未重放旧目标；不要同时运行多个 command publisher。

## 应用与离线编译

以下命令用于干净的隔离 checkout，不运行服务：

```bash
git clone https://github.com/unitreerobotics/brainco_hand_service.git /tmp/brainco-hand-build
cd /tmp/brainco-hand-build
git checkout d71996b6999edb2f838a3dca3d9621429a2ef966
git apply --check /home/jack-xiang/unitree-g1/patches/brainco-service-feedback-first.patch
git apply /home/jack-xiang/unitree-g1/patches/brainco-service-feedback-first.patch
c++ -std=c++17 -Wall -Wextra -Werror -pedantic -pthread \
  -fsanitize=address,undefined -fno-omit-frame-pointer -Iinclude \
  test/command_gate_test.cpp -o /tmp/brainco-command-gate-test
/tmp/brainco-command-gate-test
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --target brainco_hand_server -j2
```

完整服务依赖 C++17、CMake、Boost program_options、yaml-cpp、spdlog/fmt、Unitree SDK2、CycloneDDS C/C++ 和随仓库提供的 native SDK。官方 CMake 显式链接 `fmt`，有 spdlog 头文件不代表一定有 `libfmt`。产物按上游设置在源码树 `bin/brainco_hand_server`。只编译 server target，不运行 `test_brainco_hand_server`；上游这个名称是硬件动作示例，不是此补丁的离线策略测试。[官方 CMake](https://github.com/unitreerobotics/brainco_hand_service/blob/d71996b6999edb2f838a3dca3d9621429a2ef966/CMakeLists.txt)

在 PC2 上使用已存在的 Unitree/CycloneDDS 依赖做原生 aarch64 构建；本次本机策略测试产物是 x86_64，不能直接当作 PC2 服务二进制。无须安装 systemd/autostart。需要把服务的动态库路径指向相应架构目录。

### 本次 PC2 已发现的构建差异

主代理已把隔离服务源码放在 `/home/unitree/g1-integration/brainco-feedback-20261111`。PC2 的旧 SDK2 安装有单数 `MotorCmd_.hpp` / `MotorState_.hpp`，但缺少聚合 `MotorCmds_.hpp` / `MotorStates_.hpp`，因此直接使用上游构建会报缺头文件。可仅给本次编译添加本地 include overlay，不升级系统 SDK。

overlay 来源固定为官方 [unitree_sdk2 commit aaf69cb56c9d5ed63d8b284008ec3a8ab999bec1](https://github.com/unitreerobotics/unitree_sdk2/tree/aaf69cb56c9d5ed63d8b284008ec3a8ab999bec1/include/unitree/idl/go2)。已在开发机准备 `/tmp/brainco-idl-overlay/unitree/idl/go2/` 的两个聚合头文件；它们由 CycloneDDS v0.10.2 生成，依赖各自的单数类型及 CycloneDDS 头文件。加入 `-I<overlay>` 前应核对 PC2 单数类型与固定版本是否相同，避免类型定义混用。

| 文件 | 官方固定版 SHA256 |
| --- | --- |
| MotorCmd_.hpp | `fd63c8440dcb87df8b21fe483cc64a1fb041cb4a478e7bd0da6abe93de635751` |
| MotorState_.hpp | `de888d724b93c6adc1e6a7b2785a3a9c2322f30549c4d6c7ea2e14edcd964b93` |
| MotorCmds_.hpp | `853879012a3b02d6be050f401fb3cf6a1d193e10a09c89d887b81c7514ab9156` |
| MotorStates_.hpp | `b45a8a944c93708a1b874f2aef1ecb636d2e9ca0c6710891e128fdbb952d6572` |

本次已取回 PC2 两个单数头文件逐行比较：实际哈希分别是 `e5287f5c92fbd58e0d9b644917ae790a2b61d17f8e9125c42827db1b9dbc98b7` 和 `342e0428c81d5763023102aa3aae6e44908eeeb4f5039fb94ea0645254320862`。差异仅3行 include guard 名称，字段、类型名、序列化函数和 type-map/info blob 全部相同，因此本机当前安装与这两份 aggregate overlay 类型兼容；无需覆盖单数头文件或替换 SDK2 库。

PC2 还缺独立 fmt 库，但有 spdlog bundled fmt；可在本次隔离构建使用 `-DFMT_HEADER_ONLY` 并移除独立 `fmt` 链接，而不安装/升级全局依赖。此处是构建适配，是否完成 ARM64 链接以主代理构建日志为准。

主代理已完成 ARM64 隔离编译、链接及 `--help` 检查，均退出0；二进制 SHA256 为 `e4542b6a3e3f07abccf0d2157317db10889d385eef5d056119c14c8bc1a88c51`，完整编译参数与输出见 [pc2-feedback-build-linked-2026-11-11.json](../diagnostics/pc2-feedback-build-linked-2026-11-11.json)。这个结果证明已生成可启动参数入口的 ARM64 二进制，不计作手反馈或运动实测。

旧 `libunitree_sdk2.a` 还缺两个聚合类型的 `get_type_props<…>()` 实现；仅补 header 会在链接时失败。最小修复是从同一固定官方 SDK2 的 ARM64 静态库提取两个生成 IDL 对象，加入**本次二进制**，继续使用原系统 SDK2 库。这两对象只提供聚合类型属性，依赖已有单数类型与 CycloneDDS；不替换整个 SDK2 库。

```bash
# SDK2 checkout 必须是 aaf69cb56c9d5ed63d8b284008ec3a8ab999bec1。
mkdir -p /tmp/brainco-idl-objects/aarch64
cd /tmp/brainco-idl-objects/aarch64
ar x /tmp/unitree-sdk2-overlay-source/lib/aarch64/libunitree_sdk2.a \
  MotorCmds_.cpp.o MotorStates_.cpp.o
sha256sum MotorCmds_.cpp.o MotorStates_.cpp.o
```

对象 SHA256：`MotorCmds_.cpp.o` 为 `95b2531b1122ae2d0c4590f16fb3525f5fcfc23d8cea101aad53363bda4c86e8`；`MotorStates_.cpp.o` 为 `ef3461dcfd0a8f36201c1e09fb45acefd1000eda2589ee0f83b3abbe6c9ba2d4`。均为 aarch64 relocatable ELF，编译器标记 Ubuntu20.04 GCC9.4。链接时把两个对象放在 `main.cpp` 后、`-lunitree_sdk2 -lddsc -lddscxx` 前；不要添加 `--whole-archive`。

该混合链接的 ABI 前提包括现有单数类型实现及 CycloneDDS `entity_properties` 布局兼容。单数头文件已核对相同；主代理核实 PC2 `entity_properties.hpp` 与固定官方文件的 SHA256 均是 `05e38b64a4f595bb7b16cfd371be9db5fed268d020eb7cbe1e84ecaff6d16214`，证据：[pc2-dds-abi-2026-11-11.json](../diagnostics/pc2-dds-abi-2026-11-11.json)。这支持本次最小对象混合链接的 ABI 兼容性；完整实测还包括六通道序列化往返与状态 DDS 验证，不仅是成功链接。

纯软件往返验证入口已保存为 [tests/brainco_idl_roundtrip.cpp](../tests/brainco_idl_roundtrip.cpp)：对6通道 commands/states 分别执行 basic CDR 和 XCDR2 写入、读回、逐字段比较，不创建 DDS participant 或打开串口。本机官方 x86_64 库验证通过；PC2 应使用与服务相同的两份 overlay header、两份 ARM64 对象及现有 SDK2/CycloneDDS 库编译运行，检验实际混合链接后的序列化路径。

以下命令在 PC2 隔离源码目录执行；先把本项目测试源复制到该目录，并把上面两个对象放入 `idl-objects/`：

```bash
g++ -std=c++17 -Wall -Wextra -Werror \
  -Iidl-overlay -I/usr/local/include/ddscxx brainco_idl_roundtrip.cpp \
  idl-objects/MotorCmds_.cpp.o idl-objects/MotorStates_.cpp.o \
  -L/usr/local/lib -Wl,-rpath,/usr/local/lib \
  -lunitree_sdk2 -lddscxx -lddsc -lpthread -o bin/brainco_idl_roundtrip
LD_LIBRARY_PATH="/usr/local/lib:${LD_LIBRARY_PATH:-}" ./bin/brainco_idl_roundtrip
```

以下是经审阅、满足串口占用与权限前提后使用的**默认反馈模式**命令模板，本文件不自动执行：

```bash
cd /tmp/brainco-hand-build
LD_LIBRARY_PATH="$PWD/lib/aarch64:${LD_LIBRARY_PATH:-}" \
  timeout --signal=TERM --kill-after=3s 10s ./bin/brainco_hand_server -n eth0
```

显式输出模式命令模板如下，仅供后续动作授权与整链路检查时使用；`--enable-commands` 会连同上文的一次 hold 策略一起启用：

```bash
LD_LIBRARY_PATH="$PWD/lib/aarch64:${LD_LIBRARY_PATH:-}" \
  ./bin/brainco_hand_server -n eth0 --enable-commands \
  --command-timeout-ms 200 --feedback-max-age-ms 100
```

## 已完成的离线验证与下一步验收

1. 在完整官方 pinned checkout 上 `git apply --check` 通过；`git diff --check` 通过。
2. `command_gate_test.cpp` 使用 C++17、`-Wall -Wextra -Werror -pedantic`、ASan/UBSan 编译运行通过。覆盖默认禁用、没有真实 callback、不合法长度/NaN/Inf/范围、到期锁存、反馈丢失/超龄、一次 hold、退出、漏 tick 后新包不能掩盖过期及恢复后不能自动续控。
3. [tests/brainco_service_mock/worker_test.cpp](../tests/brainco_service_mock/worker_test.cpp) 编译真实 patched `main.cpp`，用同目录 `stubs/` 的假 DDS/SDK 代替外部依赖；9个默认反馈场景均通过 ASan/UBSan。覆盖启动/退出零 setter 与零 command subscriber，unit不符/初读失败/初读越界拒绝，以及运行期读取失败、位置/速度/电流/状态越界不发布缓存。这不是生产 SDK 链接测试。
4. `/tmp/brainco-native-pty-audit.py` 使用官方 x86_64 native 库与内存 PTY 仿真手：`modbus_open → stark_get_device_info → free → close` 成功路径只出现 FC03 901/1、FC04 3010/10、FC04 3000/10、FC03 970/1，没有配置或目标写。这个观察只覆盖该库和该路径；native SDK 没有随仓库公开完整实现，不能据此宣称所有架构/失败路径已获形式证明。ARM64 可在 PC2 PTY 上先重复同样检查，无需接触实物串口。
5. 主代理已在 PC2 使用真实 ARM64 native 库重复上述 PTY 成功路径，得到相同4条读请求且没有写请求。记录：[pc2-brainco-native-pty-2026-11-11.txt](../diagnostics/pc2-brainco-native-pty-2026-11-11.txt)。这是仿真串口验证，不是手部运动验证。
6. PC2 已用两份最小 ARM64 对象与旧系统 SDK2/CycloneDDS 完成6通道 commands/states 的 basic CDR、XCDR2 往返测试，编译/运行均退出0。[实测记录](../diagnostics/pc2-idl-roundtrip-2026-11-11.json)

### 首次限定时长默认反馈运行（2026-11-11 16:58 PST）

用户授权继续只读验证后，本子代理完成了一次实机尝试。启动前核对只有 `/dev/ttyUSB0`、`/dev/ttyUSB1` 两个候选端点，root `fuser` 无占用，服务二进制 SHA256 与已审阅构建一致。以默认参数 `-n eth0` 启动，外层 `timeout --signal=TERM --kill-after=3s 25s`，没有 `--enable-commands`。

日志确认右手 `/dev/ttyUSB0 / ID127 / SN BCXTR2362J2500015 / FW1.0.22.U`。12秒内未观察到左手身份绑定，未满足进入双手 DDS 验收的前提，因此没有运行独立 DDS probe；不能把这次尝试计作双手 DDS 反馈成功。仅凭该日志无法区分左手未响应、原生 SDK 身份读取失败或瞬时串口竞争，也不能推断左手当前离线。

随后只对本次进程组发送 SIGTERM，服务正常退出0，root `fuser` 再次确认两路串口均无占用。没有重复扫描，没有发送配置/运动目标。完整证据：[pc2-feedback-service-first-2026-11-11.json](../diagnostics/pc2-feedback-service-first-2026-11-11.json)。

### FTDI 首次请求等待修复

后续授权继续定位后，核对发现已经读通双手的 `brainco_serial_probe.py` 在打开端口后等待150ms，而原服务立即请求身份。BrainCo [官方 debug_detect.cpp](https://github.com/BrainCoTech/brainco-hand-sdk/blob/77e5eddd5bf79cfdddafc185448ad31b8ccb3a95/c/demo/debug_detect.cpp#L403) 明确为 FTDI/RS-485 首次请求增加150ms等待，解释冷打开时驱动 FIFO 尚未稳定可能漏掉第一条回复。

因此本补丁在每次成功 `modbus_open` 后、第一次身份读取前增加150ms主机等待，不调用任何配置 setter。持久化 mock 新增检查，确认第一次 `stark_get_device_info` 的调用时间距 open 至少150ms，连同原9个反馈场景通过 ASan/UBSan。此修复有官方依据，但首次实机失败是否由此造成仍以针对性再验证为准；不据此增加ID或baud扫描范围。

修复后 ARM64 重编译与 `--help` 均通过，二进制 SHA256 更新为 `b0df0937ea32ac6ddf80c5d851fbf7405434fbf3d8aeb59003f5d456e12ffb8b`。[构建记录](../diagnostics/pc2-feedback-settle-build-2026-11-11.json)

17:02 PST 的一次针对性默认运行已成功绑定左 `/dev/ttyUSB1 / ID126 / BCXTL2204J2600021` 与右 `/dev/ttyUSB0 / ID127 / BCXTR2362J2500015`，两者均 FW1.0.22.U。随后独立 Python 订阅器因继承了 sudo/root 身份，打开现有 `/tmp/cdds.LOG` 被拒，DDS domain 初始化失败；这次没有任何状态采样，不能将“全部 missing”解读成手状态服务无输出。服务已退出0，root fuser核对两路已释放。[完整记录](../diagnostics/pc2-feedback-service-settled-2026-11-11.json)

订阅器应使用原 `unitree` 身份运行 tv 环境 Python；不为处理这个诊断权限问题修改共享日志文件权限或机器人配置。

### 双手 DDS 默认反馈验收通过（2026-11-11 17:03 PST）

同一150ms修复版服务以默认参数运行；独立订阅器通过 `runuser -u unitree -- /home/unitree/miniconda3/envs/tv/bin/python -B ...` 执行本项目 `dds_state_probe.py` 源码，观测5秒。身份日志再次核对两只预期 SN，无单位设置、目标发送或 command subscriber。

| 状态流 | 有效帧 | 实测到达频率 | 结束时最新帧年龄 | 无效帧 |
| --- | --- | --- | --- | --- |
| G1 `rt/lowstate` | 5251 | 1047.79 Hz | 0.43 ms | 0 |
| 左 `rt/brainco/left/state` | 470 | 94.05 Hz | 15.12 ms | 0 |
| 右 `rt/brainco/right/state` | 472 | 94.30 Hz | 10.31 ms | 0 |

两手均为6通道有限且范围正确的归一化位置，三路均 `fresh`，独立 probe 退出0。上述频率为本次主机接收观测，不是传感器源时钟频率或端到端操控延迟。本体 `mode_machine=5` 仅作为读到的原始值记录，不据此判断控制模式或运动授权。

官方 Python reader stderr 有一条 `[Reader] take sample error`，原样保留；没有对应的无效样本计数或失去新鲜状态，不能由此宣称长时稳定性已经验收。随后服务正常退出0，没有使用 SIGKILL，root `fuser` 确认两路串口已释放。全部证据：[pc2-feedback-service-verified-2026-11-11.json](../diagnostics/pc2-feedback-service-verified-2026-11-11.json)。没有修改 `/tmp/cdds.LOG` 权限/归属，没有重配机器人或手部参数。

默认手部 DDS 反馈已由独立订阅器验收。下一步将这两路新鲜 measured state 接入复用原 Wuji 映射的只读/预览适配层，核对左右 SN、通道顺序、归一化值及基于反馈的步长限制；published target、measured state 和硬件执行确认应分别记录。此次成功不包含 Wuji 动作输出或 G1 双臂运动，也不证明显式命令模式的故障保持动作已经实测。

9场景 mock 的可复现命令，从本项目根目录执行。`brainco_test_source` 应指向已应用补丁的官方 checkout；此测试不需要 Boost、Unitree SDK 或硬件。不要加 `-DNDEBUG`，以保留断言。

```bash
brainco_test_source=/tmp/brainco-hand-service-pinned
c++ -std=c++17 -Wall -Wextra -Werror -pedantic -pthread \
  -fsanitize=address,undefined -fno-omit-frame-pointer \
  -Itests/brainco_service_mock/stubs \
  -I"$brainco_test_source/include" -I"$brainco_test_source" \
  tests/brainco_service_mock/worker_test.cpp -o /tmp/brainco-service-worker-test
/tmp/brainco-service-worker-test
```
