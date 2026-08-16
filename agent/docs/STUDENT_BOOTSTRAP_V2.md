# Student Bootstrap v2（contracts 0.4.0）

`GET /v1/student-bootstrap` 是公开 Godot 学生端的单一启动读模型。客户端通过
`YayaAgentApiGateway.get_student_bootstrap(attempt_context)` 调用；场景和 UI 不应直接访问
HTTP，也不应读取服务端私有 `yaya_*` 对象。

INT1 的唯一生产实现是 `walnut-world-backend` 的 `GET /v1/student-bootstrap`。它从 Backend-owned PostgreSQL 读取唯一 LaunchAuthority，并返回 server-owned Session/Build/Activation/World closure；Agent 仓只发布本合同和 validator，不运行第二个 Bootstrap 服务。Session 创建完成后，Backend Control worker 会在同一终态事务创建 starter Draft 与 Workspace，因此客户端不预置或猜测这些资源。

响应固定声明 `api_version=1.1.0`、`contract_version=0.4.0`，并一次返回：

- 当前 actor、内容版本和服务能力；
- 可原样 POST 到冻结 `createAgentSession` 的 GAME Session 请求模板；
- Build policy、编译器、sandbox 镜像、测试套件、允许能力和 32 文件/1 MiB 上限；
- 指定 world + agent profile 范围内的 Registry 修订和精确激活版本；
- 世界 revision、事件游标、state hash 以及 HTTP snapshot/events 恢复地址。

所有对象均为 closed shape。运行时验证器还会拒绝 JSON Schema 不能直接表达的跨字段漂移：
actor/content 必须等于 `request_context` 权威值；learner 必须等于学生 actor；Session、Activation
与 World 必须指向同一 world 和 agent profile；活动 Skill 的 Registry 修订必须匹配外层修订；
恢复 URL 必须精确指向返回的 world。

`session.create_request` 的 exact closure 为
`{world_id, learner_id, agent_profile_id, channel, locale, content, expected_world_revision}`，
与冻结的 `agent-session-create-request.schema.json` 完全兼容；客户端不得增删、改名或重组字段，
可把该对象原样作为 `POST /v1/agent-sessions` body。`channel` 固定为 `GAME`，并且
`expected_world_revision` 必须等于同一响应中的 `world.revision`。只读教学权威
`session.teaching_spec_version` 不属于创建请求，不能随 POST body 回传。

## 0.3 冻结边界

0.4.0 是追加发布，不修改原 `game-api.openapi.json` 或任何原 schema。基线资源
`contracts/releases/agent-contracts-v0.3.lock.json` 固定 commit `7841120` 上的旧 manifest：
134 个 manifest entries，加 manifest 自身，共 135 个字节冻结产物。Node 生成器与 Python
生产启动校验都会核对旧 entry 的 `(path, bytes, sha256)` 摘要；即使有人修改旧文件后重新生成
0.4 manifest，启动与发布门禁仍会失败。

发布候选检查：

```powershell
node scripts/generate-contract-manifest.mjs --check --git-release refs/tags/agent-contracts-v0.4.0
node scripts/validate-contracts.mjs
npm run test:godot
python -m unittest tests.test_student_bootstrap_v2
```

`--verify-git-ref` 只能在 `agent-contracts-v0.4.0` 标签实际创建并指向当前发布提交后执行。
