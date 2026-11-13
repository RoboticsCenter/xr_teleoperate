# 手部会话：复用入口与输出类

当前已有原 Wuji reader、原手指映射、实时输入/反馈预览和默认只读 BrainCo 服务。此前缺少手部 DDS 客户端输出类；现新增 [BrainCoHandSession](../scripts/teleop_hand_session.py)，默认不创建 publisher。**本轮没有继续扫描手套，没有启动服务，也没有向手发运动命令。** 新类只通过 fake publisher 软件验证，尚未接入实际会话。

## 最短接入路径

优先使用右手：已确认 Wuji `WG1KA06260821549`、SDK user `brainco-teleop`，原仓库有 `calibration.json`（right）和该用户的 `right_hand.urdf`。两者可作为本次右侧 binding 的工件来源；应记录原文件哈希和既有标定日期，不把复用旧工件写成新实测。左手缺个人模型和左侧开合校准，不能复制右侧文件或改 side 假装左侧标定完成。

实际链路应按以下顺序接入：

1. 由总会话启动 PC2 已审查的 BrainCo 服务，先确认两手 SN、归一化反馈与唯一发布源。默认服务只有反馈；正式动作需由总会话显式启用 `--enable-commands`。
2. 用当前右侧工件构造 [现有 binding schema](hand-dds-adapter.md)，指定本次 SN/user、同侧模型/映射 hash、捕获主机 boot clock、反馈来源以及现有步长/新鲜度参数。当前项目尚无真实 binding；草稿不能充当已验证会话。
3. 使用 [live_hand_preview.py](../scripts/live_hand_preview.py) 的原 reader 和输入验证，保留原 `joint_readings` 的 **0.5** 过滤。其与 `HandDDSPreview` 已具备身份、非有限数、重复/回退帧、过期输入/反馈拒绝，以及基于 measured state 的步长限制，不另写 mapper。
4. 采集循环用 `BrainCoHandSession.step(record, original_filter_passed=True)` 代替直接调用 `mapper.preview()`；只有来自已锁定原 reader 的有效输出帧才传入 True。没有新帧时调用 `check_timeout()`，不能重复旧目标或刷新旧时间。先保持默认 `enable_motion=False`；正式连接真实输出前由主代理审查总会话接管和启停。

目前另一个实质缺口是传输/时钟：现有 `live_hand_preview.py` 要求同一 Python 环境能导入 Wuji、NumPy、Unitree SDK/CycloneDDS，并在**同一主机时钟域**合流。电脑侧 G1 外部网线已改接右手套，Wi-Fi SSH 可用不等于电脑侧 DDS 已验证。若输出端放 PC2，不能把电脑 `source_t` 直接作为 PC2 monotonic 时间，也不能仅用收到时刻把旧手套数据变新；总入口还需明确转发协议、数据年龄和反馈来源。新输出类不依赖“电脑 Wi-Fi DDS 已通”的假设，也不擅自建网络桥。

## 新类的 API 与边界

```python
session = BrainCoHandSession(mapper)  # 默认仅返回 JSON 候选，不加载 DDS
result = session.step(record, original_filter_passed=True)
idle_fault = session.check_timeout()  # 采集循环空闲分支持续调用
session.close()
```

这里的 `mapper` 是现有、已用实际 binding 初始化的 `HandDDSPreview`；`record` 完全沿用其 glove/measured schema。新类不会接收外部“已算好目标”绕过 mapper。`original_filter_passed` 是可信采集代码的来源断言，**不能作为网络用户可指定的绕过开关**；默认 False。原 reader 只输出已通过过滤的角度，角度 JSON 本身不含置信度，因此输出类无法从角度反推其质量来源。

真实输出构造参数为 `enable_motion=True`、明确 `interface/domain`，以及 `exclusive_control_confirmed=True`、`feedback_identity_confirmed=True`、`command_service_confirmed=True`。这些表示总会话已完成相应核对，不能靠设 True 代替实际检查。synthetic binding 不允许启用。**没有运动 CLI 开关、隐式服务启动或默认动作。**

第一个合法且新鲜的 mapped record 才惰性创建 `rt/brainco/<side>/cmd` 的 `MotorCmds_` publisher；构造函数本身没有硬件访问。第一帧与以后每帧都经原 `step_limited(goal, measured, max_step)`，默认每通道不超过新鲜 measured 值 ±80/1000，而不是相对上一命令累加。DDS 初始化之后立即再次检查输入/反馈年龄，防止初始化阻塞后发送旧目标。

IDL 消息恰好六个 `MotorCmd_`：顺序 Thumb、Thumb_aux、Index、Middle、Ring、Pinky；只赋值 q/dq，其他字段保留官方零值工厂默认。q 必须有限且 0–1，dq 必须有限且 0.001–1；dq 是服务的归一化速度参数。编码器已按固定官方 SDK commit 的 [MotorCmds_](https://github.com/unitreerobotics/unitree_sdk2_python/blob/814556d15970dd2ecf1c9984e845ca02ab07e206/unitree_sdk2py/idl/unitree_go/msg/dds_/_MotorCmds_.py)、[MotorCmd_ 默认工厂](https://github.com/unitreerobotics/unitree_sdk2_python/blob/814556d15970dd2ecf1c9984e845ca02ab07e206/unitree_sdk2py/idl/default.py)核对；尚未用实物 publisher 验证该新类。

以下情况锁存，后续新帧也不能自动恢复：原质量未放行、mapper 拒绝、输入空窗达到默认 200 ms、时钟回退、初始化后数据过期、DDS 写入失败。`check_timeout()` 必须由采集循环调用；`step()` 也会先检查上次空窗，迟到的新帧不能掩盖超时。重新启用需重建会话和满足条件，没有后台自动重试。

成功写入只表示 `Write()` 返回 True：`published=True`、`acknowledged=False`。写入抛错/返回失败后保留 `write_delivery_unknown=True`，包括以后锁存及 close 结果，不能把失败返回解释为肯定未送达。任何拒绝结果不含旧命令候选。

`close()` 只关闭客户端 transport，不发送 home、开手、握拳或 hold，也不声称机器人已停。PC2 [补丁服务](brainco-service-integration.md) 在此前尝试过目标输出且有本轮新鲜反馈时，可能按其已审查超时/退出策略发送一次 measured-position hold；这本身是运动命令。native SDK 同步调用仍可能阻塞，这不是硬实时急停保证。

## 已验证与未验证

```bash
uv run --offline --no-project --python 3.12 --with numpy \
  python -m unittest discover -s tests -p test_teleop_hand_session.py -v
```

9 项测试通过，使用原 mapper 和本地合成工件、fake publisher：默认零发布、启用前提、质量来源门控、首帧/后续 measured 限步、过期/非有限/重放拒绝、空窗锁存、慢初始化后重新检查、写失败的送达不确定性、IDL 数值限制和 close 不发额外目标。

这些测试没有创建真实 DDS publisher，没有证明全链路遥操作已运行。最近已测的两手原过滤器仍均为 0/361；现在不继续采样或要求现场排障，但原 reader 在无合格输入时继续不输出目标。不能为了“运行全链路”降低阈值或把低质量角度伪装成原 reader 已放行的数据。
