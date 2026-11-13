# H2 分支的相机与视频复用审查

审查日期：2026-11-11。来源：`kevin-roboticscenter/xr_teleoperate`，分支 `h2/pico-wuji-head-camera`，固定提交 **`053ba99af55c1d8ddc6b87918cb0fa234625886e`**。本次仅审查源码与纯软件测试，未访问机器人、相机或头显，未启动服务，也未修改源仓库或本项目控制代码。

**最有价值的可摘取模块是 `head_camera/pico_video.py`。** 它把一个 HTTP JPEG 图像源转换成现有 XRoboToolkit 原生应用的 Remote Vision 视频；相机输入并不依赖 H2 关节或 GMSL 驱动。G1 端仍需提供 D435i 的采集与 JPEG 接口，不能将 H2 的 PC3 相机配置直接部署到 G1。

## 文件级结论

以下路径均相对于来源仓库的 `integrations/h2/`。

| 模块 | 结论 | 代码证据与本项目改动范围 |
| --- | --- | --- |
| `head_camera/pico_video.py` | **优先摘取，主体可保持** | `exact/read_command/camera_request` 在22–70行实现控制协议；73–82行为H.264编码器；85–189行为JPEG拉取、编码、视频重连与状态；103–127行用generation防止旧控制连接关闭新会话。改本项目图像源URL及H2描述文字，补独立入口和依赖即可开始纯视频验证。 |
| `head_camera/receiver.py` | **配置及生命周期适配后复用** | 第17行读同目录配置；第20–21行构造即启动订阅线程；30–35行依赖旧式 `teleimager.image_client.ZMQ_SubscriberManager`，并非仅复制2.0.8 server就具备该Python接口。51–57行已有图像年龄与live状态。当前配置指向H2 PC3 `.123.163:55558`；G1应改为实际来源。49行关闭的是singleton manager，集成时要核对与其他订阅器的所有权。 |
| `head_camera/video_service.py` | **可借用进程管理，不能原样启动** | 19行固定 `.runtime-video/bin/python`，25–28行构造即启动视频进程，源固定 `http://robot_host:8787/api/head-camera.jpg`；15行只凭13579端口开放就认为已有服务。应改为本项目独立配置及可验证的进程/协议身份，避免绑定H2部署目录。 |
| `quest_camera_service.py` / `quest_camera_preview.py` | **可参考，不建议整套搬入** | 前者26–29行构造即启动监督线程及HeadCameraReceiver，65–73行自动拉起 `teleimager.image_server` 和Vuer预览；通过 `quest_h2_teleop.environment()` 耦合Dex5 PR321运行环境。后者18–29行是另一条浏览器TeleVuer视频路径，非原生PICO Remote Vision所必需。其默认端口/证书和H2环境需解耦。 |
| `head_camera/teleimager_server.yaml`、`receiver.json`、`systemd/*` | **H2硬件配置不能直接使用** | YAML第12–14行是 `nvarguscamerasrc sensor-id=0`、NVMM/Argus管线；driver service第9–12行加载Unitree H2的 `YUSHU_4A_AGTH_G2Y_7.1` 驱动并检查双video节点；camera service第10–14行固定H2部署路径。当前G1已确认的是Orin NX/Ubuntu20.04与USB RealSense D435i，和来源的Thor/JetPack7.1/S56 GMSL组合不同。 |
| `head_camera/head_follow.py` / `head_only.py` | **不移植为G1控制入口** | `head_follow.py:13–14` 固定H2颈部pitch/yaw编号29/30及限位；61–75行写这两个motor slot、kp30/kd1并重算CRC。`head_only.py:23–30` 固定H2上身与slot31权重。G1当前29DoF配置没有在本项目确认的这套颈部机构与映射。视频观看本身不需要这些运动模块。 |

