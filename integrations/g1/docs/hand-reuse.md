# Wuji → BrainCo 现有代码复用方案

更新时间：2026-11-11。本文为源码审计和实施建议；尚未验证 G1 上的手部驱动、实物左右手或手套在线状态。

## 已确定的决定

- 按用户要求，复用 `/home/jack-xiang/branco-wuji-teleop/brainco-wuji-teleop`，不重写手指重定向算法。
- 审计时原项目 HEAD：`a66a48c949cb42957da4aa70d26e091e8c1b2c7d`，工作区干净。
- 保留原来的张开/握拳标定、拇指耦合、目标范围、平滑和基于实测位置的步长限制。
- G1 集成主要新增通信输出和双手配置；不能将旧 DaMiao 配置直接当作 G1 DDS 配置。
- 原项目保持不变；新适配与新配置放在 `unitree-g1`。

## 可复用接口

| 原文件 | 接口 | 复用方式 |
|---|---|---|
| `wuji_bridge.py` | `joint_readings(frame, min_confidence=.5)` | 原样读取五指屈曲量：弧度转角度，排除外展和填充值；不完整、非有限或低置信度帧丢弃。 |
| `wuji_bridge.py` | CLI：`--sn --side --user --rate-hz --count --timeout-s` | 用独立子进程输出 JSONL，复用手套身份/侧别/SDK 用户检查。默认原生 120 Hz，默认 5 秒无有效新帧退出。 |
| `teleop/retarget.py` | `readings(message, side=None)` | 将 `joint_angles` 或 `bends` 转为六通道读数；缺失手侧标签仍会接受，因此新集成入口应要求显式侧别。 |
| 同上 | `calibrate(open_samples, fist_samples)` | 继续使用每通道中位数标定；允许传感器方向反转。 |
| 同上 | `closure(raw, calibration)` | 得到每通道 `[0,1]` 闭合比例，缺少独立拇指对掌数据时跟随屈曲。 |
| 同上 | `LowPass(alpha)`、`__call__(fraction, dt=None)` | 保留按 120 Hz 基准和实际时间步长补偿的平滑。 |
| 同上 | `targets(fraction, minimum, maximum)` | 输出六通道整数目标，内部继续使用 `0–1000` 量纲。 |
| 同上 | `step_limited(target, measured, max_step)` | 限制目标相对实测位置的最大变化。 |
| `teleop/timing.py` | `Periodic(hz, now=None)`、`LoopStats` | 可复用单调时钟、跳过错过的周期和测量频率；DDS 发布频率与执行确认频率应分别统计。 |
| `teleop_hand.py` | `GloveStream(command, side)`、`snapshot(max_age)` | 可借用最新帧/失联逻辑，但该模块导入顶层 `teleop` 包，需处理与宇树同名包的冲突。 |

建议优先以 `importlib` 从固定文件路径加载原 `retarget.py`，并通过子进程调用原 `wuji_bridge.py`。不要同时把两个含 `teleop` 顶层包的仓库无序加入 `PYTHONPATH`。首次集成应固定原仓库提交或记录文件校验值，避免外部目录的更新悄悄改变控制行为。

现有映射管线保持如下：

```text
原 wuji_bridge.py → readings → closure → LowPass(dt)
                 → targets → step_limited(实测位置) → 新输出 backend
```

## 现有配置与左右手状态

用户已确认 G1 实物采用其称为 BrainCo VIM 的 USB/RS-485 板，左手连 `485.0`，右手连 `485.1`。原项目的 DaMiao CAN-FD 配置不对应这个后端。板上的通道标签不能直接等同于 Linux 的 `/dev/ttyUSB0/1`；设备枚举后按实际端口和手身份建立绑定。最新 PC2 枚举仍没有转换板，见 PLAN.md 的 VIM 接线记录。

审计时 `calibration.json`：

- `side` 为 `right`，生成时间为 `2026-11-09T20:50:26.405365+00:00`。
- 六通道顺序：`thumb_flex, thumb_opposition, index, middle, ring, little`。
- `thumb_opposition` 的 open/closed 均为 null。因此当前功能是拇指对掌跟随屈曲，并不是两个拇指自由度独立跟踪。保留此行为，无需扩大本次范围。
- 未发现左手校准文件。原右手文件不应复制为左手；左手继续使用原标定流程产生自己的文件。
- 校准文件只绑定 side 和时间，没有绑定手套序列号、操作者或 SDK 用户。G1 的运行配置应显式记录这些关联，并核对复用右手标定的操作者/佩戴状态。

审计时 `settings.json` 是一只 DaMiao CAN-FD 右手的配置：channel 1、hand ID 127，固定具体手序列号、手/适配器固件和本机 `libdm_device.so` 路径，且 `motion_enabled=true`。这不是 G1 已验证的设备身份，也不授权新集成默认发运动命令。新 G1 配置应独立建立，初始为只读/预览。

原配置允许的目标范围是：

```text
target_min = [100,   0,   0,   0,   0,   0]
target_max = [900, 950, 950, 950, 950, 950]
max_step = 100
segment_ms = 200
```

原 `_run` 在驱动硬限幅内再留 20% 余量，实际传给 `step_limited` 的值为 80。新集成应保留此语义。原 `config.validate(value, hardware=False)` 可以验证这些范围，但它拒绝未知字段；DDS 专属字段应放在独立配置结构中。

