# 左手 Wuji 校准与原映射预览

适用设备：已核对的左手套 **`WG1JA06260822033`**，直连 `192.168.1.100:50001`；SDK 用户 **`brainco-teleop`**。以下命令供操作者在本机终端交互运行，**本次仅核对帮助与源码，没有执行校准或重新连接手套**。

先做 SDK 个人手型模型，再采原项目的张开/握拳映射，最后预览六通道目标。后两步复用 `/home/jack-xiang/branco-wuji-teleop/brainco-wuji-teleop`，不新增重定向算法。原项目及右侧校准文件保持原样。

## 1. 准备姿态与本次输出目录

正常佩戴并固定左手套，确认发射线圈供电，让手保持在线圈 30 cm 内，避开金属遮挡与大型电机，手指自然微弯、等待初始化稳定。16:40 的检查中原过滤器已接受 359/360 帧，但 SDK 仍有部分手指 EMF 告警；先改善跟踪，保留原置信度与标定约束。详见 [Wuji 接入记录](wuji-bringup.md)。

以下各段在**同一个终端**依次执行。每次会创建新的会话目录，已有左侧应用校准也会保留，不复用固定输出文件名：

```bash
cd /home/jack-xiang/unitree-g1

WUJI_CLI=/home/jack-xiang/.local/opt/wuji-cli/2026.10.26/wuji
TELEOP_SOURCE=/home/jack-xiang/branco-wuji-teleop/brainco-wuji-teleop
G1_TELEOP_PROJECT=/home/jack-xiang/unitree-g1

mkdir -p "$G1_TELEOP_PROJECT/calibrations"
LEFT_CALIBRATION_DIR=$(mktemp -d "$G1_TELEOP_PROJECT/calibrations/left-$(date +%Y%m%d-%H%M%S)-XXXXXX")
LEFT_FINGER_CALIBRATION="$LEFT_CALIBRATION_DIR/finger-map.json"

printf '本次左侧映射将保存到：%s\n' "$LEFT_FINGER_CALIBRATION"
"$WUJI_CLI" user show
```

核对显示的当前用户为 `brainco-teleop`。若不是，先关闭自己正在运行的手套读取/可视化，再显式切换并重新查看：

```bash
"$WUJI_CLI" user switch brainco-teleop
"$WUJI_CLI" user show
```

在当前用户正确后，把现有 SDK 校准保留到本次目录，再开始标定。命令未使用覆盖选项；同名备份已存在时应保留它并创建新的会话目录：

```bash
"$WUJI_CLI" user export "$LEFT_CALIBRATION_DIR/sdk-before.zip"
```

这个备份包含当前 SDK 用户已有的校准，便于保留旧左模型版本与右模型。SDK 的活跃模型按“用户＋左右侧”保存在 `~/.wuji/sdk/users/<user_id>/models/`；应用的张开/握拳映射则单独放在本项目的 `calibrations/left-…/finger-map.json`，两者不要混用。

## 2. 交互生成左手 SDK 模型

确认备份成功，单独执行已安装 CLI 的交互命令：

```bash
"$WUJI_CLI" calib hand-model --sn WG1JA06260822033
```

核对交互界面的用户与左手 SN。按提示完成四次拇指与其他手指指尖捏合、四指弯曲约 90°、手掌展开；保持每个姿态直到采样完成。若提示替换已有同侧模型，先确认上一节已保存旧版本。结束后查看产物：

```bash
"$WUJI_CLI" user show brainco-teleop
```

