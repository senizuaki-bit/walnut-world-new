# Agent Build provider-neutral 库边界

`python/yaya_agent_build` 是 Agent 仓内稳定的 Build/CAS Python 包边界。它负责完整源码包校验、canonical source hash、digest-pinned Docker C++20 编译与 PUBLIC/HIDDEN tests，以及不可变 Artifact 的 content-addressed 发布和校验。

该包导入时只加载标准库与 `yaya_agent_contracts` DTO；不导入或装配 `yaya_agent_backend`、HTTP、PostgreSQL/psycopg、migration、worker、Provider 或 Runtime Sandbox。它仍由本仓库的统一 Python distribution 发布，并不是一个已经独立拆分版本治理的外部 distribution。

## 公共导入

后端或其他宿主应只从包根导入：

```python
from yaya_agent_build import (
    BuildResourceLimits,
    ContentAddressedArtifactPublisher,
    CppTestCase,
    CppTestSuite,
    DigestPinnedDockerCppBuilder,
    SubprocessCommandRunner,
    canonical_source_bundle_sha256,
    validate_source_bundle,
)
```

`yaya_agent_build.pipeline` 保存唯一实现。历史路径 `yaya_agent_backend.build_pipeline` 仅为迁移中的下游代码保留兼容 re-export；新代码不得从历史路径导入，也不得在 backend package 中复制或同步实现。

## 宿主构造 API

宿主负责提供已存在且非符号链接的 workspace/artifact 根目录、精确 digest 固定的镜像、编译器版本和版本化测试集：

```python
from hashlib import sha256
from pathlib import Path

from yaya_agent_build import (
    BuildResourceLimits,
    ContentAddressedArtifactPublisher,
    CppTestCase,
    CppTestSuite,
    DigestPinnedDockerCppBuilder,
)

suite = CppTestSuite(
    version="course-suite-v1",
    public_tests=(
        CppTestCase(
            test_case_id="public_01",
            visibility="PUBLIC",
            expected_stdout_sha256=sha256(b"ok\n").hexdigest(),
        ),
    ),
    hidden_tests=(
        CppTestCase(test_case_id="hidden_01", visibility="HIDDEN"),
    ),
)

builder = DigestPinnedDockerCppBuilder(
    Path("/srv/yaya/workspaces"),
    image="registry.example/yaya-cpp@sha256:" + "0" * 64,
    compiler_version="15.2.0",
    test_suites=(suite,),
    limits=BuildResourceLimits(),
)
publisher = ContentAddressedArtifactPublisher(Path("/srv/yaya/artifacts"))
```

生产构造默认使用 `SubprocessCommandRunner`，始终以参数数组、无 shell 的方式调用 Docker。单元测试或其他宿主可通过 `runner: CommandRunner` 注入等价 runner，但不得增加 host compiler fallback。`builder.build(request)` 接受冻结的 `CompileAndTestRequest`；成功结果的 `staged_artifact` 再交给 `publisher.publish(...)`。数据库事务、lease/fencing、worker receipt 和 HTTP 状态机仍由宿主负责，不进入本包。

Build 外部执行使用由请求、阶段、镜像与安全投影派生的稳定、全标签容器身份。`docker start --attach` 返回非零或丢失控制面响应时，Builder 不把不完整 attach bytes 当成终局，也不立即创建第二个容器：它先 inspect 同一容器；对 running/exited 状态用 wait/inspect 和有界 `docker logs` 恢复权威输出，对 created 或暂不可 inspect 的状态返回 `retryable` 基础设施失败。Backend worker 对该标志重试 durable job，不伪造学生代码 `REJECTED`。非零程序退出、资源上限或经权威 logs 验证的编译/测试失败才是业务终态。

## 回归门禁

- `tests/test_agent_backend_build_pipeline.py` 覆盖 Build/CAS，以及 start-attach response loss、非零 attach 返回后以 terminal logs 对账等恢复行为。
- `tests/test_agent_build_package_boundary.py` 校验新旧导出对象同一、实现只存在一份，以及干净进程导入不会加载 backend/psycopg。
- Pyright include 与 wheel package-data 显式包含 `yaya_agent_build` 和 `py.typed`。
