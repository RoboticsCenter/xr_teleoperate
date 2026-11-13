# PICO 双腕客户端安装包与构建记录

2026-11-11 20:20 更新：**用户确认新应用安装并允许两只 Object Tracker；电脑实读双路输入验证通过。** 本次对固定官方 v1.1.1 APK 中已定位的 IL2CPP 方法修改两条指令后重新打包、使用本地证书签名。没有运行 Unity 全量源码构建，也没有包含下方历史准备中的 validity 补丁。

## 现场双输入验证

用户确认安装并打开新应用，Object允许两只、Send开启。随后从既有共享reader读取5.012秒：342个唯一帧全部双腕新鲜、Head有效，每只位姿变化339次，网页tracker.ready=true。左SN `PC2310MLLA050051G`、右SN `PC2310MLL9241957G`。来源：[实读报告](../diagnostics/pico-two-object-installed-live.json)、[逐帧证据](../diagnostics/pico-two-object-installed-live.jsonl)。使用原分支`read_frame`与`pose_valid`/pose-change age，没有放宽或追加门槛。

安装和前台应用由用户确认，尚无ADB包查询；实读证明当前两路持续变化，未验证所有重启/模式切换场景，也未采集额外厂商confidence。本次仅检查输入；同期用户的网页LAUNCH会话已READY，双臂物理跟随尚未验收。

## 当前可安装产物

- 下载页面：<http://192.168.31.92:8027/>（在 PICO 浏览器打开，与电脑处于同一 Wi-Fi）。
- APK：[XRoboToolkit-G1-Wrists-1.1.1-two-object.apk](../build/pico-two-object/package/XRoboToolkit-G1-Wrists-1.1.1-two-object.apk)，34,623,033 bytes。
- SHA256：`31e8344f6c80bb80234b48817800854005a35f35d023c642a4ce36e7f9a2de32`。
- 应用 **XRoboToolkit G1 Wrists**；包名 `com.xrobotoolkit.client.g1wrists`；versionCode 2，versionName `1.1.1-g1wrists.1`；minAPI30、target31。
- 本地签名证书 SHA256：`78ebecabd180610a36b1d43b4ba6d1334c92719054cabcd65693d04bee05832e`；独立安装，不覆盖官方签名应用。

打开下载并完成安装后，关闭原 XRoboToolkit，打开 **XRoboToolkit G1 Wrists**，选择 Object 并完成两只 Tracker 提示，启用 Head，连接 `192.168.31.92`，打开 Send。当前电脑 ADB 列表为空且头显 .31.46:5555 拒绝连接，所以没有远程安装。安装完成后读取现有共享 Tracker reader 验证，不另启第二个 native SDK 客户端、不自动触发机器人 LAUNCH/START。

固定原始 APK SHA256 `6b2bb282405673d24abcb1980e3478b8f1052e90f7207b1f24cc56a59f8d8261`，与官方发布资产一致。`UIOperate.OnBodyModeDrop` token `0x060001a0`，VA `0x1002534`：Object 分支去掉“已经是 Object 则跳过”条件，并把 `CheckMotionTrackerModeAndNumber(MotionTracking, ONE)` 的数量参数改为 TWO。全库仅两条指令/三字节变化，其他模式逻辑不变；5项实际 ARM64 控制流语义测试通过。原始读取循环本来就遍历全部连接 Tracker。

5项打包测试、独立审查、签名和 ZIP 对齐验证通过。除指定 libil2cpp.so 外，DEX、所有 assets、其他 native 库逐字节等同原始 APK。HTTP 完整下载哈希也已核对。输入容忍仍沿用用户指定分支；没有新加质量、焦点或断流门槛。上述检查不等于头显已安装成功、运行时能配对两只或机器人双臂已验收。

复现与证据：[原生修改说明](../build/pico-two-object/native/README.md)、[打包说明](../build/pico-two-object/package/README.md)、[打包清单](../build/pico-two-object/package/package-manifest.json)、[下载服务记录](../diagnostics/pico-apk-download-server.json)。

## 以下为此前完整 Unity 源码构建准备（未执行）

这些步骤包含另一份 validity 补丁，**不代表当前安装包内容，也不改变当前遥操作采用原分支容忍的决定**。本机仍缺 Unity Editor 与对应 Android 模块；现已找到可用 JDK，并为上述重打包流程缓存 Android build-tools。下方“没有 javac”等表述是早期环境盘点。

## 现场限制与官方发布核对

用户已确认当前 XRoboToolkit 的 Object 模式只允许一只 Tracker；固定官方 `UIOperate.cs` 实际请求 `MotionTrackerNum.ONE`。本项目原有 [pico-two-trackers.patch](../patches/pico-two-trackers.patch) 在每次选择独立模式时请求 TWO，包括已经处于 MotionTracking 的情况。

