# 独立迭代顺序

接口工程完成后，各组按以下顺序独立实现。

## Python 后端

1. 建立 FastAPI inbound adapter，实现 `game-api.openapi.json`，先接内存 Command Store。
2. 实现持久化 `CommandStorePort`、幂等键请求哈希和未终态巡检。
3. 实现 `PolicyPort`、`SkillRegistryPort` 和内容版本固定。
4. 将 C++ Sandbox 作为独立进程或容器实现 `SandboxPort`。
5. 实现只读 `WorldPort` 和唯一写路径 `WorldUnitOfWorkPort`；用显式 stream sequence CAS 同事务提交状态、事件、Outbox。
6. 实现 Learner projector、Evidence 查询和重放。
7. 最后实现供应商无关 `DeliveryPort`、`AuditPort`、Webhook inbound adapter 与飞书 Outbox Worker。

## Godot 前端

1. 场景只依赖 `YayaAgentApiGateway`。
2. 先通过本目录 Mock Server 开发完整状态机。
3. 所有响应先通过 `YayaAgentContractValidator`。
4. `202` 显示“已接收”，收到 `APPLIED + WORLD_COMMIT` 才显示世界成功。
5. 断线后携带最后事件序号补拉；发现序号缺口时获取快照。

## LLM Adapter

OpenAI、DeepSeek 和确定性 fallback 分别实现 `LlmPort`。应用层只能依赖 `LlmPort`，不得读取供应商特有响应。任何 fallback 必须显式设置 `degraded/source/fallback_reason`。

## 合并门禁

任何实现 PR 在合并前运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-all.ps1
```

如果更改 OpenAPI、Schema、错误码或事件，必须同时更新示例和消费者合同测试。破坏性变更需要升级主版本，不能原地修改语义。
