# Agent Sandbox provider-neutral 库边界

`python/yaya_agent_sandbox` 是 certified Artifact 执行的稳定 Python Adapter 边界。正式 Backend composition 从该包构造 `DockerCppSandbox`；导入包本身只加载标准库与 `yaya_agent_contracts`，不会装配 `yaya_agent_backend`、HTTP、PostgreSQL/psycopg、migration、worker、World Engine 或模型 Provider。

## 公共导入与构造

```python
from pathlib import Path

from yaya_agent_sandbox import DockerCppSandbox

sandbox = DockerCppSandbox(
    Path("/srv/yaya/artifacts"),
    image="registry.example/yaya-runtime@sha256:" + "0" * 64,
    docker_executable="docker",
)
```

`artifact_root` 必须是已存在目录。`image` 必须使用精确 `name@sha256:<64 lowercase hex>`；构造器会通过 Docker inspect 核对本地镜像为 Linux，缺失或漂移直接失败，不回退到 host compiler/native execution。容器执行保持 networkless、只读 root/artifact mount、non-root、drop-all-capabilities 与资源上限；Sandbox 成功只返回 action intents，不拥有 World 写权限。

`ArgumentBuilder` 可由宿主注入以适配已认证 Artifact ABI。`ProductionCppSandbox` 也从同一公共包导出，供原生隔离测试使用；正式 composition 明确只装配 `DockerCppSandbox`。

## 单一实现与兼容

- `yaya_agent_sandbox/docker.py` 保存 `DockerCppSandbox` 的唯一实现。
- `yaya_agent_sandbox/native.py` 保存共享 strict intent parser/failure mapping 与 `ProductionCppSandbox`，使 Docker Adapter 不反向依赖 backend 私有模块。
- `yaya_agent_backend.sandbox_container` 与 `yaya_agent_backend.sandbox` 只保留对象同一的兼容 re-export；新代码不得从旧路径导入或在 backend 中复制实现。

`DockerCppSandbox` 接受持久 `result_root`，以 invocation/request/context/image hash 派生稳定 run identity、不可变结果 receipt 和全标签容器名。Backend 一旦持久化 `SANDBOX_DISPATCHED` 就只调用 `reconcile`，不会再次 `run`：start/create 控制面响应丢失时 inspect 同一容器，running/created/exited 状态通过 start/wait/inspect/有界 logs 对账；Docker preflight/create/inspect 暂不可用保持 retryable/unknown，不落成伪终局 `DEPENDENCY_UNAVAILABLE` Run。只有实际程序非零、资源限制、输出限制或通过完整性校验的结果才成为 terminal receipt；receipt、标签、安全投影或输出 hash 漂移均 fail closed。

数据库事务、durable receipt、lease/fencing、Run/World/Evidence 和 HTTP 状态机仍由 Backend 宿主负责，不进入本包。Fresh-PostgreSQL takeover 与 pinned-Docker focused gates 证明已物化成功但响应丢失时可恢复同一结果且不创建第二次 Sandbox 副作用；这属于实现/恢复合同证据，不替代真实 Provider 三仓 live acceptance。

## 回归门禁

- `tests/test_agent_backend_docker_cpp_sandbox.py` 覆盖真实 pinned Docker 隔离、结果解析、控制面 response loss、暂时不可用恢复和无重复执行。
- `tests/test_agent_backend_cpp_sandbox.py`、`tests/test_agent_backend_cpp_sandbox_isolation.py` 与 `tests/test_production_cpp_sandbox.py` 保留原生 Adapter 回归。
- `tests/test_agent_sandbox_package_boundary.py` 校验旧路径对象同一、实现唯一和干净进程导入隔离。
- Pyright 与 wheel 门禁显式包含 `yaya_agent_sandbox` 和 `py.typed`。
