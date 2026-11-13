# PICO 输入接通记录与只读诊断

更新：2026-11-11。用户已确认头显为 **PICO 4 Ultra Enterprise**，左右手腕各戴一个独立 PICO Motion Tracker，Wuji 提供手指输入。本轮没有运行机器人控制器、IK 输出、BrainCo 服务或任何手部程序。

## 本轮实测结论

**电脑侧官方 PCService → Python SDK 已实际接通；尚未收到头显或 Tracker 数据。**

| 项目 | 实测结果 | 证据 |
| --- | --- | --- |
| 电脑 | Ubuntu 26.04 x86_64；系统 Python 3.14.4 | `diagnostics/pico-environment.json` |
| ADB | 已有后台程序来自 `~/m2/quest-tools/adb-root`；加载其随包运行库后可以运行；`devices -l` 列表为空 | 同上 |
| USB | 本轮没有枚举到 PICO 头显；不能据此排除无线连接 | 同上 |
| 系统 Python 的 SDK | 未安装，probe 输出 `sdk_unavailable`，退出 2 | `diagnostics/pico-missing-sdk.jsonl` |
| 隔离 SDK | Python 3.12.13；官方源码构建 `xrobotoolkit-sdk==1.1.0`，可导入并初始化/关闭 | `diagnostics/pico-environment.json` |
| 无 PCService 时 | 1 秒读取，Tracker 数量和时戳均为 0，退出 1 | `diagnostics/pico-no-service.jsonl` |
| PCService | 官方 v1.0.0 Ubuntu 24.04 amd64 包在当前电脑可运行，未系统安装 | `diagnostics/pico-service-check.json` |
| 服务端口 | 实测 TCP `*:63901`；SDK 本地连接 `127.0.0.1:60061` | 同上 |
| SDK → 服务 | 日志出现 `server connect`；3 秒内读取 30 次，Tracker 数量为 0，顶层/独立 Tracker 时戳均为 0 | `diagnostics/pico-live-service.jsonl`、`diagnostics/pico-live-service-sdk.log` |
| 跟踪有效性 | 没有头显/Tracker 实际帧，未验证任何硬件位姿、SN、坐标方向、遮挡或断连行为 | 上述证据范围 |
| 清理 | 临时 PCService 已终止，SDK 已 `close()`；本轮结束未遗留输入服务或探测进程 | `diagnostics/pico-service-check.json` |

用户重插所有设备后，15:47 再次复查：ADB 仍为空、USB 仍未见 PICO；重新启动输入服务后 SDK 成功连接，5 秒读取 50 次，Tracker 数量/两类时戳仍为 0，退出 1。第二次 probe 直接使用 stdout，输出逐行 JSON 解析通过；原生日志正确分离到 stderr。证据：`diagnostics/pico-after-replug-environment.json`、`diagnostics/pico-after-replug-service.json`、`diagnostics/pico-after-replug-input.jsonl`、`diagnostics/pico-after-replug-sdk.log`。服务再次结束并确认无遗留进程。

启动前审查了官方服务 `main.cpp`、`business.cpp`、`devicemanagement.cpp`、`PXREAGRPCServer.cpp` 和 SDK `PXREARobotSDK.cpp`：此处仅运行头显网络接收服务和 SDK feedback 订阅，没有调用 SDK 的 `device_control_json` / `send_bytes_to_device`，也没有加载 Unitree 或 BrainCo 模块。服务会在 `~/.local/share/PICOBusinessSuitData/log` 写日志；本轮没有安装系统包、systemd 服务或修改网络设置。

## 已准备的隔离组件

所有下载/编译依赖位于 `~/.cache/unitree-g1-pico/`，不写原 Wuji 项目：

```text
venv/bin/python                     Python 3.12 + xrobotoolkit-sdk 1.1.0
pybind-source/                      官方 Pybind 源码，未修改
pc-service/opt/apps/roboticsservice/ 官方 .deb 解包目录
XRoboToolkit_PC_Service_1.0.0_ubuntu_24.04_amd64.deb
pc-service-release.json             下载时的官方 release 元数据
```

