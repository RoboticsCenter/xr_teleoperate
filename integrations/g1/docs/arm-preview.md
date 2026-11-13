# G1 29DoF 双臂离线 IK 预览

2026-11-11 已实际运行固定版官方 `G1_29_ArmIK`，输出左右各7个关节角、末端残差、限位结果和静态图。整个入口不接机器人、不读 DDS/PICO SDK、不创建 publisher。`motion_ready` 始终为 `false`。默认目标来自模型 FK；不是操作者追踪，也不是实机反馈。

## 直接运行

本机隔离环境已准备好：

```bash
cd /home/jack-xiang/unitree-g1
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
  ~/.cache/unitree-g1-arms/env/bin/python scripts/preview_arms.py \
  --output diagnostics/arms-synthetic.json \
  --svg diagnostics/arms-synthetic.svg
```

打开 [静态双臂图](../diagnostics/arms-synthetic.svg)；蓝色为左臂、红色为右臂，十字为目标。图是模型骨架的前视/侧视，不包含碰撞几何。

默认模型参考为 `[0,.15,0,.4,0,0,0, 0,-.15,0,.4,0,0,0]` rad。先计算参考的两侧 `L_ee/R_ee`，在模型前方 x 轴加1cm，再运行8次官方优化与滤波。该参考仅为离线示例，不能作为机器人启动姿态。

本次 [实际结果](../diagnostics/arms-synthetic.json)：14个有限关节角，8次 IPOPT `Solve_Succeeded`；左右位置残差均约 **2.27mm**、旋转残差约 **0.00591rad**，全部满足模型关节限位。默认验收阈值是2cm/0.1rad，均可在命令行收紧；这是离线预览阈值，不能直接当成实机控制安全阈值。

报告同时输出 `q_rad`、`q_deg`、关节名称、14个参考角、两侧目标/求得的4×4变换、每次求解状态、最小限位余量与软件版本。返回码0只表示离线求解通过，2表示缺依赖、输入/源码错误、优化失败、限位或残差不通过。

## 固定官方实现与依赖

