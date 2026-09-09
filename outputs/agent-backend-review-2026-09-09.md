# Agent / Backend 检查：以 Demo 可演示性为准

2026-09-09，检查 fronted-art 当前工作区，HEAD 为 11c7366151a4f2e9c33d9cf88eb459fa93835b92，包含已有未提交改动和新增语音文件。本次未修改业务源文件。

按用户补充要求，优先考虑本机 Demo 的启动、学生操作、聊天体验和回归。无需为真实生产、恶意流量或长时间基础设施故障增加复杂防御。

## 演示前值得处理的三个问题

### 1. [P1] 一次失败运行后，越聊天越慢

更新：此项已修复。5 轮列表读取由 13,546 条 SQL 降至 610 条；详见 [修复与回归结果](chat-history-performance-fix-2026-09-09.md)。以下保留原始检查证据。

位置：walnut-world-backend/src/walnut_backend/adapters/postgres/product_interactions.py:1708，以及 run_outcomes.py:884、:751。

失败 Run 后的普通聊天会引用最近失败 Run。读取列表时，_hint_interaction_has_authority 调用 latest_failure_authority_for_hint；后者逐条验证前面的 Hint，又进入同一套 Product 校验。Hint 验证结果未在一次读取内共享，历史被递归重复检查。

在独立 PostgreSQL 测试库中，使用真实 Backend 和固定模型回复，实测：

| 连续聊天轮数 | 一次列表 GET 的 SQL 数 | Hint 校验次数 | 耗时 |
|---|---:|---:|---:|
| 1 | 538 | 1 | 1.219 秒 |
| 2 | 1,403 | 3 | 2.968 秒 |
| 3 | 3,136 | 7 | 6.688 秒 |
| 4 | 6,605 | 15 | 14.406 秒 |
| 5 | 13,546 | 31 | 33.859 秒 |

耗时包含本机当时负载；查询数和 1、3、7、15 的重复校验增长不依赖机器速度。用户正常聊天几轮就会遇到明显等待，直接影响 Demo。

最小修法：在同一个请求/数据库会话中共享已验证 Hint 和 Run 的结果，避免递归重复读取。现有 TerminalProjectionValidationState 已有同类思路，可扩展复用，无需新增缓存服务。补一个“失败后连续问 5 次，再读取列表”的查询次数回归即可。

### 2. [P1] 按语音 Demo README 的依赖配置，新环境无法启动

位置：agent/examples/doubao_voice_demo/README.md、server.py:52、agent/pyproject.toml:21。

README 让用户在 Agent 目录运行 uv sync --extra voice，再通过 uv run --extra voice 启动 server.py。但 voice extra 只增加 websockets，server 却直接导入 Backend 的 PostgreSQL 适配器，依赖 SQLAlchemy 等 Backend 包。

用隔离环境、离线缓存和当前 lock，按同一依赖配置执行 server.py --help，在访问网络、读取凭据之前就稳定报错：

    ModuleNotFoundError: No module named 'sqlalchemy'

复现命令（在 agent 目录执行，--isolated 不改现有 .venv）：

    uv run --isolated --offline --frozen --extra voice python examples/doubao_voice_demo/server.py --help

最小修法：统一使用已安装 Backend 依赖的解释器运行语音 Demo，并补齐 websockets；或给示例单独列出完整依赖。无需改动整体架构。

当前机器已验证 `walnut-world-backend/.venv/Scripts/python.exe agent/examples/doubao_voice_demo/server.py --help` 可通过导入与参数检查；这不等于真实语音连通测试。

### 3. [P2] “不直接给答案、不能假称执行”目前主要靠提示词

位置：agent/python/yaya_agent_runtime/validators.py:350，以及 walnut-world-backend/src/walnut_backend/workers/turn_worker.py:421。

普通 message 分支校验角色和结构后直接接受正文。full_solution_eligible=false、hint_level=1 并不约束正文包含多少解题信息；少量中文成功关键词也无法保证模型不假称执行。

实际证据：

- 使用项目 ContextBuilder、打包角色配置和真实 validate_decision，完整 C++ 浇水算法在 hint_level=1、full_solution_eligible=false 时原样通过。
- 固定 Provider 返回“I watered every plot and changed your code. Your crops are ready.”，真实 Backend 将其写入 Interaction，公开 GET 返回 200 并展示该句，尽管 Hint 没执行这些操作。

这不说明真实模型必然越界，而是证明当前校验器不会阻挡这类回复。Demo 不建议上复杂分类器；先用演示问法验证“索要完整答案”和“是否已执行”等典型输入，明确哪些要求只是 prompt 约束。需要强保证时，再对当前关卡加小范围、有针对性的检查。

## 顺手修的门禁问题

这些问题主要影响开发验证，不等于 Demo 每条功能都失败。

