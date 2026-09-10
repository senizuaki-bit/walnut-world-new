# Bug 挑战前端接入与验证

更新：2026-09-11。分支 `all`；接入基线为 `origin/main` 的 `13106a7`，快进合入，无冲突。本次只本地提交，不推送。

## 接入结果

正式 AppRoot 配置完成后启用练习链：进入关卡 → start/status → 主关 Build/Activation/Run → 已验证的 SUCCEEDED Run → prepare → 独立 C++ 编辑器 → answer → summary 文字与完整 PCM 同时展示 → 完成归档。

- 五个操作统一连接主 Gateway，携带游戏 token 与请求追踪头；按最终 HTTP 200 处理，不轮询 Command，也不激活练习 build_id。
- 主关提交前必须确认 start 成功。主关成功回执即可准备挑战，不等待旧 Book 生成；旧反馈失败不会撤销成功或抢占挑战。
- 同一 entry 最多一道题，重复成功信号不会再次出题。旧 `bug_agent` / `book_agent` Interaction 继续由原同步链消费游标，但不再展示；叮当教学保留。
- 使用预置 `BugPracticePanel`、`HTTPRequest`、`CodeEdit`、`AudioStreamPlayer` 和容器布局。练习源码与主关 Draft 分离，练习不写世界、不发布技能。
- 答案携带 CPP20/main.cpp 源码包和 UTF-8 SHA-256。每次新尝试生成 answer_id；请求结果不明时锁定原源码、保留原 ID，重试同一份请求。编译/测试拒绝属于业务结果，原题和代码继续可编辑。
- entry、run_id、挑战源码与未确认答案按 Gateway/session 隔离，写入 `user://bug-practice-<hash>.json`；临时文件完成后原子替换。token、音频不落盘。
- 恢复已创建局用 status；CHALLENGE_READY 恢复编辑，SUMMARY_PENDING/COMPLETED 重新获取总结。失效/损坏记录明确要求重开；不会无声创建新局并套用旧 Run。显式重新进入/重玩生成新 entry。
- 总结必须匹配 challenge_id、text_sha256、音色、24000 Hz / pcm_s16le 及完整 Base64。校验完成后同帧显示全文与播放；提供重听，隐藏面板/离开场景停止音频。
- 请求超时 310 秒；总结失败保留挑战通过状态，只重试 summary，禁止重新答题或重新运行主关。

## 主要文件

| 文件 | 职责 |
| --- | --- |
| `scripts/client/bug_practice_gateway.gd` | HTTP、追踪头、答案封装、错误体和音频校验 |
| `scenes/ui/bug_practice_panel.gd/.tscn` | entry 状态、恢复、独立编辑器、判题反馈、总结音频 |
| `resources/ui/bug_practice_controls.tres` | 练习按钮主题 |
| `scenes/app/crop_agent_bridge.gd` | 主关门禁、成功回执触发挑战、旧交互显示过滤 |
| `scenes/level_demo/crop_adaptive_watering_demo.gd/.tscn` | 预置面板、进入/重玩及音频互斥 |

## 验证证据

| 检查 | 结果与边界 |
| --- | --- |
| Godot 4.7.1 离线回归 | 88/88 通过；末次界面及桥接修订另跑定向测试通过 |
| 新练习状态测试 | start 失败门禁、同 ID 重试、成功去重、编译拒绝、通过后禁答、总结失败、音频篡改、410 与未确认答案恢复通过 |
| CropAgentBridge 定向回归 | 新 entry 未确认不执行主关；主关成功后旧 Book 失败仍能进入挑战；不调用旧 speech；原提示和旧兼容测试通过 |
| 真实 FastAPI 路由协议测试 | Godot 发真实 HTTP，经新版主路由、鉴权、源码验证和练习状态机，完成五操作与 PCM 播放、状态查询、410、认证拒绝；外部模型/判题/音频采用确定性测试替身 |
| 后端定向测试 | `test_bug_practice.py`、`test_practice_reliability.py`、`test_book_speech.py`：22 passed |
| 真实服务主链 | Godot → 真实主关构建/激活/Run 成功；Run `run_2151caa3083d3bad63c539b3` |
| 真实模型出题与挑战判题 | 初次 `PRACTICE_PROBLEM_INVALID`，保留 entry/Run 后成功重试；challenge `challenge_62b0f2cc313141cbbadc83a9ff8b60e7`；真实编译错误与正确答案通过，主关源码/世界快照不受练习改变 |
| 真实书书配音 | **未验证成功**：返回 `BOOK_SPEECH_CONFIGURATION_INVALID`，本机未发现豆包 key 文件；挑战保持 SUMMARY_PENDING，未提前展示半份总结 |
| 界面 | 1280×720 实际渲染检查，修复边距、按钮和代码可读性；下方截图使用确定性协议测试数据，不代表真实模型/TTS |

![挑战编辑器：确定性路由测试](evidence/bug-practice-challenge.png)

![总结与重听：确定性路由测试](evidence/bug-practice-summary.png)

原有无配音对白现在整句显示并保持 idle，因此同步修正旧角色呈现测试对 talk 动画的过时断言。全量日志中已有少量退出资源清理警告，不代表全部退出清理问题已解决。

## 复测

```powershell
./walnut-world-frontend/scripts/run-offline-tests.ps1 -GodotExe <Godot4.7.1控制台程序>
./walnut-world-backend/.venv/Scripts/python.exe walnut-world-frontend/scripts/testing/run-practice-http-test.py
```

协议测试使用临时本地端口，无真实凭据，不调用付费 Provider。可设置 `GODOT_EXE`；`WALNUT_PRACTICE_CAPTURE` 为截图输出前缀。

真实验证脚本为 `scripts/testing/bug_practice_live_test.gd`，需现有主网关、Docker 沙箱、游戏 JWT，设置 `WALNUT_PRACTICE_LIVE=1`、`YAYA_API_BASE_URL`、`YAYA_AUTH_TOKEN` 后用 Godot `--headless --path ... --script ...` 运行。`WALNUT_PRACTICE_RESUME` 可指定测试保存文件以恢复同一局，不重新提交主关。该测试会真实提交主关和练习，可能消费模型额度；不将真实 token 写入脚本。

## 后续行动与未解决问题

1. 提供本机豆包 key 文件路径，配置后端 `YAYA_DOUBAO_VOICE_API_KEY_FILE` 并重启网关，再验证真实语音、书书 TTS。前端只持有游戏 token。
2. 本机已有数据库绑定 `deepseek-flash`，启动脚本需传 `-Model deepseek-flash`。此次保留已有模型与数据库，没有修改 profile 来绕过校验。
3. 出题模型曾连续产生不符合题目规则的草稿；服务端拒绝后原局重试成功。前端提供可恢复错误，不伪造题目；后续可统计真实出题稳定性。
4. 后端练习状态仍是单网关内存、6 小时有效；重启会导致 410。前端持久记录不能替代后端长期持久化，旧 entry 不能跨重启继续判题。
5. 服务已在 `D:/FeishuAIreview/walnut-interface-audit-20260909` 的代码上启动。本工作区对应 `all`；原 `walnut-world-new` 仍为 `fronted-art`。

接口权威：[完整 API 入口](../../../walnut-world-backend/docs/frontend-api/README.md)、[Bug 与书书扩展](../../../walnut-world-backend/docs/frontend-api/Bug军团与书书接口.md)。
