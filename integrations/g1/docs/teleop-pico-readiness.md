# 本轮完整遥操作准备：PICO 输入与 APK

检查时间：2026-11-11 18:09 PST 起；已按后续澄清更正。此前“ready”的表述来自我们的进度报告，实际状态是 **电脑端 SDK 已编译，双腕 Unity 工程及补丁已准备好，新双 Tracker APK 尚未生成**。用户没有另一个待提供的 APK；不能再把查找新文件列为用户待办。

## 当前结果

**官方 PCService 输入服务和原版右腕单 Tracker 流已恢复；我们尚未构建出双腕 APK。** 主代理的 10 秒接收记录有 854 个匹配右腕的报文，SN 为 `PC2310MLL9241957G`；`Motion.len=1`，数组长度为 2，不能把数组尾项计作第二只有效 Tracker。新双腕 APK 的直接构建缺口是本机没有可用的 **Unity 2022.3.16f1 Android 构建环境**，详见下节。

| 项目 | 本次实际结果 | 证据范围 |
| --- | --- | --- |
| 旧 PCService | 原 supervisor PID `3303147` 已消失；启动前没有任何 `RoboticsServiceProcess` 实例 | `/proc` 与进程参数检查 |
| 启动前端口 | TCP `63901`、`60061` 均无监听 | `ss -lntp` |
| 新 PCService | 官方缓存二进制已运行，限时 60 分钟；启动后两个端口均监听 | 见下节 PID/PGID 和记录 |
| 头显连接 | 主代理后来实收 854 个右腕报文，0 个双 Tracker 报文；无解析失败和回调丢弃 | 单 Tracker 传输已恢复；旧包没有 confidence/trackingValid 元数据，仍不表示可运动 |
| ADB | 现有 ADB 工具运行成功，设备列表为空 | 无法通过 ADB 核对当前头显已安装的包和版本 |
| 双腕 Unity 工程 | `dual-wrist-client` 有预备源码/构建入口；没有新 APK、`g1-wrist-build.log`、`Library`、`Temp`、`Logs` | 工程准备完成，尚未实际 Unity 编译 |
| 电脑 SDK | `_core.abi3.so` 与 `libPXREARobotSDK.so` 确实存在 | 电脑端 Python/native SDK，与 Android APK 是不同产物 |

原始记录：[本地构建与 ADB 盘点](../diagnostics/teleop-pico-artifacts-2026-11-11.json)、[扩大 APK 搜索](../diagnostics/teleop-pico-apk-search-2026-11-11.json)、[服务启动](../diagnostics/teleop-pico-service-2026-11-11.json)、[主代理单 Tracker 复测](../diagnostics/teleop-pico-current-20261111-1810.jsonl)。搜索记录中的“用户确认已有新 APK”是澄清前的历史上下文，应以本文首段更正为准。

## 实际构建缺口：可运行的 Unity Android 构建环境

本轮再次检查 PATH，以及 `/opt`、`/usr/local`、`~/.local`、`~/Applications` 和 Unity/UnityHub 常用位置，没有发现 Unity Editor 或 Hub 可执行文件。工程 `ProjectVersion.txt` 固定要求 **2022.3.16f1（d2c21f0ef2f1）**。未找到配套 Android SDK/NDK 或 `sdkmanager`；PATH 没有 `javac`，但在 PrismLauncher 私有 Java 目录发现了一个 `javac`，因此不能笼统声称整台电脑没有 JDK。这个私有编译器并不能替代缺失的 Unity Editor 与 Android Build Support。