2026-11-11 再查官方 Releases：适用于本轮 PICO 的公开版本仍为 v1.1.1，其两个 APK 已缓存；最新 SwanV1.0 发布说明明确针对 Swan。未发现官方已经构建好的 PICO 4 Ultra 双 Tracker 修正版，因此没有拿 Swan APK 替换当前客户端。[官方 Releases](https://github.com/XR-Robotics/XRoboToolkit-Unity-Client/releases)

发布清单保存在 [pico-build-official-releases.json](../diagnostics/pico-build-official-releases.json)。只说明本次查到的公开发布，不声称所有未来版本都只有单 Tracker。

## 已备齐的具体构建输入

完整官方工程已解包至：

```text
/home/jack-xiang/.cache/unitree-g1-pico/dual-wrist-client
```

固定 commit：`cdc53166b0bf412efae71046c6a225eb5091605f`。下载的源码归档约88.6MB，SHA256为 `4af9a633d104229515a2a44062b86b92bf5b3927b4acc78ec29e12b7c5098070`。已依次应用两份补丁：

1. `patches/pico-two-trackers.patch`：独立模式请求 TWO。
2. [patches/pico-tracker-validity.patch](../patches/pico-tracker-validity.patch)：清除旧数据并传输原生跟踪质量。

工程内另有 `Assets/Editor/G1WristBuilder.cs`，源文件保存在 [pico-build-G1WristBuilder.cs](../diagnostics/pico-build-G1WristBuilder.cs)。它由同一固定官方 `VoiceDuplexBetaBuilder.cs` 的构建方式调整而来，仅负责本地 BuildPipeline；没有上传、安装或启动步骤。

构建入口输出独立应用 **XRoboToolkit G1 Wrists**，包名 `com.xrobotoolkit.client.g1wrists`，ARM64/IL2CPP、min API30、target API31，使用本机构建的默认调试签名。它不具备官方 APK 的签名私钥，不伪装成可覆盖官方包的更新。这里仅准备构建设置，尚未在头显验证启动和运行权限。

## 未编译的有效性补丁行为（当前 APK 不包含）

- 每次跟踪循环开始前移除之前的 `Motion`；只有选择独立模式、运行时处于 MotionTracking 且应用有焦点才重新填入。
- 每轮创建新的 joints 数组；断开一只、查询失败或数量下降时，不留下上一轮数组尾部。
- 核对 native 返回值、SN 数组与数量范围；坏数量不会被当作成功数组继续访问。
- 每个成功查询条目发送 `confidence` 和 `trackingValid`；SDK 的 STATIC_ACCURATE/6DOF_ACCURATE 对应 true，3DOF_NOT_ACCURATE/6DOF_NOT_ACCURATE 对应 false。低质量位姿仍作为明确标记的诊断条目传输，机器人接收端必须拒绝跟随它。
- 发送 `Motion.sequence`、`Motion.timeStampNs`、每条 `queryTimeStampNs` 与 `timestampSource=application_query`。这些是应用查询序号/时间，**不是传感器采样时间**；现有 SDK 结构没有提供可在本补丁中诚实填入的传感器时戳。
- 不改变位置单位、坐标系、四元数顺序或任何重定向算法。

PC 端需使用完整报文接收器，并检查本地到达时间、应用焦点、countValid、序号、SN、confidence/位姿。旧 Python SDK 的独立缓存 getter 不会因为客户端多了字段就自动获得有效性保证。参考 [PICO 输入验证](pico-input-validation.md)。

## 早期全量源码构建环境盘点

固定工程要求 **Unity 2022.3.16f1 (d2c21f0ef2f1)**。本机 PATH、`/opt`、用户 Unity/UnityHub 标准目录以及 `.local/.config` 文件扫描均未发现 Editor/Hub；没有 `javac`、Gradle、sdkmanager，只有 Java 运行时。公开 Unity Linux Editor 下载 HEAD 返回 **3,075,118,144字节**，还不包括 Android 模块；本轮未下载编辑器或改变系统环境。

需要安装/提供一个已激活的该版本 Editor，包含 Android Build Support、SDK/NDK Tools、OpenJDK。优先用该 Editor 配套模块，避免自行混装版本。Unity 官方要求通过 Hub 管理和激活许可证；本轮没有登录账户、接受许可条款或假定用户已有可用许可证。[Unity Android 环境要求](https://docs.unity3d.com/2022.3/Documentation/Manual/android-sdksetup.html)、[指定 Editor 版本](https://unity.com/releases/editor/whats-new/2022.3.16f1)、[Hub 许可证管理](https://docs.unity.com/en-us/hub/manage-license)

证据：[环境盘点](../diagnostics/pico-build-environment-2026-11-11.json)、[编辑器包大小](../diagnostics/pico-build-editor-download-size.json)。这是一项真实环境缺失，不是需要用户批准普通代码修改。

## 环境备齐后的构建命令

下面的命令已经按官方 Editor batch 参数和本地构建入口核对，但**没有实际执行 Unity 编译**。将 `PICO_UNITY_EDITOR` 改成实际已激活的 Editor 路径；其余文件在本机已经备齐。

```bash
set -euo pipefail
PICO_UNITY_EDITOR=/path/to/Unity/2022.3.16f1/Editor/Unity
PICO_WRIST_PROJECT=/home/jack-xiang/.cache/unitree-g1-pico/dual-wrist-client
PICO_WRIST_APK="$PICO_WRIST_PROJECT/Builds/Android/XRoboToolkit-G1-Wrists.apk"

test -x "$PICO_UNITY_EDITOR"
test -f "$PICO_WRIST_PROJECT/Assets/Editor/G1WristBuilder.cs"
env G1_WRIST_APK_PATH="$PICO_WRIST_APK" \
    G1_WRIST_VERSION_NAME=1.1.2-g1wrists.1 \
    G1_WRIST_VERSION_CODE=12 \
    "$PICO_UNITY_EDITOR" -batchmode -nographics -buildTarget Android \
    -projectPath "$PICO_WRIST_PROJECT" \
    -executeMethod G1WristBuilder.BuildBatch \
    -logFile "$PICO_WRIST_PROJECT/g1-wrist-build.log" -quit
test -s "$PICO_WRIST_APK"
sha256sum "$PICO_WRIST_APK"
```

构建入口核对启用场景存在、结果为 BuildResult.Succeeded；失败抛出 BuildFailedException，并在 finally 恢复更改前的 PlayerSettings。成功后还需检查 APK manifest/签名、在头显安装并验证，不能只以文件名判断已支持两个 Tracker。

**不要运行仓库根目录的 `build_unity_android.py` 作为本轮构建。** 该文件使用 Python2 风格 `os.environ.has_key`，会删除 ProjectVersion、调用内部签名路径和上传服务；它不是当前所需的公开、本地构建入口。官方 AutoBuilder 还会递增版本并弹窗，故本项目提供单独 batch 入口。[固定官方构建脚本](https://github.com/XR-Robotics/XRoboToolkit-Unity-Client/blob/cdc53166b0bf412efae71046c6a225eb5091605f/build_unity_android.py)

## 从干净工程重建输入

缓存已备齐，无须现在重复执行。以下是缓存丢失或换构建机器时的固定版本步骤；空临时目录避免覆盖别的 Unity 工程。

```bash
set -euo pipefail
G1_INTEGRATION=/home/jack-xiang/unitree-g1
PICO_REBUILD_PROJECT=$(mktemp -d /tmp/g1-pico-wrists.XXXXXX)
git -C "$PICO_REBUILD_PROJECT" init
git -C "$PICO_REBUILD_PROJECT" remote add origin \
  https://github.com/XR-Robotics/XRoboToolkit-Unity-Client.git
git -C "$PICO_REBUILD_PROJECT" fetch --depth=1 origin \
  cdc53166b0bf412efae71046c6a225eb5091605f
git -C "$PICO_REBUILD_PROJECT" checkout --detach FETCH_HEAD
git -C "$PICO_REBUILD_PROJECT" apply --check \
  "$G1_INTEGRATION/patches/pico-two-trackers.patch" \
  "$G1_INTEGRATION/patches/pico-tracker-validity.patch"
git -C "$PICO_REBUILD_PROJECT" apply \
  "$G1_INTEGRATION/patches/pico-two-trackers.patch" \
  "$G1_INTEGRATION/patches/pico-tracker-validity.patch"
cp "$G1_INTEGRATION/diagnostics/pico-build-G1WristBuilder.cs" \
  "$PICO_REBUILD_PROJECT/Assets/Editor/G1WristBuilder.cs"
```

## 已验证与未验证

已完成官方文件 Git blob/SHA256 核对；两补丁在固定原始文件和完整源码归档上均 `git apply --check`、实际应用成功。完整预备工程记录：[pico-build-staged-project.json](../diagnostics/pico-build-staged-project.json)，补丁检查：[pico-build-patch-validation.json](../diagnostics/pico-build-patch-validation.json)。没有冒充已通过 C#/Unity 编译、APK安装、双Tracker位姿或机器人动作测试。

安装后的必要验证是：Object 模式显示两只不同SN；两路有效质量和当前帧持续到达；遮挡/断开一只时不出现旧数组尾部；关闭Send/丢焦点时接收端停用新目标；按实际SN绑定左右腕，再完成安装变换/相对姿态标定。


后续独立实读发现：4.003秒255帧均通过原分支live，但左Tracker有一次0.602863m位置跳变（25.07ms观察间隔）。双路新鲜数据不等于准确/稳定跟踪；没有更改用户指定分支的容忍或新增跳变门控。见[独立实读报告](../diagnostics/pico-two-object-independent-live.json)。用户会话同期已进入双臂FOLLOWING；这只是软件状态，物理动作反馈尚未核验。
