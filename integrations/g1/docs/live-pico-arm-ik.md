# PICO 右臂流式 IK 预览

`scripts/live_pico_arm_ik.py` 接收 stdin JSONL，输出受限的右臂关节候选。它没有 PICO SDK、网络连接、Unitree publisher 或运动选项；`motion_ready=false`、`commands_published=0` 始终成立。左臂关节固定，右臂只跟随相对平移，目标朝向保持建立参考时的机器人朝向。

## 模型与计算方式

复用 `preview_arms.load_ik()` 中经 SHA256 核对的官方 `G1_29_ArmIK`、URDF 和滤波器；固定 commit `817fb00c63cde15e5f24a0f8fa08e1e33ed89d3b`。仅加载一次模型，在发布 `ready` 前用模型姿态预热 IPOPT；随后通过 optimizer parameter 更新参考，不重复构建模型。源码来源及无图形适配见 [双臂离线预览](arm-preview.md)。

沿用 `preview_pico_single_arm.py` 的受限目标函数：保留官方平移/旋转误差项，将姿态正则化锚定于本次参考、保留平滑项。约束右臂相对参考各关节不超过 `±0.03 rad`，左7关节等于参考。目标平移复用 `single_target()` 的径向限幅，默认比例 `0.02`，最大平移 `0.01 m`。输出须同时通过模型关节限位、2 mm 位置残差、0.03 rad 旋转残差与关节偏移检查；官方求解失败时返回的旧关节角不能成为候选。模型计算出的力矩被丢弃。

第一有效 PICO 帧建立操作者零点，候选严格等于14关节参考，不运行求解来改变初始姿态。此后默认每帧运行2次官方优化/滤波，`--iterations` 可指定1–8次。关节顺序固定为左7+右7，具体名称沿用 `preview_arms.JOINTS`。

## 输入合同

先启动进程，等 stdout 的 `type=ready`，再请求新鲜机器人状态并发送一个 `reference`。直接把启动前排队的参考送入进程可能超过100 ms门限，程序会拒绝。

| reference 字段 | 要求 |
| --- | --- |
| `type`、`reference_id` | `reference`；本进程未用过的非空 ID |
| `q_rad` | 14个有限、模型限位内的关节角，左7+右7，单位 rad |
| `state_sequence` | 非负整数；后续新参考必须严格递增 |
| `state_valid` | 显式 `true`，由状态上游完成身份/有效性检查 |
| `received_monotonic_ns` | 本机时钟域的状态接收时间或保守采样时间下界；建立参考前后均检查年龄≤100 ms，禁止直接使用 PC2 单调时钟 |
| `clock_domain` | 固定 `local_monotonic` |
| `reference_kind` | 正常入口为 `live`；`--offline-replay` 仅接受 `recorded` |
| `right_sn` | 要绑定的右腕 Tracker SN，不使用数组位置绑定 |
| `axis_config.id` | 显式轴/单位配置 ID；输出的 `calibration_id` 沿用此值 |
| `axis_config.pose_unit` | 固定 `m`，不猜测厘米/毫米 |
| `axis_config.verified` | 显式布尔值；`false` 表示尚未现场验证，绝不自动改成 true |
| `axis_config.T_robot_axes_xr_axes` | 3×3 正交旋转矩阵、det=+1，将输入相对平移转换到机器人轴；没有内置默认矩阵 |

根代理的状态桥可使用 `local_request_sent_ns - remote_sample_age` 给出保守时间下界，避免把网络等待或远端数据年龄藏掉。可附带 `time_provenance=conservative_rtt_bound`、`source_clock_id`、`age_upper_bound_s`，本组件原样回显这些来源字段；仍以本机 `now - received_monotonic_ns` 检查上界。此处建立的是固定姿态参考，**不代替输出层持续检查实时机器人反馈**，候选因此保留 `current_robot_feedback_checked=false`。

后续直接输入 `pico_raw_probe.py` 的标准化 `type=sample` 记录，必需字段包括 `source=native_json_callback`、`snapshot_atomic=true`、`issues=[]`、`device_id`、`packet_sequence`、`local_monotonic_ns`、`app_timestamp_ns`、`motion_declared_count`、`trackers`。Tracker 中要求唯一 SN、`representation_ok=true`、有限且四元数归一化的 `xyzw_pose`。只使用标准化后的有效条目；`Motion.len=1` 而原数组长2时，不能从尾项构造第二只 Tracker。

`tracking_valid=false` 或 confidence 为2/3时拒绝。只有同时存在已知有效 confidence（0/1）和显式 `tracking_valid=true` 才回显 true。发布版缺少置信度时可以计算**数学预览**，但 `tracking_valid=null`、`tracking_quality_known=false`；这类结果不能冒充有效跟踪来通过运动输出门控。

## 时效、跳变与拒绝行为

- 每个 PICO 包在求解前和求解后检查原始接收时间，默认最大年龄100 ms；不得用求解完成时间刷新旧包。
- 包序号、接收时间、应用时间戳都必须严格递增。应用时间戳仅用于检测重复/倒退，不被解释为已校准的传感器时间。
- 原点应在参考后200 ms内捕获；已有原点时相邻输入间隔不得超过200 ms。每个参考会话最多60秒。
- 相邻输入的位置变化大于10 cm或旋转变化大于0.5 rad时拒绝；即使不跟随旋转，也检查旋转跳变。该门限不能检测所有微小 recenter、遮挡或追踪异常。
- 任一无效、过期、跳变、断连事件或 IK 失败将进入 `LATCHED`；拒绝记录没有 `q_candidate_rad`。继续跟随需要新的 reference ID、新状态序号及新鲜参考，不会自动复用旧零点恢复。
- 同一实例不接受换头显或序号/应用时戳回退；需要重建接收会话与参考。元数据 start/summary/无害event只回显忽略状态，不产生目标。

