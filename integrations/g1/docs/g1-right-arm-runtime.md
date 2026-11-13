# PC2 右臂有限会话运行器

实现：[`g1_right_arm_runtime.py`](../scripts/g1_right_arm_runtime.py)，复用 [`g1_lowcmd_session.py`](../scripts/g1_lowcmd_session.py)。这次新增工作只做代码和 fake 集成验证；真实预演、控制服务切换和运动试验由主代理记录。早期开发模式设计文档中的“0–21 零阻尼”方案已被当前实现的**显式正阻尼**替代。

默认运行会订阅 `rt/lowstate`、校验 CRC 并进行只读 `CheckMode`，不创建 `rt/lowcmd` publisher。`--enable-motion` 加三项现场/控制归属参数也只是允许后续门控；收到新鲜有效目标、持续使能且检查通过后才惰性创建 motor publisher。未包含 `ReleaseMode`、`SelectMode` 或自动恢复动作。RPC 请求本身有 DDS writer，它不是运动命令 writer。

## 本次接口

在 PC2 现有 tv Python 环境中使用。下面是默认预演的命令形状；`CONFIG_JSON` 与 `REFERENCE_ID` 由电脑编排器生成：

```bash
timeout --signal=TERM --kill-after=2s 42s \
  /home/unitree/miniconda3/envs/tv/bin/python -u -B scripts/g1_right_arm_runtime.py \
  --interface eth0 --seconds 30 --reference-id "$REFERENCE_ID" \
  --config-json "$CONFIG_JSON"
```

源码也可由 [`pc2_arm_client.py`](../scripts/pc2_arm_client.py) 经已核对主机密钥的 SSH 内存加载，无需系统安装。`--plan` 完全离线，不导入 Unitree SDK 或创建网络实体。正常运行的初始化默认限 5 秒，可设置 `--init-timeout-s 1..10`；会话总时长 `--seconds` 最大 60 秒，包含等待 trigger 的时间。

Config 必须显式提供 `right_kp[7]`、`right_kd[7]`、`nonright_kd[22]`、`calibration_id`、`enable_source`、`gain_review_id`。运行器固定 `max_command_hz=200`，拒绝其他频率配置；其余字段见 `Config` 定义。配置参数不是从原项目会自动启用运动的配置读取。

默认策略 `input_policy_id="vendor_tracking"` 要求目标 `tracking_valid=true`，且 `supervised_input_accepted=false/null`。当前原版 PICO APK 缺失厂商有效性字段，不能填成 true。明确选择 `supervised_position_only_v1` 时仍保留 `tracking_valid=null`，同时要求同名 policy 和 `supervised_input_accepted=true`，`max_session_s<=10`；显式 `tracking_valid=false` 总是拒绝。监督策略的固定 SN、同包 trigger、新包序号/时间、焦点与位置边界检查由电脑端 [`pico_supervised_input.py`](../scripts/pico_supervised_input.py) 完成，PC2 验证声明与有限目标边界。这是受现场监督的短时位置实验，不是对厂商跟踪有效性的替代证明。

`--seconds 30` 与 `Config.max_session_s=10` 分别控制启动/等待窗口和**首次真正使能后**的最长 10 秒，不能通过新目标或新 nonce 延长。动作模式还必须明确传入 `--enable-motion --suspended-confirmed --operator-ready --exclusive-control-confirmed`；这些 flag 是调用方对当次已确认条件的声明，不会自动发现控制独占。短窗没见 writer 不等于已独占。

## JSONL 与跨机时间

stdout 每行一个 JSON 事件；SDK/native stdout 重定向到 stderr。事件为 `runtime_ready`、`challenge`、`runtime_status`、`runtime_final`；初始化失败为 `runtime_error`。最终事件不是 `runtime_summary`。

`runtime_ready.mode` 含真实 `CheckMode` 的 `code/data`，`readiness` 保留查询发出时的 PC2 monotonic 时间和三项确认。`name="ai"` 的预演可以接收并验证协议，状态报告 `motion_service_not_inactive`；不会伪造模式满足。首次真正使能要求最近 1 秒内成功且 name 为空的查询。若 WAIT 后第一次按住时原查询已超过 0.5 秒，运行器先只读刷新、废弃该目标，再发新 challenge 要求重新采样。

每个 challenge 包括：

| 字段 | 用途 |
| --- | --- |
| `session_id`、`nonce`、`sequence` | 单会话、单次使用、严格匹配 |
| `issued_monotonic_ns`、`max_age_ms` | PC2 的发出时间与寿命，默认 80ms，不能当电脑时钟 |
| `reference_id` | 启动时电脑提供的固定参考 UUID |
| `q14`、`feedback_age_s` | 诊断用途，不直接作为跨机新鲜 IK 参考 |

电脑收到 challenge 后，必须等待新的 PICO 原始包和同包使能信息再生成 IK 目标。不能把之前缓存的目标重新贴上新 nonce。PC2 使用 **nonce 发出时间**作为目标与使能产生时间的保守下界；不会使用回复到达时间或直接相减两机 monotonic 时钟。服务器无法单独证明远端确实重新采样，这项生产者契约由电脑端代码与原序号校验共同落实。

目标回复严格只接受以下字段，额外的远端 `received_t` 等字段也拒绝：

```json
{
  "type": "target",
  "session_id": "来自当前challenge",
  "nonce": "来自当前challenge",
  "sequence": 1,
  "input_sequence": 101,
  "enable_sequence": 101,
  "generated_after_challenge": true,
  "q": [0, 0, 0, 0, 0, 0, 0],
  "tracking_valid": null,
  "held": false,
  "calibration_id": "与Config相同",
  "reference_id": "与CLI相同",
  "enable_source": "与Config相同",
  "input_policy_id": "supervised_position_only_v1",
  "supervised_input_accepted": false
}
```

