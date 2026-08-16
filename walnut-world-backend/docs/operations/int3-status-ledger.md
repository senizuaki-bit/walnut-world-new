# INT3 当前状态、错误标记与后续执行台账

更新时间：2026-08-16（Asia/Shanghai）

这份台账记录“当前真实状态”，不把代码通过、Mock、历史 Run、Aily 技能卡片或降级回答写成真实端到端通过。原 Godot 前端与 sibling Agent 保持冻结；旧账号飞书资产完整保留，目标账号采用独立副本。真实 DeepSeek + Godot 主链已在 `181.879s` 内 PASS，并同步到目标 Base、Dashboard、统一成长档案和妙搭；当前唯一未闭合的展示尾链是公网 HTTPS MCP 与 Aily 三次真实问答。

## 状态标记

- `✅ 已完成`：已有当前状态的直接证据。
- `🟨 已实现待真实验收`：代码或资产已存在，但真实学习链尚未跑通。
- `⛔ 外部阻塞 / 未完成`：必须有用户授权、真实 Provider 或平台能力后才能继续。
- `🛠 已修复错误`：曾经会造成错误结论、串档、重复写或验收失败，现已修复并有回归。
- `⚠️ 操作注意`：不是产品失败，但错误命令或环境假设会误导执行。
- `🔐 禁止落盘`：密钥、JWT、Provider Key、OAuth device code、验证码或直接身份信息。

## 一、当前交付矩阵

| 项目 | 状态 | 当前证据 | 不能声称的内容 |
|---|---|---|---|
| Backend 三个只读 API + MCP | 🟨 本地 production 全验，待公网接 Aily | 三路由与三个 MCP 工具读取同一 PostgreSQL 权威数据；本地只暴露三个只读工具，错角色返回 `AUTHORIZATION_DENIED`、跨租户返回 `NOT_FOUND` | 尚无可用公网 HTTPS endpoint，Aily 未做真实工具问答 |
| PostgreSQL 权威闭合 | ✅ 已完成 | 真实主链产生 4 Run（3 失败、1 成功）、11 Evidence、4 projection、Profile revision 4、13 次 Provider generation | 不把首次失败链或测试数据混入当前结果 |
| Base 结构 | ✅ 真实非零验收完成 | 目标三表为 `1 / 4 / 9`，稳定业务键唯一，Evidence 与同 Run 可追溯 | 不含原始代码、聊天、凭据或直接身份信息 |
| 幂等同步器 | ✅ 两次真实重放完成 | 首次写成 `1 / 4 / 9` 并创建一个当日区块；第二次新增记录、文档、区块均为 0 | 不将更新计数误写成重复新增 |
| Dashboard | ✅ 非零指标已回读 | 今日活跃 1、完成率 0.25、平均尝试 2.5、高频错误 3、关注学生 1；其余 AI/Patch、阶段与 7 日趋势组件均有真实数据 | 不以静态配置替代 `get-data` 回读 |
| 成长档案母版 | ✅ 母版与真实子档案完成 | 旧账号 v1 母版保留；目标母版生成唯一学生子档案，固定 11 栏与当日区块已回读 | 不把母版误计为第二份学生档案 |
| 学生—文档绑定 | ✅ 真实子文档验收完成 | 唯一子档案通过 ownership HMAC；`template_version=v1`，仅一个 `2026-08-16` 日区块，固定 11 栏含 4 Run 与 9 条脱敏 Evidence 链接 | 母版与子档案不混计 |
| 妙搭教师工作台 | ✅ 发布态真实数据已回读 | 最新 release 为 `finished`；线上三业务表 `1 / 4 / 9`，配置链接与 Base/Dashboard/母版一致 | 不把估算行数替代实际 SELECT 计数 |
| 单一 Aily 教师助手 | 🟨 三个自定义技能已安装、权限已部分收紧，MCP 未接 | task `7674363221256211415` 已完成；三个准确名称的自定义技能均自动审核、安装并独立回读各 1 项；203 个权限条目进入限制且最终回读 `aria-checked=true` | 四个内置技能因平台控件禁用仍存在；MCP 未接公网、未配置到 Aily，不能声称三个技能已取得真实 Backend 事实 |
| 5 分钟真实 E2E | 🟨 Godot→Backend→飞书主链 PASS | 真实执行 `181.879s`；PostgreSQL、Base、Dashboard、成长档案、妙搭同源闭合且重复同步幂等 | Aily 三次真实问答仍待公网 MCP，故整条七项验收尚未最终标绿 |
| 新账号复制迁移 | ✅ 数据资产迁移完成 | 目标账号 Base、三表、Dashboard、母版、唯一子档案、妙搭与 Aily 三技能均存在；旧账号资产保留 | 仅 Aily 的公网工具连接与真问答未完成 |

## 二、已创建的真实飞书资产

### 新账号资产（当前迁移目标）

