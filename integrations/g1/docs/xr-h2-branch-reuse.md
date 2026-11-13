# H2 PICO/Wuji/相机分支的 G1 复用结论

审查日期：2026-11-11。来源：[用户指定分支](https://github.com/kevin-roboticscenter/xr_teleoperate/tree/h2/pico-wuji-head-camera)，固定提交 `053ba99af55c1d8ddc6b87918cb0fa234625886e`。隔离源码位于 `/home/jack-xiang/.cache/unitree-g1-reuse/kevin-xr-teleoperate`。本轮只做源码审查和纯软件测试，没有部署、启动控制入口或操作硬件。

## 建议摘取清单

路径均相对上游 `integrations/h2/`。“可直接摘取”指模块实现可保留，仍需接入当前接口并验证。

| 模块 | 结论 | 本项目如何使用 |
| --- | --- | --- |
| `head_camera/pico_video.py` | 视频协议/编码核心可直接摘取 | HTTP JPEG→H.264→原生PICO Remote Vision；另接 G1 D435i JPEG源和图像过期检测。原生头显选ZEDMINI接收；单目画面复制到双眼，不是立体。视频路径不依赖新双Tracker APK。 |
| `pico_tracker/geometry.py` | 纯数学函数可直接摘取 | `heading_basis`、`IncrementalWrist` 支持初始参考、头显前向与Tracker安装偏移；接现有原子输入与G1 IK。实际偏移/轴向仍需标定，不自动改变当前固定朝向的小幅策略。 |
| `quest_auto_record.py` | `AutoRecording` 类可直接摘取 | 注入现有运行器状态和recorder，提供START自动录制、退出保存、手动接管和异常处理。它不提供相机/关节采集后端。 |
| `pico_tracker/input.py` | SN选择/配置保存可抽取 | 左右SN绑定、重复绑定拒绝有用；不要连同其位姿变化年龄判定替换现有输入有效性逻辑。 |
| `quest_h2_web.py` | 界面可抽取，控制后端需替换 | 状态、Tracker选择、相机、启停、录制UI接当前G1运行器；删去H2模式转换、Dex5增益/握持、头电机接口。H2 LAUNCH会初始化控制器并回零，不能当作无输出预演。 |
| `quest_wuji.py::WujiHands` | 双手协调层可适配 | 预检、逐侧启动、失败停止双方接当前TeleopHandSession；应用层回滚不能撤销已发生动作。 |
| `wuji_glove/send_input.py` | 小转发器可适配 | stdin JSONL→Unix socket；另接本项目身份/时效检查。它自己不建立SSH或验证payload。 |
| `h2_record.py` | 录制框架可适配 | 保留CSV、meta/status、视频索引与measured/commanded分列；换BrainCo每手6通道、当前`rt/lowcmd`和实际相机源。双臂索引15..28恰好与G1相同；原代码不含头相机录制，也不是硬件同步。 |

## 保留当前项目的部分

- Wuji读取和手指映射继续来自用户指定的 `/home/jack-xiang/branco-wuji-teleop/brainco-wuji-teleop`。分支的`stream_latest_tips.py`读五指尖位置，`left_dex5_adapter.py`重定向为Dex5的20关节并发布`rt/dex5/*`，不适用Revo2六通道。其confidence与连续过期检查也不等同于本项目。
- 保留`pico_raw_probe.py`完整callback、`Motion.len`截断、同包focus/trigger与nonce/RTT时效协议。分支使用分离cached getters、位姿变化年龄和最小跨机偏移估计，不能直接替换这些检查。
- 保留本项目G1 IK/低层运行器。`pico_tracker/arm_teleop.py`固定H2控制器，退出会go-home；头跟随访问H2颈部slots29/30和权重槽31。相机传输可复用，不代表这些电机入口可用于G1。
- 不移植H2 PC3 S56/GMSL/Argus/Thor服务配置。G1现有设备是PC2 Orin与D435i。
- 不照搬`h2_link.py`、`configure_wifi.py`或socat部署：H2地址、PC1→PC2路由、路径和端口约定不同，函数会修改路由/重启转发。G1已有无线SSH `.31.65`，使用当前连接层，另选不冲突的UI端口。

## 该分支不能直接补齐的事项

没有新双Tracker APK或Unity客户端工程；电脑端双SN支持不能证明当前头显ONE/Object限制已解决。不能靠复制代码解决`focus=false`或Wuji源置信度不足。分支README的H2实测不能记为G1实测。

新功能主要在一个大型新增提交中，宜按文件/类摘取。fork主目录`teleop/robot_control`相对当前已审查官方`817fb00c63cde15e5f24a0f8fa08e1e33ed89d3b`无文件差异；H2集成另用PR321运行时`39b79bca34b7547b21edad3fe3e642bb5cac7147`，不要混用。根LICENSE标示Apache-2.0并列出依赖；摘取时保留版权、许可证及固定来源。

## 验证与后续顺序

- AutoRecording：12项原测试通过，仅fake recorder与临时文件。
- PICO：隔离运行原有纯几何/快照/双SN测试9项通过，未初始化SDK或硬件。
- 相机：11项通过，4项因缺PyAV跳过；H.264实际编解码未在本轮验证。
- 原双Wuji整测试模块因缺`zmq`在导入时失败，不计为通过；未运行完整H2套件或bootstrap。

建议先按需求摘取 **视频发送器或操作界面**，再接 **AutoRecording**；实际腕部标定阶段引入 **geometry.py**。手指只借协调/传输骨架，保持原读取与映射。本轮未实施这些集成。

详细行号、固定源码链接和验证边界：[PICO](reuse-review-pico.md)、[Wuji/录制](reuse-review-wuji.md)、[相机](reuse-review-camera.md)。