## 原驱动与 DDS 的差异

原 `teleop.hand.RevoHand(settings, *, sdk=None, context=None)` 提供 `state()`、`move(positions, *, hold=False)`、`hold()`、`close()`。其实现固定使用 DaMiao + BrainCo SDK，不能通过现有配置切换成 DDS。原 `teleop_hand._run` 也直接构造该类，不能将现成 CLI 当作通用 backend 运行器。

原 CAN-FD 驱动会核对归一化单位、六通道保护电流、turbo、设备身份，并检查有限时长位置写的应答；这些是驱动能力，不是重定向算法的一部分。

官方 [brainco_hand_service](https://github.com/unitreerobotics/brainco_hand_service) 的接口是左右手各一对 `rt/brainco/{left,right}/{cmd,state}` topic，位置/速度均为 `0–1`。发送端对旧目标除以 1000，接收端对 `q` 乘以 1000。保留原通道顺序，并在实机核对两个拇指通道。

审计官方 [main.cpp](https://github.com/unitreerobotics/brainco_hand_service/blob/main/main.cpp) 发现以下必须处理的语义差异：

- 服务启动后会直接进入写命令循环，初始目标为零且速度为 1；启动它不是只读探测。
- 服务目标循环为 100 Hz，采用位置+速度接口，不具备原 `segment_ms=200` 的有限时长语义。
- 当前循环没有命令新鲜度检查，会重复执行最后目标；主机停止发布不能等价于手停止。
- DDS 状态只填位置、速度和归一化电流，未携带原驱动使用的完整故障状态或硬件身份，也没有逐命令执行应答。

以上是官方 main 分支源码观察，不证明 G1 现在运行的服务就是这个版本。部署前先确认机器人端的实际版本与配置。

## 新 backend 的最小接口和行为

建议保留 `state / move / hold / close` 的概念，明确改变返回值语义，而不是让原 `_run` 无修改地调用 DDS 并误报确认成功：

| 接口 | 要求 |
|---|---|
| `state()` | 返回六通道 `0–1000` 实测位置、接收单调时钟、状态年龄；拒绝无反馈、过期、长度错误、非有限及越界数据。 |
| `move(positions)` | 只有已启用状态才发布；验证整数、目标范围、相对新鲜实测位置的步长；边界处转换为 `0–1`。 |
| 发布结果 | 使用 `published`；未实现应用层应答时不得记录 `acknowledged=true`。底层驱动身份、故障可用性另行标注。 |
| `hold()` | 按当前有效反馈保持位置；没有新鲜反馈时不能编造一个位置。真正失联处理依赖机器人端 watchdog。 |
| `close()` | 关闭订阅/发布器与子进程；不能把断开 DDS 本身当作停车机制。 |

每只手独立绑定 side、glove SN、SDK user、校准文件和 DDS topic。主机同一只手只允许一个命令发布者；不要同时运行宇树的 BrainCo 控制器和新输出 backend 向同一个 topic 写目标。

原流程默认为 `--stale-s .5`，输入失联时 hold；必须保留这个主机检测，同时为机器人端补上命令超时处理、首次有效命令前不运动、恢复后的显式重新启用。不要通过不断重发旧目标来掩盖手套掉线。

旧 `source_t` 是手套桥接进程所在主机的 `time.monotonic()`，仅同一主机可直接比较。若将桥接进程移到 G1，不能直接沿用该数值与笔记本时钟比较，应另设接收时刻及跨机时间语义。

还需保留启动位置检查：原默认 thumb 最小值为 100，而实测位置可能在范围外；原 `hold()` 会将实测位置夹到目标范围，因此它也可能产生小幅运动。新集成不能把这个方法当作无条件只读操作，或在预览/未启用阶段调用它。

## 实施步骤和验收

1. 在 `unitree-g1` 建立独立配置、锁定原代码版本，加载原映射模块；提供仅计算目标的预览入口，不导入硬件驱动。
2. 使用旧右手校准和合成样本验证输出与原算法一致；左右手错误、无效帧和旧帧不得混入。
3. 确认 G1 手部实际接线、串口/设备身份和服务版本；只读订阅左右手反馈。原 DaMiao 配置不参与此过程。
4. 为 DDS 建立上述 backend，测试单位转换、反馈新鲜度、步长范围、失联和恢复；适配机器人端执行语义后再开展实机验证。
5. 用原项目流程完成左手校准，分手核对指序后接入双手；PICO/G1 手臂链路单独调试，最后合并启停逻辑。

## 本次验证记录

在原仓库运行现有测试，隔离环境仅安装 `numpy` 和 `pytest`，禁用字节码和 pytest 缓存：

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --no-project --python 3.12 \
  --with numpy --with pytest python -m pytest -q -p no:cacheprovider
```

结果：**36 passed in 1.19s**。测试覆盖映射、手套解析、参数/传输过滤、时序及无硬件的网页控制流程。本次没有连接手套或手、没有发运动命令，也没有修改原项目。原 README 的 120 Hz 实测数据属于旧硬件链路历史记录，不能视作 G1 DDS 集成已经达到的性能。