应看到该用户的 `left_hand.urdf`；右侧 `right_hand.urdf` 不属于本次目标。不要把右模型复制为左模型。这里保留终端交互，不添加 `--json` 或 `--jsonl`；官方说明结构化模式会跳过用户与已有模型替换确认。[官方手部模型标定](https://docs.wuji.tech/docs/zh/wuji-cli/latest/hand-model-calibration)

本机 CLI 是 `2026.10.26`，其帮助已确认 `--sn` 和默认 900 秒整体超时；实际交互提示以已安装版本为准。按一次 Ctrl+C 可取消，不需要启动 BrainCo 服务。

## 3. 先验证原读取器的有限帧输出

上一步完成并退出后，使用同一命名用户与同一左手 SN 读取 30 个有效样本：

```bash
uv run --no-project --python 3.12 --with wuji-sdk==2026.11.9 \
  python -B "$TELEOP_SOURCE/wuji_bridge.py" \
  --sn WG1JA06260822033 --side left --user brainco-teleop \
  --count 30 --timeout-s 5
```

正常时输出 30 行包含 `hand_side=left`、指定 SN、用户与五指 `joint_angles` 的 JSON，然后结束。若报告无新鲜有效数据，先处理跟踪/佩戴问题，再重新运行这一段；不降低 `--min-confidence`，也不直接进入开合映射标定。

## 4. 用原项目采左手张开/握拳映射

关闭上一段读取器后，执行原 `teleop_hand.py calibrate`。此路径只采手套样本并写指定 JSON，不实例化手驱动；不需要加载原项目已启用动作的 `settings.json`。

```bash
uv run --no-project --python 3.12 --with numpy --with wuji-sdk==2026.11.9 \
  python -B "$TELEOP_SOURCE/teleop_hand.py" calibrate \
  --side left \
  --calibration "$LEFT_FINGER_CALIBRATION" \
  --seconds 2 \
  --glove-cmd "python -B '$TELEOP_SOURCE/wuji_bridge.py' --sn WG1JA06260822033 --side left --user brainco-teleop"
```

`uv run` 为父进程和 `--glove-cmd` 中的 `python` 提供同一个隔离依赖环境；已核对其子进程 Python 解析一致。`-B` 避免在原项目写入 Python 字节码。原运行器用 `shlex.split` 启动读取器，不把这段命令交给 shell 执行。

原程序会两次从 `/dev/tty` 提问，需要在正常交互终端运行：

1. 出现 `Open the gloved hand flat, fingers together, then press Enter`：左手平展、手指并拢，按 Enter 后保持约 2 秒。
2. 出现 `Make a full fist, thumb folded across the fingers, then press Enter`：左手握完整拳，拇指跨在手指前方，按 Enter 后保持约 2 秒。

成功后打印本次 `finger-map.json` 路径。原程序保留六通道顺序及原有拇指对掌随屈曲映射。输入不足或张开/握拳范围太小会报错；调整姿态后用新的会话目录重新采样。

**原 `calibrate` 会覆盖传给它的目标文件。** 上面的新目录方案避免覆盖已有结果。若希望重新采集，重新运行第 1 节的新目录创建部分，并继续使用新路径；不要把 `--calibration` 指向原项目的 `calibration.json`，也不要覆盖已验收的左侧文件。

## 5. 本项目六通道 dry-run 预览

仍在同一个终端，用刚生成的左侧文件启动预览：

```bash
uv run --no-project --python 3.12 --with numpy --with wuji-sdk==2026.11.9 \
  python -B "$G1_TELEOP_PROJECT/scripts/preview_hand.py" \
  --source-dir "$TELEOP_SOURCE" \
  --side left --calibration "$LEFT_FINGER_CALIBRATION" \
  --sn WG1JA06260822033 --user brainco-teleop
```

`preview_hand.py` **没有额外的 `--dry-run` 参数**；它内部固定调用原运行器 `run --dry-run --settings settings.example.json`，并检查示例配置的 `motion_enabled=false`。此入口只输出六通道目标，既不打开 BrainCo 驱动，也不发布 G1 控制命令。

慢慢做张开、半握、握拳，观察 `thumb_flex / thumb_opposition / index / middle / ring / little` 六通道是否连续、方向正确，核对双手标签没有混入。按 **Ctrl+C** 结束实时预览；`--duration` 只作用于 `--demo`，不会给实时输入设置时限。没有机器人 measured state，因此预览不等于已验证硬件步长限制或实机执行。

如果另开了终端，重新设置 `WUJI_CLI`、`TELEOP_SOURCE`、`G1_TELEOP_PROJECT`，并把 `LEFT_FINGER_CALIBRATION` 指向前面打印的已验收文件；不要为了找文件而再次生成一个空会话并误当成已校准。

## 本文核对范围

- 实际读取已装 CLI 的 `calib hand-model --help`、`user show/export/switch --help`。
- 实际读取原 `teleop_hand.py calibrate --help`，审查 `calibrate()`、`GloveStream` 与原映射函数。
- 实际读取本项目 `preview_hand.py --help` 与固定 dry-run 调用链。
- 仅准备和检查命令；没有执行模型标定、开合采样、实时预览或新的硬件连接。