复用 [Unitree xr_teleoperate commit 817fb00c…](https://github.com/unitreerobotics/xr_teleoperate/tree/817fb00c63cde15e5f24a0f8fa08e1e33ed89d3b) 中的三个文件：

- `teleop/robot_control/robot_arm_ik.py` 的 `G1_29_ArmIK` 类。
- `teleop/utils/weighted_moving_filter.py` 的 `WeightedMovingFilter` 类。
- `assets/g1/g1_body29_hand14.urdf` 模型。

入口先核对三个完整文件的 SHA256，再通过 Python AST 只提取上述两个类，在显式依赖命名空间内执行。不导入官方遥操作模块、顶层示例、其他机器人类、图形库或控制器。滤波器与官方 IK 的目标函数、权重、约束、求解次数上限、异常处理均保留。

[离线适配差异](../patches/arms-headless-ik.patch) 仅三处：用同一 URDF 构建无网格模型及两个空几何模型；禁用 pickle 模型缓存的读取和写入。没有重写 IK 算法。关节顺序必须精确匹配左7+右7，否则拒绝运行。通过此方式避免下载几十MB网格、可选显示依赖及加载旧模型缓存，仍使用原 URDF 的运动学/惯性定义。

本机现有 PyPI `pin==4.1.0` 缓存没有 `pinocchio.casadi` 扩展，因此直接创建 uv/PyPI 环境不能运行此官方实现。已采用 [上游安装文档](https://github.com/unitreerobotics/xr_teleoperate/blob/817fb00c63cde15e5f24a0f8fa08e1e33ed89d3b/README.md) 使用的 conda-forge Pinocchio 路径，在本项目独立缓存中安装，无需改系统或原 Wuji 环境：Python3.10、Pinocchio3.1.0、NumPy1.26.4、CasADi3.6.7。完整可复现包列表见 [arms-conda-explicit.txt](../diagnostics/arms-conda-explicit.txt)。

已有 micromamba 时重建环境：

```bash
micromamba create -y -p ~/.cache/unitree-g1-arms/env \
  --file diagnostics/arms-conda-explicit.txt
```

本机 micromamba 位于 `~/.cache/unitree-g1-arms/bin/micromamba`。固定源码缓存路径为 `~/.cache/unitree-g1-arms/source`，可用 `--source-dir` 指定别处。首次在另一台机器准备源码时执行以下下载；**预览入口本身从不联网**：

```bash
python3 - <<'PY'
from pathlib import Path
import urllib.request
commit = "817fb00c63cde15e5f24a0f8fa08e1e33ed89d3b"
root = Path.home() / ".cache/unitree-g1-arms/source"
files = ("teleop/robot_control/robot_arm_ik.py",
         "teleop/utils/weighted_moving_filter.py",
         "assets/g1/g1_body29_hand14.urdf")
for relative in files:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://raw.githubusercontent.com/unitreerobotics/xr_teleoperate/{commit}/{relative}"
    target.write_bytes(urllib.request.urlopen(url, timeout=30).read())
PY
```

## 保存的 PICO 样本回放

入口可读取 `scripts/pico_probe.py` 保存的 JSONL，只处理 `type=sample` 记录。它不初始化 SDK，也不自动拿当前日志里的设备当左右手。必须同时提供 `--pico-jsonl` 和完整 `--calibration`。缺少任一项直接拒绝；没有标定时直接用默认合成模式。

标定 JSON 必须包含下表。当前现场尚未获得这些标定，不能填写虚构数据来宣称 Tracker 已接通。

| 字段 | 内容 |
| --- | --- |
| `schema` | 固定 `g1-arms-offline-calibration-v1` |
| `left_sn` / `right_sn` | 两个不同的实际 Tracker SN；始终按SN查找，忽略数组顺序 |
| `pose_unit` | `m`，需要10cm实测确认；入口不猜单位 |
| `reference_sample_index` | 从0开始的 sample 序号；不计 JSONL metadata/summary 行 |
| `q_reference_rad` | 14个模型限位内的参考关节角；当前离线使用，实机需与clutch时反馈匹配 |
| `position_scale` | `(0,2]`，明确的人体平移缩放 |
| `T_robot_axes_xr_axes` | 4×4纯旋转矩阵，无平移；PICO轴转换需现场逐轴确认 |
| `T_tracker_wrist` | `left`/`right` 各一个4×4 Tracker→操作者腕部安装变换，包含旋转和平移 |
| `T_robot_wrist_ik` | `left`/`right` 各一个4×4机器人腕部→官方IK末端变换，与Tracker安装变换不同 |

官方 `L_ee/R_ee` 由 wrist-yaw 关节平移 `[0.05,0,0]` 得到。因此机器人参考腕定义为 wrist-yaw frame 时，`T_robot_wrist_ik` 是旋转单位阵、x平移0.05的齐次矩阵。若选择其他腕/TCP定义，须另行确定变换。此模型不能证明 Revo2 掌心/抓取TCP已正确标定。

计算顺序：对参考帧和回放帧分别做 `T_robot_axes_xr_axes @ T_xr_tracker @ T_tracker_wrist`；使用两个输入腕姿态的相对平移/旋转更新模型参考腕；最后乘机器人 `T_robot_wrist_ik`。参考帧目标严格等于模型参考末端；官方优化的正则项仍可能产生小偏差，坐标上的无跳变不能等同于实机首次启用无跳变。

```bash
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
  ~/.cache/unitree-g1-arms/env/bin/python scripts/preview_arms.py \
  --pico-jsonl /path/to/pico.jsonl \
  --calibration /path/to/measured-wrist-calibration.json \
  --sample-index -1 --output diagnostics/arms-replay.json
```

回放仍标记 `saved_pico_offline_replay_unverified_freshness`。它只验证保存的数据能否经过坐标与 IK 计算，不能证明缓存数据新鲜、Tracker跟踪有效、参考空间未重置。PICO流的原子快照、逐设备有效性及断连缓存问题仍按 [PICO接入文档](pico-input.md) 处理。

## 已执行的失败测试

```bash
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
  ~/.cache/unitree-g1-arms/env/bin/python -m unittest discover \
  -s tests -p test_preview_arms.py -v

OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
  ~/.cache/unitree-g1-arms/env/bin/python scripts/preview_arms.py \
  --translation-m 2 --output diagnostics/arms-unreachable.json
```

第二条预期返回2。目标在模型前方2m，优化器可能正常结束，但末端残差仍大于1m，因此程序必须拒绝；不能只检查 IPOPT 的 `success`。

[11项测试结果](../diagnostics/arms-tests.txt) 包括真实官方优化、网络socket及控制SDK导入禁止、越界可达性、左右SN顺序交换、安装偏移随旋转更新、相对参考无坐标跳变、反射矩阵/无效四元数拒绝、源码哈希不符拒绝、保存文件完整回放，以及官方异常回退检测。

额外通过 `ipopt.max_iter=0` 仅注入到诊断实例，触发 `Maximum_Iterations_Exceeded`。[失败报告](../diagnostics/arms-solver-failure.json) 证明：官方方法返回原输入关节时，入口仍识别 `solver_success=false`、立即停止后续迭代，且 `offline_ok=false`。正常入口保持官方30次优化器迭代上限不变。

## 实机前仍需完成

两只腕部 Tracker 配对、SN绑定、轴/尺度/安装变换实测；有效新帧与recenter检测；以实机当前关节建立clutch参考；目标速度/加速度/工作空间约束；失联保持；Revo2几何/惯性及碰撞验证；单独的G1输出层启动持位与停止策略。此入口没有运动输出选项，不能用它启动实机双臂。
