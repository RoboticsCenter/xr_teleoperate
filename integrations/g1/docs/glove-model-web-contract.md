# 网页 Wuji 个人手型模型校准接口

`scripts/dashboard_glove_model.py` 的 `GloveModelManager` 只运行 Wuji 官方个人手型模型校准，不做手指重定向，不导入机器人控制器，不启动 BrainCo 服务。构造、status和网页GET不启动任何进程。

```python
manager = GloveModelManager(
    root,
    before_start=stop_both_glove_readers,
    on_finish=refresh_models_and_selected_reader,
    simulation=False,
)
manager.start('left')
manager.status('left')
manager.continue_calibration('left', prompt_id=None)
manager.cancel('left')          # 或cancel()取消当前唯一会话
manager.close()
```

`start` 在锁内保留唯一会话并立即返回；备份、CLI和回调全部在后台线程执行。SDK当前用户和模型存储是全局的，因此同时只允许一侧个人模型校准。协调器应阻止双侧手指START、读取重启和开合采样；已有PICO/手臂服务不由本管理器操作。

`before_start(side)` 先停止两只手套读取；随后管理器持有模型全局锁及两个已有 reader lease。`on_finish(side, success)` 在reader leases释放后调用，且不持manager状态锁；callback返回之前会话仍active。取消发生在callback内部时，协调器须在重开reader之前再次检查`cancel_requested`，不能只依赖callback进入时的`success`值。只刷新/恢复输入，不自动启动机器人手输出。

网页约定由根HTTP服务实现：

| POST路径 | JSON | 行为 |
| --- | --- | --- |
| `/api/gloves/model/start` | `{"side":"left"}` | 点击即授权备份后替换该用户该侧个人模型，开始后台流程 |
| `/api/gloves/model/continue` | `{"side":"left","prompt_id":"0"}` | 仅当`awaiting_continue=true`时需要；prompt_id可省略 |
| `/api/gloves/model/cancel` | `{"side":"left"}` | 请求取消该侧当前校准 |

GET状态放在`services.hands[side].model_calibration`。主要字段：

- `active/running`只对正在校准的那侧为true；`active_side`显示全局所有者。
- `phase`: idle/preparing/backing_up/connecting/calibrating/cancelling/finishing/completed/cancelled/error。
- `state`保留SDK姿态状态，`step_name/step_index/step_total`标识当前动作。
- `progress_fraction`使用SDK给出的`feedback.progress`，范围0..1；未知则null，不从时间伪造。
- `instruction`、最多8条`diagnostics`、`feedback`原始对象及`log_tail`供页面实时展示。
- `backup_path/backup_bundle/output_dir`指向本项目的新会话备份，`model_path/model_sha256/model_verified`记录产物。
- `completed/success/error/exit_code/terminal_event/cancel_requested`区分成功、失败与取消。
- `simulation`始终如实标记，`robot_output=false`。

## 官方CLI流程

已安装CLI：`/home/jack-xiang/.local/opt/wuji-cli/2026.10.26/wuji`，commit `fd337026cbd69d49247644db19ba122b8e2146b9`。当前命名用户必须是`brainco-teleop`，不会自动切换用户。左SN为`WG1JA06260822033`，右SN为`WG1KA06260821549`。

后台依次：

1. 读取注册表并以`user show --json`核对当前用户。
2. 将注册表及完整当前用户目录复制到新的`calibrations/sdk-model-<side>-<timestamp>-<uuid>/files-before/`，保存SHA256清单。有已有校准时同时用官方`user export`生成`sdk-before.zip`；不传覆盖开关。空用户也保留文件备份，不因官方不能导出空bundle而阻止第一次校准。
3. 通过只读`get hand_side --sn <known-SN> --json`核对侧别，再核对当前用户未变。
4. 执行`calib hand-model --sn <known-SN> --jsonl`。`--sn`和`--handedness`不组合使用。
5. 只有CLI退出0、schema v2成功terminal事件、返回用户和侧别匹配、预期URDF有效且另一侧模型未变化，才报告成功。

SDK JSONL自动完成六种姿态，网页不要另加终端确认或用Enter跳过约束：四次拇指与其他指尖分别捏合、四指约90度弯曲、手掌平展。真实模式`awaiting_continue=false`；`continue_calibration`不会向CLI注入输入。模拟模式用六个Continue展示向导，不调用CLI、不写真实SDK模型、不获取生产reader/model锁。

取消先向本次拥有且PID身份匹配的进程组发一次SIGINT，让CLI释放设备；等待后才有界升级TERM/KILL。CLI默认900秒校准上限及自身每姿态约束保留；管理器不自动重试，不在输入/CLI错误后悄悄开始另一次采集。取消或异常后仍保留备份，既不自动恢复覆盖，也不冒充模型未变化；协调器比较模型hash并作废受影响的旧手指映射。

官方依据：[2026.10.26校准指引](https://github.com/wuji-technology/wuji-cli/blob/5b11537b9cb3d80de5462fcf57964313eb3a41ec/skills/wuji-cli-calib/SKILL.md)、[用户与备份指引](https://github.com/wuji-technology/wuji-cli/blob/5b11537b9cb3d80de5462fcf57964313eb3a41ec/skills/wuji-cli-user/SKILL.md)、[校准JSONL说明](https://docs.wuji.tech/docs/zh/wuji-cli/latest/hand-model-calibration)、[get参数JSON的value字段约定](https://docs.wuji.tech/docs/zh/wuji-cli/latest/parameters)。

本轮只读取CLI帮助、版本和本地`user show`；未连接实际手套做身份查询、未开始校准、未写真实SDK用户模型。针对性测试全部使用临时fake CLI/SDK目录；备份、进程退出、错侧/错用户、取消、reader互斥及模拟向导经过这些软件测试，不属于真实校准验收。
