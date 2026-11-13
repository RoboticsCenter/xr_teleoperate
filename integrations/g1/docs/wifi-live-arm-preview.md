# Wi-Fi 实时右臂预演

机器人外部网线拔除后，PC2通过192.168.31.65 SSH访问，DDS在机器人内部eth0订阅。`wifi_feedback_client.py`在PC2内存执行限时、只接收快照请求的`pc2_feedback_rpc.py`；不安装软件，不创建运动publisher。

两台主机的monotonic起点不同。状态最大年龄使用完整SSH往返时间加PC2样本年龄；电脑端的保守接收时间下界为请求发出时刻减去PC2样本年龄。缓存同序号样本保留第一次下界，不能重复刷新为新帧。该方法不是时钟同步；按短时间内普通时钟速率误差可忽略的假设计算。误差会导致保守拒绝，不能拿跨主机原始时间直接相减。当前预算100ms。

`run_live_arm_preview.py`先加载和预热官方IK，等PICO流实际出现后获取新鲜机器人参考，再用后续PICO原始回调时戳捕获原点。默认只做位置映射，比例0.02，平移径向上限1cm，每关节相对参考±0.03rad，固定左臂和末端朝向。输出只是候选，没有发布者。

```bash
python3 scripts/run_live_arm_preview.py --seconds 12 \
  --output diagnostics/live-arm-preview.jsonl
```

需要保持官方PICO PCService运行、头显连接192.168.31.92、Object模式及Send；同时只能运行一个native SDK接收器。命令会提示PC2 SSH密码。Ctrl-C退出，SIGTERM执行子进程清理，远端读取器也有有限生命周期。

本轮实际结果：`teleop-live-arm-preview-20261111-1828.jsonl`记录12秒850个候选，最大右关节偏移0.0131314rad（约0.75°）、左臂0，未输出运动。前一次启动顺序错误导致origin过期，已修正，未放宽时效。初版仅检查状态字段；后续加入真实SDK LowState CRC检查，只有CRC通过的本体反馈可作新参考。最新CRC版硬件结果单独记录，不能追溯标记早期样本CRC已验。

初始20次Wi-Fi状态采样RTT5.58–18.75ms、最大双手年龄上界37.65ms，三路均新鲜。纯软件8项测试覆盖跨时钟界限、旧样本、错误/重放、CRC拒绝与无命令协议。原版PICO不提供Tracker置信度，预演保持tracking_valid=null；这不代表双腕或实机闭环完成。