Pybind commit：`8941e1dbbf4313fe83846f82984eaa8c992690bf`。PCService 源码审查 commit：`85bac4dbc1fd5cef42c74a160d9c30aa3491f122`；执行文件来自官方 release v1.0.0，不能仅凭 tag 推断其二进制与审查源码逐字一致。

下载包 SHA-256：`bce661f0be0b8a246ceecb2e5f1675a81c26b834648dc7fdf23f8c0bfe2a5d19`。

来源：[PICO 官方入口](https://github.com/Pico-Developer/XRoboToolkit)、[Pybind 官方仓库](https://github.com/XR-Robotics/XRoboToolkit-PC-Service-Pybind/tree/8941e1dbbf4313fe83846f82984eaa8c992690bf)、[PCService 官方发布](https://github.com/XR-Robotics/XRoboToolkit-PC-Service/releases/tag/v1.0.0)。

## 运行只读诊断

**这个版本的 PCService 同时只保留一个 SDK feedback 客户端：新 probe 连接会取消旧客户端的输入流。不要与遥操作 reader 同时启动。** 这是已审查的[服务实现](https://github.com/XR-Robotics/XRoboToolkit-PC-Service/blob/85bac4dbc1fd5cef42c74a160d9c30aa3491f122/RoboticsService/PXREAGRPCServer/PXREAGRPCServer.cpp#L61)的行为；只读 getter 不代表对另一个输入客户端没有影响。

终端一，前台运行已解包的输入服务（Ctrl-C 结束）：

```bash
cd "$HOME/.cache/unitree-g1-pico/pc-service/opt/apps/roboticsservice"
env LD_LIBRARY_PATH="$PWD:$PWD/lib:$PWD/SDK/x64" \
    QT_PLUGIN_PATH="$PWD/plugins" QT_QML_PATH="$PWD/qml" \
    ./RoboticsServiceProcess
```

终端二，运行诊断，默认每秒读取 10 次、持续 3 秒：

```bash
cd /home/jack-xiang/unitree-g1
~/.cache/unitree-g1-pico/venv/bin/python scripts/pico_probe.py \
    --seconds 5 --output diagnostics/pico-wrist-input.jsonl
```

首次看到两个 SN 后，分别移动左/右手腕核对物理身份，再明确绑定：

```bash
~/.cache/unitree-g1-pico/venv/bin/python scripts/pico_probe.py \
    --seconds 5 --left-sn '<左手腕实际SN>' --right-sn '<右手腕实际SN>' \
    --output diagnostics/pico-wrist-bound.jsonl
```

`python3 scripts/pico_probe.py --help` 不需要 SDK。没有 `--output` 时 JSONL 写 stdout；C++ SDK 日志转到 stderr。每次记录包括头显、控制器位姿、独立 Tracker SN/位姿、SDK count、逐流/顶层时戳及本机单调读取时间。SDK 没有控制器 SN getter，输出明确标记 `not_exposed_by_sdk`；不会伪造 SN。

| 退出码 | 含义 |
| --- | --- |
| 0 | 本轮曾观察到两个不同 SN 的格式正确位姿缓存，且顶层时戳至少前进一次；若指定侧别则对应 SN 也曾同次出现。**不表示 Tracker 有效或机器人可动作。** |
| 1 | 读取正常结束，但上述输入观察条件不足，包括当前完全没有头显数据的情况 |
| 2 | SDK 缺失、API 不匹配、初始化/读取错误或参数错误 |
| 130 | 用户中断，已尝试关闭 SDK |

SDK 的原生初始化/关闭不提供调用超时；若未来网络故障导致原生调用卡住，可用外层 `timeout 15s` 包住一次 5 秒 probe。当前无服务及有服务两种实际运行均正常结束。

## 数据报告的边界

- `xyzw_pose` 顺序是 `[x,y,z,qx,qy,qz,qw]`。只检查 7 项、有限值及四元数模长接近 1，不自动修正异常四元数、不改变坐标、不输出机器人目标。
- `tracking_valid=null`、`source_freshness=unknown`、`motion_ready=false` 始终保留。即使位姿和 SN 都合理，也不能用缓存 getter 判断其在本次采样仍被跟踪。
- 顶层/独立 Tracker 时戳分别报告 `unavailable / first_positive / advanced / unchanged / regressed`。`ms_since_observed_value_change` 只是电脑观察到这个数值变化后经过的时间，**不是传感器数据龄期**；`source_age_ms` 仍为 null。
- 顶层时戳变化不证明每个 Tracker 的数据变化；独立 Tracker 时戳可能一直为 0；只比较前后时戳不能生成原子快照。SN、pose、count 等 getter 之间也可能跨帧。
- 当 count 与数组长度不一致或 SN 重复时会报告问题。任何数组顺序都不决定左右侧；只用显式 SN 匹配。
- 更正此前 `docs/pico-input.md` 的表格描述：本次实际安装的 commit 中，独立 Tracker 的 pose/SN getter 返回长度为 `NumMotionDataAvailable` 的 vector/list，底层最多 3 项，**不是固定返回 3 项**。其 `Motion.len` 被忽略、断连缓存不清等风险仍存在。[实际绑定源码](https://github.com/XR-Robotics/XRoboToolkit-PC-Service-Pybind/blob/8941e1dbbf4313fe83846f82984eaa8c992690bf/bindings/py_bindings.cpp#L415)

## 头显侧待完成步骤

头显型号已由用户确认是 **PICO 4 Ultra Enterprise**。其[官方产品页](https://www.picoxr.com/global/products/pico4-ultra-enterprise)明确支持 Motion Tracker 的全身及物体跟踪。**实际系统版本、已安装 APK、头显网络和 Send 状态仍未知**；16:01 再次执行 ADB 设备枚举，列表仍为空。

待接通步骤：

1. 让头显与运行 PCService 的这台电脑在同一局域网。本机当前 Wi-Fi 地址为 `192.168.31.92`；实际连接前重新核对地址。G1 的 `192.168.123.164` 不是本轮 PCService 地址。
2. 头显打开对应版本的 XRoboToolkit，Network / PC Service 选择或手工输入电脑 Wi-Fi IP，确认状态为 WORKING。
3. 启用独立 Motion Tracker 数据及 Send。不要用 Full Body 的左右腕估计替代两个手腕 Tracker 的独立 SN 数据；具体选项取决于已安装 APK 版本。手指继续用 Wuji。
4. 在 probe 中看到两个 SN 后，分别移动每个手腕进行绑定，保存实际帧；在这之前仍不能说输入链路完全接通。
5. 后续单独解决每路有效性/断连失效传播、原子快照、安装坐标与相对零点、clutch 和 G1 双臂 IK。现成手指映射不解决这些双臂问题。

头显连接和 Send 操作参照[Unity 官方使用说明](https://github.com/XR-Robotics/XRoboToolkit-Unity-Client#pose-sync-between-xr-device-and-robot-pc)。本轮没有给头显安装/覆盖 APK，官方 release 的 APK 与当前开发分支也不能默认视为相同功能。

现有 ADB 可复用；只读查看设备：

```bash
env LD_LIBRARY_PATH="$HOME/m2/quest-tools/adb-root/usr/lib/x86_64-linux-gnu/android" \
    "$HOME/m2/quest-tools/adb-root/usr/lib/android-sdk/platform-tools/adb" devices -l
```

## 测试

```bash
python3 -m unittest discover -s tests -p 'test_pico_probe.py' -v
```

本轮 13 项测试通过，覆盖缺失 SDK、无 SDK help、SN 侧别绑定/重复、缓存尾部、数组不一致、异常四元数、时戳回退、顶层时戳不冒充跟踪有效、异常时关闭 SDK，以及无输入退出状态。纯软件测试没有替代头显/Tracker 的硬件验证。

## Enterprise 型号确认后的 APK 与双腕要求

已核对当前[官方 Object Tracking 指南](https://developer.picoxr.com/document/unity/object-tracking/)：覆盖 PICO 4 / PICO 4 Ultra 系列，列出的系统要求是 **5.13.0 或更新**；示例显式请求 `MotionTrackerNum.TWO`。独立 Object Tracking 与 Body Tracking 互斥；Tracker 需处于头显可见范围，离头显超过约 1 米或进入跟踪盲区时，数据可能失准或不更新。此指南当前对应 SDK 3.4.0；下面的发布版源码内置 SDK 3.1.2，不能把新 SDK 接口直接混用进旧版项目。

已下载[官方 PICO v1.1.1 发布](https://github.com/XR-Robotics/XRoboToolkit-Unity-Client/releases/tag/v1.1.1)的两个 APK，只保存在 `~/.cache/unitree-g1-pico/`，没有安装、覆盖或卸载头显应用：

| 缓存文件 | APK 实际 manifest | SHA-256 |
| --- | --- | --- |
| `XRoboToolkit-PICO-1.1.1.apk` | min Android API 30；target 31 | `6b2bb282405673d24abcb1980e3478b8f1052e90f7207b1f24cc56a59f8d8261` |
| `XRoboToolkit-PICO-1.1.1-SDK29.apk` | min Android API 29；target 32 | `6089e1ce175cf3e292cea5919c2b97711151186ca39c4a5569786a12fc167eb2` |

两者都是 `com.xrobotoolkit.client`，versionName `1.1.1`、versionCode `1`，解析到相同的 APK v2 证书 SHA-256 `468b842eb1ce2b3023a73068f0862ac13bd4c6760c32c963dc191b4f3b0dc851`。这里记录的是 manifest/证书提取结果，不是完整 APK 签名密码学校验。证据在 `diagnostics/pico-apk-inspection.json`。实际 Android API 和当前应用签名尚未读回，暂不根据文件名或 Enterprise 名称猜选覆盖安装包。

官方当前标记 Latest 的 [SwanV1.0 / EnterpriseSDK](https://github.com/XR-Robotics/XRoboToolkit-Unity-Client/releases/tag/SwanV1.0)发布说明明确针对 **Swan 设备**，不是凭名称就适用于 PICO 4 Ultra Enterprise 的升级包，本轮没有下载它作为部署候选。

**双腕关键差异：**v1.1.1 的 [`UIOperate.cs`](https://github.com/XR-Robotics/XRoboToolkit-Unity-Client/blob/v1.1.1/Assets/Scripts/UI/UIOperate.cs#L198)在切换到独立 `MotionTracking` 模式时调用 `CheckMotionTrackerModeAndNumber(..., MotionTrackerNum.ONE)`；已审查开发 commit `cdc53166` 仍是 ONE。数据发送代码确实会遍历多个已连接 SN，但“能够遍历”不等于发布版已经正确请求两个 Tracker。因此，已下载官方 APK 可作为连接/现状核对基线，**双独立腕 Tracker 仍待实机验证**。如果设备端只启用一个，后续需要在隔离 Unity 集成中显式请求 TWO，并处理已有模式下的数量检查、完成回调和每路有效性；不能只靠 PC 把同一个 Tracker 当作两侧。

## 16:02 起的临时连接窗口

已启动仅用于头显输入的 PCService，供头显连接 **`192.168.31.92`**。启动记录：`diagnostics/pico-enterprise-service-running.json`。

- 服务 PID：`3053833`；监督进程/进程组：`3053832`。
- 2026-11-11 16:02:21 PST 开始，`timeout 15m` 自动结束，约 **16:17:21 PST** 到期；这里是历史记录，使用前用 `ss -ltnp '( sport = :60061 or sport = :63901 )'` 核对仍在监听。
- 提前停止本轮进程组：`kill -TERM -- -3053832`；先核对 PID 的命令仍为本轮 `timeout` / `RoboticsServiceProcess`，避免会话恢复后误用旧 PID。
- 30 秒、5 Hz 的有限 probe 已完成：150 次读取，0 Tracker，时戳均为 0，退出 1。记录在 `diagnostics/pico-enterprise-wait-input.jsonl`、`diagnostics/pico-enterprise-wait-sdk.log`。没有无限循环探测。
- 服务继续等待用户连接；probe 已退出，不会持续占用 SDK 订阅。如果头显显示 WORKING 并开启 Send，可再次运行上面的 5 秒 probe。

此连接窗口没有启动机器人/手部输出，也没有证明头显已经连上。

## 两个独立 Tracker 的最小源码补丁

已提供 `patches/pico-two-trackers.patch`，基于官方 Unity commit **`cdc53166b0bf412efae71046c6a225eb5091605f`**：`UIOperate.OnBodyModeDrop()` 的独立 Motion 分支请求 `MotionTrackerNum.TWO`，并在每次选择该模式时检查数量，即使系统已经处于 MotionTracking。没有改手套映射、位姿变换、数据发布或设备固件。

核对结果：

- 该项目内置 PICO Integration SDK **3.1.2**，实际枚举包含 `NONE=0, ONE=1, TWO=2, THREE=3`；此版本 `CheckMotionTrackerModeAndNumber()` 的注释允许独立 Motion 模式请求 0–3 个，包装层向 native 原样传递数量。故补丁不依赖将 SDK 升级到 3.4。[已审查 SDK 源码](https://github.com/XR-Robotics/XRoboToolkit-Unity-Client/blob/cdc53166b0bf412efae71046c6a225eb5091605f/PICO%20Unity%20Integration%20SDK/Runtime/Scripts/Features/PXR_MotionTracking.cs#L1358)
- native 调用要求 `UPxr_GetAPIVersion() >= 0x200030C`；实际头显 Runtime/API 仍未读回，枚举存在不代表本机运行时调用已经成功。
- 下载的 `UIOperate.cs` Git blob hash 与官方 tree 中的 `5688eb2b4ec7af8d04077bbfc2195ed90d88f88e` 一致；在隔离原始文件目录执行 `git apply --check` 通过。记录：`diagnostics/pico-two-trackers-patch-check.json`。
- 主代理进一步移除仅在模式变化时才检查数量的条件；修订后的补丁再次通过 `git apply --check`，SHA-256 和检查结果保存在同一诊断文件的 `root_revision` 字段。
- **未 Unity 编译、未安装补丁 APK、未实机验证。** 这是可审阅源码补丁，不是双腕接通验收。

保留的限制：检查函数可能启动系统连接/标定面板，返回成功不等于两个 SN 的有效位姿已经到达；完成回调和两路实际输入仍需实机核对。既有缓存清理、逐路时间戳和有效性问题也仍需处理。补丁修复了模式不变时跳过数量请求的问题，但不宣称解决上述输入有效性问题。

构建环境：在当前 PATH、`/opt` 和用户常见 Unity 目录未找到可用 Unity Editor/Hub。项目 [`ProjectVersion.txt`](https://github.com/XR-Robotics/XRoboToolkit-Unity-Client/blob/cdc53166b0bf412efae71046c6a225eb5091605f/ProjectSettings/ProjectVersion.txt)锁定 **Unity 2022.3.16f1**（revision `d2c21f0ef2f1`），需要 Android Build Support 及 Android SDK/NDK/OpenJDK 模块。不能将补丁检查当作 Unity 构建通过。

准备独立 Unity 工作区后，应用补丁的命令如下（尚未执行完整仓库克隆/构建）：

```bash
git clone https://github.com/XR-Robotics/XRoboToolkit-Unity-Client.git \
    "$HOME/.cache/unitree-g1-pico/unity-two-trackers"
cd "$HOME/.cache/unitree-g1-pico/unity-two-trackers"
git checkout --detach cdc53166b0bf412efae71046c6a225eb5091605f
git apply --check /home/jack-xiang/unitree-g1/patches/pico-two-trackers.patch
git apply /home/jack-xiang/unitree-g1/patches/pico-two-trackers.patch
```

后续在 Unity Hub 用 **2022.3.16f1** 导入该副本；等待原有包解析完成，核对 PICO SDK 仍为 3.1.2。打开 File → Build Settings，切换 Android，保留项目已启用的 `Assets/Main.unity` 场景，检查 External Tools 的 Android 模块路径与设备实际 API 兼容，再执行 **Build** 输出到独立 APK。自签名测试 APK 应使用独立 package ID，例如 `com.xrobotoolkit.client.twowristprobe`，保留官方已安装应用，完成设备信息与签名核对后再决定部署。没有在本轮创建密钥或改项目设置。

不把仓库的 `build_unity_android.py` 当作通用本地一键命令：已审查版本依赖企业内部 CI/签名目录，含上传流程，并会删除 `ProjectVersion.txt`。上面给出的是 Unity 编辑器的本地构建路径。

## 头显已开机、用户询问 URL（16:12 后续）

用户确认头显已开机、Tracker 尚未配对。当前选定路线使用 **XRoboToolkit 原生头显应用**：

1. 在 PICO 应用库打开 XRoboToolkit；进入 **Network → PC Service → Enter**。
2. 只输入 **`192.168.31.92`**，确认/连接。已填相同 IP 时可以点击 Reconnect；连接成功应显示 **WORKING**。
3. 先打开 **Head、Controller、Send**，验证头显与控制器输入。未配对腕部 Tracker 不妨碍先完成这一步。

这是[官方应用说明](https://github.com/XR-Robotics/XRoboToolkit-Unity-Client#pose-sync-between-xr-device-and-robot-pc)的连接操作。PCService 对头显的 TCP `63901` 接收自定义协议，**`http://192.168.31.92:63901` 不是网页入口**；`60061` 是 SDK 的本地 gRPC 端口，也不是头显浏览器入口。Remote Vision 的 Listen 输入框负责视频源，不是 PC Service 位姿连接栏。

如果用户眼前是浏览器或 TeleVision 的 URL 页面，需要先确认当前应用：那属于另一条 WebXR/网页输入路线，本轮没有部署对应的 G1 网页遥操作服务。电脑上已有的 `https://192.168.31.92:8012` 已由主代理核实属于其他项目的 **Piper 实机控制入口**，不得将其作为 G1 地址提供，也不启动或复用该服务。

需要下载安装包时，使用[官方 PICO v1.1.1 发布页](https://github.com/XR-Robotics/XRoboToolkit-Unity-Client/releases/tag/v1.1.1)，或下面的官方 HTTPS 资产直链。它们只用于安装准备，不能直接操控机器人：

- [普通版 APK，最低 Android API 30](https://github.com/XR-Robotics/XRoboToolkit-Unity-Client/releases/download/v1.1.1/XRoboToolkit-PICO-1.1.1.apk)
- [SDK29 兼容版 APK，最低 Android API 29](https://github.com/XR-Robotics/XRoboToolkit-Unity-Client/releases/download/v1.1.1/XRoboToolkit-PICO-1.1.1-SDK29.apk)

已准备独立目录 `~/.cache/unitree-g1-pico/apk-download/`，仅含两份未修改官方 APK 与中文下载说明页；截至本次记录**没有启动 HTTP 下载服务**。优先使用上面的官方 HTTPS 链接。准备记录：`diagnostics/pico-apk-download-directory.json`。

Tracker 配对入口是头显中的 **PICO Motion Tracker** 应用，可在应用库找到；它负责配对、连接与标定。[PICO 官方应用页](https://store-global.picoxr.com/en/detail/1/7198091750718816316) 在应用内点击 Pair/配对，按界面引导使每个 Tracker 进入配对模式；官方测试流程采用长按 Tracker 按钮约 6 秒后开始配对。[PICO 官方检查流程](https://developer.picoxr.com/document/distribute/functional-requirement-checklist-for-official-apps/) 两个设备均显示连接后，本方案使用 **Object Tracking / 独立 MotionTracking**。具体双 Tracker 请求仍受前面记录的发布版 ONE 限制及未构建补丁影响；配对成功本身不等于两个腕部有效位姿已经到达电脑。

本轮只读复查：ADB 仍为空；5 秒/25 次读取仍未出现非零顶层时戳或格式有效的头显/控制器位姿缓存，Tracker 数量为 0。证据：`diagnostics/pico-headset-on-input.jsonl`、`diagnostics/pico-headset-on-summary.json`。没有因 Tracker 未配对进行持续等待。

输入服务已检查旧 PID 的完整命令行后，只重启本项目实例，连接窗口延长到 30 分钟：

- 新服务 PID **`3092999`**；监督进程/进程组 **`3092998`**；旧 `3053832/3053833` 已结束。
- 16:12:44 PST 启动，约 **16:42:44 PST 自动到期**。实际存活以当前 `ps` / `ss` 结果为准。
- 只启动本轮 `RoboticsServiceProcess`，没有启动 HTTP 或机器人输出。
- 提前停止前先核对进程命令，再运行 `kill -TERM -- -3092998`。
- 完整重启记录：`diagnostics/pico-enterprise-service-extended.json`。

## 用户确认 connected 后的实际读取（16:39–16:40）

已观察到 `192.168.31.46:46226 → 192.168.31.92:63901` 的 TCP ESTABLISHED 连接，但 **本轮尚未收到位姿输入**。用现有 SDK/probe 读取 5 秒、50 次后的结果：

| 数据项 | 实测结果 |
| --- | --- |
| 头显位姿 | 50 次均为 `[0,0,0,0,0,0,0]`，四元数模长为 0，格式检查不通过 |
| 左/右控制器位姿 | 均为同样的 7 个 0，未观察到有效格式位姿 |
| 顶层时戳 | 全部 0，无递增 |
| 独立 Tracker 时戳 | 全部 0，无递增 |
| 独立 Tracker | count=0，没有 Tracker SN；没有左右绑定 |
| SDK 日志 | 成功连接本地 PCService，但本轮未出现设备状态输入 |

证据：`diagnostics/pico-connected-input.jsonl`、`diagnostics/pico-connected-summary.json`、`diagnostics/pico-connected-sdk.log`。这不属于有效坐标、单位或硬件跟踪验证，也不能仅凭零缓存确定头显里的哪一个开关关闭了。

进一步检查 PCService 日志发现：应用连接 UID `PA98Y0QGL9030086G` 已于 **16:36:44** 标记 offline/remove tcp；随后虽然 socket 仍显示 ESTABLISHED，`ss -ti` 显示接收已有约 90 秒没有更新。这个 UID 是头显应用连接标识，不是某只腕部 Tracker 的 SN。应用层活跃状态不能由 TCP 连接存在代替。有关日志已保存到 `diagnostics/pico-connected-service-before-restart.log`。

下一步头显操作：佩戴并保持头显唤醒，让 XRoboToolkit 位于前台；点击 **Reconnect** 恢复 **WORKING**，勾选 **Head、Controller**，开启 **Data & Control → Send**。如果启用了 **Switch w/ A Button**，需确认 A 键没有把 Send 切回关闭。先读取头显和控制器流；Tracker 尚未配对不应阻止这一步。随后在 PICO Motion Tracker 系统应用完成两只配对，选择独立 Object/Motion 模式，再按实际 SN 核对左右侧；不按返回数组顺序猜左右。

旧输入服务原定 16:42:44 到期，且应用层已离线；本轮读取完成后核对了旧 PID 的完整命令行，只重启本任务输入实例延长 30 分钟，没有操作其他应用或机器人服务：

- 新服务 PID **`3196921`**；监督进程/进程组 **`3196920`**。
- 16:40:47 PST 启动，约 **17:10:47 PST 自动结束**；旧 `3092998/3092999` 已停止。
- 输入地址仍是 **`192.168.31.92`**。服务端已恢复监听，用户需要在头显点击 Reconnect。
- 提前停止前先核对 PID 命令，再执行 `kill -TERM -- -3196920`。
- 重启记录：`diagnostics/pico-connected-service-renewed.json`。
