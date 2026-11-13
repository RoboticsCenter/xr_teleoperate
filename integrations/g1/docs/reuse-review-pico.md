# H2 分支的 PICO 模块复用审查

审查对象：`kevin-roboticscenter/xr_teleoperate`，分支 `h2/pico-wuji-head-camera`，固定提交 `053ba99af55c1d8ddc6b87918cb0fa234625886e`。本次只读源码及隔离纯软件测试，未访问机器人、启动 SDK/服务或发送命令；未修改上游和原 Wuji 仓库。

## 结论与摘取范围

| 上游模块 | 建议 | 当前 G1 项目如何使用 |
|---|---|---|
| `integrations/h2/pico_tracker/geometry.py` | **可以直接摘取纯数学函数与对应测试** | `heading_basis`、`IncrementalWrist` 可用于下一阶段头显前向对齐、Tracker 安装偏移补偿和完整腕部位姿映射。输入仍来自本项目原子回调；输出接已有 G1 IK 与限幅/时效检查。 |
| `pico_tracker/input.py` 的 `TrackerInput.select` | **适配后复用** | 复用双 SN 选择、同一 SN 不可同时绑定左右、原子保存配置；改为本项目路径和数据结构。不要连同 `status()` 的位姿变化年龄判断整体替换现有有效性门控。 |
| `config/pico_tracker.example.json` | **可借用配置字段** | 两侧 SN 和两侧 `offset_tracker_m` 字段有价值；示例是空 SN、零偏移，不是本机已完成标定。 |
| `pico_tracker/stream_trackers.py` | **保留参考，不替换当前读取器** | 它使用分离的 SDK cached getters；本项目 `pico_raw_probe.py` 的同包 callback、`Motion.len` 截断、重复时间戳拒绝和同包扳机信息更符合已发现的实机数据问题。 |
| `pico_tracker/receive.py` | **仅借鉴临时文件原子替换、会话/序号字段** | 最小跨机时钟偏移算法只能估计相对最快样本的额外延迟，不能证明绝对新鲜度；保留现有 nonce 与 RTT 保守时效协议。 |
| `pico_tracker/arm_teleop.py` | **不能原样移植或运行** | 固定 H2 IK、H2 控制器、31 关节头部索引及退出回零行为；G1 保留现有低层运行器。 |

立即可摘取项不会解决当前 `appState.focus=false`，也不会补出第二只 Tracker 或新 APK。最有价值的是把 `geometry.py` 用于后续实际安装偏移/腕姿标定，当前单右腕、固定朝向、小幅验证继续沿用本项目已测试路径。

## 精确源码依据

### 纯数学映射