已检查的用户常见 Unity `.ulf` 文件不存在，但这只说明没有发现本地激活凭据，**不代表用户或机构没有许可证**。当前首先缺 Editor，尚无法实际打开它验证许可状态。Unity 官方要求 Android Build Support、SDK、NDK、JDK；许可证应通过官方 Hub 或相应组织许可流程正常激活。[Unity 2022.3 Android 环境要求](https://docs.unity3d.com/2022.3/Documentation/Manual/android-sdksetup.html)、[Unity 官方许可证管理](https://docs.unity.com/en-us/hub/manage-license)

因此，本机当前不能执行已准备的 `G1WristBuilder.BuildBatch`。下一项具体工作是为该工程配齐 **已正常激活的 Unity 2022.3.16f1 + 配套 Android Build Support/SDK/NDK/OpenJDK**，再执行现成 batch 构建入口、核验产物。不是等待用户提供一个并不存在的新 APK，也不是继续修改 PC SDK。本轮没有下载多 GB 编辑器、安装系统软件、登录许可账户或尝试绕过激活；现成构建命令见 [双腕构建准备](pico-two-tracker-build.md)。

## 新服务的准确生命周期

- 可执行文件：`/home/jack-xiang/.cache/unitree-g1-pico/pc-service/opt/apps/roboticsservice/RoboticsServiceProcess`
- 二进制 SHA256：`fe6f4963a468178e7667a103374a80b87bbdc63d2e6c2b4006f1cd9e1f6f1bbc`
- 启动：**18:09:43 PST**；预计 **19:09:43 PST** 发送 TERM；若未退出，5 秒后 KILL。
- timeout supervisor PID：`3530961`；进程组 PGID：`3530961`；实际服务 PID：`3530962`。
- 头显连接目标：**电脑 `192.168.31.92:63901`**；本机 SDK 端口 `127.0.0.1:60061`。
- 日志：[teleop-pico-pcservice-2026-11-11.log](../diagnostics/teleop-pico-pcservice-2026-11-11.log)。

本次服务的提前停止命令（仅在 PID/进程组仍对应上述实例时使用）：

```bash
kill -TERM -- -3530961
```

这是 PICO 网络输入服务，不是 Unitree 或 BrainCo 控制服务。本代理未创建 native SDK feedback receiver、未采集 PICO 帧、未向头显发送控制命令，接收器由主代理独占安排。当前 PCService 的单 SDK feedback 客户端限制仍适用，不要同时启动旧 `pico_probe.py` 与新的原始报文接收器；依据见 [PICO 输入验证](pico-input-validation.md)。

## APK 实际搜索范围与内容

在误解来源澄清前，曾重新执行 `rg --files --hidden --no-ignore` 搜索 `*.apk` 和 `*.APK`，覆盖：

```text
/home/jack-xiang
/tmp
/mnt
/media
/opt
```

共发现 6 个 APK：4 个已缓存的官方 PICO `1.1.1` 文件/副本，以及 2 个 2026-11-10 的 Quest 工程旧 APK。没有发现新的双腕 APK。部分 `/tmp/systemd-private-*`、`/tmp/snap-private-tmp` 与 `/opt/bytedance/lark/vulcan` 无读取权限，错误原样保存在诊断记录；未尝试越过这些与构建无关的目录权限。

现有两种 PICO APK 的本次 SHA256 与此前已解析 manifest/签名的记录完全一致：

| 文件 | 本次 SHA256 | 已有 manifest 记录 |
| --- | --- | --- |
| `XRoboToolkit-PICO-1.1.1.apk` | `6b2bb282405673d24abcb1980e3478b8f1052e90f7207b1f24cc56a59f8d8261` | `com.xrobotoolkit.client`，versionName `1.1.1`，versionCode `1` |
| `XRoboToolkit-PICO-1.1.1-SDK29.apk` | `6089e1ce175cf3e292cea5919c2b97711151186ca39c4a5569786a12fc167eb2` | 同上 |

来源：[既有 APK manifest/签名解析](../diagnostics/pico-apk-inspection.json)与本次重新计算的 hash。通过文件哈希一致关联旧的解析结果；这些文件仍为官方旧版 APK，不是新双 Tracker 构建，也没有重复下载、安装或覆盖应用。

## 我们实际构建出 APK 后的具体核验

1. 记录实际路径、文件大小、修改时间和 SHA256，解析 manifest 的 package、versionName、versionCode、min/target SDK 及 APK 签名。
2. 与 [双腕构建准备](pico-two-tracker-build.md) 的 `G1WristBuilder` 对照，预期独立包名为 `com.xrobotoolkit.client.g1wrists`、默认版本 `1.1.2-g1wrists.1` / code `12`。
3. 核对 APK 使用的源码/构建日志或通过应用运行数据确认双 Tracker 和有效性补丁确实在产物内。manifest/hash 能确认身份，不能独自证明 `MotionTrackerNum.TWO` 或断连数据清除逻辑生效。
4. 不自动覆盖头显应用。已安装应用、Android 系统与连接状态尚未通过 ADB 读回。安装/启动后的验收仍需两个不同 Tracker SN、各自的新鲜数据与跟踪质量、遮挡/断开失效行为，然后再绑定左右腕并核对安装变换。

本轮仅新增本文件和 `diagnostics/teleop-pico-*` 记录，并恢复限时 PICO 输入服务；未改 PLAN、README、原 Wuji 项目或头显应用，未启动任何机器人控制服务。
