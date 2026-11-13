# Wuji 双手套网络接入审查

审查日期：2026-11-11（PST）。本次只读取官方公开文档和已保存诊断；没有扫描、连接设备、修改网络或设备参数。用户已确认两只手套供电充分，本文不推断供电故障。

## 当前结论

左手已在本机 ASIX 网卡所在的以太网中被发现，右手的数据路径仍未证实。两只 USB 网卡经过同一个 USB Hub，不表示它们的 RJ45 网络互通；没有第三张 USB 网卡也不能单独证明右手未接入，因为两个以太网设备可以通过交换机共用一张主机网卡。

官方支持左右手套以不同 IP 在同一子网同时在线，并明确提醒多张主机网卡处于同一手套子网会造成路由/冲突问题。优先整理右手 RJ45 数据链路。适合本项目的目标拓扑是：**一张手套网卡通过以太网交换机接左右手套，G1 保留独立网卡**。这是根据官方网络要求给出的工程建议，尚未进行本机硬件验证，也不代表任意 UGREEN Hub 内置以太网交换功能。

## 已有本机证据

| 项目 | 已保存结果 | 能说明什么 |
| --- | --- | --- |
| 左手 | `WG1JA06260822033`，`192.168.1.100:50001`，UDP | 2026-11-11 16:53 的 discovery 只列出左手 |
| 手套网卡 | ASIX AX88179B，`enx6c1ff772bfe8`，`192.168.1.250/24`，carrier 1、100 Mbps | 左手通过此网卡可达；链路速率本身不证明另一端设备身份 |
| G1 网卡 | Realtek RTL8156，`enx6c1ff762d908`，`10.42.0.1/24`、`192.168.123.10/24`，carrier 1、1000 Mbps | 与手套网卡为不同的以太网接口 |
| 本机 USB 枚举 | 两张 USB 网卡，均在同一 USB Hub 层级中 | USB 上游共享不能替代以太网交换或桥接 |
| 右手目标路由 | `192.168.1.101` 走 `enx6c1ff772bfe8`，源地址 `.1.250` | 普通 ping 默认走左手侧，不是在检查 G1 侧 RJ45 网络 |
| PC2 单次 ARP | `eth0` 对 `.1.101` 发出 1 个 ARP 请求，等待 2 秒，无目标回应 | 只说明当时该接口上此默认 IP 无回应；不能证明手套缺电、实际 IP 未变或所有数据链路断开 |

来源：[USB/网络拓扑快照](../diagnostics/wuji-usb-network-topology-2026-11-11.json)、[发现结果](../diagnostics/wuji-discovery-multi-adapter-2026-11-11.json)、[PC2 单次 ARP](../diagnostics/wuji-right-pc2-arp-2026-11-11.json)。历史右手目标为 `WG1KA06260821549` / `.1.101:50001`，本次尚未重新确认其在线身份；见现有 [Wuji bring-up 记录](wuji-bringup.md)。

## 官方文档给出的约束

### 线束、以太网与供电

