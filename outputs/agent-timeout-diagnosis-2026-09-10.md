# Agent 非 live 套件超时排查（2026-09-10）

## 结论

发布时记录的 Book outcome 用例超时由本机遗留的测试 Docker 沙箱容器触发。旧测试清空 PostgreSQL 业务表和磁盘回执，却没有清理未正常回收的沙箱容器。下一次测试使用同一个确定性 run ID，遇到旧容器的不同请求摘要，被沙箱完整性检查拒绝。

这不是豆包额度、模型响应慢或 Book 生成逻辑导致的超时。仅清理已确认属于旧临时测试目录的退出容器，原始用例即恢复通过（1 项，22.014 秒，含 PostgreSQL/编译准备）；没有修改模型、生产沙箱校验或业务代码。

## 证据链

1. 原始失败用例为 `test_agent_backend_book_outcome_matrix.AgentBackendBookOutcomeMatrixTests.test_book_provider_permanent_mastery_learner_reason_is_never_published`。第一条主关命令停在 `VALIDATING`，尚未走到 Book 的专项断言。
2. 6 秒诊断探针确认 fake LLM 已调用 1 次，没有等待远端模型。
3. 第一处错误是 `SandboxResultIntegrityError: Sandbox container identity or security projection drifted`。镜像、非 root 用户、工作目录、禁网、只读根目录、非 privileged 和日志配置均符合预期，只有恢复标签不匹配。
4. 遗留容器 `yaya-sbx-89cac66946490d6d48705b61` 已退出，创建于 2026-09-09，挂载的是 `Temp/yaya-outcome-authority-01eehhlk/artifacts/…`。其确定性 run ID 与新测试相同，请求摘要不同。
5. 工具错误被运行时保守标为 `SIDE_EFFECT_COMMIT_UNKNOWN`；Hub 查询不到技能回执，报告 `UNKNOWN_COMMIT_STATE`。后续重试撞上未到期的 claim，记录 `AGENT_TURN_CLAIM_FAILED` / `AgentTurnLeaseConflict`，最终表现为 45 秒超时。
6. 核实容器 ID、退出状态、沙箱标签和精确挂载来源后，仅删除该测试容器；用未经探针修改的原始测试重跑通过。

完整套件中的 World CAS 并发用例也撞上同类问题。单独重跑两次，两个竞争者都报 `SandboxResultIntegrityError`，并未进入 CAS。再核实两个遗留容器 `yaya-sbx-978e3bea98768c1cb5d95046`、`yaya-sbx-f2d41b03c16ccac078e91d4c` 均已退出，挂载 `Temp/yaya-invocation-cpp-59_is04k/artifacts/…`，才定向清理。本次总计清理 3 个经过归属确认的旧测试容器；没有修改世界并发控制。

## 修复

- 在测试辅助模块新增按**实际挂载来源**判断归属的容器清理函数。
- 重置测试数据库配套回执时，先清理属于该临时目录的沙箱；相关测试类结束时也执行清理，避免失败用例污染后续类。
- 通过规范化绝对路径及目录从属判断识别归属，不仅按标签或字符串前缀批量删除；删除使用已检查的容器 ID。
- 新增真实 Docker 回归：同时创建本测试容器和相似路径名的另一个测试容器，验证重置移除前者、保留后者，并清空回执。修复前该断言失败，修复后通过。
- 非 live runner 清单随新增测试更新为发现 642 项、排除原有 2 个 live 项、执行 640 项。

## 超时解除后发现的旧预期

全套继续执行后，四个 `test_session_run_history_cross_*` 用例仍按旧设计要求：一条历史失败 Run 被修改后，Book 必须拒绝生成。现有 `ContextBuilder` 和 `test_agent_runtime_book_current_completion.py` 已明确改为只总结本次成功 Run，不读取 `list_session_runs`。这些旧断言与当前设计冲突。

已保留四种历史污染输入（session、actor、content、world），将预期更新为：本次成功正常完成；历史读取接口调用次数为零；Book 恰好调用一次；模型上下文没有 `session_runs`，没有历史读取工具，也没有被污染的标识；提示词明确限定本次完成。当前 Run、技能版本、世界提交和 Evidence 的原有拒绝测试保持原义。

另一个隔离测试原先断言 Docker daemon 中不存在任何 `yaya-sbx-*` 容器，导致其他工作区的容器也让它失败。改为按本用例的两个 run ID 检查回收；读写隔离、网络隔离、超时限制和活动任务清理断言均保留，不删除其他工作区容器。

## 影响边界

当前单网关仍复用 Agent 的 runtime 和 Docker 沙箱，因此这些共享组件仍有用途，不能整体删除。此次冲突发生在旧测试反复重置数据库、复用固定身份的环境；遗留容器属于测试目录，与正在运行的游戏容器无关。

生产代码未改动，也没有绕过请求摘要或容器安全校验。真实生产环境若发生身份复用或恢复数据不一致，仍会拒绝错误容器；本修复不声称解决所有此类故障的业务重试策略。

测试进程被强制结束或宿主机断电时，Python 清理钩子无法保证执行。若临时根目录已更换，不能为了让测试通过就全局删除沙箱，应先核对遗留容器的挂载归属，再定向处理。

## 验证

- 原始超时单例：通过，1 项。
- 清理回归和 runner 合同：通过，6 项。
- 首次完整非 live 套件：实际执行 640 项，耗时 1441.053 秒；632 项通过、8 项失败，无跳过。8 项分别为 4 个过时 Book 历史断言、1 个全局容器数量断言、1 个遗留容器导致的 CAS 用例失败、2 个测试依赖缺失。
- 缺失依赖为项目 `agent/pyproject.toml` 的 `test` extra 中已声明的 `rfc3339-validator==0.1.4`、`rfc3986-validator==0.1.1`。Agent 专用虚拟环境已有它们，本轮借用的后端测试解释器缺少；已从本机 uv 缓存按既有版本补齐，没有改校验器或放宽标准验证。
- 修复后的定向复测：32 项全部通过，耗时 259.830 秒，覆盖整个 Book outcome 矩阵、Docker 隔离与超时、World CAS 并发、容器清理回归、两项 RFC 格式校验、当前完成总结及 runner 合同。首次全量中失败的 8 项在复测中均已有通过记录；四个 Book 用例已按当前语义重命名。
- 最终测试发现核对：642 项发现、640 项非 live、2 项显式 live 排除，无导入错误。
- Ruff 检查、格式检查、Git 空白检查通过；临时诊断探针已删除，没有把调试日志或运行时秘密提交到仓库。

验证口径：首次全量为 632 通过 / 8 失败，随后修复并针对相关部分完成 32 项全通过；未在修复后再运行第二遍完整 640 项，因此不声称存在单次全量全绿的日志。

对应前次记录：`main-publish-validation-2026-09-10.md` 中的旧套件超时条目。

## 复验入口

安装原有 Agent 测试依赖并准备项目要求的固定摘要 GCC 镜像和 PostgreSQL 镜像后，从仓库根目录执行：

```powershell
python agent/scripts/run-non-live-python-tests.py
```

该入口会核实完整测试发现数量，明确排除两个真实 Provider 测试，并拒绝任何额外 skip。此次诊断不需要真实模型密钥。
