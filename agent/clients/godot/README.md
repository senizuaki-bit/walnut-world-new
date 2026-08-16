# Godot 接口边界

场景和 UI 只能依赖 `YayaAgentApiGateway`，不能直接创建 `HTTPRequest` 或解析后端私有字段。

当前交付包含两类 Transport：

- `FixtureTransport`：测试 Runner 内的纯本地确定性适配器，用于单元与合同测试；
- `YayaHttpAgentApiTransport`：正式 REST 适配器；

仓库当前不宣称已交付生产 `ReplayTransport`。后续若增加 Fixture、Replay 或离线适配器，必须实现同一个 `YayaAgentApiTransport` 结果联合，并通过同一套 Adapter Contract Test。HTTP 响应进入 UI 前必须调用 `YayaAgentContractValidator`；校验失败时显示明确错误并保留经过请求路径或 `Location` 验证的 `command_id`，不得使用默认字段继续回放。

## 固定调用边界

- 所有 HTTP 调用都携带当前 attempt 的 `X-Request-Id`、`X-Trace-Id`、`X-Correlation-Id` 和 `X-Schema-Version`。资源体中的 `request_context` 是创建资源时的不可变来源上下文，不能被轮询 attempt 覆盖。
- 所有写方法都显式接收 `idempotency_key`，Gateway 会在发出请求前校验格式。
- `get_world_events(request_context, world_id, after_sequence, limit=100)` 的 `limit` 只能是 `1..500`。
- Skill 激活成功后通过 `get_skill_activation(request_context, activation_id)` 回查不可变激活资源和 Registry 修订，不能只相信 `202`。
- `202` 必须同时具有合法 `Location` 与 `Retry-After`；世界快照/事件必须保留版本头，证据必须保留 `ETag`。

Transport 成功时返回 `{ok, status, headers, value}`，失败时返回 `{ok, status, headers, error}`，且不允许任何额外字段。失败结果中的 HTTP 状态必须和 26 项错误目录一致。Gateway 只把验证通过的值交给 UI；网络失败、超时、取消、非法 UTF-8、JSON 损坏/重复键和本地合同错误均返回 `status=0`、空 headers、`scope=CLIENT_LOCAL` 的显式失败，不会制造“看起来成功”的默认对象。

## 异步 HTTP 使用

`YayaHttpAgentApiTransport` 必须挂在仍位于 SceneTree 中的宿主 `Node` 上。Bearer token 应由运行时安全配置注入，不能写进场景或仓库：

生产 `api_base_url` 只接受规范的 `https://` origin（可带合法端口和路径）。明文 `http://` 仅用于 OpenAPI 声明的本机 Mock：host 必须精确为 `127.0.0.1` 或 `localhost`，端口必须显式为 `8790`，且不能带 base path。userinfo、查询、片段、反斜杠、非规范 authority 和依赖 DNS 解析的“本机”名称都会在构造 Authorization header 前 fail-closed。Transport 禁用自动重定向，避免允许的入口把 Bearer 转发给未校验的目标；需要迁移 endpoint 时必须先更新受信配置，再发起新的 attempt。

```gdscript
const AgentApiGateway = preload("res://agent_api_gateway.gd")
const HttpTransport = preload("res://http_agent_api_transport.gd")

var transport := HttpTransport.new(self, api_base_url, access_token, 15.0, 8)
var agent_api := AgentApiGateway.new(transport)

func refresh_command(context: Dictionary, command_id: String) -> void:
	var result: Dictionary = await agent_api.get_command(context, command_id)
	if not result.ok:
		show_explicit_error(result.error)
		return
	apply_command(result.value)

func cancel_refresh(request_id: String) -> void:
	if not agent_api.cancel_attempt(request_id):
		push_warning("该 HTTP attempt 已结束或不存在：%s" % request_id)

func _exit_tree() -> void:
	agent_api.shutdown_transport()
```

每个业务调用和 Fixture 都跨帧异步完成，调用方必须使用 `await`。Transport 为每个 attempt 创建独立的 `HTTPRequest`，默认超时 15 秒、最多并发 8 个、响应上限 8 MiB，并关闭隐式 gzip 解压；达到并发上限返回 `LOCAL_TRANSPORT_BUSY`，响应过大返回 `LOCAL_TRANSPORT_RESPONSE_TOO_LARGE`，不会排队或吞掉错误。`cancel_attempt(request_id)` 会让正在等待的调用以 `LOCAL_TRANSPORT_CANCELLED` 恢复，不会留下永远等待的协程。

`npm run test:godot` 同时运行合同边界测试和真实本地 HTTP 测试。后者由 Godot 内的 TCP 测试服务接收真实 `HTTPRequest`，覆盖 GET/POST 映射、Bearer、四个 attempt headers、Idempotency-Key、JSON 失败、50ms 超时、主动取消、主线程非阻塞、并发上限，以及非法 base URL/跨 authority 重定向绝不触达凭据接收端的负例。
