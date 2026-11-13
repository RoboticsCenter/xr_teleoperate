# 原 Wuji 映射到 BrainCo DDS 单位的纯软件预览

实现：[scripts/preview_hand_dds.py](../scripts/preview_hand_dds.py)。这是 JSONL 输入/输出的预览适配器，**没有实时 DDS 订阅/发布、串口驱动或手套连接**；未来实时采集程序还需把两路输入整理成下述快照格式。

原手指算法保持为：

```text
原格式 glove.joint_angles
  → 原 readings → 原 closure → 原 LowPass(dt) → 原 targets
  → 原 step_limited(新鲜 measured state)
  → 六通道位置除以 1000 → JSON command_preview
```

适配器按 `sources.lock.json` 校验原 `teleop/retarget.py` 和 `settings.example.json` 的 SHA-256，再以文件路径加载纯映射模块，不导入两项目同名的 `teleop` 包。目标范围从原示例配置读取；默认步长为原 `max_step` 的 80%，即 **80 / 1000**。下一帧始终相对新鲜实测位置限步，不相对上次预览目标累加。

## 单位与六通道合同

| 序号 | 原项目通道 | DDS 约定名称 |
| --- | --- | --- |
| 0 | `thumb_flex` | `Thumb` |
| 1 | `thumb_opposition` | `Thumb_aux` |
| 2 | `index` | `Index` |
| 3 | `middle` | `Middle` |
| 4 | `ring` | `Ring` |
| 5 | `little` | `Pinky` |