输出 `q_candidate_rad` 只存在于 `accepted=true` 的记录。`sequence`、`received_t` 及 `source_packet_sequence`、`source_received_monotonic_ns`、`source_app_timestamp_ns` 均来自原包；`processed_monotonic_ns` 是单独记录的处理时间。失败时 `rejection_details` 提供可用的求解状态、残差和偏移诊断，但不输出可执行目标。`received_t` 为原ns换算秒，需要保持高精度身份时使用原整数ns字段。

## 启动与离线回放

```bash
cd /home/jack-xiang/unitree-g1
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
  ~/.cache/unitree-g1-arms/env/bin/python scripts/live_pico_arm_ik.py \
  --seconds 60 --max-records 10000
```

正常入口仅从 stdin 读reference/sample，不会自己连接头显或机器人。stdout为逐行JSON；模型初始化及原生日志转stderr。输入窗口从模型ready后计时，默认30秒，最多3600秒；默认最多10000条记录。即使上游一直不关闭stdin，时间窗口结束也会退出。native求解本身不是硬实时执行，生产调用还应有外层进程deadline。返回0表示至少有一个候选且没有拒绝，1表示没有候选或发生拒绝，2表示初始化错误，130表示中断。

回放需要显式 `--offline-replay` 和 `reference_kind=recorded`。它使用原始事件时间比较记录先后，保留所有旧时间戳，输出 `mode=offline_recorded_replay`、`source_age_ms=null`；不会把保存的姿态包装成当前新鲜反馈。可复现本次录制回放：

```bash
python3 - <<'PY' | OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
  ~/.cache/unitree-g1-arms/env/bin/python scripts/live_pico_arm_ik.py \
  --offline-replay --seconds 30
import json, sys
from pathlib import Path
sys.path.insert(0, 'scripts')
from preview_pico_single_arm import recorded_arm_state
from live_pico_arm_ik import HYPOTHESIS_AXES
rows = [json.loads(s) for s in Path('diagnostics/teleop-pico-current-20261111-1810.jsonl').read_text().splitlines()]
samples = [r for r in rows if r.get('type') == 'sample']
q, _ = recorded_arm_state(Path('diagnostics/pc2-feedback-service-verified-2026-11-11.json'))
ref = dict(type='reference', reference_id='OFFLINE_RECORDED_ONLY', q_rad=q,
           received_monotonic_ns=samples[0]['local_monotonic_ns'], state_sequence=1,
           state_valid=True, reference_kind='recorded', clock_domain='local_monotonic',
           right_sn='PC2310MLL9241957G', axis_config=dict(id='UNVERIFIED_REPLAY_HYPOTHESIS',
           verified=False, pose_unit='m', T_robot_axes_xr_axes=[list(r) for r in HYPOTHESIS_AXES]))
for record in [ref] + samples:
    print(json.dumps(record))
PY
```

## 本次验证结果与限制

17项测试在独立 Python3.10 / Pinocchio3.1.0 / CasADi3.6.7 / NumPy1.26.4 环境通过，包含真实官方求解、禁网络/控制SDK导入、单次模型加载、原序号/时戳保留、慢求解后的过期拒绝、未知/无效追踪区分、重复帧/跳变锁存、固定左臂、径向限幅、求解失败回退拒绝及stdin不结束时的有限退出。

```bash
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
  ~/.cache/unitree-g1-arms/env/bin/python -m unittest discover \
  -s tests -p test_live_pico_arm_ik.py -v
```

真实 PICO 录制 SHA256：`e526c31e006483fbbf0942028a2c69f1824cbcb35ef9fb380797826c08c023a3`。全量854帧回放、默认每帧2次求解的本次结果如下；关节参考来自另一次保存的机器人状态，**没有声称两份录制是同步实机参考**。

| 项目 | 实测 |
| --- | --- |
| 模型加载与预热 | 155.83 ms，仅一次 |
| 整个CLI离线回放 | 1.19 s，读取参考+854样本，共855条；退出1 |
| 候选 / 拒绝 | 276 / 578；第277帧失败，其后保持锁存 |
| 已接受的275次非零点求解 | 中位2.65 ms，P95 4.33 ms，最大5.01 ms |
| 已接受最大右末端位置/旋转残差 | 1.987 mm / 0.002551 rad |
| 已接受最大关节参考偏移 | 0.007401 rad；左7关节始终与参考完全相等 |
| 首次拒绝 | 第277帧位置残差 **2.112 mm > 2 mm**，旋转0.002722 rad；两次IPOPT均 `Solve_Succeeded`，模型限位正常 |
| 置信度与时间 | 候选的tracking_valid全为null，原序号/接收时戳/应用时戳保留 |

第277帧的目标平移只有约3.275 mm，未触及1 cm限幅；拒绝来自严格残差要求，不能解释成“计算成功所以可执行”。没有通过放宽阈值隐藏这项限制。后续需要在真实新鲜参考姿态下审查工作空间和拟合误差；不自动扩大关节范围、修改比例或跳过失效帧。

上述耗时是这次离线CPU测量，不是实时截止时间保证。组件没有连续反馈控制、速度/加速度约束、碰撞检查、Revo2载荷模型、完整Tracker安装标定或开发模式控制权管理。实际输入仍缺原生跟踪有效性、现场轴/单位确认；这些不能由成功IK补齐。所有硬件接入与输出由独立层处理，本次未连接硬件。