- CLI 状态：新账号已完成认证；旧账号资产保持原状。新账号对旧账号资产的跨账号读取不可用，按权限边界处理，不以删除、转移或放宽权限规避。
- Base：[核桃世界｜学习洞察](https://larkcommunity.feishu.cn/base/Q6V9biulZaezaHskZqacYm2JnHe)
- Base token：`Q6V9biulZaezaHskZqacYm2JnHe`
- 学生档案：`tblqvSmGCoSQPbz5`
- 每日学习记录：`tblCjoEvjxVvVgBs`
- 学习证据摘要：`tbl1GKtqWcNb67xM`
- Dashboard：[班级学习看板](https://larkcommunity.feishu.cn/base/Q6V9biulZaezaHskZqacYm2JnHe?table=blkK3ldZA0pePRGb)
- Dashboard ID：`blkK3ldZA0pePRGb`
- 成长档案母版：[儿童学习成长档案 v1](https://larkcommunity.feishu.cn/docx/CHZ9dln03o9wkQxF4DTccWPgnOd)
- 母版 token：`CHZ9dln03o9wkQxF4DTccWPgnOd`
- 妙搭 app ID：`app_17c6bc5hz7d`
- 妙搭应用：[核桃世界｜教师学习数据中心](https://dcniaqwtmoca.feishuapp.com/app/app_17c6bc5hz7d)
- 新妙搭线上库：已迁移 40 个初始 schema changes、4 个认证用户只读策略，并写入 4 行配置 seed。
- 新妙搭 release：`7674328927129062596`；commit `b01db82cbe261c3acab889f621d605769cf41b0b`；状态 `finished`，无错误日志。
- 发布回读：班级概览、学生列表、每日记录可访问；Base、Dashboard、母版链接可点击；线上三业务表实际为 `1 / 4 / 9`，Evidence 深链与同 Run 对齐。
- 目标 Base 当前为 `1 / 4 / 9`；唯一学生子档案含一个 `2026-08-16` 日区块，固定 11 栏与 ownership HMAC 均独立验收通过。
- 访问范围：已向 `Range` 成功提交当前目标账号用户；当前 CLI 的 GET 仅回传 `scope / require_login / apply_config`，不回传 target 名单，因此名单持久化缺少独立 readback，但当前账号已实测可打开线上应用。

### 旧账号 Base 与 Dashboard（保留，不删除）

- Base：[核桃世界｜学习洞察](https://hcn3j6gp4127.feishu.cn/base/XAcGbyppoaxEDMs8q6dclHoYn0f)
- Base token：`XAcGbyppoaxEDMs8q6dclHoYn0f`
- 学生档案：`tblbYveoeSjbGWt9`
- 每日学习记录：`tbl80mytH7SIdrvP`
- 学习证据摘要：`tblisP7Rivj056Lt`
- Dashboard：[班级学习看板](https://hcn3j6gp4127.feishu.cn/base/XAcGbyppoaxEDMs8q6dclHoYn0f?table=blkTg5qKiB8id2O6)
- Dashboard ID：`blkTg5qKiB8id2O6`
- 只读复核：11 个组件；三张表当前均为 0 条记录。

### 旧账号成长档案母版（保留，不删除）

- 母版：[儿童学习成长档案 v1](https://hcn3j6gp4127.feishu.cn/docx/OEkcdFpgmoU1rzxQK7actaawnbf)
- token：`OEkcdFpgmoU1rzxQK7actaawnbf`
- revision：3
- 固定栏目：基本信息、日期、今日任务、完成结果、尝试次数、主要错误、AI辅助程度、知识点阶段变化、今日进步、下一步建议、Evidence链接。

### 旧账号妙搭（保留，不删除）

- 应用：[核桃世界｜教师学习数据中心](https://hcn3j6gp4127.feishuapp.com/app/app_17c63e4z6px)
- app ID：`app_17c63e4z6px`
- release ID：`7674288197869899041`
- commit：`ac3ce80371ca1f070bb72cacdc97fb0a25d29cd1`
- 发布状态：`finished`
- 访问策略：`require_login=true`、范围 `Range`、申请审批开启。

### 新账号 Aily（当前迁移目标）

- 助手：[核桃世界｜教师学习助手](https://aily.feishu.cn/agents/agent_4kur1swqbbr6xgw/detail)
- agent ID：`agent_4kur1swqbbr6xgw`
- 已完成：名称、描述、`IDENTITY.md`、`SOUL.md`；明确事实只来自三个 Backend 只读工具，并固定“客观事实 / AI推断 / 教学建议”结构。
- Aily task：`7674363221256211415` 于本轮 `04:53` 完成。
- 三个自定义技能已创建、自动审核通过并安装；在 Skills 页分别独立搜索回读，每个准确名称均只出现 1 项：`查询学生学习进度`、`查询班级共性问题`、`查看证据摘要及档案链接`。
- 四个内置技能仍存在：`飞书卡片生成`、`用户工作画像`、`技能调试优化`、`AI生成技能`。其“移除/自动调用”控件回读为 `cursor-not-allowed / disabled`，属于当前平台限制；不能宣称已移除，也不通过非正常手段绕过平台控件。
- 安全授权模式已从“默认允许”切换为“部分限制”；最终回读 `aria-checked=true`。共 203 个权限条目加入限制，覆盖 `write / write_only / create / delete / update / send / copy / upload / manage / edit / publish / move`，并复核中文宽权限项。
- 未完成：Backend MCP 仍无可用公网 HTTPS endpoint，尚未配置到 Aily。三个自定义技能资产存在不等于已经接通真实 Backend 工具，也不能据此生成或声称真实学生事实。

### 旧账号 Aily（保留，不删除）

- 助手：[核桃世界｜教师学习助手](https://aily.feishu.cn/agents/agent_4kupymjnavr5f/detail?settings=profile)
- agent ID：`agent_4kupymjnavr5f`
- 查询学生学习进度：`skill_4kuq3wrdjarfa`
- 查询班级共性问题：`skill_4kuq4jkpvhkjj`
- 查看证据摘要及档案链接：`skill_4kuq4x6sm460p`
- 当前状态：用户最新截图证明三个 v1.0.1 技能均已审核通过并上架；它们仍属于旧账号，技能 ID、可见范围、审核结果和运行凭据均不能跨账号继承。

## 三、Backend 与本地代码

核心实现：

- `src/walnut_backend/api/routes/feishu_learning.py`
- `src/walnut_backend/api/routes/feishu_mcp.py`
- `src/walnut_backend/application/feishu/learning_queries.py`
- `src/walnut_backend/adapters/postgres/feishu_learning.py`
- `src/walnut_backend/application/feishu/learning_sync.py`
- `src/walnut_backend/adapters/lark_cli/feishu_learning.py`
- `scripts/sync_feishu_learning.py`
- `config/int3_feishu_assets.json`
- `config/int3_feishu_assets.target.json`
- `docs/operations/int3-feishu-demo.md`

冻结情况：

- `walnut-world-frontend`：工作树 clean，未修改。
- sibling `agent`：工作树 clean，未修改。
- 妙搭本地仓：已发布版本对应工作树 clean。

当前 PostgreSQL：

- 容器：`walnut-int3-postgres`
- 监听：仅 `127.0.0.1:55432`
- 主库：`walnut_int3`
- migration head：`019_int2_skill_patch_authority`
- 首次长路径失败链已完成全库备份；随后仅在一个事务内临时关闭 3 个具名用户触发器并按精确主键回滚，事务已提交，审计、前置权威行、触发器状态与数据库指纹均独立 PASS。
- 当前真实学习结果：4 Run（3 失败、1 成功）、11 Evidence、4 projection、Profile revision 4、13 次 Provider generation；不是 Mock 或手工插入。
- 目标飞书已连续同步两次：首次生成 Base `1 / 4 / 9` 与一个当日档案区块，第二次新增记录、文档、区块均为 0。

已验证门禁：

- E32 教师入参修复后的当前官方 Backend 全量门禁：`PASS`；以 Windows PowerShell 5 原样运行 `scripts/verify_all.ps1`，连接隔离库 `walnut_int3_feishu_test`，自然退出码为 0。
- Agent contract release：147 个 byte-pinned wire contracts 通过。
- Alembic：通过。
- Ruff：通过。
- Pyright：0 errors / 0 warnings / 0 informations。
- `compileall`：通过。
- Pytest：580 passed、0 skipped、3 warnings，用时 963.40 秒。
- 此结果来自修正后的完整命令；早先遗漏 `+asyncpg` 的数据库 URL 和 PowerShell 7 原生参数问题均属于操作错误，不作为门禁失败结论。
- 三条 warning 仍是两条 Alembic `path_separator` deprecation 与一条既有 pytest `record_property` / xunit2 兼容性提示；没有 skip 或测试失败。
- INT3 定向 Unit / Contract / Transport / PostgreSQL 回归已包含在上述 580 项全量结果中。
- 官方当前全门禁为 Agent contract 147、Alembic、Ruff、Pyright、compileall 与 Pytest 580 项全部 PASS。
- 进程级本地 MCP 冒烟：当前 Gateway 连接主权威库并在 `127.0.0.1:8790` 启动成功；标准 MCP 请求不携带 Walnut 私有追踪头时，`initialize` 协商 `2025-06-18`，`tools/list` 只返回 3 个工具，三者 `readOnlyHint=true`。
- 真实数据本地 MCP 验收：三个工具均返回 PostgreSQL 同源事实与档案/Dashboard链接；错角色为 `AUTHORIZATION_DENIED`，跨租户为 `NOT_FOUND`，查询前后学生业务写表不变。
- 本轮 production 认证核验：Backend 固定 `tenant_yaya`、teacher actor 与恰好 `learner:read / class-insights:read / evidence:read` 三个 scope；教师 JWT 最大寿命 900 秒，Authorization 由当前 Windows 用户 DPAPI 保护。本地 edge 固定监听 `127.0.0.1:18792`，以高熵 capability path 接入，在服务端注入短期 Authorization，并在凭据到期时 fail closed；`initialize`、`tools/list` 和三个工具调用均 PASS。
- 真实 E2E 前补启 `127.0.0.1:8790` production Backend；无 token 请求返回 `401`、教师 JWT MCP 返回 `200`、学生 JWT bootstrap 返回 `200`，证明本次不是以 development auth 绕过认证。
- 妙搭 server/client typecheck：通过；ESLint：通过；最终 production build 退出码为 0；线上发布与三页面 readback 通过。

本轮 Provider 与临时公网入口核验：

- 仅依据 DeepSeek 官方接口定义确认模型标识 `deepseek-v4-flash`，并已完成一次真实最小 Provider 调用，结果 PASS。Provider Key 原文不写入本台账、仓库、URL 或命令输出。
- 已安装并核验 `cloudflared 2026.8.2`，Windows 数字签名状态有效。
- Cloudflare 临时隧道最近两次入口申请均返回 `429`；当前没有公网入口，也没有把失败地址写入 Aily。

本轮真实 Godot E2E：

- 首次长路径失败被如实记录；备份与精确事务回滚完成后，以短 runtime 路径重跑。
- 真实 DeepSeek relay + Godot phase1 在 `181.879s` 内 PASS，产生 4 Run（3 失败、1 成功）、11 Evidence、4 projection、Profile revision 4 与 13 次 Provider generation。
- Frontend 与 sibling Agent 未修改，Provider Key 仅从受控本地文件注入且未进入台账、日志或飞书资产。
- Cloudflare 临时隧道两次均返回 `429`，没有形成公网入口，也没有把失败地址配置到 Aily。

## 四、错误与修复记录

### E01：最初 Backend 没有挂载飞书三路由

- 标记：`🛠 已修复错误`
- 影响：合同存在，但 API 实际不可调用。
- 修复：新增专用只读 route / application / PostgreSQL adapter，并在 app factory 挂载。

### E02：曾把 class ratio 的非整数值误判为不可序列化并抑制

- 标记：`🛠 已修复错误`
- 影响：真实班级比例会被错误隐藏。
- 根因：把未声明 canonical JSON 的冻结 class schema 错套到拒绝浮点的内部合同。
- 修复：该路由经冻结 schema 校验后使用有限标准 JSON；非抑制单元返回真实 `count / cohort`，小样本仍双 null。

### E03：最初试图复用学生本人 Run/Evidence 读取约束

- 标记：`🛠 已修复错误`
- 影响：教师 actor 与学生 actor 不同，直接复用会错误拒绝或诱发放宽授权。
- 修复：建立 teacher-only SELECT store，先 tenant，再 HMAC learner ref，再角色/用途/审计；没有放宽原学生接口。

### E04：首版权威闭合不足

- 标记：`🛠 已修复错误（高优先级）`
- 曾缺失：Profile 内外身份、完整 Skill provenance、Content task、source Event、commit receipt、derived Learner Evidence、Run Evidence graph。
- 风险：篡改过的投影字段可能进入 Base、文档或妙搭。
- 修复：复用 writer validator 并回查所有权威行；task 严格只允许 `task_id / concept / task_sha256`，额外 `task_name`、原始代码或聊天字段 fail closed。

### E05：伪名密钥设为必填后，Compose 没有传给 Backend

- 标记：`🛠 已修复错误`
- 影响：标准 Compose 在应用创建阶段失败。
- 修复：只向 Backend 注入 `WALNUT_FEISHU_PSEUDONYM_SECRET`；缺失时 compose config fail closed；不向 worker/relay 扩散。

### E06：每日记录搜索投影字段不完整

- 标记：`🛠 已修复错误`
- 影响：第二次同步虽然不产生重复记录，却会重复走 `PENDING -> block_replace -> APPENDED`，不是真正 no-op。
- 修复：真实 lark-cli 搜索返回全部 15 个成长事实字段；重复同步零 daily write、零 staging、零 block replace。

### E07：非法 JSON / Schema 请求最初绕过访问审计

- 标记：`🛠 已修复错误`
- 修复：使用已认证 transport context 写 `FAILED` audit，resource 固定为 `invalid`，不采信非法 body 中的身份；审计失败返回锁定错误合同。

### E08：成长档案最初没有绑定到唯一学生

- 标记：`🛠 已修复错误（高优先级）`
- 风险：若两名学生在 Base 中的文档 URL 被互换，可能向错误孩子的文档追加或替换。
- 修复：每份子文档写入匿名、域分离 HMAC 所有权封印；绑定 learner ref、fsp key、tenant fingerprint、token、URL；创建后及每次 mutation 前回读验证；只允许母版同一 HTTPS 飞书 origin。

### E09：最近 7 天曾按滚动 168 小时计算

- 标记：`🛠 已修复错误`
- 影响：不符合教师看板“自然日”直觉，跨午夜边界会错一天。
- 修复：按 `Asia/Shanghai` 的今天及前 6 个自然日计算，并有跨午夜回归。

### E10：同步脚本曾依赖外部 `PYTHONPATH`，演示手册还用了本机不存在的 `py -3.12`

- 标记：`🛠 已修复错误`
- 影响：干净 PowerShell 中直接 `ModuleNotFoundError` 或找不到 Python launcher。
- 修复：脚本自行加入仓库 `src`；手册统一使用 `.\.venv\Scripts\python.exe`；无 `PYTHONPATH` 启动回归通过。

### E11：一次 PostgreSQL 回归使用了错误角色 `postgres`

- 标记：`⚠️ 操作错误，非产品失败`
- 现象：3 个数据库认证失败。
- 正确连接：测试库使用 `postgresql://walnut@127.0.0.1:55432/walnut_int3_feishu_test`；更正后相关测试全部通过。

### E12：曾错误调用 `.venv\python -m ruff`

- 标记：`⚠️ 操作错误，非代码失败`
- 原因：venv 有 Python 包入口但没有 Ruff native binary。
- 正确命令：`uvx --offline --from ruff==0.15.22 ruff check ...`。

### E13：官方门禁脚本的 uvx 参数写法在 PowerShell 7 下不兼容

- 标记：`🛠 已修复错误`
- 现象：PowerShell 7 把逗号/引号作为原生参数传给 uvx；Windows PowerShell 5.1 可运行。
- 修复：`Invoke-UvxChecked` 先构造显式 argv 数组再 splat，兼容两种 PowerShell。
- 更正结果：修正后的官方完整门禁为 PASS；该次错误命令不再计作产品或门禁失败。

### E14：目标账号登录第一次把多个 domain 写成一个参数

- 标记：`⚠️ 操作错误，未改变账号状态`
- 现象：CLI 返回 `unknown domain`。
- 正确写法：分别重复 `--domain base --domain docs --domain drive --domain apps`。
- 后续又生成过一次临时 device flow，但用户尚未扫码；该请求应自然过期，禁止记录或复用 device code，迁移时重新生成。

### E15：Aily 技能早期网络实现允许弱 TLS / 重定向语义

- 标记：`🛠 已修复错误`
- 修复：v1.0.1 移除 TLS bypass，固定 HTTPS host、禁止 redirect、限制超时/响应大小/键格式/事实白名单，并保持 GET-only 与 fail closed。

### E16：完整 migration 回归曾漏写 SQLAlchemy asyncpg driver

- 标记：`⚠️ 操作错误，非产品失败`
- 错误 URL：`postgresql://walnut@127.0.0.1:55432/walnut_int3_feishu_test`。
- 现象：INT2 migration scratch fixture 按原 URL 尝试导入未安装的 `psycopg2`，连续产生 setup `E`；并非 migration 断言失败。
- 完整门禁正确 URL：`postgresql+asyncpg://walnut@127.0.0.1:55432/walnut_int3_feishu_test`。
- 注意：部分 Backend helper 会自动补 asyncpg，所以较窄的定向测试使用前一个 URL 也能通过；官方全量门禁不能依赖这种隐式转换。
- 更正结果：使用显式 `+asyncpg` 后 Alembic 与全量 Pytest 均通过；官方最终结果为 537 passed、0 skipped、3 warnings。

### E17：Windows 直接执行 `npm run build` 无法运行 Bash 脚本

- 标记：`🛠 已修复操作错误，非产品失败`
- 现象：项目 `build` 脚本入口为 `./scripts/build.sh`，Windows 默认命令执行环境不能直接运行 Bash 脚本。
- 修复：在可执行 Bash 的环境中运行项目构建链；最终 production build 退出码为 0。

### E18：裸用 `npx fullstack-cli` 命中了错误的交互式包

- 标记：`🛠 已修复操作错误，非妙搭失败`
- 现象：未限定作用域的包名启动了错误的交互式 CLI，不能作为妙搭 full-stack 发布工具。
- 修复：改用项目声明的 `@lark-apaas/fullstack-cli`，不再使用裸包名。

### E19：Windows `cmd` 不兼容内联 `NODE_ENV=...`

- 标记：`🛠 已修复操作错误，非代码失败`
- 现象：`NODE_ENV=production ...` 是 POSIX shell 语法，在 Windows `cmd` 中不能直接执行。
- 修复：在兼容该语法的构建环境中执行 server/client production build；最终退出码为 0。

### E20：production bundle 曾缺少 `@vercel/nft`

- 标记：`🛠 已修复依赖错误`
- 现象：构建链在打包服务端依赖时找不到 `@vercel/nft`。
- 修复：补齐该开发依赖并重新安装后，最终 build 退出码为 0。

### E21：用 `du` 扫描工作目录曾长时间挂起

- 标记：`🛠 已修复操作错误，非产品失败`
- 影响：阻塞排查，但没有改变代码、线上库或飞书资产。
- 修复：停止依赖全目录 `du`，改用有边界的文件与构建产物检查；发布流程得以继续。

### E22：CLI init 曾报 `spawnSync npm ENOENT`

- 标记：`🛠 已修复环境错误`
- 现象：init 子进程在当时的执行环境中无法解析 `npm`。
- 修复：改用能够解析 npm 的正确 CLI/执行环境，完成初始化与后续构建；最终 build 退出码为 0。

### E23：新妙搭发布链最终构建闭合

- 标记：`🛠 已修复并发布`
- 汇总：E17—E22 均已排除；production build 最终退出码 0，线上库迁移 40 changes、配置 seed 4 rows 均已完成。
- 结果：release `7674328927129062596` 已 `finished`，commit `b01db82`，线上 URL 与最小页面均已回读。

### E24：只删写策略时误删了认证用户 SELECT 策略

- 标记：`🛠 已修复数据可见性错误`
- 现象：妙搭页面能打开，但配置链接与业务投影均表现为空集合；线上配置表实际已有 4 行。
- 根因：`int3_projection_read_only.sql` 删除了写策略时也删除了“查看全部数据”策略，RLS 对认证用户静默过滤所有行。
- 修复：四张表仅恢复 `FOR SELECT TO authenticated USING (true)` 的“教师只读”策略；仍无认证用户 INSERT/UPDATE/DELETE 策略，Controller 继续要求 `walnut_teacher`，应用访问范围只含当前目标账号用户。
- 验证：发布页重新回读到 Base、Dashboard、母版链接；学生与记录仍为真实 0 行空状态。

### E25：曾尝试直接在 online 环境执行策略 DDL

- 标记：`⚠️ 操作错误，未应用任何线上语句`
- 现象：妙搭以 `forbid ddl/dcl operation in online env` 拒绝，明确返回 `No statements were applied`。
- 正确路径：先在 dev 应用策略，执行 `db-env-diff` 只出现 4 个 `CREATE POLICY`，再用 `db-env-migrate --yes` 发布到 online；最终 4 changes 已迁移。

### E26：权限审计曾把缺失字段误读成空数组

- 标记：`⚠️ 审计结论错误，已更正`
- 错误结论：把 `access-scope-get` 未返回的 `.data.users` 经 jq `length` 得到的 0 解释为“显式用户列表为空”。
- 真实证据：响应键只有 `apply_config / require_login / scope`，`users / departments / chats / targets` 均为字段不存在，长度应记为 `null`；它既不能证明为空，也不能回读证明 target 已持久化。
- 更正表述：`access-scope-set` 的请求体含 1 个目标用户且执行返回成功，当前账号也已实测打开应用；但 CLI GET 存在 target-list 可观测性缺口。

### E27：首版 MCP 错误要求 Aily 不会发送的四个 Walnut 私有头

- 标记：`🛠 已修复客户端兼容错误`
- 影响：标准 Aily MCP 请求不会发送 `X-Request-Id`、`X-Trace-Id`、`X-Correlation-Id`、`X-Schema-Version`；沿用全站强制头会在进入三个教师工具前被 transport 拒绝。
- 修复：仅对精确路径 `POST /integrations/feishu/v1/mcp` 自动生成前三个 attempt identity，并在缺失时使用 schema `1.0.0`；显式发送的非法值仍拒绝，其他 HTTP/WS 路径的原合同完全不变。

### E28：早期 MCP 协议声明与实现不闭合

- 标记：`🛠 已修复协议合规错误`
- 问题：早期实现曾宣称兼容旧协议版本却没有闭合相应批处理语义，同时缺少 `ping`、接受 JSON-RPC `null` id，并为 notification 返回 JSON-RPC 响应体。
- 修复：只声明 MCP `2025-06-18`，不再错误宣称旧版批处理；实现 `ping`，拒绝 `null` id，notification 返回无 JSON-RPC body 的 HTTP `202`。`initialize`、`notifications/initialized`、`tools/list`、`tools/call` 与协议版本头均有明确边界。

### E29：环境与公网隧道只读预检的首条 PowerShell 命令解析失败

- 标记：`⚠️ 操作错误，未执行检查且无任何写入`
- 现象：首条命令在 `foreach (...) { ... }` 后直接接管道，PowerShell 在解析阶段即抛出 `ParserError`；该次失败没有产生任何环境检查结果，也没有修改环境或文件。
- 修复：改用显式数组收集结果后再输出，完成只读预检。非敏感结论仅为：`WALNUT_FEISHU_PSEUDONYM_SECRET` 只存在于 User scope；两条 `WALNUT_FEISHU_MCP_*_URL` 与全部 Provider 环境变量均未设置；`cloudflared`、`ngrok`、`caddy`、`tailscale` 均未安装。
- 约束：不记录伪名密钥值，只记录存在范围；上述工具缺失只证明当前机器没有现成隧道入口，不能据此声称公网 MCP 已部署或不可部署。

### E30：旧账号 SkillHub 审核状态记录过期

- 标记：`⚠️ 状态表述错误，已按用户截图更正`
- 旧记录仍写三个 v1.0.1 技能“等待企业管理员审核”，但用户最新截图明确显示三项均“技能已上架”。
- 更正：旧账号三个技能已审核通过并上架；这不改变跨账号隔离事实，新账号仍不能继承其技能 ID、可见范围、审核或凭据。

### E31：Aily 入参审计首次查询使用了不存在的 Learner Profile 列

- 标记：`⚠️ 只读查询错误，无写入`
- 错误命令假定 `learner_profiles` 有 `content_unit_id/content_version` 两列，PostgreSQL 返回 `column does not exist`，未执行任何业务或审计写入。
- 修复：先从 `information_schema.columns` 回读真实结构；内容单元、版本与哈希位于已校验的 `profile_json.content`（行上另有 `content_hash`），后续只按该权威结构读取。

### E32：首版 MCP 工具要求教师提供不可见的权威上下文

- 标记：`🛠 阻断级可用性错误，已修复并完成定向回归`
- 问题：学生工具强制要求完整 `content_ref`（含 64 位 hash），班级工具还强制要求 HMAC `class_ref` 与时间区间；教师界面只展示匿名学生代号，Aily 又没有第 4 个发现工具，因此无法从“刚才这位学生”可靠生成这些值。
- 修复：保持恰好三个工具；学生工具只必填界面可见的 `learner_ref`，班级工具可零参数调用。Backend 在已认证 tenant 内从 SHA 校验后的唯一权威 Profile 解析缺省 content，班级引用只由 tenant 派生，默认窗口按 Asia/Shanghai 今日及前 6 个自然日。0 个候选返回 NOT_FOUND，多个候选要求显式 content 消歧；显式值仍做原 authority 校验，禁止模型猜 hash/HMAC。解析的 ALLOWED/DENIED/FAILED 均写 append-only audit，审计失败不释放结果。
- 验证：定向 65 passed，覆盖最小入参、显式消歧、零/多内容、默认窗口、学生 pre-read denial、跨租户和真实 PostgreSQL endpoint；随后当前树官方全量门禁 580 passed、0 skipped。

### E33：真实 PostgreSQL 新测试曾尝试删除 append-only Audit

- 标记：`⚠️ 测试清理错误，数据库保护生效且无数据损害`
- 首次测试的业务/MCP断言均已通过，但 cleanup 尝试 `DELETE FROM audit_records`，PostgreSQL append-only trigger 正确拒绝并返回 `audit_records are append-only`。
- 该 DELETE 与临时 Profile DELETE 位于同一事务，异常使事务整体回滚，没有删除或破坏审计/业务数据；遗留的 UUID 隔离临时 Profile 后续仅按两个精确 tenant ID 删除成功，审计记录按设计保留。
- 修复：测试 cleanup 不再删除 Audit，只清理精确临时 Profile；真实 PostgreSQL 两项重跑通过，完整定向组 65 passed。

### E34：E32 手册合并补丁首次上下文不匹配

- 标记：`⚠️ 文档补丁校验错误，无文件改动`
- 首次尝试同时更新 runbook 与演示手册时，演示手册已有措辞与补丁预期不一致；`apply_patch` 在校验阶段整体失败，没有部分写入。
- 修复：先回读当前原文，再拆为两次精确补丁；现已写明学生工具只需匿名 `learner_ref`、班级工具可零参数、content/class 由 Backend 权威解析、Evidence ID 必须沿用学生查询结果。

### E35：production Backend 子进程首次缺少仓库 `PYTHONPATH`

- 标记：`🛠 已修复启动错误，无公网暴露或数据库写入`
- 现象：首次启动的 Backend child 无法 import `walnut_backend`，uvicorn 在监听前退出；没有生成 active runtime、没有端口监听，也没有业务或审计写入。
- 修复：启动器只为 Backend child 补入仓库 `src` 的 `PYTHONPATH`，并为启动探测加入短连接超时；后续 production Backend 成功监听并通过本地核验。

### E36：PowerShell 自动日期转换造成 PID 启动时间误判

- 标记：`🛠 已修复运行时身份校验错误`
- 现象：PowerShell 7 的 `ConvertFrom-Json` 自动把状态中的日期字符串转换为日期对象，使 PID 启动时间比较发生错误判定；不是 Backend 认证失败。
- 修复：在 PowerShell 7 路径使用 `ConvertFrom-Json -DateKind String` 保留权威字符串后再显式解析，运行时 PID 校验恢复正确。

### E37：Aily MCP 表单没有自定义 Header，而首版 edge 依赖客户端 Authorization

- 标记：`🛠 已修复阻断级安全兼容错误，修复前未上公网`
- 问题：新账号 Aily 的 MCP 表单无法配置所需 Header；首版 edge 又只转发客户端 `Authorization`，导致平台不能安全调用。把短期教师 JWT 放入 URL、技能源码或匿名开放都不可接受。
- 修复：edge 改为每次启动生成高熵 capability path，教师 Authorization 仅在服务端从当前 Windows 用户 DPAPI 保护文件解密并注入上游；凭据到期立即 fail closed。capability 与 JWT 原文均不写入本台账或仓库。

### E38：edge 默认端口与真实 LLM relay 冲突

- 标记：`🛠 已修复端口编排错误，冲突时未启动`
- 现象：首选 `127.0.0.1:18791` 已被真实 Provider relay 使用；edge 启动前检查拒绝复用该端口，没有覆盖或中断 relay。
- 修复：edge 固定改为 `127.0.0.1:18792` 并补充端口合同测试；本地三工具冒烟随后 PASS。

### E39：venv launcher PID 与真实 listener PID 不同

- 标记：`🛠 已修复进程归属判定错误，修复前未上公网`
- 现象：Windows venv launcher 的 PID 与真正占用 Backend 端口的子进程 PID 不同，首版 edge 因严格相等校验而拒绝启动。
- 修复：运行状态同时记录 launcher 与 listener PID，并验证 listener 必须是受控 launcher 的直接子进程且命令行仍为固定 uvicorn Backend；没有因此放宽到任意占用端口的进程。

### E40：secure runtime 重启与 edge 启动发生竞争

- 标记：`⚠️ 并发编排错误，无公网暴露`
- 现象：secure agent 重启 active runtime 的同时，主执行流尝试启动 edge；edge 只返回 `EDGE_EXITED`，没有形成公网链路。
- 处理：发现竞争后停止额外写操作并移交单一执行流；后续由同一 active runtime 完成本地 production 三工具核验。

### E41：cloudflared 启动后误给 PowerShell 只读 `$Host` 赋值

- 标记：`⚠️ 启动编排命令错误，进程仍在且无数据泄漏`
- 现象：cloudflared 进程已经启动后，编排脚本误用 PowerShell 只读自动变量 `$Host`，命令随后报错；这不是 cloudflared 二进制或签名失败。
- 结果：已启动的进程仍在运行，没有输出或泄漏 Provider Key、教师 JWT、capability 或学生数据；公网是否可用另以 E43 的外部探测为准。

### E42：一次 PowerShell `foreach` 后直接接管道导致解析失败

- 标记：`⚠️ 操作错误，无写入`
- 现象：只读检查命令在 `foreach` 语句后直接追加管道，PowerShell 在执行前抛出 `ParserError`。
- 结果：该次命令没有执行、没有修改文件/进程/飞书资产，也没有产生可作为结论的检查结果。

### E43：Quick Tunnel 域名已生成，但当前网络拦截 SRV DNS

- 标记：`⚠️ 历史入口失败；当前现况见 E63`
- 现象：Cloudflare Quick Tunnel 返回了临时域名，但当前网络无法完成所需 SRV DNS 查询；从公网访问该域名得到 Cloudflare `1033`。
- 结论：该历史入口未部署；后续两次入口申请均为 `429`，当前仍无公网 endpoint。

### E44：尝试启动本地 `cloudflared proxy-dns` 被工具策略拒绝

- 标记：`⚠️ 工具策略拒绝，无系统变更`
- 现象：为验证当前网络的 Cloudflare DNS 路径而尝试启动本地 `cloudflared proxy-dns`，命令在执行前被工具策略拒绝。
- 结果：该 DNS 代理没有启动，没有修改系统 DNS、网络配置、进程或飞书资产；不得把该尝试记成网络修复或有效探测。

### E45：内联 Backend 恢复命令被工具策略拒绝

- 标记：`⚠️ 工具策略拒绝，无状态变化，已改用安全启动路径`
- 现象：第一次尝试用内联命令恢复 production Backend 时在执行前被工具策略拒绝，没有启动/停止进程，也没有数据库或凭据状态变化。
- 处理：改用不含密钥的临时启动脚本补启 Backend；随后无 token `401`、教师 JWT MCP `200`、学生 JWT bootstrap `200` 均验证通过。

### E46：真实 E2E runtime 路径设计过长，触发 Windows 路径限制

- 标记：`🛠 历史失败已备份、精确回滚并重跑 PASS`
- 现象：本次 Build receipt 路径达到 270 字符，而 Windows `LongPathsEnabled=0`。Docker 编译器探针已退出 `0`，随后 receipt 落盘触发 `OSError`；Godot phase1 在首次正式 Build 后 `15.05s` 失败。
- 权威结果：PostgreSQL 正确记录 `BUILD_WORKSPACE_ERROR / SANDBOX_COMPILE_ERROR`，现场为 `1 Session / 1 Draft / 1 rejected Build / 2 Commands`，仍为 `0 Run / 0 Evidence / 0 Projection / 0 relay dispatch`。
- 当前处理：全库备份、具名触发器事务回滚与独立指纹核验均 PASS；短 runtime 路径重跑已在 `181.879s` 内完成真实主链。

### E47：第一次只读表清单查询的标识符转义错误

- 标记：`⚠️ 只读查询错误，无数据库变化`
- 现象：首次读取数据库表清单时标识符转义错误，只有该查询失败；没有执行 INSERT、UPDATE、DELETE、DDL 或事务性业务动作。
- 更正：后续按真实 schema 与正确标识符读取，得到 E46 所列权威部分链计数；首次失败查询不作为产品结论。

### E48：append-only 触发器首次拒绝回滚

- 标记：`🛠 安全拒绝后精确完成`；首次 DELETE 被 append-only 触发器拒绝且整笔事务回滚，随后在已授权事务内仅临时关闭 3 个具名用户触发器，精确回滚提交并独立 PASS。

### E49：备份、触发器与指纹只读命令口径/引号错误

- 标记：`⚠️ 只读命令错误，无状态变化`；数次检查因命令口径或引号失败，均未执行写入；改用真实 schema 与固定参数后完成核验。

### E50：trust 数据库无密码路径触发 StrictMode

- 标记：`🛠 已修正`；无密码连接被错误读取未定义密码变量，改为按认证模式分支后通过。

### E51：relay 空 direct 与 FILE 配置冲突

- 标记：`🛠 已修正`；空 direct 值仍与 FILE 来源同时进入校验，改为只接受单一有效来源。

### E52：Provider key 文件 ACL 首次被拒绝

- 标记：`🛠 已修正`；首个临时 key 文件不满足最小 ACL 合同，收紧权限后才允许启动。

### E53：真实 PASS 被 banner `StartsWith` 误报

- 标记：`🛠 验收器误报已修正`；权威运行已 PASS，但 banner 前缀断言过窄，修正后以结构化回读为准。

### E54：只读验收的字段、模板与 `PYTHONPATH` 错误

- 标记：`⚠️ 只读错误，无外部变化`；先后出现 `evidence_type` 字段名、JS 模板字符串和 Python 模块路径错误，均修正后重读。

### E55：同步 receipt fence 错比 projection

- 标记：`🛠 已修正`；首次把 projection 当作父 workflow receipt 比较，改为核对真实 parent workflow 后同步通过。

### E56：Event 类型与同刻时间字面比较

- 标记：`🛠 已修正`；Event 类型取值与 `Z / +00:00` 同一时刻被当作不同文本，改为真实类型和时刻归一化比较。

### E57：lark-cli PowerShell wrapper 与 `.cmd` 响应形状解析错误

- 标记：`🛠 已修正`；wrapper 返回 envelope/矩阵形状与旧假设不同，改按当前 CLI 合同解析。

### E58：`cmd` 把 XML `<` 当作重定向

- 标记：`⚠️ 命令解析错误，无目标写入`；XML 不再内联交给 `cmd`，改用安全参数/标准输入路径。

### E59：Markdown URL 被误判为裸 URL

- 标记：`🛠 已修正`；Base 回读将链接渲染为 Markdown，验收先规范化再比较，三类链接均精确匹配。

### E60：Dashboard 首次错用 `--app-token`

- 标记：`⚠️ 只读参数错误`；改用当前 CLI 的 `--base-token` 后 10 个数据组件全部回读正确。

### E61：中断启动会话顺带结束 Backend

- 标记：`⚠️ 进程编排错误`；验收请求未到达服务端，不计产品失败；重启并确认监听后重新验收。

### E62：误判 cloudflared 缺失并发起强制重装

- 标记：`⚠️ 操作判断错误`；重装在完成前中断，随后回读确认既有安装与签名，不把该尝试记为修复。

### E63：Cloudflare 临时隧道两次 `429`

- 标记：`⛔ 外部限流`；两次尝试均未形成公网入口，未向 Aily 写入任何失败地址。

### E64：复杂 `Start-Process` 命令被工具策略拒绝

- 标记：`⚠️ 执行前拒绝，无状态变化`；命令未执行，未启动进程、修改文件或改变云端资产。

### E65：命名 Tunnel 两次按官方参数启动均被 SRV DNS 截断

- 标记：`⛔ 当前本机网络阻塞，无公网链路或业务数据外发`
- 已创建的命名 Tunnel `78154683-fb8e-4bd9-8d8a-1ee457067382` 两次均使用 Cloudflare 官方参数启动；两次都因当前香菇加速器/本机网络把 cloudflared 所需的 SRV DNS 查询截为空结果而无法建立连接，Tunnel 当前为 `inactive`。
- 两次失败期间 Worker、VPC Service 与本地 edge 之间没有形成公网链路，没有向 Cloudflare 或 Aily 发送学生业务数据；失败后 `cloudflared` 已停止。不得把已创建资产或本地 PASS 写成公网 MCP 已接通。

### E66：临时自定义 DoH 绕行尝试已精确撤销

- 标记：`⚠️ 临时网络诊断已撤销，无遗留进程或文件`
- 为定位 E65 曾临时尝试自定义 DoH 绕行；该尝试没有形成可用 Tunnel，随后已撤销，相关临时文件与进程均已删除。
- 当前没有保留自定义 DNS/DoH 代理、失败公网地址或额外业务服务；后续只复用已创建的最小 Cloudflare 资产，不追加第二套 ingress 设计。

## 五、尚未完成、真实阻塞与已解除项

### B01：真实 Provider 可用性

- 标记：`✅ 已解除`
- 真实 DeepSeek relay 已参与 Godot 主链并在 `181.879s` 内 PASS；不再把 Provider 或真实模型调用列为阻塞。
- Provider Key 只允许经受控本地注入进入运行进程；本台账、仓库、URL、飞书资产与命令输出均不记录其原文。
- PostgreSQL 已回读 4 Run、11 Evidence、4 projection、Profile revision 4 与 13 次 Provider generation。

### B02：新账号 Aily 权限与工具尚未闭合

- 标记：`🟨 未完成：仍待公网 MCP 与三次成功真实问答`
- Aily task `7674363221256211415` 已完成；三个准确名称的自定义技能均已创建、自动审核、安装，并在 Skills 页独立回读各 1 项。旧账号技能 ID 与审核状态没有被冒充为可迁移资产。
- 安全授权已切换为“部分限制”，203 个宽权限条目加入限制且最终回读 `aria-checked=true`。
- 四个内置技能仍存在；其“移除/自动调用”控件为 `cursor-not-allowed / disabled`，属于当前平台限制，不能宣称已经移除。
- 三个自定义技能已安装，但自定义 MCP 尚未连接；B03 闭合后仍须完成学生进度、班级共性问题、Evidence/档案链接三个工具各一次成功真实问答与链接回读。本地三工具 PASS 或技能能按失败合同拒绝编造，都不能替代该 Aily 验收。

### B03：Aily 没有公网 HTTPS Backend/MCP 服务端连接

- 标记：`⛔ 当前本机 SRV DNS 阻塞，云端最小资产已创建但链路未接通`
- 当前技能调用妙搭 `/api/*` 会遇到浏览器 OAuth 302；Aily 服务端运行时不会携带真人浏览器 Cookie。
- 已有本地服务端：`POST /integrations/feishu/v1/mcp`，恰好暴露 `query_learner_progress`、`query_class_common_issues`、`get_evidence_summary_and_links`；production Backend、DPAPI 保护的 15 分钟教师凭据和 `127.0.0.1:18792` 受限 edge 均已通过本地调用，但本地通过不等于 Aily 已连接。
- 必填非敏感部署配置：`WALNUT_FEISHU_MCP_DASHBOARD_URL=https://larkcommunity.feishu.cn/base/Q6V9biulZaezaHskZqacYm2JnHe?table=blkK3ldZA0pePRGb`；`WALNUT_FEISHU_MCP_TEACHER_WORKSPACE_URL=https://dcniaqwtmoca.feishuapp.com/app/app_17c6bc5hz7d`。
- 用户已授权创建临时最小 ingress；本轮实际创建且仅创建：workers.dev subdomain `walnut-int3-260816`、Worker `walnut-int3-mcp`（deployment `6b4bf3559fb74d44b7845798026961cc`）、命名 Tunnel `78154683-fb8e-4bd9-8d8a-1ee457067382`、VPC Service `01a00957-4ab4-7141-b7ba-0ffa7b0dcecc`。这些是临时传输资产，不存储学习数据，也不是第二套产品 Backend 或权威数据库。
- 本地 edge `127.0.0.1:18792` 已核验只列出并成功调用恰好三个只读工具；命名 Tunnel 因 E65 所述 SRV DNS 问题仍为 `inactive`，`cloudflared` 已停止，因此 Worker/VPC Service 当前没有到本机 Backend 的公网可用链路，也没有业务数据外发。
- 已实现的安全边界：公网候选入口只能到高熵 capability path；edge 只在服务端注入 DPAPI 保护的短期教师 Authorization，再转发到唯一 Backend 的精确 MCP 路径；其余 path/method fail closed，Backend 固定 tenant、teacher actor 与三个 read scope。
- 当前需要：暂时停止会截断 SRV DNS 的香菇加速器/本机网络路径，复用现有 Tunnel、VPC Service 和 Worker 建立链路并完成公网三工具核验；成功后才配置 Aily HTTP Streaming，不重复创建一套云资产。
- 不做：不把 JWT/API Key 写入技能源码或 URL，不把接口改成匿名公开，不把临时域名生成误写成部署成功。

### B04：新飞书 CLI 账号登录

- 标记：`✅ 已解除`
- 新账号已完成 CLI 认证，并已在该账号下创建新 Base、三表、Dashboard、成长档案母版和妙搭应用。
- 旧账号资产未删除、未移动、未覆盖；新账号无法跨账号读取旧资产，符合平台权限隔离。
- 跨企业时 Aily 审核不能继承，仍按 B02 / B03 单独处理。

### B05：真实 Godot 端到端

- 标记：`✅ 已解除`
- 首次失败链已备份并精确回滚；短路径真实 DeepSeek + Godot 主链在 `181.879s` 内 PASS，PostgreSQL 与目标飞书资产均已闭合。
- 剩余工作只归 B02 / B03：公网 MCP 与 Aily 三次真实问答。

## 六、下次执行的安全顺序

1. 暂停会截断 SRV DNS 的香菇加速器/本机网络路径，只读确认 cloudflared 所需 SRV 查询恢复，不新增 DNS/DoH 代理。
2. 刷新短期教师 JWT，启动/回读本地只读 edge，并复用现有命名 Tunnel、VPC Service 与 Worker；确认公网仍只暴露三个工具。
3. 在 Aily 以 HTTP Streaming 连接已验证的 MCP endpoint，不增加学生业务写权限。
4. 分别完成学生进度、班级共性问题、Evidence/档案链接三次成功真实问答，核对“客观事实 / AI推断 / 教学建议”与链接。
5. 保存非敏感验收结果后停止临时隧道，不保留临时凭据；云端临时资产的后续保留或精确删除按实际演示需要处理。

## 七、绝对禁止

- `🔐` 不记录或提交 Provider Key、JWT secret、学生 JWT、伪名 raw secret、OAuth device code、验证码或凭据。
- 不用 fixture relay、固定 Mock、手工 Base 数据或历史 Run 替代真实验收。
- 不在真实 Run 前执行 `--apply` 污染空 Base。
- 不删除或迁走原账号资产；目标账号采用副本。
- 不修改 Godot 前端或游戏内 Agent Runtime。
- 不把教师接口改成匿名公开，不给 Aily 学生业务写权限。
- 不把平台禁用控件下仍存在的四个 Aily 内置技能写成“已移除”，也不绕过平台限制；不把三个已安装技能写成“已接通 MCP”。
- 不对已验收权威数据做泛化删除或回滚；妙搭 OAuth 授权或企业管理员审核动作也不得擅自执行。

## 八、相关手册

- [INT3 五分钟真实演示](int3-feishu-demo.md)
- [Backend 运行手册](runbook.md)
- [新账号非敏感飞书资产配置](../../config/int3_feishu_assets.target.json)
- [旧账号保留配置](../../config/int3_feishu_assets.json)