保持原顺序，不根据别处 thumb/aux 命名自行互换。位置 `MotorStates_.states[i].q × 1000` 回到原项目单位；限步后的目标 `÷ 1000` 对应 `MotorCmds_.cmds[i].q`。示例：实测均为 `0.500`，原闭合映射目标 `[900,950,950,950,950,950]`，步长 80 时输出六个 `q=0.580`。[宇树官方服务实现](https://github.com/unitreerobotics/brainco_hand_service/blob/d71996b6999edb2f838a3dca3d9621429a2ef966/main.cpp)

`dq` 在该服务中是归一化手指速度参数，不是 rad/s，也不是原 DaMiao 的 `segment_ms`。本项目已审核的服务门控要求 **`0.001 ≤ dq ≤ 1`**；此预览同步拒绝零速及超界值，速度必须在绑定文件中逐通道显式填写。原 `segment_ms=200` 只作为未转换的说明字段输出，不据此推算速度或承诺完成时间。

预览 JSON 只列该服务消费的 `cmds[].q/dq`，没有构造 IDL 实例或序列化报文。`published=false`、`acknowledged=false`、`hardware_output=false` 均明确输出；DDS 发布成功与硬件执行确认也不能互相替代。

## 可立即运行的合成验证

```bash
cd /home/jack-xiang/unitree-g1
uv run --no-project --python 3.12 --with numpy \
  python -B scripts/preview_hand_dds.py --demo

uv run --no-project --python 3.12 --with numpy \
  python -m unittest discover -s tests -p test_preview_hand_dds.py -v
```

demo 在临时目录创建标为 `synthetic_test_only` 的左侧测试数据，五条快照中四条生成预览、一条因反馈过期拒绝。它不读取真实右侧校准来假装左手已校准，也不写 SDK 用户目录。相同运行结果可重复；普通绑定入口拒绝 synthetic 类型。

本次 **19 项测试通过**。其中审计钩子禁止网络 socket、子进程、硬件 SDK 导入及串口打开，demo 仍能完成；另覆盖源文件哈希、单位/顺序、实测限步、速度下限、身份错配、过期/未来/回退/重复帧、非有限数、错误长度和缺失校准。

结果：[合成预览 JSONL](../diagnostics/hand-dds-demo-2026-11-11.jsonl)、[验证摘要](../diagnostics/hand-dds-validation-2026-11-11.json)。这些是软件验证，不是已录制的真实手套或手部反馈。

## 绑定文件：校准后由操作者确认

真实左手目前仍缺个人 `left_hand.urdf` 和左侧开合映射，不能生成可用的真实绑定。完成 [左手校准步骤](left-glove-calibration.md) 后，填写以下字段；示例中的占位路径/哈希和 `calibration_confirmed=false` 会被拒绝。

```json
{
  "schema_version": 1,
  "kind": "operator_calibrated",
  "side": "left",
  "glove_sn": "WG1JA06260822033",
  "sdk_user": "brainco-teleop",
  "clock_id": "REPLACE_WITH_CAPTURE_HOST_AND_BOOT_ID",
  "feedback_source_id": "REPLACE_WITH_VERIFIED_PC2_LEFT_HAND_SOURCE",
  "calibration_confirmed": false,
  "calibrated_at": "REPLACE_WITH_COMPLETION_TIMESTAMP_AND_TIMEZONE",
  "calibration": {
    "path": "REPLACE_WITH_PROJECT_LEFT_FINGER_MAP.json",
    "sha256": "REPLACE_WITH_SHA256"
  },
  "sdk_model": {
    "path": "REPLACE_WITH_NAMED_USER_MODEL_DIRECTORY/left_hand.urdf",
    "sha256": "REPLACE_WITH_SHA256",
    "side": "left",
    "sdk_user": "brainco-teleop",
    "kind": "operator_calibrated"
  },
  "alpha": 0.3,
  "max_step_1000": 80,
  "max_glove_age_s": 0.2,
  "max_feedback_age_s": 0.2,
  "speed_normalized": [0.2, 0.2, 0.2, 0.2, 0.2, 0.2]
}
```

相对路径以绑定 JSON 所在目录为基准。适配器要求文件存在且哈希匹配、校准侧别与六通道一致、每指有效开合范围、同侧同用户的模型证据、侧别模型文件名，以及有效的 21 个非固定关节 URDF。速度示例只服务于预览，不代表该速度已在实机验收。

`calibration_confirmed` 是操作者对当前佩戴者及校准适用性的记录，哈希用于锁定文件；它们不提供独立的身份认证或精度证明。模型本身按用户/侧别共享，绑定文件额外固定本次手套 SN。输入捕获端必须核对实际使用的 SDK 模型，不能仅填写一个期望的哈希。

填好后可仅校验文件并获得规范绑定 ID，不读取任何输入：

```bash
uv run --no-project --python 3.12 --with numpy \
  python -B scripts/preview_hand_dds.py \
  --binding /absolute/path/to/left-binding.json --check-binding
```

输出 `binding_id` 是绑定 JSON 按键排序、紧凑编码后的 SHA-256。更换 SN、用户、模型、目标反馈源或任何配置后会改变 ID，旧输入快照不再适用。

## 输入快照格式

每行一个对象。`glove` 保留原 `wuji_bridge.py` 输出字段；外层绑定和模型证据，以及 `measured` 的来源/接收时刻，需要未来采集层添加：

```json
{
  "binding_id": "OUTPUT_FROM_CHECK_BINDING",
  "clock_id": "SAME_AS_BINDING",
  "sdk_model_sha256": "SAME_AS_VERIFIED_RUNTIME_MODEL",
  "now_monotonic_s": 100.02,
  "glove": {
    "sn": "WG1JA06260822033",
    "hand_side": "left",
    "sdk_user": "brainco-teleop",
    "source_t": 100.01,
    "seq": 10,
    "timestamp_us": 1100000,
    "joint_angles": {"thumb": 40, "index": 50, "middle": 60, "ring": 55, "little": 45}
  },
  "measured": {
    "hand_side": "left",
    "topic": "rt/brainco/left/state",
    "source_id": "SAME_AS_BINDING_FEEDBACK_SOURCE_ID",
    "sample_id": 25,
    "received_t": 100.015,
    "channels": ["Thumb", "Thumb_aux", "Index", "Middle", "Ring", "Pinky"],
    "q_normalized": [0.5, 0.5, 0.5, 0.5, 0.5, 0.5]
  }
}
```

时间约定：`source_t`、反馈 `received_t` 和预览的 `now` 必须属于同一主机同一次启动的 monotonic 时钟；设备 `timestamp_us` 只用于去重/顺序检查，不与主机秒数相减。`sample_id` 应在接收新的 DDS 样本时递增，不能在轮询同一缓存时递增。DDS 手状态没有 SN 或源时间戳，`source_id` 是由已核对身份的采集层赋予的绑定标签，不是消息自带认证。

同一反馈样本在仍新鲜时可以复用，但其 ID、时间和值必须保持不变；手套帧序号、设备时间和主机生成时间必须推进。计数器重启或回绕时应停止并重新建立采集会话，不把旧缓存重新标为新鲜。

读取实时快照 stdin 时，脚本使用本进程的 `time.monotonic()`；它本身不启动输入程序。读取保存文件并显式 `--replay-clock` 时才使用行内的 `now_monotonic_s`，以保留录制时的新鲜度关系：

```bash
uv run --no-project --python 3.12 --with numpy \
  python -B scripts/preview_hand_dds.py \
  --binding /absolute/path/to/left-binding.json \
  --input /absolute/path/to/captured-snapshots.jsonl --replay-clock
```

离线回放只证明录制时的关系，不能证明设备现在在线。没有 `--replay-clock` 的旧录制按当前时钟检查，通常会被判为过期。

## 拒绝与后续集成边界

任何缺失/未确认校准、错侧、错 SN/用户/模型、错反馈来源、无效六元素、非有限值、时钟回退、旧帧或过期状态都会拒绝。拒绝记录没有 `command_preview`，不保留旧目标给外部继续发送；滤波状态重置，恢复时仍相对当前实测位置限步。若目标范围与实测限步范围无交集，或原整数取整超过限步，明确拒绝而不绕开任一限制。

退出码：`0` 表示绑定校验/普通输入全部通过，或 demo 的预设正负样本执行完；`1` 表示普通输入含拒绝或为空；`2` 表示参数/配置失败。`--help` 不需要 numpy 或硬件 SDK。

后续尚需实现并验收：实际 Wuji 与 DDS 接收层到快照 schema 的转换、真实模型/映射绑定、手状态单一来源、服务端命令超时与接管顺序、IDL 发布及逐通道实机核对。本预览没有提供隐藏的发布开关，也没有把停止输出解释为机器人已停止。
