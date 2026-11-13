# Wuji 单次输入质量检查

`scripts/wuji_quality_probe.py` 把此前的 3 秒检查整理为可重复执行命令。它只发现、连接指定输入手套、读取身份和订阅数据；不会导入 G1/BrainCo 控制器、输出运动目标、改 IP/路由、切换用户或写校准。**2026-11-11 18:04–18:05 已分别实测左右手，各一次 3 秒采样**：两侧身份正确、约 120 Hz，但原过滤器均为 0/361 通过。右手运行前由主代理单独修正临时路由，详见[接入记录](wuji-bringup.md)。

## 使用

现场确认手套正常佩戴、指尖模块固定、线圈周围环境调整完毕后，在本项目根目录执行一次：

```bash
uv run --offline --no-project --python 3.12 --with wuji-sdk==2026.11.9 \
  python scripts/wuji_quality_probe.py \
  --sn WG1JA06260822033 --side left --user brainco-teleop --seconds 3 \
  --output diagnostics/wuji-quality-left-next.json \
  2> diagnostics/wuji-quality-left-next.log
```

本机已缓存此 SDK 和 Python，因此示例使用 `--offline`。脚本复用项目内 `scripts/wuji_probe.py` 的只读模型摘要和 `sources.lock.json`，应从完整项目运行。`--help` 不依赖 Wuji SDK；不要为运行它修改原项目的 Python 环境。

`--sn` 和 `--side` 必填；`--seconds` 默认 3，必须为有限正数且不超过 30。`--user` 可选，给出时必须与当前 SDK user 完全一致；不会替用户切换。标准输出为一份 JSON，SDK 日志留在标准错误；`--output` 额外保存同一 JSON。重复使用相同输出文件名会覆盖旧报告，需保留历史时自行换名。

当前已发现并读回身份的直连左手为 `WG1JA06260822033`，右手为 `WG1KA06260821549`。右手运行时将示例中的 SN/side 改为该 SN 和 `right`，同时使用独立报告名。邻网 `WG1KA06260811540` 不是本次右手，不应将该共享 SN 填入右手检查。当前右手 `.1.101` 通过 Realtek `.1.251/32` 的专用 `/32` 路由连接，左手 `.1.100` 通过 ASIX `.1.250/24`；重连后应先核对临时路由是否仍生效。

## 连接和时间边界

每次执行只调用一次 `scan()`，要求指定 SN 恰好匹配一项 `DeviceType.WujiGlove`、UDP、直接 IPv4:port 地址。Unknown、Zenoh、同 SN 多项、缺失 SN、主机名及 URI 均拒绝；不根据名字猜设备类型，不自动选择另一只手套，也不回退到按 SN 再发现。

连接使用本次发现地址，设置 `enable_bridge=False`、`timeout_ms=1000`、`retry_count=1`、`auto_time_sync_interval_ms=None`。连接后读回真实 SN、左右侧和 FW；只有 SN/侧别完全匹配才订阅。按 SDK 定义，关闭周期时间同步**不取消 connect 内的首次时间同步**；发现、连接、身份查询和订阅仍产生正常网络协议流量，不等于网络零发送。

采样结束按顺序关闭两路订阅并 `disconnect_all()`，仅涉及本进程连接。SDK 在独立子进程中运行；父进程总等待上限为 `seconds + 10` 秒，超时先终止，1 秒后仍未退出则强制结束进程组，不自动重试。因此 `--seconds 3` 的正常采样窗口为 3 秒，总超时处理约不超过 14 秒；30 秒采样对应约 41 秒总预算。SDK 初始化、发现、身份读取和关闭均包含在总预算内。

## 怎样读结果

`status=sampled` 和退出码 **0 只表示拿到了可统计的 IK 输入并完成检查**，不表示通过率合格。即使接受率为 0，也可返回 0，让诊断报告保留完整信息。未发现设备、身份不符、无帧、原过滤器异常、关闭异常或总超时返回 1；参数错误返回 2。

检查以下字段：

| 字段 | 含义 |
| --- | --- |
| `identity` | 从已连接设备读取的 SN、侧别、FW |
| `source_filter.sha256` | 运行前已与 `sources.lock.json` 校验的原 bridge 文件摘要 |
| `sample.ik.unique_frame_keys` / `duplicate_frames` | IK 流按 `(seq, timestamp_us)` 去重后的帧数及重复数 |
| `sample.ik.observed_hz` | 唯一帧数除以本次采样窗口时长，不是硬件承诺频率 |
| `sample.ik.accepted_by_original_filter` / `accepted_fraction` | **原函数 `joint_readings(frame)`** 的接受数和比例；分母为唯一 IK 帧数 |
| `sample.ik.confidence_by_finger` | 每指 IK 置信度 min/max/mean、缺失/非有限数、低于 **0.5** 数量 |
| `sample.raw_emf.confidence_by_finger` | 同次订阅源 EMF 位姿的各指置信度，低值参考线 **0.85** |
| `sample.timestamp_matching` | 严格同 `header.timestamp_us` 匹配的数量、未匹配数量和两种独立质量统计 |
| `personal_model_available` | 指定侧当前命名用户是否有可读模型；仅检查文件，不验证佩戴者或精度 |
| `teleoperation_ready` | 始终为 **false**；该检查不验收遥操作 |

