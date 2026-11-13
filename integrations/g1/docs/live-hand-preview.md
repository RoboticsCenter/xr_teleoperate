# 真实 Wuji 输入 + BrainCo 反馈的有限时长预览

`scripts/live_hand_preview.py` 将锁定原项目的 `wuji_bridge.py` 输出，与指定一侧 `rt/brainco/<side>/state` 的实测位置合并，再交给已有 `HandDDSPreview` 计算六通道 JSON 候选目标。它只创建 DDS subscriber，没有命令 publisher、控制器或发布开关，不启动机器人侧服务、不改SDK用户或原项目。

本轮仅完成软件与mock验证。尚无可用于本入口的、经用户确认的实际操作者模型/张开握拳校准 binding；没有用虚构标定启动真实输入预览。左右 BrainCo 串口读通、DDS反馈可用、手套连接和手指校准属于不同验收项。

## 启动前工件

需要 [手部DDS预览文档](hand-dds-adapter.md) 的 `operator_calibrated` binding，包含实际 `side`、`glove_sn`、命名 `sdk_user`、校准完成时间、六通道标定文件及SHA256、该操作者对应侧21自由度模型文件及SHA256、确认字段与速度/步长/新鲜度参数。入口拒绝 `synthetic_test_only`，不会自动把现有右手标定套给左手，也不会创建“已确认”的用户标定文件。

实时入口对两个字段有更具体的要求：

```bash
python3 scripts/live_hand_preview.py --show-clock-id
```

返回本机当前boot的 `clock_id`，格式为 `linux-monotonic:<boot_id>`，填入binding。机器重启后应重建本次采集binding里的clock字段；不能继续使用旧boot的回放标识。

`feedback_source_id` 严格描述本次接收配置。例如左侧、domain0、UGREEN网口：

```text
dds:domain=0;interface=enx6c1ff762d908;topic=rt/brainco/left/state
```

这不是机器人或手的SN认证。BrainCo DDS state消息没有设备SN或源采样时间；同一domain/topic上的其他发布者不能由这个字段自动排除。本入口输出 `feedback_identity_verified=false`，来源身份范围始终是topic与接收配置。正式接入前仍需单一来源核对。

本地工件检查不导入任何硬件SDK，也不联网：

```bash
python3 scripts/live_hand_preview.py \
  --side left --sn YOUR_LEFT_GLOVE_SN --user YOUR_CALIBRATED_USER \
  --binding /path/to/confirmed-left-binding.json \
  --interface enx6c1ff762d908 --domain 0 --check-binding
```

`--check-binding` 成功仅表示本地证据一致。开始采集时还会只读查询SDK当前用户，核对其实际缓存路径 `~/.wuji/sdk/users/<user_id>/models/left_hand.urdf` 的hash与binding一致。未通过则不连接手套，也不初始化DDS。

原 `wuji_bridge.py` 的hash依据本项目 `sources.lock.json`；纯映射与示例设置由已有 `load_original()` 校验。运行时不使用原项目已启用运动的 `settings.json`。

## 开始有限时长预览

同一个Python环境必须能够导入 `wuji_sdk`、`unitree_sdk2py` 及其 CycloneDDS 依赖，因为原读取器作为该Python解释器的子进程启动。下面的 `python3` 应在已准备好两套SDK的环境内运行；本轮没有创建或声称已验证这样的联合硬件环境。

```bash
python3 scripts/live_hand_preview.py \
  --side left --sn YOUR_LEFT_GLOVE_SN --user YOUR_CALIBRATED_USER \
  --binding /path/to/confirmed-left-binding.json \
  --interface enx6c1ff762d908 --domain 0 \
  --seconds 5 --idle-timeout 2 --rate-hz 20 \
  > diagnostics/live-left-preview.jsonl
```

必须显式指定side、SN、user、binding和网口。缺项、hash不符、左右不符、无确认标定或时钟域不符均在连接前拒绝。此命令假定指定topic已有经过核对的反馈服务；脚本不会替你启动服务。

范围：`--seconds` 为0.1–60秒，默认5；`--idle-timeout` 为0.1–5秒，默认2；读取输出频率为1–120Hz，默认20。原读取器继续负责SDK手类型、SN、左右侧检查，以及其原有手指有效性/置信度筛选。原映射、滤波、目标范围和基于实测位置的步长限制继续由 `HandDDSPreview` 调用原代码完成。

输出为JSONL：`session`、`live_hand_preview`/`refused`、`summary`。候选目标位于 `result.command_preview`，**published始终false**，没有硬件执行确认。每条成功/拒绝映射记录附有 `input` 快照，供离线审阅；它不是可以自动重新发给机器人的命令记录。若要离线复现，应取该字段作为 `preview_hand_dds.py --replay-clock` 的输入。

启动包装器将原读取器的Python输出流保留为JSON管道，把SDK原生stdout日志转到stderr；执行的仍是经过hash核对的原文件。代码没有改写SDK帧，也没有替换原读取/映射函数。

## 时间与失联行为

- Wuji `source_t` 原样保留，是本机原读取器调用 `time.monotonic()` 的时刻；不是手套传感器采样时间。采集端另记 `glove_received_monotonic_s`，不会用收到时间刷新旧帧。
- `seq` 与 `timestamp_us` 仅检查严格递增，设备时间不与本机monotonic相减。重复、回退或设备重启结束当前采集，不静默重新绑定。
- DDS `received_t` 在本机回调接收时记录；重复使用同一反馈样本时保留其时间/本地sample_id，不能把旧反馈变成新反馈。消息没有源时戳，因此只能验证到达新鲜度。
- 所有参与年龄计算的进程由本机同一监督器启动，继承相同时钟命名空间。入口不能读取远端机器生成的 `source_t` 当成本机实时数据。
- 旧输入、无效身份、无效六通道反馈、管道EOF或读取错误均拒绝。无输入/无反馈超过idle时间退出；反馈短暂过期时不产生候选目标。任何缺口都不会重复输出上次候选目标。

主进程为采集worker建立自己的进程组，原bridge归属该组。正常结束先清理自己的bridge和subscriber；Ctrl-C、SIGTERM或超时也只终止本入口创建的子进程。worker在 `seconds+12` 秒内未退出时触发硬期限，随后有限次发送信号回收子进程组，避免SDK初始化/关闭挂起。不会停止其他遥操作进程或SDK服务。

退出码：0表示到达采集时长、至少产生一次预览且没有映射拒绝；2表示配置/输入/反馈/初始化/关闭错误；3表示worker硬超时；130表示用户中断。所有情况均不代表机器人已运动或停止。

## 已执行的软件测试

```bash
python3 -m unittest discover -s tests -p test_live_hand_preview.py -v
```

15项测试通过：真实schema合流、source/receipt时间保留、旧输入拒绝、重复设备帧拒绝、缺帧/缺反馈超时、DDS过期不刷新、错误身份、无效反馈、缺binding先拒绝、当前SDK模型hash检查、boot/左右binding检查、EOF停止、硬超时仅回收自身进程组、异常时bridge/subscriber清理，以及本地打印子进程的日志/JSON分离。测试只使用临时合成工件、mock SDK/DDS和本地打印程序，没有真实硬件访问或用户模型校准。