上面仅为协议形状，七个 q 必须来自实际新鲜参考与 IK；不能把零值示例作为实机目标。两个原始输入序号必须严格递增，不允许通过递增 challenge 序号伪造原始新包。

WAIT 阶段未回复的 nonce 到期会作废并自动换新，不因 SSH/PICO 初始化耗时立即终止；电脑 reader 应持续 drain 并选最新 challenge。**回复已过期或已轮换 nonce 仍然拒绝**。合法 `held=false` 只报告 `waiting_for_trigger`，不调用低层 step，不锁死等待；进入 ACTIVE 后松开即退出。ACTIVE 后必须持续获取新 challenge 并回复新包，80ms 内无回复终止。每次只允许一个 outstanding nonce；一次读取收到多条输入也拒绝，不积压目标。

电脑以独立 [`wifi_feedback_client.py`](../scripts/wifi_feedback_client.py) 的 RTT 保守年龄界限建立 IK 参考，生成固定 reference_id。PC2 首次使能另取自己当时的 7 个实测右臂 q 作为 anchor；两端参考相差导致目标超过 ±0.03rad 时拒绝，不扩大限幅或自动换 anchor。

## 输出与退出语义

单 owner loop 以固定 5ms 调度，过晚不会补发突发帧；这不是硬实时保证。状态 callback 使用官方 native CRC，要求完整 35 槽并验证前 29 个物理关节的有限 q/dq、motorstate、序号/tick、新鲜度及 PR/机型字段。callback 错误锁存，不用后来的正常包掩盖。快照完成后再读取 PC2 clock；不修改实际 callback 时间戳。

- 右臂 motor22–28：首帧 q 等于当时实测，kp=0，然后至少 1 秒渐增；目标相对 anchor 限 ±0.03rad，默认目标速度限 0.05rad/s，反馈过快/跟踪误差/失效均锁存。
- 物理 motor0–21：mode=1，q/dq/tau/kp=0，使用显式配置的 22 个正 kd；这是阻尼，不维持身体/左臂/腰腿姿态，吊装仍然必要。
- 物理 motor22–28：mode=1、dq/tau=0、显式右臂增益；保留槽29–34所有命令字段为0，不能使用 arm_sdk 的 weight29。
- `mode_pr=0`、`mode_machine` 来源于有效实测并锁定；reserve 清零，CRC 在其余字段完成后计算。数字5不被解释为 Regular。

增益配置上界40/1/8来自本项目对官方示例的保守约束：官方 [G1低层示例](https://github.com/unitreerobotics/unitree_sdk2_python/blob/814556d15970dd2ecf1c9984e845ca02ab07e206/example/g1/low_level/g1_low_level_example.py) 用右臂 kp40/kd1，[RL Gym helper](https://github.com/unitreerobotics/unitree_rl_gym/blob/276801e46c5d433564f24658bac64f254b7d2d4b/deploy/deploy_real/common/command_helper.py) 的阻尼示例用 kd8。**这些不是官方公布的最大增益，也不是装 BrainCo 负载后的实测安全值**。当前实际选择必须单独审阅并由 Config 显式表达。

EOF、SIGINT/SIGTERM、总时限、松开 trigger、状态/输入/循环超时和异常均进入同一退出路径。如果已经成功输出过且最新反馈仍有效，运行器**明确尝试一次全 29 关节阻尼帧**，然后锁存并关闭；该阻尼是主动命令，属于 `--enable-motion` 的退出策略。若反馈失效或此前传输错误，不盲发、不重试，不把停止发包称为停止机器人。默认预演没有这次运动输出。

`runtime_final` 记录实际写计数、damping 返回、错误、`last_action_unknown`；`physical_stop_verified=false`、`mode_restored=false` 始终保留。DDS Write 成功仅表示本地发送接口成功，不证明执行器已停止或模式已恢复；没有 final 或远端异常消失时必须按最后物理状态未知处理。计数包含退出阻尼帧。

Python 线程 watchdog 将初始化限制为配置时长、正常循环单次阻塞限制约1秒、清理限制约2秒；超出会 `_exit(3)`，无法保证最后阻尼。同步 DDS/native 调用持有 GIL 时，线程也可能无法及时执行，所以建议由外部 `timeout` 限制整个远端进程。外部 kill 同样不等于物理停止；现场持遥控器人员仍负责异常停机与接回控制。本运行器不自动释放/恢复高层服务。

## 可复现验证

```bash
python3 scripts/g1_right_arm_runtime.py --plan
python3 -m unittest discover -s tests -p test_g1_lowcmd_session.py -v
python3 -m unittest discover -s tests -p test_g1_right_arm_runtime.py -v
```

2026-11-11：低层会话 20 项、运行器 23 项测试通过。运行器 fake 集成覆盖 WAIT nonce 轮换/总时限、持续松开与首次使能、首帧/退出阻尼、challenge 时间下界、过期与重放、源序号、默认未知跟踪拒绝/显式监督、模式不满足、查询刷新、反馈 callback 竞态与阻塞时仍过期、EOF/信号/时限、传输错误无重试、非阻塞管道半行/背压。

主代理第一次真实默认预演已出现启动反馈时间竞态，零目标接受、零运动输出，证据 [`right-arm-pc2-preview-20261111-first.jsonl`](../diagnostics/right-arm-pc2-preview-20261111-first.jsonl)。上述 snapshot 后取 clock 修复和回归测试针对该失败；不能把 fake 通过当作修复后实机预演或运动验收。