源码链接：[视频桥](https://github.com/kevin-roboticscenter/xr_teleoperate/blob/053ba99af55c1d8ddc6b87918cb0fa234625886e/integrations/h2/head_camera/pico_video.py)、[图像订阅](https://github.com/kevin-roboticscenter/xr_teleoperate/blob/053ba99af55c1d8ddc6b87918cb0fa234625886e/integrations/h2/head_camera/receiver.py)、[相机监督器](https://github.com/kevin-roboticscenter/xr_teleoperate/blob/053ba99af55c1d8ddc6b87918cb0fa234625886e/integrations/h2/quest_camera_service.py)、[头部输出](https://github.com/kevin-roboticscenter/xr_teleoperate/blob/053ba99af55c1d8ddc6b87918cb0fa234625886e/integrations/h2/head_camera/head_follow.py)。

## 与现有 PICO 客户端的兼容范围

- 来源README及操作文档采用现有XRoboToolkit 1.1.1原生客户端：**Remote Vision → ZEDMINI → Listen → 电脑IP**，不需要浏览器viewer，也没有要求为这条视频路径另编一个APK。参见 [OPERATOR_FLOW.md:10](https://github.com/kevin-roboticscenter/xr_teleoperate/blob/053ba99af55c1d8ddc6b87918cb0fa234625886e/integrations/h2/OPERATOR_FLOW.md#L10)。这不意味着双腕跟踪APK的问题已解决。
- `camera_request`只接受协议magic `CA FE`、版本1、camera=`ZED`、非HEVC、**2560×720**；输出限制最高30fps、1–8Mbps。编码使用PyAV的 `libx264`，不是NVIDIA硬编；来源README标注测试环境为PyAV16.0.1/NumPy1.26.4。应先在电脑端独立环境复现，不能推断G1 PC2已有这些依赖。
- 视频将一张1280×720图像复制并排，得到2560×720。**双眼看的是同一画面，不是立体深度**（`pico_video.py:159–167`）。它没有通用旋转/裁剪/双目校正模块；尺寸不同时只做resize。D435i若未来需要深度或双目显示，另属输入协议与标定工作。
- 此代码与本项目缓存中固定Unity客户端的 `CameraRequestSerializer` / `NetworkDataProtocolSerializer` 字段结构一致。该核对只涉及源码协议；本轮未实测本台PICO的视频呈现、延迟或与Tracker并行稳定性。
- 来源作者报告过H2视频实测，但也明确记录最新视频恢复改动尚待完整联合会话验证。不能把其15–20fps作者记录当成本G1的实测成绩。见 [README.md:94–116](https://github.com/kevin-roboticscenter/xr_teleoperate/blob/053ba99af55c1d8ddc6b87918cb0fa234625886e/integrations/h2/README.md#L94)。

## 初始化与失联行为

- `pico_video.py` 普通导入不会开socket或访问硬件；调用 `main()` 才监听13579和状态13581，收到OPEN_CAMERA后才拉JPEG和连接头显视频端口。它没有Unitree DDS或电机输出依赖。
- `HeadCameraReceiver`和两个Service类的构造器有线程/网络/进程副作用，不能通过实例化来做静态配置审查。原JPEG端点在 `quest_h2_web.py:846–852` 依赖 `head.status()['live']`，过期返回503；单独移植视频桥时，应为新的JPEG源保留等效过期检测，否则陈旧但持续返回200的JPEG会被重复展示。
- `head_only.py` 注释说Prepare只订阅，但实际上92–93行调用的官方helper `Init()`（`upstream/unitree-h2-head-sdk/h2_arm_sdk_dds_example.py:108–115`）同时创建 `rt/arm_sdk` publisher和subscriber；此时尚未调用Write。START后 `head_only.py:108–117` 每4ms继续Write，输入失效时保留最后目标；退出128–132行还发送101次SDK权重渐退。它既不是纯相机服务，也不符合本项目默认无publisher的只读入口约定。

## 最小复用边界

建议仅摘取 `pico_video.py` 与其 `test_head_video.py` 到本项目的独立视频模块，并保留来源提交与许可证记录；为G1补一个D435i JPEG源、配置URL与状态描述。可采用已有/适配后的TeleImager作为采集端，**不要为获取JPEG启动整个H2 dashboard、H2 arm controller或head-only程序**。

对应数据流为：

```text
G1 D435i RGB采集 → 带过期状态的JPEG接口 → 复用pico_video.VideoBridge
                                      → 原生XRoboToolkit Remote Vision
```

这条复用能新增机器人视角画面，但不会替代本项目的PICO输入有效性、Wuji映射、G1低层输出和无线命令时效处理。

## 本轮验证

在来源checkout以 `PYTHONDONTWRITEBYTECODE=1`、`PYTHONPATH=integrations/h2` 运行：

- `test_head_video.py`：**4通过，4跳过**。通过的是二进制请求布局、目的IP限制、控制帧长度/端序、截断拒绝；跳过的是H.264编码及VideoRecovery整组，因为现有解释器缺PyAV。
- `test_head_follow.py`：**7通过**，仅纯数学及fake publisher，验证来源代码的H2行为；不代表适用于G1。
- `test_head_only.py` 未执行：它导入H2/Dex5运行环境，本次未bootstrap来源的嵌套运行时依赖。

没有安装新依赖，没有启动摄像头/视频服务器或物理输出。来源相机子模块固定为官方TeleImager **`d90ab16d918324f94cf3fa70f2b8c54059012cc0`（2.0.8）**；同一集成还使用另一个旧TeleImager客户端提交 **`57cf2a40572227273fa001cd17833b755331ec97`**，迁移时应按实际导入API区分。仓库根LICENSE声明Apache-2.0，第三方依赖许可证独立；来源README明确不提供H2私有相机驱动包。
