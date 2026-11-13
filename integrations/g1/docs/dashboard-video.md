# G1 网页相机与 PICO 视频

网页相机按钮启动独立摄像头进程；打开网页、LAUNCH 双臂或导入模块都不会启动摄像头。Stop camera 关闭本次相机和 PICO 视频进程，不改变遥操作的运行状态。相机没有十秒或其他固定运行时长。

`scripts/dashboard_camera.py` 提供 `CameraManager(root, python, password, simulation=False, source_url=...)`，接口为 `start(pico_video=False)`、`stop()`、`status()`、`jpeg()`、`close()`。网页服务在 `http://127.0.0.1:8025/api/camera.jpg` 返回 `jpeg()`；无新鲜帧时应返回 503。状态包括 `running/live/error/fps/frames/age_s`、布尔 `pico_video` 和单独 `pico_error/pico_status`。

视频编码、XRoboToolkit 相机协议、headset TCP 断线重连及 JPEG 源恢复直接复用 `vendor/xr_h2/head_camera/pico_video.py`，源码未改。来源是指定仓库 `h2/pico-wuji-head-camera` 分支提交 `053ba99af55c1d8ddc6b87918cb0fa234625886e` 的 `integrations/h2/`。G1 控制与 IK 四个模块来自该集成指定的 PR321 runtime 提交 `39b79bca34b7547b21edad3fe3e642bb5cac7147`。每个文件的 SHA256 与来源在 `vendor/xr_h2/SOURCE.json`；算法代码保持原文件内容，H2 摄像头驱动和系统服务没有部署到 G1。

G1 适配部分使用 SSH 在 PC2 内存运行 `scripts/pc2_camera_source.py`。使用已有 tv Python 的 OpenCV/V4L2，枚举 RealSense 节点实际格式，选择 RGB 节点，以 JSONL 传输 JPEG。不会把固定 `/dev/video4` 当成 RGB，也不安装 pyrealsense2、驱动或相机服务。凭据从管理进程经匿名 stdin pipe 到 worker，仅内存用于 SSH 认证，不出现在命令行、环境、文件或日志。严格核对项目中已有的 PC2 SSH 主机密钥。

接收帧的新鲜期限沿用分支 `head_camera/receiver.py` 的 2 秒，源出错后的重试间隔 2 秒、采集循环等待 0.02 秒也来自该文件。掉线后相机继续尝试恢复，遥操作不会因相机错误停止。PICO 侧恢复逻辑完整保留原版：源请求失败等待 0.1 秒后重试；headset TCP 断开等待 0.5 秒后重新连接并创建新的编码器。这里没有新增遥操作输入有效性或动作容忍规则。

勾选 PICO 视频后才监听本机 TCP 13579；原版桥的内部状态端口是 loopback 13581。PICO XRoboToolkit Remote Vision 配置电脑 Wi-Fi IP（本次为 `192.168.31.92`）、ZEDMINI / H.264 / 2560×720。G1 D435i 彩色单目图复制到双眼，**不是立体深度视频**。视频桥 JPEG 源仍为电脑 loopback 的网页接口，无需将控制网页开放到局域网。

本机 Python 需要 `pexpect`；启用 PICO 视频还需 `av` 与 `numpy`。`.venv-dashboard` 已安装这些依赖。PC2 使用 `/home/unitree/miniconda3/envs/tv/bin/python`，只读检查确认有 `cv2/numpy`，没有 `pyrealsense2/v4l2-ctl`，存在六个 RealSense `/dev/video*` 节点。此检查没有打开相机。

验证使用合成 JPEG、fake V4L2 和原分支视频测试，不启动机器人控制。`simulation=True` 的图片明确显示 SIMULATION，不会连接 PC2。运行检查：

```bash
.venv-dashboard/bin/python -m unittest discover -s tests -p test_dashboard_camera.py
PYTHONPATH=vendor/xr_h2 .venv-dashboard/bin/python -m unittest discover -s vendor/xr_h2/tests -p test_head_video.py
```

未将上述离线验证记为实机相机或 PICO 显示验收；真实画面需用户点击 Start camera 后观察。
