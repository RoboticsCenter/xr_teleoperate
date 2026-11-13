# 单右腕到G1右臂：当前运行入口

入口是 `scripts/run_right_arm_teleop.py`。它将电脑端PICO回调、官方IK进程、经SSH读取的CRC有效G1参考，与PC2内部eth0上的低层运行器接在一起。默认只预演；真实输出需要显式选项和当前控制归属验证。

```bash
python3 scripts/run_right_arm_teleop.py --seconds 30 \
  --output diagnostics/right-arm-session.jsonl
```

输入PC2 SSH密码后开始。当前固定PC2地址192.168.31.65，严格核对已保存的主机密钥。脚本源码在PC2内存执行，外层timeout及内层watchdog限定生命周期，不改系统SDK。PICO PCService使用电脑192.168.31.92，原版Object单右腕SN为PC2310MLL9241957G；关闭PICO系统菜单，使XRoboToolkit保持前台。不能同时启动第二个native SDK接收器。

## 启停与实际输出范围

用户明确要求直接使用Tracker与Wuji，当前选择 `supervised_tracker_window_v1`，**不要求PICO控制器或扳机**。它适用于原版APK缺少置信度的10秒监督微动实验：原始 `tracking_valid` 始终保持null，仅独立标识本项目的输入接受决定。静止与旧客户端缓存冻结无法可靠区分；应用时间戳前进不等于Tracker内部采样新鲜。实测坐标安装标定、长期运行和双Tracker支持仍未完成。

有效Tracker目标到达后，PC2开始本机最长10秒的授权时间窗，明确记录 `authorized_ten_second_window`，不伪造“有人按住”字段；Input=1或2都可用。失焦、断流、坏包、位姿跳变或求解失败会结束本次会话，不自动重新使能。位置缩放0.02、最大目标平移1cm、关节相对参考±0.03rad、目标限速0.05rad/s，左臂目标和末端朝向固定。PC2输出仅右motor22–28采用位置控制，其余物理关节使用阻尼，因此**不保持全身姿态**，适用现场条件仍是已经确认的吊装/支撑，现场操作者持G1遥控器。

增益采用已核对官方示例的右kp40/kd1，非右kd8，并用2秒渐增kp。这些是示例参数，不是已验证BrainCo负载的最优或安全上限。参考姿态来自本次CRC通过的新鲜反馈，首次帧q取PC2当下实测、kp0；不执行全身回零示例。

真实输出选项为 `--enable-motion --control-release-verified`。这两个参数不是自动释放控制的后门：必须先有本次成功空名称CheckMode及控制归属检查，并维持已确认的现场条件。`g1_release_once.py`默认只打印计划，显式`--release`才执行一次官方ReleaseMode；无重试或自动恢复。主运行器不会自行ReleaseMode。

电脑仅回复收到PC2单次nonce后的新PICO包，保留Tracker序号；协议为不含held或enable字段的 `tracker_target`。PC2用自身nonce发出时刻作为目标时间下界，80ms内未返回不复用旧nonce。200Hz本机循环独立检查状态与目标年龄，不能靠持续转发缓存延长时间窗。官方publisher初始化固定等待200ms，因此采用“准备但不Write→新nonce→新输入”的两阶段启动；电脑此时继续计算IK，不停止输入时效检查。预演即使校验了输入也不会把`ai`伪装成已释放。

退出时如果此前确有输出且当前反馈有效，会尝试一次全身阻尼帧并锁存。发送中断或该帧Write成功均不证明物理停止，现场操作者仍持遥控器。SIGINT/SIGTERM、SSH EOF与超时走同一明确退出路径；native调用阻塞时外层timeout最终终止进程，但不能保证硬件停机。终态记录实际Write数、阻尼尝试及最后动作是否未知，不将“目标已发往PC2”算作电机成功输出。

## 已验证与待验证

- 实时输入→IK：12秒850候选，右目标最大变化约0.75°、左0；后续CRC版295候选/296有效CRC状态，均无运动输出。
- PC2完整路径首轮预演发现异步反馈时间竞态，已修正snapshot后取now，23项运行器测试通过。
- 第二轮已进入真实PC2循环，因报文`appState.focus=false`被电脑端停止，accepted_targets=0、actual_write_count=0；已请求现场恢复应用前台。
- 用户恢复前台后为focus=true/Input2；已按用户要求改用无控制器时间窗，不再把手势模式作为阻点。新实机预演PC2接受142目标，随后Tracker在19.1ms内位移跳变30.3cm，IK正确锁存；0运动输出，未ReleaseMode。见 `right-arm-tracker-window-preview-20261111.jsonl`。
- 最新13项编排、33项输入策略、30项运行器、23项低层会话、19项PICO解析测试通过；这些不是实际电机动作验收。

最新实机结果与服务到期时间以 `PLAN.md` 和本次JSONL终态为准。Wuji手部路径独立复用原reader/mapper，当前0帧通过原过滤器；本入口不会启动手指输出服务。
