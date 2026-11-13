# G1 本机遥操作网页

当前网络（2026-11-12）：电脑/PICO接收已切至 KBE Lighting 5GHz / `192.168.2.208`；PICO页面 <http://192.168.2.208:8027/>。G1 已迁移至同一 Wi-Fi 的 `192.168.2.117`，严格 SSH 和电脑相机画面已验证。以本地 `config/network.json` 和网页显示为准；下文旧地址属于历史记录。见[网络配置](network-config.md)。

地址：<http://127.0.0.1:8025/>。入口 `scripts/teleop_dashboard.py`；仅监听本机回环地址。浏览页面不会启动机器人、Tracker reader 或相机。当前控制服务 PID/地址写在 `diagnostics/dashboard-server.json`。

## 使用

1. 在“手臂输入”选择“腕部 Tracker”或“PICO 手柄”，点击“保存选择”。默认仍为 Tracker。输入服务若未运行，点击“启动输入服务”。头显使用 XRoboToolkit：Tracker 模式开启 Head + Object + Send；手柄模式开启 Head + Controller + Send。电脑 Wi-Fi IP 以网页当前显示为准（本轮 `192.168.2.208`）。
2. 机器人使用对应固件及腰部配置的 **Regular 模式**。用户已确认本机是三自由度腰硬件。Unitree官方按键图分别标注单腰R1+X、三腰R1+Y（该图标记长按2秒）；需以本机对应版本说明为准。[官方按键图，第2页](https://oss-global-cdn.unitree.com/static/5014b2b84df94951b1e2197081e02822.pdf)。当前入口使用 `G1_29_ArmController(motion_mode=True)` 的 `rt/arm_sdk` 路径，保留原运动服务；不调用 ReleaseMode 或自动切换模式。上游遥操作文档只明确记录R1+X路线并排除Running（R2+A），**本项目尚未验证三腰Regular与当前手臂输出的配合**。[上游说明](https://github.com/unitreerobotics/xr_teleoperate/blob/main/README.md)。只读 CheckMode 返回服务名不等于已确认实体机器人的 Regular 子模式。
3. **LAUNCH 初始化反馈与发布器，但手臂控制权重保持 0**；START重新采样三个腰关节的当前姿态，沿用指定分支的腰部增益保持；原厂继续控制腿部。腰部姿态保持不等于动态腰部平衡。READY 后保持舒适起始姿态、面向前方，点 START 捕获所选腕部及机器人末端参考，逐渐接入手臂控制。行走继续由机器人的运动控制器／遥控器处理，网页不发送行走速度。LAUNCH 不要求 PICO 已有输入；START 缺输入时留在 READY，恢复后可重新点击。
4. **STOP 将手臂控制权重降至 0 并退出会话**，不再调用旧的双臂归零回位。它不是整机急停，也不停止遥控器发起的行走。SSH 连接故障时不能远程证明权重已释放或物理停机；网页明确保留未知状态。[官方运动接口流程](https://github.com/unitreerobotics/xr_teleoperate/wiki/Motion)。
5. 在“机器人视角”点击“启动画面”，即可在电脑网页查看 G1 相机；相机独立于手臂和手指启停。仅在电脑查看时，不勾选“同时传到 PICO 头显”。该选项使用原分支 Remote Vision 编码与重连实现；PICO 测距画面已占用视频端口时，先保留电脑画面。

电脑相机源通过 G1 已运行的 `videohub` 服务读取 JPEG（官方 `VideoClient.GetImageSample()` 图像接口），不再直接打开被原厂服务占用的 `/dev/video4`，也不停止 `master_service`。为适应当前 2.4 GHz Wi-Fi，预览在 PC2 等比例缩至最大 640×360、JPEG 质量 70，每秒最多发送 5 帧；网页沿用每 350 ms 更新一次图像。停止画面仅结束本页的读取进程，不停止原厂相机服务或机器人控制。原有失联恢复与图像新鲜度规则保留。

重新启动网页（若已有服务运行先停止该服务；不要启动第二份）：

```bash
cd /home/jack-xiang/unitree-g1
.venv-dashboard/bin/python scripts/teleop_dashboard.py
```

启动时交互读取 G1 SSH 密码；密码只保留在进程内存、通过匿名 stdin 交给工作进程，不写入参数、环境、日志或 HTTP 返回值。Ctrl+C 停止网页及本页拥有的会话/服务。测试页面使用 `--simulation --port 8026`，不连接机器人。

## 手指速度与跟随停顿

2026-11-12按用户要求将手指位置+速度命令的归一化期望速度从0.2提高到0.6，即200→600/1000；左右手均使用此默认值，API每侧speed_normalized可核验。它是速度参数，不是三倍实测运动速度或完成时间承诺。[BrainCo位置+速度协议](https://www.brainco-hz.com/docs/revolimb-hand/en/revo2/modbus_pro.html)。源Wuji读取、个人模型、开合映射、alpha=0.3滤波、相对新鲜实测位置80/1000限步、0.1秒机器人反馈检查及电流/力限制不变。

当前主要响应因素：速度指令；原滤波与限步；Wi-Fi串行snapshot/target往返；原reader置信度过滤。低质量输入或反馈短暂过期时保持上一目标，有效后恢复；reader退出或共享连接真正断开时需明确重试。界面STOP会停止发送新目标，即使手套仍有数据也不会跟随。输入约120Hz不等于命令发布或实物跟随频率。

## 躯干主动平衡

用户确认三自由度腰硬件，并要求使用三腰Regular控制双臂和手指。01:09被动查询读到FSM501，原厂腰部kp=200/kd=5、滚转与俯仰目标随时间变化；本项目未发送模式切换。之前FSM500观测对应腰部kp=200/1200/1200。

01:13用户START期间的联合订阅发现真实缺陷：旧适配清零腰部12–14后，共享arm_sdk权重升到1，最终rt/lowcmd腰部kp/kd也全变0，而腿部增益保持；STOP后腰部kp200/kd5恢复。证据见[联合观测](../diagnostics/regular-3dof-waist-incident.json)。因此不能把零字段或mode=0理解为交还腰部控制权。[官方arm7示例](https://github.com/unitreerobotics/unitree_sdk2_python/blob/master/example/g1/high_level/g1_arm7_sdk_dds_example.py)、[官方ArmSdk封装](https://github.com/unitreerobotics/unitree_sdk2/blob/main/include/unitree/dds_wrapper/robots/g1/g1_pub.h)也显示腰部使用相同共享权重。

修复保留指定分支的三腰mode=1/kp=300/kd=3，在START重新采样当前腰姿态并保持，STOP将共享权重降为0，原厂恢复腰部接管。腿部命令保持由原运控生成，Wuji映射、PICO校准和输入容忍不变。这恢复了分支已有的腰部支撑，**不实现原厂动态腰部补偿与手臂接管同时独立运行**；公开SDK未找到经过证实的逐腰关节权重或排除接口。软件回归测试不能代替修复后的实机稳定性验收。

## 确切复用范围

指定来源 `kevin-roboticscenter/xr_teleoperate`，分支 `h2/pico-wuji-head-camera`，提交 `053ba99af55c1d8ddc6b87918cb0fa234625886e`。该分支 H2 集成依赖的运行时是 Unitree PR321 提交 `39b79bca34b7547b21edad3fe3e642bb5cac7147`，这里从同一运行时选择 G1_29 类，未使用该仓库顶层另一版本的控制器。

原样文件与 SHA256 见 `vendor/xr_h2/SOURCE.json`。正在使用：几何/heading/IncrementalWrist、read_frame、PICO 视频发送器、PR321 G1_29 IK/控制器及其过滤器。`AutoRecording` 已摘取，当前网页记录后端使用会话 JSONL，尚未接该类的腕部相机同步录制能力，不能称为完整原仓库数据集录制。

`dashboard_worker.py` 对分支 `pico_tracker/arm_teleop.py` 的集成包括 G1 类、单／双腕选择、电脑→PC2 SSH 传输、stdin/HTTP 启停适配及省去 H2 头电机。IK 数学、权重、速度/位置限制沿用源代码。首次建模后补 `model.createData()`，与源类缓存加载路径一致，解决新增末端 frame 后 Pinocchio 工作内存长度不匹配；不更改求解算法。`pc2_branch_arm.py` 的项目内子类仅适配正常运动模式的控制权与线程生命周期：初始实测关节目标、START采样三腰保持姿态、权重 0→1／1→0、清零腿部及未使用命令字段、停止发布线程；腰部保留源增益，vendor 文件保持原样。约一秒的权重过渡是接入／释放过程，没有固定会话时限。

输入容忍逻辑沿用源代码：

- `read_frame` 使用真实采样时刻，接受年龄 0–0.5 秒；接收时刻不会把旧帧标成新帧。
- 用户后续明确取消必须移动的要求：Tracker pose-change age 只作诊断，不再用于拒绝静止姿态；姿态/四元数有效性仍使用原 `pose_valid`。
- 输入失效或暂时断流时保持最后目标；同一 streamer session 恢复有效数据后自动继续。
- streamer session 改变时仍保持；需 STOP 后重新 LAUNCH/START 捕获参考。没有自动换参考姿态。
- 没有旧项目的固定 10 秒会话、跳变锁存、trigger/focus 门控或 nonce 策略。准备和进程退出等待是生命周期管理，不限制跟随会话长度。
- SSH 存活探测属于新增无线传输适配；失联最终导致会话错误，保持物理结果未知。没有修改 Tracker 输入阈值来掩盖网络故障。

## 当前范围与记录

现支持左／右／双腕控制和左右独立手指输出，双 Tracker APK 已由用户安装并实收双路输入。左右手套个人模型与开合校准均已保存；Wuji 读取和 Revo2 手指映射仍来自原项目。START 会准备已勾选的手套输入，逐侧等待有效数据后接入，缺输入或某侧失败不结束另一侧或手臂。STOP 取消等待与输出；源 reader 报错退出后不自动反复重启。当前正常行走模式适配仅完成软件测试与机器人只读查询，尚未验收边走边遥操作的实际效果。

每次 LAUNCH 在 `diagnostics/dashboard-runs/<session>/` 保存摘要、runtime.log、targets.jsonl；管理日志位于相邻 `.worker.log`。`有效 IK 目标` 是电脑端生成并提交的目标数量。新适配器另外统计成功返回的 DDS Write 调用，二者都不代表动作已执行或物理停机；异常退出无法取得最终值时仍显示未知。当前没有同步录制相机视频。

2026-11-12 的 `type: closed` 启动故障是电脑 worker 已传 `--control-mode normal_walking`、旧 PC2 脚本尚未接受该参数导致 argparse 退出。两端现已匹配，每次 LAUNCH 打包当前脚本；原有日志保留。若再出现启动退出，网页会显示版本、依赖、网络等已识别原因或指向本次 runtime.log。所有后续旧日期段落是演进记录，使用方式以上方当前流程为准。

本机依赖环境：`.venv-dashboard`（pexpect/numpy/PyAV），既有 PICO venv（native SDK），既有 G1 arms env（Pinocchio/CasADi/meshcat/logging-mp）。G1 模型资产位于隔离缓存 `~/.cache/unitree-g1-reuse/runtime-pr321/assets/g1`。原 Wuji 仓库未修改。

## 验证边界（2026-11-11）

- 会话/相机/worker：33 项测试通过，包括完整 fake following→holding→following→STOP。
- PC2 适配：12 项 fake 测试通过；仅导入类的 PC2 实机检查退出 0，未初始化 DDS、未构造控制器。
- G1 IK：8 项测试通过，包括真实模型 FK/IK、固定左臂、有效右腕增量目标、JSONL 进程正常退出。
- 原分支视频：8 项通过，包括实际 H.264 编解码与断流/连接恢复；没有打开真实相机。
- Playwright：模拟页面 LAUNCH→START→超过 10 秒继续运行→STOP、相机模拟启停通过。真实网页仅检查待机、按钮状态及拒绝缺 token 的 POST。

以上是软件/只读验证。尚未通过新入口执行实际手臂动作、校验腕部坐标方向/安装偏移或验证实机相机画面。

## 左臂与手指控制更新（2026-11-11 19:30 后）

本节替代上方旧的“仅右腕、手部未接入”范围说明。

网页支持左臂／右臂／双臂选择、独立 Tracker SN 绑定、共享输入检查、左右手指独立启停和张开／握拳采集。先检查输入、选择SN并保存；双臂要求两只不同Tracker的有效数据。单臂模式固定另一臂在START的关节值，使用同一原版IK。当前右SN已有记录，左SN仍需有效输入后绑定；不会使用旧缓存SN或镜像右腕冒充左腕。

勾选的手部在手臂实际START成功后启动，也可独立启动一侧；某手未就绪不退出另一手或手臂。总STOP停止全部跟随。无线手指桥接使用新隔离构建，详见[手部传输](dashboard-hand-transport.md)。原Wuji读取、confidence过滤、LowPass、closure/targets/step_limited均直接复用原项目；手指目标依据实测反馈限步。没有旧200ms输入锁存或固定会话时长。源reader因5秒无合格帧退出时报告该侧未就绪，不自动重扫；用户可再次检查输入。

用户明确选择“先添加控制，稍后校准”。右手保留既有个人模型与校准；左手缺个人模型和独立开合校准，所以左手START仍不可用。个人模型按[左手校准步骤](left-glove-calibration.md)创建，随后在网页采集张开和握拳，保存到本项目config/hand-calibration-left.json。不会复制右模型作为左模型。

用户此前已通过右腕网页入口运行46.52秒、提交1833个IK目标并STOP；软件记录完成，实际Write次数/物理停止仍未知。新增左腕/双腕、手指无线输出尚未实机验收，本轮仅软件检查和PC2隔离编译，未进行实物校准。


## 手柄作为手臂输入

PICO 左／右手柄直接对应左／右臂，也可选择单臂。切换来源只能在手臂和手部输出停止时保存。手指仍使用 Wuji 手套映射；手柄按钮不用于手指或行走控制。选择手柄后无需 Tracker SN，原 SN 配置会保留，切回 Tracker 即恢复。

手柄与腕部 Tracker 的手臂校准分别存储在 `config/arm-calibration-controllers.json`、`config/arm-calibration.json`。手柄未校准时沿用原几何的 1:1 相对位移；已有腕部方向／比例修正不会套到手柄。仍可从“检查输入”进入原人工距离校准流程，PICO 测距页跟随所选来源。LAUNCH 无新增输入门槛，START 捕获头朝向与所选手柄中立姿态；启动后暂时断流保持目标、有效输入恢复后继续，无固定会话时长或必须移动要求。

接收适配 `scripts/pico_arm_stream.py` 使用同一已安装官方 SDK 的 JSON 回调，单客户端同时读取 Tracker 和 Controller，再由 `arm_pose_source.py` 投影到原腕部数据格式，原 IK／控制器不变。绕开 Python getter 对已关闭数据区的缓存，分别检查数据区和各侧时间；静止姿态有效。当前头显包没有逐手柄硬件 tracking-valid 标志，因此这里只能验证本包包含有限有效位姿及其新鲜度，不能独立证明手柄定位硬件状态。离线与网页测试不能代替实机手柄跟随验收。

## 跟随头显的参考坐标

“参考坐标”可选“固定方向（START）”和“跟随头显（位置与朝向）”。固定模式保留已有行为；头显模式每帧以头显当前位置为原点，头显的水平朝向对应机器人前方，上方保持竖直向上。输入位置为 `R_headingᵀ (p_controller − p_head)`，输入朝向为 `R_headingᵀ R_controller`；`R_heading` 只包含水平朝向，不包含头显俯仰或侧倾。START 记录相对中立姿态及机器人当前手腕姿态，再使用原有增量映射和 IK；它不把头显原点冻结在START的位置。

保持手相对头显不变时，一起走动或水平转身不会改变手臂目标。点头和侧倾不会旋转参考坐标轴；真实点头同时发生的头部位置变化仍会移动原点。单独左右转头或移动头部会改变手相对头显的位姿。这里没有向机器人发送躯干或行走命令；机器人行走仍由原厂控制器和遥控器负责。

切换参考坐标必须先结束跟随，保存选择后下次 LAUNCH/START 使用新模式。头显模式持续需要有效 Head 和所选手柄/Tracker 数据；临时缺失时沿原规则保持最后目标，有效数据恢复后继续，不自动换起点。读取器将每个控制器/Tracker 的接受样本与同一 SDK 包内的 Head 位姿配对；缓存输入不会借用较新的 Head。包内配对不表示传感器具有严格硬件时间同步。

校准按输入来源和参考坐标分开保存：固定 Tracker 使用 `arm-calibration.json`，固定手柄使用 `arm-calibration-controllers.json`；头显模式分别使用 `arm-calibration-headset.json`、`arm-calibration-controllers-headset.json`。切换不会覆盖旧文件或草稿，也不会把固定模式的方向/比例修正直接套入头显模式。新模式未校准时沿原默认 1:1 映射，可重新做三轴人工距离校准。采集时保持头朝向稳定，以实际测量距离为准；网页和 PICO 测距显示都采用当前参考坐标。

水平头显映射标识为 `headset_heading_v1`。旧版完整三维旋转的头显校准不能混用，文件保留但需重新采集；已有固定方向校准仍兼容。原始输入日志及每帧目标记录映射版本，便于核对实际方向。离线方向、共同移动、点头/侧倾测试通过不等于已经完成实物跟随验收。

## 双 Tracker 实读更新

当前已启用双臂，左SN `PC2310MLLA050051G`、右SN `PC2310MLL9241957G`。4秒采样283帧均有两路新鲜输入，每路位姿更新279次。该次使用现有头显Object数据流；用户随后确认UI仍限制一只，不能把这次短暂双流视为数量限制已修复。该次检查未触发机器人动作。仍需按实际左右腕佩戴核对绑定。

此前“原APK只能输出一只”的表述过强：它仅在模式切换时请求ONE，已处于Object时可能保留已有数量，读取循环支持多只。此次通过原分支SDK路径确认两路变化，未采原始Motion.len或额外confidence。现在已按等价TWO改动生成独立安装包，见下；尚未安装或验证头显运行。


## Object 两只 Tracker 安装包更新

已提供 **XRoboToolkit G1 Wrists**，在 PICO 浏览器访问 <http://192.168.31.92:8027/> 下载并完成安装。关闭原应用，打开新应用，选择 Object 完成两只 Tracker 提示，启用 Head，连接 `192.168.31.92` 并开启 Send。它每次选择 Object 都显式请求 TWO，不再保留原先切换时 ONE 的限制；电脑端原分支容忍不变。

当前仅通过两条原生指令修改、打包、签名与独立审查；头显安装及实际双流尚待验证，不自动运行机器人。安装包说明与验证证据见 [双 Tracker 构建记录](pico-two-tracker-build.md)。双臂和左右 SN 配置已保存，手部校准仍按用户选择稍后完成。


## 新应用安装后的现场验证（20:20）

用户确认新应用Object允许两只并开启Send。当前双腕实读验证通过：5.012秒342个唯一帧全部新鲜且Head有效，每只位姿变化339次；网页tracker.ready=true，左SN `PC2310MLLA050051G`、右SN `PC2310MLL9241957G`。用户已LAUNCH的会话进入READY，可从现有页面按START捕获双腕中立参考。检查未发任何控制请求，手部启用与校准状态未改。详见[现场证据](../diagnostics/pico-two-object-installed-live.json)。


后续独立实读发现：4.003秒255帧均通过原分支live，但左Tracker有一次0.602863m位置跳变（25.07ms观察间隔）。双路新鲜数据不等于准确/稳定跟踪；没有更改用户指定分支的容忍或新增跳变门控。见[独立实读报告](../diagnostics/pico-two-object-independent-live.json)。用户会话同期已进入双臂FOLLOWING；这只是软件状态，物理动作反馈尚未核验。


## 网页手套完整校准（20:41）

现支持个人模型→原开合映射→只读六通道预览的完整网页流程，详见[网页手套校准](glove-calibration-web.md)。无需额外终端；本轮只准备功能，未替用户开始真实采集。左模型按钮可用；右既有模型可直接启动只读输入并采开合，或显式重新校准。模型/映射备份及取消均已接入。


## 静止 Tracker 与当前手部状态更新

用户明确要求移除“Tracker需要持续移动”的限制，现已移除本项目中的pose-change age门槛；静止姿态只要数据包新鲜、数值有效即可使用。旧数据包仍保持目标，同一输入session恢复新鲜包后继续。不会因为这个改动自动启动机器人。

左右SDK个人模型均已由用户完成，左开合映射也已保存；右尚需完整张开/握拳采样。输入停止后，未保存的采样对会失效并提示重新采集，不再显示陈旧的“张开已采集”。页面区分待校准与真正的读取/输出错误。用户目前已关闭G1以省电，实际手部跟随需开机并恢复Wi-Fi后才能验证。