源文件在执行前验证 hash，随后只执行已校验的字节，不调用其 `main()`，不向原仓库写 `__pycache__`。原过滤阈值保持 **0.5**，CLI 不提供降阈值选项。原函数仍负责五指、每指五个角度、有限数和每指置信度判断；没有复制重定向算法。

`emf_poses` 是 **EMF 位姿估计**，不是线圈电压/ADC。它与关节 IK 置信度属于不同层；不能把关节均值直接当作 EMF 置信度。官方说明关节流保留对应源 EMF 时间戳，因此这里只做完全相同时间戳匹配，不按 seq、最近时间或数组位置拼接；有歧义的时间戳也不配对。SDK 无此资源、订阅/接收失败或没有匹配时会明确记录 unavailable/error/0 个匹配，不推断来源正常或异常。[官方手部跟踪数据参考](https://docs.wuji.tech/docs/zh/wuji-glove/latest/sdk-data-reference/hand-tracking.md)

原始 EMF 低于 0.85 的计数仅是本次统计，**不等同于 SDK 错误事件数**：官方告警还有持续约 0.5 秒的触发条件。程序不把低值统计转换成新的硬件告警，不读取/猜测未公布的硬件错误路径。缺少 hardware error codes 也不代表硬件正常。[官方手套故障排查](https://docs.wuji.tech/docs/zh/wuji-glove/latest/troubleshooting.md)

左侧个人模型缺失时仍允许诊断，报告会标明缺失且不标就绪；不会覆盖已有右侧模型。即使之后短时接受率恢复到 100%，仍不能据此认定佩戴、个人模型、手指映射及整条机器人控制链已验收。

## 现有错误的含义

最新实测是 **18:04 左手、18:05 右手**，均使用本脚本：每侧 361 个唯一 IK 帧、360 个源 EMF 帧，其中 360 组时间戳严格匹配，另有 1 个 IK 未匹配。两侧原过滤均接受 **0/361**，源 EMF 五指均为 **360/360 帧低于 0.85**。左手仍缺个人模型，右手已加载个人模型；不能把两侧低质量简单归因于缺模型。报告：[左手](../diagnostics/wuji-after-rewire-left-quality-20261111-180428.json)、[右手](../diagnostics/wuji-after-rewire-right-quality-20261111-180522.json)。

历史 **17:23** 左手检查使用一次性脚本，同样是 361 个唯一 IK 帧、0/361 通过；当时只保存 IK 质量及 SDK 日志，**没有可逐帧对齐的源 EMF 质量**。本次新增的 EMF 实测不能倒推那次 EMF 数值。[历史报告](../diagnostics/wuji-errorcheck-quality-2026-11-11.json)

17:28 已另行完成官方 doctor 实物检查，左手五路 EMF 断线检查为 5/5 正常；该检查不测量本节的逐帧置信度，不更新 17:23 的质量结论。[doctor 检查说明](wuji-doctor-review.md)

历史 `wuji-left-input-preview-2026-11-11.log` 末尾的 `RuntimeError: No fresh valid glove data; check tracking and the SDK hand model` 来自原 `wuji_bridge.py` 在连续 5 秒没有通过过滤的新帧后主动退出。这不等于已证明 Python/SDK 崩溃、USB 掉线或网络断流。最新 120 Hz 连续输入配合 0% 接受率表明，应分别核对通信与跟踪质量。[原预览日志](../diagnostics/wuji-left-input-preview-2026-11-11.log)

## 软件验证

```bash
python3 -m unittest discover -s tests -p test_wuji_quality_probe.py -v
```

12 项离线测试通过：覆盖参数边界、Unknown/Zenoh/歧义 SN 拒绝、读回身份不符时不订阅、源代码校验先于执行、原函数和 0.5 默认值复用、重复/非有限帧统计、同时间戳匹配、EMF 缺失、个人模型缺失、订阅关闭和超时结束。

在实物运行前，还用本机缓存的 **wuji-sdk 2026.11.9** 实际构造 `ConnectOptions` 并检查 `SdkManager.connect` 签名，确认上述 kwargs 和 `address=` 可用；该单独的离线检查没有创建 SDK manager 或访问设备。随后 18:04–18:05 的两次实物采样已验证正常连接、双路订阅、时间戳匹配和退出；这证明诊断入口可用，**不表示输入质量或遥操作已通过验收**。