- [`geometry.py:9`](https://github.com/kevin-roboticscenter/xr_teleoperate/blob/053ba99af55c1d8ddc6b87918cb0fa234625886e/integrations/h2/pico_tracker/geometry.py#L9) 的轴矩阵与本项目 `scripts/preview_pico_single_arm.py:45` 相同：XR right/up/back → robot forward/left/up。
- `geometry.py:40–49`：`heading_basis` 在 START 时固定头显水平前向；不会持续用转头改变双臂坐标。头显朝向过度竖直时拒绝。
- `geometry.py:52–70`：`IncrementalWrist` 保存测得的机器人末端参考，计算 Tracker 旋转后的安装偏移、相对平移和相对姿态；初始输入保持原机器人末端位姿。该模块只有标准库数学依赖，没有 H2、SDK、DDS 或设备初始化。
- 摘取后仍需给出真实安装偏移与坐标验证；该类本身不提供工作空间/速度/关节限制。G1 当前 `live_pico_arm_ik.py:135` 和 `preview_pico_single_arm.py:40` 使用固定朝向、缩放/裁剪平移策略，不能悄然改成该类的全尺度位置加旋转输出。

### 双 Tracker 能力和 APK 边界

- [`stream_trackers.py:32–40`](https://github.com/kevin-roboticscenter/xr_teleoperate/blob/053ba99af55c1d8ddc6b87918cb0fa234625886e/integrations/h2/pico_tracker/stream_trackers.py#L32) 调用 `get_motion_tracker_pose()` / `get_motion_tracker_serial_numbers()`，确实是独立 Motion Tracker 输入，不是手柄位置替代。
- [`input.py:35–50`](https://github.com/kevin-roboticscenter/xr_teleoperate/blob/053ba99af55c1d8ddc6b87918cb0fa234625886e/integrations/h2/pico_tracker/input.py#L35) 按 SN 绑定左右，拒绝两侧重复 SN，且只允许从当前帧中选择设备。`status(both=True)` 在 `53–80` 检查两侧。
- `arm_teleop.py:28,42` 支持 `--both-arms`，默认仅左臂；没有现成“仅右臂”入口，与本项目当前右腕优先路线不同。
- [`README.md:43–48`](https://github.com/kevin-roboticscenter/xr_teleoperate/blob/053ba99af55c1d8ddc6b87918cb0fa234625886e/integrations/h2/README.md#L43) 记录 PC Service 1.0.0、Python SDK 1.1.0、头显 app 1.1.1，未记录更改 Tracker 数量请求的 Unity 实现。固定提交的文件树中没有 APK 或 Unity 客户端工程。因此“支持双 SN 的电脑程序”不能证明可解决本项目原版客户端 ONE/Object 限制；该分支不能交付现成双腕 APK。

### 不适合倒换的时效/有效性逻辑

- `stream_trackers.py:32–38` 先取 timestamp，再分别取 poses/SNs，再复查 timestamp；这不等于本项目的单包原子快照。`head_pose` 还在复查后单独读取。没有按 `Motion.len` 截断有效数组，也没有携带 `appState.focus`、同包 trigger 或 Tracker confidence。
- `stream_trackers.py:41–53` 的 `pose_age_ms` 是“数值最后变化距今时间”，`input.py:63` 和 `arm_teleop.py:148` 以 1000ms 为阈值。有效但静止的 Tracker 也会超过此阈值；反之数值变化不能替代 tracking-valid。这只能是诊断指标，不能原样取代当前输入策略。
- [`receive.py:38–49`](https://github.com/kevin-roboticscenter/xr_teleoperate/blob/053ba99af55c1d8ddc6b87918cb0fa234625886e/integrations/h2/pico_tracker/receive.py#L38) 使用 `min(received_mono - sample_mono)` 建模两机偏移。新会话首包无论已经排队多久，都会被映射成当前接收时间；正常基线网络延迟也被隐藏。它能观察随后相对最短路径增加的排队，不能证明首包和会话切换的新鲜度。
- 本项目 `scripts/pico_raw_probe.py:119–175` 已按完整 callback 包校验 `Motion.len`、SN、重复时间戳，并显式保留未知 confidence；`36–94` 读取同包右 trigger/focus。`scripts/run_right_arm_teleop.py:35–58` / `61–73` 与 PC2 协议联合要求 challenge 后新采样；应继续保留这些行为。

### H2 输出入口的具体绑定

- [`arm_teleop.py:35–37`](https://github.com/kevin-roboticscenter/xr_teleoperate/blob/053ba99af55c1d8ddc6b87918cb0fa234625886e/integrations/h2/pico_tracker/arm_teleop.py#L35) 直接导入 `H2_ArmController`、`H2_ArmIK`。
- `116–124` 创建 IK/DDS/H2 controller，`118` 构造 `H2_ArmController(motion_mode=True)`，随后才启动 IPC 等待 START。不能把 LAUNCH 当只读预览。
- `62`、`122` 访问头部关节 `[29,30]`；`165` 调用双臂控制；`175–176` 在 finally 中执行 `ctrl_dual_arm_go_home()`。这些硬件/退出语义均不适合直接套入本项目 G1 开发模式。
- 失去 Tracker 时 `146–152` 仅停止更新目标并报告 holding；同一 session 数据恢复后可继续，未沿用本项目故障锁存/重建参考行为。
- [`README.md:8–20`](https://github.com/kevin-roboticscenter/xr_teleoperate/blob/053ba99af55c1d8ddc6b87918cb0fa234625886e/integrations/h2/README.md#L8) 明确其运行时固定在 H2/Dex5 PR321 `39b79bca...`，甚至和同仓库另一个 H2 runtime 的关节索引/启动逻辑都不互换。整目录 cherry-pick 不会变成 G1 适配。

## 本轮验证范围

- 从上游测试 AST 隔离运行原有 **9 项**纯测试：`test_pico_tracker.py` 的6项 `Coordinates`、2项输入/快照时效测试，以及 `test_dual_wuji.py` 的1项 `DualTrackers`，全部通过。
- 测试方法保留这些原测试方法，去除会引入未初始化 H2 submodule/dashboard 的其他 imports/测试类；不执行 bootstrap、不初始化 SDK、不触碰硬件。没有声称跑通整个上游套件。
- 这些测试覆盖轴映射、初始姿态保持、相对旋转、头显前向、杆臂补偿、坏四元数、选择/缺失/过期与双 SN 去重；不覆盖 cached getter 原子性、`Motion.len`、丢跟踪、绝对跨机延迟或 G1 运动。
- 双腕数据是否能由当前头显实际产生、物理轴/安装偏移、姿态跟随行为均未通过本轮审查验证。
