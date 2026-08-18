# 核桃代码世界 · Walnut World

> 孩子用 C++ 写出的代码，会被编译、测试、认证成一件 **Skill**——AI 小核桃真的调用它去干活，世界由确定性引擎客观判定成败。
> AI 只在真实证据上做分层教学，永远不能替孩子改代码，也不能宣布一个没有发生的成功。

**队名**：核桃世界　|　**命题**：核桃编程｜AI 原生少儿编程学习产品创新

---

## 提交项

| 提交项 | 地址 / 说明 |
|---|---|
| 世界观 PV | https://meek-bublanina-794c71.netlify.app/ |
| 3—5 分钟 Demo 视频 | https://cheerful-heliotrope-66b1ac.netlify.app/ |
| 在线体验入口 | 本仓库 https://github.com/senizuaki-bit/walnut-world-new<br/>初版 demo https://github.com/senizuaki-bit/walnut-content-engine/releases/tag/demo-2026-07-20 |
| 学习洞察【多维表格】 | [核桃世界｜学习洞察](https://larkcommunity.feishu.cn/base/Q6V9biulZaezaHskZqacYm2JnHe?table=blkK3ldZA0pePRGb) |
| 教室数据中心【秒搭】 | https://dcniaqwtmoca.feishuapp.com/app/app_17c6bc5hz7d/records |
| 完整方案文档 | https://my.feishu.cn/docx/EXevdqxO4oeC0ix2W5JcGz1VnHb |

建议体验顺序：先看**世界观 PV** 建立概念 → 再看 **Demo 视频**理解教学闭环 → 最后按下面的[快速开始](#快速开始)在本地跑起来。

---

## 一、产品定位

### 要解决的问题

核桃编程已服务千万级青少年，下一阶段的挑战不是"做对一道题"，而是同时解决三件事：

| 真实环节 | 痛点 | 影响 |
|---|---|---|
| 孩子写代码 | 代码只为通过题目，结果抽象，没有"我的代码真的在工作"的感受 | 学习动机依赖外部要求，难形成主动创作 |
| AI 辅导 | 题目一简单，AI 一句话就泄底；只看聊天文本又脱离真实错误 | 无法区分独立完成、轻提示完成和 AI 代做 |
| 程序运行 | 大模型若直接控制游戏，会出现"说完成了，但世界并没完成" | 教学证据不可信，孩子、教师、家长都无法复核 |
| 教师与家长 | 编译错误、提示使用、修复过程分散在不同记录里 | 复盘成本高，只看得到结果，看不到成长过程 |

> **我们的重新定义**：真正要解决的不是"让 AI 帮孩子把题做对"，而是**让孩子写出的代码成为 AI 可以真实调用、世界可以客观验证、教学系统可以持续复用的能力**。

### Skill 是什么

**Skill** 是学生编写的 C++ 代码，经过**编译 → 测试 → 认证 → 激活**后形成的世界行动能力。

它不是提示词，不是一次性答案，也不是能直接改写世界的脚本：

- 从**学生**看：是自己创造、保留在技能树里的数字工具；
- 从 **Agent** 看：是有明确输入、输出和权限边界的可调用 Tool；
- 从**系统**看：是带来源、哈希、认证状态和不可变版本的软件产物。

### 一次完整体验

```
发现问题 → 体验规则 → 写成 Skill → 真实执行 → 世界变化
```

1. **提出目标**：芽芽说明剧情与客观完成条件
2. **创造并认证**：孩子写 C++，后端完成构建、测试、认证、激活
3. **真实执行**：小核桃调用**精确 Skill 版本**，Sandbox 输出结构化动作
4. **世界验证**：WorldEngine 原子提交合法变化，生成 Run / WorldEvent / Evidence / Receipt
5. **教学回流**：失败触发分层提示，成功后由书书总结；脱敏证据进入飞书

### 五个角色 = 五种权威（不是预设对白）

| 角色 | 何时出现 | 依据 | 绝对不能做 |
|---|---|---|---|
| 芽芽 | 任务开始、世界状态变化 | 剧情、经营目标、任务规则 | 评价代码或宣布掌握 |
| 小核桃 | 构建、激活并真实调用 Skill 时 | 精确 Skill 版本、Run、世界回执 | 无运行结果时声称完成 |
| 叮当师傅 | 编译失败、运行失败或主动求助 | 真实错误、变量轨迹、世界结果 | 第一次失败就贴完整答案 |
| Bug 先生 | 连续三次同类失败 | 真实公开边界案例 | 为剧情编造 Bug |
| 书书 | 世界目标客观完成后 | 代码版本、提示与运行历史 | 提前结算，或把一次成功说成永久掌握 |

### AI 分层教学 L0—L4

| 层级 | AI 做什么 | 学生控制权 |
|---|---|---|
| L0 | 只展示事实（"1 号番茄仍差 5，6 号土豆已超目标"） | 自己观察并修改 |
| L1 | 定位代码区域，不给写法 | 接受或拒绝提示 |
| L2 | 解释一个概念 | 继续独立修改 |
| L3 | 提供局部支架（缺失表达式 / 分支骨架） | 完成剩余代码 |
| L4 | 生成结构化修改提案（改动前后、理由、证据、影响范围） | **显式接受或拒绝；接受后仍须手动构建、激活、运行** |

前端进一步收紧 L4：**同类失败累计 4 次且学生主动看完 L3 后**，才出现"AI 修改提案"入口。

提示深度由真实证据决定，不是随机的（见 `agent/python/yaya_agent_runtime/pedagogy_policy.py`）：

```
失败 0~1 次 → 等级 1      失败 2 次 → 等级 2
失败 3~4 次 → 等级 3      失败 ≥5 次 → 等级 4（此时才允许 Patch）
```

且阶段本身封顶：REVIEW ≤1、HEURISTIC ≤2、RECTIFICATION ≤3。**没跑过代码时**（无失败证据）只能走 REVIEW / HEURISTIC 启发式提问；**跑过并失败后**才进入 RECTIFICATION 针对性讲解；**目标完成后**切换到书书出成长总结。

### Demo 关卡：《作物适配浇水器》

8 块土地种着不同作物，旧浇水器统一浇到湿度 60——番茄仍缺水，土豆却浇多了。

| 土地 | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 |
|---|---|---|---|---|---|---|---|---|
| 当前湿度 | 20 | 65 | 45 | 90 | 60 | 35 | 55 | 50 |
| 目标湿度 | 60 | 70 | 50 | 65 | 60 | 70 | 50 | 65 |
| 缺口 | 40 | 5 | 5 | -25 | 0 | 35 | -5 | 15 |
| 正确水量 | 2 | 1 | 1 | 0 | 0 | 2 | 0 | 1 |

规则：`缺口 = 目标 - 当前`；≥30 浇 2 份，0<缺口<30 浇 1 份，≤0 不浇。1 份 = 250 毫升。

难度边界只用**一层 for 循环 + 一维数组 + 差值计算 + if/else if**，却能暴露写死目标、差值方向、下标错位、分支顺序、循环边界等多种真实问题。

代码卷轴的初始代码是**填空模板**，孩子一打开就知道要做什么：

```cpp
for (int i = 0; i < 8; i++) {
    // 第一步：将两个标记替换为目标数组名和当前数组名
    int gap = /*目标*/[i] - /*当前*/[i];

    // 第二步：将边界和份数标记替换为正确数字
    if (gap >= /*边界*/) {
        cout << "WATER " << i << " /*份数*/\n";
    } else if (gap > /*边界*/) {
        cout << "WATER " << i << " /*份数*/\n";
    }
}
```

> 注意两个 `/*边界*/` 和两个 `/*份数*/` **各自填不同的数**（30/2 与 0/1）。模板故意编译不过，所以第一次构建是编译错误——这也是分层教学的第一个真实证据。

---

## 二、架构

### 权威边界（本项目最核心的设计）

> **AI 决定"建议做什么"，孩子的 C++ 决定"具体怎么做"，网关决定"谁能调用哪个版本"，WorldEngine 决定"事情是否真的发生了"。任何角色都不能绕过世界回执宣布成功。**

| 关键对象 | AI 可以 | AI 不可以 | 最终权威 |
|---|---|---|---|
| 世界任务结果 | 解释运行轨迹，指出可观察事实 | 没有运行结果就声称完成，或改写世界事实 | 编译器、运行时、WorldEngine |
| 孩子的学习状态 | 给出带 Evidence 与置信度的推断候选 | 凭一次对话判定"已掌握" | LearnerProjector 与可回放规则 |
| 实时教学介入 | 选择角色、提示层级、表达方式 | 绕过教学策略直接给整题答案 | PedagogyPolicy 与安全规则 |
| 新关卡与新内容 | 分析数据、生成候选版本 | 未经审核直接上线 | 教研 / 关卡 / 叙事 / 质量保障流程 |

### 四端职责

```text
                    agent/
              合同 + 运行时库（Python）
                 │  不可变 Wire 合同 / Ports
                 ▼
          walnut-world-backend/   ◀── 唯一生产 HTTP Gateway
            唯一 PostgreSQL 写入 + Alembic 迁移权威
                 │  只读投影
        ┌────────┴────────┐
        ▼                 ▼
walnut-world-frontend/    miaoda-teacher-workbench/
   Godot 4.5.2 学生端          NestJS + React 教师工作台
```

| 目录 | 负责 | 技术栈 |
|---|---|---|
| `agent/` | 定义**怎么通信**：不可变 Wire 合同、Ports、provider-neutral Runtime / Build / Sandbox | Python |
| `walnut-world-backend/` | 定义**事实**：唯一 HTTP 网关、全部持久化、客观世界引擎、耐久 worker | Python 3.12 / FastAPI / SQLAlchemy / PostgreSQL |
| `walnut-world-frontend/` | 定义**学生体验**：只消费后端，**不在本地编译 C++** | Godot 4.5.2 |
| `miaoda-teacher-workbench/` | 定义**教师体验**：只读消费后端投影，不直接写业务数据 | NestJS + Vite + React |

### 运行拓扑

```text
postgres (private)
  → migrate: alembic upgrade head (一次性)
  → backend 127.0.0.1:8790 → :8000     ← 唯一对外监听
  → llm-relay :8081                     ← 私有；Provider key 持有者；不发布端口
  → docker-engine (私有 DinD daemon)
       → sandbox-image (按精确 digest 一次性拉取)
       → workflow-worker (Control + Build + Turn + terminal 交接)
  → learner-worker (耐久 learner / product 投影)
```

`workflow-worker` 在后端自有表与工作单元里闭合 Control、Build/Certification、Activation、精确版本 Turn 与 Run/World/Event/Evidence，再把 terminal hand-off 耐久交给 `learner-worker`（闭合 Learner、AgentInteraction、Workspace）。

**Provider 失败时**：保留已提交的客观 Run / World / Evidence，但**不发布** `provider_fallback` Interaction，**不推进** Learner。

### 一次 Turn 的生命周期

```
网关 accept → workflow job → _prepare → provider 派发 → Sandbox
   → 世界提交 → 终局角色（teaching / bug / book）→ 投影
```

每条耐久记录都交叉校验：Command / Job / Turn / Run / Evidence / World 快照 / Workspace checkpoint / 展示头。**权威行在数据库层是 append-only**（触发器强制），所以历史不可篡改——这也意味着修复只能落在校验逻辑上，不能靠改数据。

### 问叮当（Hint）为什么是真 Agent

`turn_worker.py` 的 `_execute_hint` 强制要求：

```python
if decision.source != "provider":  hint_mismatches.append("SOURCE")
if decision.degraded:              hint_mismatches.append("DEGRADED")
...
if hint_mismatches: raise WorkflowInvariantError(...)
```

只要不是模型真答的、或是降级答案，这个 Turn **直接失败**，而不是给一个假的。生产代码里没有任何 stub / offline LLM 实现，relay 配置也禁止回退到普通 `LlmPort`。每次调用都在 `HINT` 命名空间（ordinal 300）落**耐久回执**（真实请求 / 响应 wire）。

---

## 三、环境配置

### 前置条件

| 依赖 | 版本 | 说明 |
|---|---|---|
| Windows | 10/11 | 启动脚本为 PowerShell |
| **Windows PowerShell 5.1** | 内置 | ⚠️ 见下方「已知坑」 |
| Docker Desktop | 运行中 | PostgreSQL 与 C++ Sandbox 都跑在容器里 |
| Python | 3.12.x | 后端 `.venv` |
| Godot | 4.5.2 | 已放在 `tools/godot-4.5.2/` |
| Node.js | 18+ | 仅教师工作台需要 |
| LLM Provider Key | DeepSeek | 存成**文件**，不写进代码或环境变量 |

### Provider 密钥

存成一个纯文本文件，只放 key 本身：

```
C:\Users\<你>\.walnut-secrets\deepseek-v4-flash.key
```

然后指给启动脚本：

```powershell
$env:WALNUT_LLM_UPSTREAM_API_KEY_FILE = 'C:\Users\<你>\.walnut-secrets\deepseek-v4-flash.key'
```

> 私有 `llm-relay` 实现 `YAYA_RECOVERABLE_LLM_V1`：按稳定 `dispatch_id` 原子创建 / 重放，再用只读 GET 对账，结果至少保留 604800 秒。普通 `/v1/chat/completions` POST 因为没有客户端可寻址的耐久结果（响应丢失后无法区分"未执行"和"已成功"），**配置校验会直接拒绝**。

### 其余环境变量

`scripts/start-persistent-play.ps1` 会**自动生成**认证密钥、化名密钥、relay 密钥和数据库口令，本地体验只需上面那个 key 文件。

如果要手动组栈（或部署），完整变量清单见 [`walnut-world-backend/docs/operations/runbook.md`](walnut-world-backend/docs/operations/runbook.md)，至少需要：

```powershell
$env:POSTGRES_PASSWORD               = '<secret>'
$env:WALNUT_AUTH_HMAC_SECRET         = '<32+ 字符>'
$env:WALNUT_FEISHU_PSEUDONYM_SECRET  = '<32+ 字符，稳定不可变>'
$env:WALNUT_TENANT_ID                = '<tenant-id>'
$env:WALNUT_BUILD_IMAGE              = 'walnut/backend@sha256:<digest>'
$env:WALNUT_POSTGRES_IMAGE           = 'postgres:16.9-alpine@sha256:<digest>'
$env:WALNUT_SANDBOX_IMAGE            = 'gcc@sha256:<digest>'
$env:WALNUT_LLM_UPSTREAM_ENDPOINT    = 'https://api.deepseek.com/chat/completions'
# ... 见 runbook
```

所有镜像都必须是 **digest 锁定**的，不接受 tag。

> **`WALNUT_FEISHU_PSEUDONYM_SECRET` 必须长期稳定**：它决定学生在飞书表里的匿名学号。换了这把密钥，同一个孩子会变成另一个人，同步就会新建重复记录而不是更新。

### ⚠️ 已知坑

1. **必须用 Windows PowerShell 5.1 跑启动脚本**，不要用 PowerShell 7。
   pwsh 7 的 `Start-Process` 会把用 `SetEnvironmentVariable(…, $null)` 删掉的变量，以**空字符串**传给子进程；relay 读到空 key 会拒绝启动（`WALNUT_LLM_UPSTREAM_API_KEY must be a bounded non-whitespace secret`）。

2. **脚本文件要带 UTF-8 BOM**。5.1 读无 BOM 的 UTF-8 会按 ANSI 解码，含中文的路径会变成乱码。

3. **重新 seed 前要清空产物目录**。seed 要求 `artifacts/` 为空（`artifact root must be empty before the first Build`）；只删数据库不删 `%LOCALAPPDATA%\WalnutWorld\persistent-play\artifacts` 会失败。

---

## 四、快速开始

### 一条命令拉起全栈 + 游戏

```powershell
$env:WALNUT_LLM_UPSTREAM_API_KEY_FILE = 'C:\Users\<你>\.walnut-secrets\deepseek-v4-flash.key'
& '.\walnut-world-backend\scripts\start-persistent-play.ps1' -Action Start
```

它会依次：起 PostgreSQL 容器 → `alembic upgrade head` → seed 权威数据 → 独立校验七项权威 → 起 llm-relay / gateway / workflow-worker / learner-worker → 拉起 Godot 客户端。

成功时最后一行是：

```
PERSISTENT_PLAY_READY gateway=8790 relay=20999 token_lifetime=28800
```

其他动作：

```powershell
.\walnut-world-backend\scripts\start-persistent-play.ps1 -Action Status   # 查看状态
.\walnut-world-backend\scripts\start-persistent-play.ps1 -Action Stop     # 停止（保留数据卷）
```

### 怎么玩

1. 进入清泉试验田，芽芽发布《作物适配浇水器》任务
2. 打开**代码卷轴**，看到填空模板
3. 点**问叮当**求助——此时还没跑过代码，叮当只会启发式提问，不会直接给答案
4. 填空后点**运行**：保存草稿 → 构建 → 认证 → 激活 → 小核桃调用你的 Skill → 世界判定
5. 浇错了？再点问叮当——这次它拿着**真实的失败证据**讲，越卡越具体
6. 同类失败累计 4 次且看完 L3 后，才会出现「AI 修改提案」，且**必须你显式接受**，接受后仍要自己重新构建、激活、运行
7. 目标客观完成后，书书出成长总结

### 完全重来（清空存档）

```powershell
.\walnut-world-backend\scripts\start-persistent-play.ps1 -Action Stop
docker rm -f walnut-play-postgres; docker volume rm walnut-play-pgdata
Remove-Item -Recurse -Force "$env:LOCALAPPDATA\WalnutWorld\persistent-play\artifacts",
                            "$env:LOCALAPPDATA\WalnutWorld\persistent-play\build-workspaces",
                            "$env:LOCALAPPDATA\WalnutWorld\persistent-play\state.json"
```

然后重新 `-Action Start`。

> 关卡本身**可以反复重玩**：Run 的判分基线是「关卡初始态」而不是「当前世界」，所以浇过一次水不会让下一次判分失真。

### 同步学习数据到飞书

只读预演（不写任何东西）：

```powershell
cd walnut-world-backend
.\.venv\Scripts\python.exe .\scripts\sync_feishu_learning.py --tenant-id tenant_yaya --identity user
```

确认无误后真正写入：

```powershell
.\.venv\Scripts\python.exe .\scripts\sync_feishu_learning.py --tenant-id tenant_yaya --identity user --apply
```

需要 [`lark-cli`](https://www.npmjs.com/package/lark-cli) 已登录，且该账号对目标多维表和成长文档有编辑权限。

同步是**按业务键 upsert + 文档 append**：旧数据只会被更新或追加，**不会被删除**。默认资产配置是 `config/int3_feishu_assets.json`（指向「核桃世界｜学习洞察」）；旧账号那份保留在 `config/int3_feishu_assets.legacy.json`。

### 教师侧只读接口

唯一入口 `POST /integrations/feishu/v1/mcp`，无会话 Streamable HTTP JSON-RPC，只声明 MCP `2025-06-18`，只暴露三个工具：

- `query_learner_progress` — 查询学生学习进度
- `query_class_common_issues` — 查询班级共性问题
- `get_evidence_summary_and_links` — 查看证据摘要及档案 / Dashboard 链接

必须使用**短期只读教师 JWT**（actor type = teacher，仅含 `learner:read` / `class-insights:read` / `evidence:read`）。原始代码、聊天、凭据和直接身份信息**不进入**飞书聚合视图。

---

## 五、开发与测试

```powershell
# 前端（Godot，每个测试是独立脚本）
cd walnut-world-frontend
..\tools\godot-4.5.2\Godot_v4.5.2-stable_win64.exe --headless --path . --script res://tests/client/<name>_test.gd

# 后端单元测试
cd walnut-world-backend
.\.venv\Scripts\python.exe -m pytest tests/unit -q

# 后端集成测试（需要一个独立的 PostgreSQL，不要指向你的存档库）
$env:WALNUT_TEST_DATABASE_URL = 'postgresql+asyncpg://walnut:<pw>@127.0.0.1:<port>/walnut_test'
.\.venv\Scripts\python.exe -m pytest tests/integration -q

# Agent 合同与运行时
cd agent
$env:PYTHONPATH = 'python;..\walnut-world-backend\src'
..\walnut-world-backend\.venv\Scripts\python.exe -m pytest tests -q
```

**opt-in 开关**（默认不跑，避免误花 token / 误连外部服务）：

| 变量 | 作用 |
|---|---|
| `YAYA_REAL_GATEWAY_E2E=1` | 前端跑真实网关 E2E |
| `YAYA_LIVE_GENERATION_BUDGET` | Agent 跑真实 Provider E2E（**会真实消耗 token**） |
| `WALNUT_INT1_REAL_PROVIDER_E2E=true` | 后端真实 Provider 联调 |

集成测试请务必用**独立数据库**：这些用例带单调时间戳校验，在被污染或复用的库上会出现 `Product workspace update timestamp regressed` 之类的假失败。

**已知抖动**（与代码改动无关，已用「改动前提交」对照验证）：Agent 套件里若干并发 / 租约用例在负载高时会间歇失败，典型是 `session_activation_failure_matrix` 的 inflight 接管、`skill_invocation` 的 CAS 竞态、`outbox` 租约接管（只留 0.1 秒余量），以及 `docker_cpp_sandbox` 的"无残留容器"断言——后者会被机器上**其他容器**干扰，跑之前先清干净。判断某条失败是否真是回归，靠的是在改动前的提交上跑同一条用例做对照，而不是看它是否失败过。

### 合同校验

```powershell
$env:WALNUT_CONTRACT_PATH = (Resolve-Path .\agent).Path
py -3.12 walnut-world-backend\scripts\verify_contract_release.py --agent-repo $env:WALNUT_CONTRACT_PATH
```

逐字节核对 manifest（v0.6.0，147 个文件）、排序、hash 与历史 release 锁定。**任何字节漂移都必须停止部署**；重新生成 manifest 不能洗白旧 release 的漂移。

---

## 六、当前状态与边界

### 里程碑

| 里程碑 | 内容 | 状态 |
|---|---|---|
| INT1 | 耐久单一 Gateway 权威 + 学生全链 | 已交付验收 |
| INT2 | World 呈现 + 学生确认的 Skill Patch | 已交付（受控真实 Provider M2 PASS，run `868a`） |
| INT3 | 飞书教师只读 MCP + 教师工作台 | 已交付 |

**真实 Provider 证据**：run `868a` 用时 301.012 秒，DeepSeek V4 Flash 结果为 `source=provider`、`degraded=false`；经历 4 次客观失败后学生显式请求并接受 Patch，再手动 Build / Activate / Run，随后以只读方式恢复同一权威指纹。

### 明确未证明 / 排除项

这一节故意写得直白，不把未验证的东西说成已完成：

| 项目 | 事实 |
|---|---|
| 合同 v0.6 release identity | **NOT_PROVEN** — 147 entries 的 candidate 已就位，但 tag 尚未发布 |
| 公开 Gateway pending write response-loss | **NOT_PROVEN** — 私有 Provider relay 的恢复已验证，公开链路未做故障注入验收 |
| production private DinD | 未证明 |
| 飞书生产接入 | 接口合同与角色边界已设计，当前只做只读脱敏教师查询；业务写路径排除 |
| 儿童真实用户验证 | 当前证据以工程回归和内部评审为主，尚无课堂试点数据 |
| WSS / Client Event Batch / 自动多文件 Patch | 默认排除 |

### 关键版本

- 合同：v0.6.0（147 文件）
- 数据库 migration head：`019_int2_skill_patch_authority`
- Godot：4.5.2　|　Python：3.12.13　|　PostgreSQL：16.9

---

## 七、目录结构与阅读顺序

| 目录 | 说明 |
|---|---|
| `agent/` | 合同（`contracts/manifest.json`）、Python 运行时、五份顶层设计文档 |
| `walnut-world-backend/` | API、Alembic 迁移、worker、运维脚本与手册 |
| `walnut-world-frontend/` | Godot 场景、autoload、客户端测试 |
| `miaoda-teacher-workbench/` | 教师工作台 server / client / shared |
| `tools/` | 本地工具运行时（Godot、Node），不纳入版本控制 |

新协作者建议按顺序读：

1. **接口合同权威** — `agent/contracts/manifest.json` 及其引用的 OpenAPI / AsyncAPI / JSON Schema
2. **五份顶层设计** — `agent/01_~05_核桃代码世界_*.md`（系统架构 / 前端 / 后端 / Agent / 联调规范）
3. **运行与部署手册** — `walnut-world-backend/docs/operations/runbook.md`
4. **数据库迁移权威** — `walnut-world-backend/migrations/`
5. **验证证据账本** — `agent/docs/INT2_CROSS_REPO_VALIDATION_REPORT.md`、`walnut-world-backend/docs/operations/int3-status-ledger.md`

---

## 团队

| 成员 | 分工 |
|---|---|
| 孙浩淞 | 总体架构、Agent Runtime、跨仓合同与联调 |
| 叶明敏 | 产品与关卡设计、AI 分层教学流程、原型 |
| 刘沛权 | 世界观、美术与前端实现、演示内容 |