Wuji 的专用 USB-C 转以太网供电适配器同时提供供电和以太网通信。采集板通过专用 C-C 线束连接该适配器，适配器另接网线和电源线；电源线官方要求/推荐 USB-A to USB-C，不能把普通 USB-C 扩展坞的外观当作等价接法。这里的专用线束与供电线是不同连接。[电气与网络集成](https://docs.wuji.tech/docs/zh/wuji-glove/latest/electrical-network-integration.md)、[快速开始](https://docs.wuji.tech/docs/zh/wuji-glove/latest/getting-started)

官方要求专用 C-C 线两端白色矩形标识朝向一致，并说明反向连接可能仍有电而没有数据。因此，在两只手套供电充分的已知条件下，线束方向仍是可检查的数据链路因素；**没有证据表明当前右手实际接反**。[快速开始](https://docs.wuji.tech/docs/zh/wuji-glove/latest/getting-started)

### IP、双手同网段和发现范围

| 问题 | 官方信息及对本项目的含义 |
| --- | --- |
| 默认 IP | 左手 `192.168.1.100`，右手 `192.168.1.101`，掩码 `255.255.255.0` |
| DHCP | 手套使用静态 IP，不支持 DHCP；主机对应网卡应在相同子网，地址不能冲突 |
| 一张网卡接两只 | 官方明确左右手默认 IP 不同，可在同一网段同时在线；交换机拓扑是实现这一要求的工程建议 |
| Discovery 跨网段 | 官方描述发现同网段设备，且要求对应网卡和设备同子网；未提供跨路由子网发现保证，不能假设经过 G1/PC2 自动中转 |
| 端口 | 官方故障排查给出 UDP `50000` 用于发现、`50001` 用于数据，端口可修改；当前发现结果为 `:50001`，应尊重实际读回配置 |
| 区分左右手 | 多设备使用序列号定位；SDK 也支持 handedness 和不同 `device_name`，但序列号更适合锁定本项目既定设备 |

来源：[电气与网络集成](https://docs.wuji.tech/docs/zh/wuji-glove/latest/electrical-network-integration.md)、[SDK 设备发现与连接](https://docs.wuji.tech/docs/zh/wuji-sdk/latest/device-connection)、[手套故障排查](https://docs.wuji.tech/docs/zh/wuji-glove/latest/troubleshooting.md)。

SDK 的 `enable_bridge` 可让已建立的设备连接供其他 SDK 实例访问，包括其他主机。它与物理手套的本地 UDP 发现是不同层次；在列表中看见共享设备，不能据此证明该手套直连本机网卡。文档未在这些页面定义 discovery 的跨路由配置或按网卡绑定方式；不以猜测的组播地址、端口改动或桥接设置替代链路确认。[SDK 设备发现与连接](https://docs.wuji.tech/docs/zh/wuji-sdk/latest/device-connection)

### 多 USB 网卡处在相同子网

Wuji 官方专门列出多网卡导致 ping 走错路径、其他设备占用目标 IP、同子网冲突等情况；建议核对 `ip route get`、邻居 MAC，并让实际接手套的网卡成为该子网的明确出口。本机现有快照中 `.1.0/24` 仅走 ASIX，当前并没有证据说明已发生双网卡同子网冲突；**直接向 G1 网卡再添加同样 `/24` 才会引入需要额外处理的竞争路径**。[手套故障排查](https://docs.wuji.tech/docs/zh/wuji-glove/latest/troubleshooting.md)

官方快速开始中的 `ip addr flush` 示例会清除该接口现有 IP。不要套用到正在承担 G1 通信的网卡。若将来确实需要独立网卡分别接手套，应优先采用不同子网的明确规划；固定 `/32` 路由、源地址选择或策略路由属于额外工程方案，应在真实接线、设备 IP 和返回路径已确定后设计，不是当前右手未发现时的盲试操作。[快速开始](https://docs.wuji.tech/docs/zh/wuji-glove/latest/getting-started)

### 修改 IP 是设备持久配置

官方 SDK 支持通过 `glove.ip().get()`、`glove.port().get()` 读回网络参数，通过 `.set(...)` 修改；网络参数写入会自动持久化到设备 Flash，`save_params()` 不是保存这些设备参数的操作。官方同手性双设备排障流程要求给其中一只改 IP 后重启。当前左右手默认 IP 本来不同，**不需要为了双手同时使用而先改 IP**。若历史配置已经变化，应以实际读回值为准。[设备运维](https://docs.wuji.tech/docs/zh/wuji-glove/latest/sdk-data-reference/device-operations)、[SDK 设备参数](https://docs.wuji.tech/docs/zh/wuji-sdk/latest/device-params)、[手套故障排查](https://docs.wuji.tech/docs/zh/wuji-glove/latest/troubleshooting.md)

## 建议的数据拓扑与后续判据

```text
电脑 ASIX 网卡 192.168.1.250/24
    └─ 以太网交换机
         ├─ 左手 Wuji 专用适配器 ─ 专用 C-C 线束 ─ 左手采集板
         └─ 右手 Wuji 专用适配器 ─ 专用 C-C 线束 ─ 右手采集板

电脑 Realtek 网卡 192.168.123.10/24 ─ G1
```

交换机是以太网数据交换设备，与 USB Hub 不是同一功能。上述拓扑保留现有 G1 数据链路，也利用左右手已不同的默认 IP。图中省略已确认正常的独立供电连接。

交给 `state_probe` / 主代理的下一步依据：

1. 先确认“右手接 G1 那组适配器”中，右手专用适配器 RJ45 实际终点，以及中间是否确有以太网交换机/网桥；不要把 USB Hub、供电 USB 或可见网卡数量当成这条链路的证明。
2. 沿实际 RJ45 路径确定唯一应承接右手的主机接口，再结合已保存的地址、路由和邻居信息判断；当前普通 `.1.101` 流量走左手网卡这个结论已经足够，无须重复同一路径扫描。
3. 若后续在已确认正确的数据链路上仍无法发现，再核对右手历史静态 IP 是否改过、专用线束的数据方向、连接会话占用。端口/防火墙排查应建立在二层/IP 可达证据之上；开放 UDP 端口不能使不通的 ARP 链路变通。
4. 接入完成的判据是同时发现正确的左右 SN、记录实际地址/端口，并由原 Wuji reader 分别读到连续数据。仅有 ping、网卡 carrier 或 USB 枚举不足以替代该判据；读帧成功也不自动等于 BrainCo 或 G1 运动验证。

本审查没有实施这些接线、网络或设备操作，也没有修改现有 Wuji→BrainCo 映射。

### 右手的最短隔离步骤（后续建议，未执行）

在采购或调整交换机拓扑前，先复用已验证的左手 ASIX 数据通路：保持右手专用适配器和既有供电，仅将其 RJ45 网线临时接到当前 ASIX 网口，替换左手的 RJ45 连接；保持 ASIX `192.168.1.250/24` 和 G1 网线不变。右手默认 `.1.101/24` 与该主机地址匹配。与此同时只核对 Wuji 专用 C-C 线（采集板↔专用适配器）白标同向，不把此方向要求套用到普通供电 USB 线。

官方要求手套与对应主机网卡同子网，没有规定左右手必须各占一张网卡；本次检查的文档未见禁止上述临时 RJ45 替换的限制。随后只做一次有界发现，核对实际右手 SN `WG1KA06260821549` 和 UDP 地址。若能发现，说明右手在已知通路可达，继续查原多适配器数据路径；若不能，仍保留右手实际静态 IP 可能已修改、线束数据方向和设备链路等分支，不能直接认定设备损坏。Zenoh 共享条目 `WG1KA06260811540` 不算这只右手通过隔离检查。[电气与网络集成](https://docs.wuji.tech/docs/zh/wuji-glove/latest/electrical-network-integration.md)、[用户须知](https://docs.wuji.tech/docs/zh/wuji-glove/latest/user-notice.md)

## 补充：左手 0/361 与 EMF 告警（17:23 记录）

最新 [质量采样](../diagnostics/wuji-errorcheck-quality-2026-11-11.json) 收到左手 `WG1JA06260822033` 的 **361 个不同关节角帧 / 3 秒，约 120.33 Hz**；五指都有 5 个角度槽位且角度有限。原 reader 接受 0 帧，因为中指、无名指、小指的 `hand_joint_angles.fingers[i].confidence` 在全部 361 帧都小于 `0.5`；三指均值约 `0.319 / 0.129 / 0.181`。这是有数据但未通过原有输入质量门限，不能称为网络未传输。

这里有两种不同的置信度，必须分别记录：

| 层次 | 官方含义 | 本项目的阈值/已有证据 |
| --- | --- | --- |
| `emf_poses.poses[i].confidence` | EMF 单指定位置信度，范围 0–1 | SDK 低于 `0.85` 持续 `0.5 s` 激活低置信度告警，恢复至 `0.85` 清除；本次日志确实出现五指 `EmfConfidenceLow` |
| `hand_joint_angles.fingers[i].confidence` | 手部模型的 IK 求解置信度 | 原 reader 要求每指均达到 `0.5`，任一不合格就丢整帧；0/361 指这个筛选结果 |

源 EMF 与派生关节角置信度不是同一个量，阈值也不等价。单有 IK 低分不能证明原始 EMF 故障；本次另有 SDK 的 EMF 告警作为独立证据，但尚未保存同帧原始数值来判定两者关系。[官方手部追踪数据定义](https://docs.wuji.tech/docs/zh/wuji-glove/latest/sdk-data-reference/hand-tracking.md)、[本次错误记录](../diagnostics/wuji-errorcheck-errors-2026-11-11.json)、[原 reader](../../branco-wuji-teleop/brainco-wuji-teleop/wuji_bridge.py)

### 最短现场排查顺序（供后续执行，本审查未执行）

1. **整理佩戴和初始姿态。** 戴稳手套并调整手臂绑带，手指微弯、自然放松，不握拳、不交叉指尖 EMF 模块；避免拉扯模块和手背线束。官方将此作为连接软件前的姿态要求。[用户须知](https://docs.wuji.tech/docs/zh/wuji-glove/latest/user-notice.md)
2. **先移开干扰源，再检查同手距离。** 让左手离开 G1 金属机身、大型电子设备、金属桌面和磁性附件，发射线圈与指尖之间不要有大面积金属；五指保持在本手背发射线圈的 `30 cm` 工作范围内。官方产品参数对金属/铁磁材料给出建议避空距离 `≥3 cm`，这是一般建议，不能据此保证 G1 附近一定无干扰。[故障排查](https://docs.wuji.tech/docs/zh/wuji-glove/latest/troubleshooting.md#emf-低置信度告警)、[产品参数](https://docs.wuji.tech/docs/zh/wuji-glove/latest/introduction.md#emf-相关参数)
3. **保持原阈值进行一次短质量复测。** 由独立只读采样器同时记录源 EMF 与关节 IK 置信度，按下节的同时间戳关系比较。原始 EMF 仍差时继续查磁场/传感器链路；原始稳定而 IK 差时再查佩戴几何和左手模型匹配。这是分层定位思路，不是单个数值即可证明某个部件损坏。当前日志明确左手加载内置默认 URDF、缺少个人左手模型；暂不通过降低原 reader 阈值或覆盖既有右手标定绕过质量问题。
4. **仍异常再做针对性健康诊断。** 官方 `wuji doctor --sn WG1JA06260822033 -v --json` 会扫描并采实时数据，可区分各指 RX 接收与 TX 发射断线；这是一项后续实物检查建议，本轮没有运行。若执行完整 doctor，触觉检测要求将手套脱下、静置、不按压；不要把佩戴时的触觉警告当成已确认坏点。该命令的退出 0 也不代表没有警告，应读具体 EMF 检查项。[官方健康诊断](https://docs.wuji.tech/docs/zh/wuji-cli/latest/doctor.md)

专用 C-C 两端白色矩形标记同向仍是接线检查项，但**左手已经稳定收到数据，不宜用“整条线反接导致完全无数据”解释当前 0/361**。该检查更适用于完全无数据的右手；当前两手都没有已证实的线束朝向问题。[用户须知](https://docs.wuji.tech/docs/zh/wuji-glove/latest/user-notice.md)

对“双手发射线圈相互干扰”的核对：已查官方产品介绍、用户须知、机械集成和排障页，未找到明确的双线圈串扰机制、双手最小间距或禁止同时开启规定。官方只明确一般磁场/金属干扰。若后续把另一只手移远做对照，应标为**待验证的工程假设**，保持被测左手姿态和周围金属条件尽量一致；不能声称已确定右手线圈造成当前问题，也不编造安全间距。

### 本机 SDK 可用的只读诊断接口

核对对象为本机 SDK `2026.11.9` 的 `wuji_sdk/__init__.pyi`（缓存位置 `/home/jack-xiang/.cache/uv/archive-v0/l--w3MU5hJLs7sDj/lib/python3.12/site-packages/wuji_sdk/__init__.pyi`），仅查看文件，没有实例化 `SdkManager` 或连接设备。

| 接口/来源 | 可获得的事实 | 限制 |
| --- | --- | --- |
| `glove.emf_poses().subscribe()` | `EmfPoseArray.header`；`poses[0..4]` 按拇、食、中、无名、小指排列，每项有 `pose.position`、`pose.orientation`、`confidence` | 这是 EMF **位姿估计结果**，不是原始线圈电压/波形；不能单凭置信度区分金属干扰和硬件损坏 |
| `glove.hand_joint_angles().subscribe()` | 每指 5 个角度槽位与 IK confidence，保留原 reader 接受数 | 与 EMF 数据按同一 SN、`header.timestamp_us` 对齐，输出匹配数/未匹配数；不能拿不同时间的汇总值做因果判断 |
| `glove.hand_model_path().get()`、现有 SDK 日志 | 当前模型路径/内置回退信息 | 读回不等于执行校准；本次已知用内置 LEFT URDF |
| `glove.resources()` / `params()` / `topics()` | 设备实际公布的资源及 `can_get`、`can_sub` 能力；随后只能读取已公布的可读资源 | `.pyi` 未给 WujiGlove 专用的线圈电压或硬件错误状态 typed getter；不能凭名称猜测不存在的 `error_status()` 或原始传感器路径 |
| 既有 `~/.wuji/logs/error_codes.log` / `sdk_<日期>.log` | SN、具体部件、故障码及 active/cleared/history 状态 | 历史/已清除事件不是当前活跃故障；SDK 生成的低置信度告警与设备硬件上报能力分开记录 |
| `wuji_sdk.describe_error(full_code)` | 离线解释完整错误码，返回名称、原因和处理建议 | 不连接设备，不查询当前故障；已有离线结果可直接复用 |

官方明确关节角、骨架和指尖位姿由 EMF 帧解算，**时间戳与对应源 EMF 帧相同**；因此可以按 `header.timestamp_us` 严格关联。`tip_poses` 的 `seq` 为独立流计数，不应泛化成所有源/派生流的序号都相同。源 EMF 的 frame_id 为 `l_hand_emf_tx` / `r_hand_emf_tx`，其位置相对于手背发射线圈；不要与 PICO 世界位姿混用。[手部追踪](https://docs.wuji.tech/docs/zh/wuji-glove/latest/sdk-data-reference/hand-tracking.md)

原始置信度低于 `0.85` 的帧数适合统计质量，但不能直接换算为 SDK 告警次数：官方还有连续 `0.5 s` 的触发条件。停掉订阅后告警会因 SDK 不再收到手部数据而清除，也不应将这种清除记作硬件恢复。[故障排查](https://docs.wuji.tech/docs/zh/wuji-glove/latest/troubleshooting.md#emf-低置信度告警)

本次现有 SDK 日志另外给出两个重要边界：`spatial calibration active on 5/5 channels (model_id=1)` 表明已加载五路空间标定，不证明当前测量精度；`SDK error-code log attached without device hms_codes` 表明此次没有设备 `hms_codes` 资源上报证据，因此不能以“只有低置信度告警、没看到硬件码”排除线圈硬件问题。[SDK 日志](../diagnostics/wuji-errorcheck-sdk-2026-11-11.log)、[官方故障日志定义](https://docs.wuji.tech/docs/zh/wuji-sdk/latest/device-fault-logs.md)

设备资源应以实际清单为准，未知路径不能通过盲猜读取来证明不存在；官方也明确要求按 resources 的输出原样访问。[资源发现与参数读取](https://docs.wuji.tech/docs/zh/wuji-cli/latest/parameters.md)。如需保存下一次短采样，仅记录源 EMF、派生 IK 与相关日志即可；不要调用 `.set()`、`set_rate()`、`calibrate*()`、重启或故障清除来改变排查基线。