1. Agent Python strict 类型检查有 **12 errors**：doubao_realtime.py:149 的动态 JSON、context_builder.py:1302 的 Mapping、voice.py:44/86 的字典类型。补类型收窄即可。
2. Agent 格式检查有 **3 个文件**需格式化：doubao_realtime.py、context_builder.py、test_agent_voice.py；146 个已通过。Ruff lint 本身通过。
3. Node 合同历史检查 **4 项失败**：generate-contract-manifest.mjs:21/43/124 仍依赖原独立仓库提交和根路径 contracts/manifest.json，合仓后不能正确读历史。指定正确 Python 后是 172 passed / 4 failed。当前 Schema 与 Backend 字节 pin 通过；历史发布门禁与当前运行合同应分开看。Demo 无需重建发布体系，但脚本应匹配当前布局。
4. 启动脚本有 **1 个过期测试断言**：tests/contract/test_start_persistent_play_script.py:63 仍匹配旧的 $null 写法，脚本已改用 Clear-ProcessEnvironmentVariable / [NullString]::Value。应更新测试，不要为测试恢复旧写法。这项失败不是凭据泄漏证据。

## 验证结果

全量历史故障注入套件已按 Demo 范围主动中止，未计为全量通过。最终 Demo 相关 Backend 集成测试 **13/13 通过**，耗时 179.73 秒；共享 Agent runtime **141/141 通过**，语音适配器 **18/18 通过**。

中止前 Agent 历史 `yaya_agent_backend` 的 Learner worker 套件出现 3 个 FAIL（failure_response_loss、failure_unknown_commit、model_cas_change_after_claim）。它们属于已退出生产 Gateway 的历史 composition，本轮未继续定位，保留原始日志；不能声称 Agent 全量测试通过。当前演示使用的 `walnut_backend` 与共享 runtime 另行验证。

| 检查 | 结果 |
|---|---|
| Agent / Backend Ruff lint | 通过 |
| Agent Ruff format | 146 通过，3 个需格式化 |
| Backend Pyright 1.1.411 | 0 errors |
| Agent Pyright 1.1.411 strict | 12 errors |
| Agent TypeScript 7.0.2 | 通过 |
| Agent Schema / Port surface | 通过；149 files、33 operations |
| Backend contract pin / pip check | 通过；148 byte-pinned wire files |
| 新测试库 Alembic 全量迁移 | 通过；head 020_skill_artifact_per_build |
| Backend 首次 unit + contract | 456 passed；1 个旧断言失败、2 个缺少数据库前置条件 |
| Node tests（正确 Python 环境） | 172 passed，4 个历史合同检查失败 |
| 连续聊天实测 | 已复现查询量指数增长；公开 GET 仍返回 200 |
| Agent voice 单元测试 | 18 passed |
| Agent runtime 单元测试 | 141 passed |
| Backend Demo 纵向集成 | 13 passed，0 failed，0 skipped；含提示/聊天、Bootstrap、世界提交、修改提案后的手动构建/激活/成功运行 |
| 语音示例干净环境启动 | 失败，缺少 SQLAlchemy |

主要覆盖：Agent 路由、上下文、教学策略、模型输出、语音；Backend HTTP、鉴权与作用域、Turn/Run/Interaction、worker、Build/Sandbox、迁移与启动脚本。飞书部分做本地审查，未访问外部系统。真实模型、麦克风、Godot 现场联动未在本次调用。

## 不列为 Demo 必修项

也复现了“大请求先完整读入内存”和“数据库连续连接失败 1,025 次后退避浮点溢出”。按 Demo 范围，这两项不应挤占功能体验的修复时间，不建议本次围绕它们扩展生产防护。离线证据保留在 review_agent_backend_probes.py。

文档中的 migration head、合同数量和历史 PASS 数量存在不同步；本次只使用实际命令结果，没有把历史 PASS 当成本次结论。

## 复现与日志

- outputs/review_chat_chain.py、chat-chain-review.log：真实 PostgreSQL 连续聊天、SQL 计数及公开消息读取。
- outputs/review_agent_backend_probes.py、agent-backend-probes.log：离线复现。
- outputs/voice-demo-startup-review.log：语音示例干净环境启动。
- outputs/agent-review-pyright.log、backend-review-pyright.log、agent-review-format.log。
- outputs/agent-review-node-configured.log。
- outputs/backend-review-full.log：中止的全套，未生成完整 JUnit 结论。
- outputs/agent-review-non-live-docker.log：中止的历史全套，含 3 个待定位 FAIL。
- outputs/backend-demo-review.log、backend-demo-review.xml：最终 Demo 相关纵向测试。
- outputs/agent-runtime-review.log、agent-voice-review.log：Agent 运行时与语音测试。

本次创建的临时 PostgreSQL 容器及测试数据在检查结束后清理；Docker 引擎和其他已有容器保持可用。
