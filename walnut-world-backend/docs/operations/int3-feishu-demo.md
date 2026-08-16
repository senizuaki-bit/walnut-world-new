# INT3 飞书教师学习数据中心：5 分钟真实演示手册

本文只验收 `Godot -> walnut-world-backend -> PostgreSQL -> 飞书 Base/成长档案/妙搭/Aily` 这一条链。Godot 与 sibling Agent 保持冻结；不得以 SQL、fixture、固定 JSON 或手工录入 Base 代替真实学习记录。

## 固定资产

- Base：[核桃世界｜学习洞察](https://larkcommunity.feishu.cn/base/Q6V9biulZaezaHskZqacYm2JnHe)
- Dashboard：[班级学习看板](https://larkcommunity.feishu.cn/base/Q6V9biulZaezaHskZqacYm2JnHe?table=blkK3ldZA0pePRGb)
- 成长档案母版：[儿童学习成长档案 v1](https://larkcommunity.feishu.cn/docx/CHZ9dln03o9wkQxF4DTccWPgnOd)
- 妙搭教师工作台：[核桃世界｜教师学习数据中心](https://dcniaqwtmoca.feishuapp.com/app/app_17c6bc5hz7d)
- Aily 教师助手：[核桃世界｜教师学习助手](https://aily.feishu.cn/agents/agent_4kur1swqbbr6xgw/detail)

Base 的三张表固定为“学生档案”“每日学习记录”“学习证据摘要”。竞赛数据模型采用“一租户对应一个班级”；当前资产只允许绑定 `tenant_yaya`。学生、学习日和 Evidence 均使用域分离 HMAC 业务键，不同步姓名、账号、原始代码、原始聊天、凭据或直接身份信息。

## 计时前准备（不计入 5 分钟）

1. PostgreSQL 必须已迁移到 `019_int2_skill_patch_authority`，并且启动的是唯一 Backend Gateway、workflow worker、learner worker 和可恢复 LLM relay。启动要求沿用 [Python 后端运行与验收手册](runbook.md)，不得启动第二套产品 Backend 或数据库。
2. 在同一受控进程环境设置 `WALNUT_DATABASE_URL`、固定的 `WALNUT_FEISHU_PSEUDONYM_SECRET`、生产 JWT 配置和真实 Provider 配置，并设置 `WALNUT_FEISHU_MCP_DASHBOARD_URL=https://larkcommunity.feishu.cn/base/Q6V9biulZaezaHskZqacYm2JnHe?table=blkK3ldZA0pePRGb` 与 `WALNUT_FEISHU_MCP_TEACHER_WORKSPACE_URL=https://dcniaqwtmoca.feishuapp.com/app/app_17c6bc5hz7d`。伪名密钥一旦绑定到本资产不可轮换；配置不匹配时同步器必须在读取或写入前失败。这两个 URL 只生成受信任展示链接，不是 MCP 公网地址。
3. `lark-cli auth status` 必须显示 user identity 可用。妙搭应用 OAuth 授权、Aily 权限收紧和 MCP 连接/审核属于显式管理动作，应在授权人确认后完成。浏览器 OAuth 只让真人打开妙搭，不能把 Cookie 传给 Aily 的服务端运行时；Aily 真实查询必须把 Backend 的单一 `POST /integrations/feishu/v1/mcp` 部署到公网 HTTPS，并通过该连接获得 `query_learner_progress`、`query_class_common_issues`、`get_evidence_summary_and_links` 恰好三个只读工具。平台凭据栏只注入短期教师 Authorization；服务端固定 tenant、teacher actor 和 `learner:read`、`class-insights:read`、`evidence:read`，不得把 API Key/JWT 写入源码、URL、文档、Compose、日志或截图。
4. 向 Godot 进程注入由同一 Gateway 签发的短期 student JWT；不得把 JWT、Provider key 或伪名密钥写入本仓、命令参数、日志或截图。
5. 先执行一次只读预检：

   ```powershell
   .\.venv\Scripts\python.exe .\scripts\sync_feishu_learning.py --tenant-id tenant_yaya --assets .\config\int3_feishu_assets.target.json --identity user
   ```

   只有权威 PostgreSQL 中已经存在真实 `game_runs`、同内容哈希的 Evidence、成功 learner projection 和 Learner Profile 时，才允许进入 `--apply`。

## 5 分钟演示

### 0:00–1:40　完成真实学习任务

在现有 Godot 学生端完成一项真实任务。展示任务结果，但不改 Godot 或 Agent。等待 Backend 的 workflow worker 和 learner worker 都闭合；同一业务事实必须形成 `game_runs`、`game_evidence`、成功 learner projection 和更新后的 Learner Profile。

### 1:40–2:20　同步并立即重放

在 Backend 根目录执行两次完全相同的命令：

```powershell
.\.venv\Scripts\python.exe .\scripts\sync_feishu_learning.py --tenant-id tenant_yaya --assets .\config\int3_feishu_assets.target.json --identity user --apply
.\.venv\Scripts\python.exe .\scripts\sync_feishu_learning.py --tenant-id tenant_yaya --assets .\config\int3_feishu_assets.target.json --identity user --apply
```

第一次按稳定业务键 upsert 三张 Base 表，并为该学生复制一次 v1 母版。复制后先写入唯一的匿名 HMAC 所有权封印，封印绑定 learner ref、学生业务键、租户绑定指纹以及文档 token/URL；验证通过后才可按学习日追加固定结构区块。文档 URL 必须属于母版配置的同一 HTTPS 飞书域。第二次必须复用原 Base 记录、原学生文档和原日期区块，并在每次 append/block replace 前重新读取、验证封印。任一 search 返回多个候选、错学生链接、封印缺失/重复/篡改、文档追加状态不明确、模板漂移、租户绑定或密钥指纹不一致时都应停止，不得猜测写入。

### 2:20–3:20　展示 Base、Dashboard 和成长档案

依次打开固定资产链接，核对：

- “每日学习记录”的任务结果、尝试次数、主要错误、AI 辅助程度、Skill Patch 使用、知识点阶段、建议、数据时间和 Evidence 引用来自刚才那条 Run；
- “学习证据摘要”只含白名单事实，Evidence 业务键能追溯到同 Run；
- Dashboard 的今日活跃、完成率、平均尝试、AI/Patch、阶段分布、高频错误、关注学生和 7 日趋势发生相应变化；“最近 7 天”固定按 Asia/Shanghai 的今天及前 6 个自然日计算，不按滚动 168 小时截断；
- 每个学生仍只有一份长期文档，当天仍只有一个日期区块；所有 v1 栏目存在，缺失值为“暂无数据”。

### 3:20–4:20　展示妙搭教师工作台

打开妙搭应用，依次展示班级概览、学生列表、学生详情、每日记录、成长档案按钮和脱敏 Evidence 卡片。Evidence 卡片链接应定位到该学生详情页的对应卡片，不应指向 localhost、原始证据或无认证公开地址。

### 4:20–5:00　询问唯一 Aily 教师助手

向“核桃世界｜教师学习助手”询问：

> 请查询刚才这位学生今天的学习进度，说明班级共性问题，并给出证据摘要、成长档案和班级看板链接。

演示时把 Base/妙搭“学生代号”栏中的匿名 `lrn_*` 一并告诉助手；不要让教师输入 content hash 或 HMAC class ref。学生工具由 Backend 从该生唯一权威 Profile 解析 content，班级工具可零参数并由 JWT tenant 解析 class，Evidence ID 只能沿用学生工具返回的 `recent_evidence`。回答必须固定组织为“客观事实 / AI推断 / 教学建议”。客观事实只能来自上述单一 MCP 服务的三个只读工具响应；无数据或工具不可达时必须明确写“暂无可核验数据”，不得补数字。教师助手不得调用学生业务写接口。

## 验收证据

演示结束后保留下列非敏感证据：

- 同一 Run ID 对应的脱敏 Evidence 引用、projection 数据时间和 Learner Profile 高水位；
- 两次同步报告及三张表第二次同步前后的记录计数；
- 同一学生文档 token、URL、匿名 HMAC 所有权封印、`template_version=v1` 和同一日期区块未重复，封印中的学生业务键及租户绑定与 Base 完全一致；
- Dashboard 截图、妙搭 Evidence 深链和 Aily 三段式回答；
- teacher 角色正向查询、student 错误角色 `403`、另一租户查询被拒绝的自动化测试结果。

如果真实 Provider、公网 HTTPS MCP、Aily 短期只读教师 JWT、妙搭 OAuth 或管理员审核任一未完成，应把对应步骤记为 `NOT RUN`，不能用本地 MCP 测试、空 Base、历史 Run、fixture relay 或 Aily 的安全降级回答记作端到端通过。
