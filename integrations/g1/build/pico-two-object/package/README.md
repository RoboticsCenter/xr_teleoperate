# G1 Wrists 双 Object Tracker 安装包

产物：`XRoboToolkit-G1-Wrists-1.1.1-two-object.apk`。

- 应用名称：**XRoboToolkit G1 Wrists**。
- 独立包名：`com.xrobotoolkit.client.g1wrists`。
- 版本：`1.1.1-g1wrists.1`（versionCode 2），最低 Android SDK 30 与原版一致。
- 本地开发签名，与官方签名不同。无需卸载或覆盖 `com.xrobotoolkit.client`。
- Object 菜单请求两只 Tracker，已有 Object 模式也重新执行原 SDK 的数量检查。

这是原版 1.1.1 的精确原生补丁重打包，并非 Unity 项目重新编译。补丁只改动 `UIOperate.OnBodyModeDrop` 中的两条 ARM64 指令、共 3 个字节。`../native/manifest.json`、`before.txt`、`after.txt` 保存方法映射与反汇编依据。原 APK、全部 DEX、Unity 资源、IL2CPP metadata 和其他原生库保持原字节。没有修改授权、追踪质量或校准检查。

已验证：原版与新包的签名、新包 package/label/version、ZIP 完整性、ZIP 对齐、完整资源和代码比对。**尚未安装到头显，也未验证头显实际启动或双 Tracker UI。** 本地静态通过不代替头显验证。

## 重新生成

在本项目根目录执行（需要现有 Java/JDK 与 Python 3.10+）：

```bash
python3 build/pico-two-object/package/setup_tools.py
python3 build/pico-two-object/package/build_apk.py
python3 -m unittest discover -s build/pico-two-object/package/tests -v
```

原 APK 默认路径是 `~/.cache/unitree-g1-pico/XRoboToolkit-PICO-1.1.1.apk`，可用 `--original-apk` 指定。构建器固定检查原 APK、原生库和补丁证明的 SHA-256，拒绝未声明的原生修改。`--patched-lib` 和 `--patch-manifest` 可以指定相同证明结构的文件。它只写当前 `package/` 目录，不运行 adb，也不启动任何硬件服务。

工具来自官方发布地址并核对发布校验值；不会安装系统软件、运行 sdkmanager 或自动同意许可证。下载、解包、编译、对齐、签名、验证记录在 `tool-provenance.json`、`package-manifest.json` 和 `logs/`。生成使用固定工具与同一个本地签名密钥；这描述可重复执行的流程，不承诺跨机器位级相同的构建结果。

`keys/g1-wrists-local.p12` 为本机专用开发密钥（目录 0700、文件 0600），不要公开分发；保留它可以为此独立包签署后续更新。开发 key alias 为 `g1wrists`，采用通用 Android 开发口令，不是发布凭据。密钥、下载工具与 APK 已列入本目录 `.gitignore`。

原 APK 的 `extractNativeLibs=true`、压缩 `.so` 和未压缩 Unity 数据保持不变。使用官方 zipalign 在签名前完成 ZIP 对齐，再验证签名和对齐；这不表示修改过 ELF 或验证了 16 KB ELF 兼容性。

## 来源

- [XRoboToolkit 官方 v1.1.1 发布](https://github.com/XR-Robotics/XRoboToolkit-Unity-Client/releases/tag/v1.1.1)；[发布 API 与原 APK SHA-256](https://api.github.com/repos/XR-Robotics/XRoboToolkit-Unity-Client/releases/tags/v1.1.1)。
- [apktool 3.0.3 官方发布及 SHA-256](https://apktool.org/blog/apktool-3.0.3/)。
- [Google Android 工具仓库清单](https://dl.google.com/android/repository/repository2-3.xml)（Build Tools 35.0.1 的原始 SHA-1 与大小）。
- [Android apksigner 官方说明](https://developer.android.com/tools/apksigner)与 [zipalign 官方说明](https://developer.android.com/tools/zipalign)。

原 APK SHA-256：`6b2bb282405673d24abcb1980e3478b8f1052e90f7207b1f24cc56a59f8d8261`。

本次 APK SHA-256：`31e8344f6c80bb80234b48817800854005a35f35d023c642a4ce36e7f9a2de32`。

## Signing credentials

Before invoking `build_apk.py`, provide `G1_WRISTS_DEBUG_STOREPASS` in the process environment. Reusing the existing local keystore requires its existing password; a new local keystore requires your chosen password. The build exits before reading or generating artifacts if this variable is absent or empty. Passwords, signing keys, and generated APKs are not stored in Git.

For an interactive shell, enter the password without echoing it:

```bash
read -r -s -p 'Keystore password: ' G1_WRISTS_DEBUG_STOREPASS
export G1_WRISTS_DEBUG_STOREPASS
# Run the build command documented above.
unset G1_WRISTS_DEBUG_STOREPASS
```
