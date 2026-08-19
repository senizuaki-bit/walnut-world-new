# 核桃代码世界 · Walnut World

> 让孩子用 C++ 创造 AI 小核桃的**真实能力**。
>
> 孩子写的代码不是为了通过一道判题，而是被编译、测试、认证成 Agent 可以真实调用的 **Skill**；
> 世界是否改变由确定性引擎裁定，AI 只能围绕真实发生的事教学。

## 提交项

| 提交项 | 地址 |
|---|---|
| 世界观 PV | https://meek-bublanina-794c71.netlify.app/ |
| 3—5 分钟 Demo 视频 | https://cheerful-heliotrope-66b1ac.netlify.app/ |
| 在线体验入口 | 本仓库 · https://github.com/senizuaki-bit/walnut-world-new<br>初版 demo：https://github.com/senizuaki-bit/walnut-content-engine/releases/tag/demo-2026-07-20 |
| 学习洞察【多维表格】 | https://larkcommunity.feishu.cn/base/Q6V9biulZaezaHskZqacYm2JnHe?table=blkK3ldZA0pePRGb |
| 教室数据中心【秒搭】 | https://dcniaqwtmoca.feishuapp.com/app/app_17c6bc5hz7d/records |

建议体验顺序：先看**世界观 PV** 建立概念，再看 **Demo 视频**理解教学闭环，最后跑起来看数据看板。

## 这个产品在解决什么

编程教育里有三件事很难同时成立：让孩子愿意长期写代码、让 AI **因人而异但不代做**、让教研内容依据**真实学习证据**进化。

| 真实环节 | 常见做法的问题 |
|---|---|
| 孩子写代码 | 只为通过一道题，结果抽象，缺少「我的代码真的在工作」的感受 |
| AI 辅导 | 题目一简单，AI 一句话就泄露答案；只看聊天文本又脱离真实错误 |
| 程序运行 | 大模型若直接控制游戏，会出现「说完成了，但世界并未完成」的假成功 |
| 教师与家长 | 编译错误、提示使用、修复过程分散在各处，复盘成本高 |

**本方案的重新定义：** 要解决的不是「让 AI 帮孩子把题做对」，而是**让孩子写出的代码成为 AI 可以真实调用、世界可以客观验证、教学系统可以持续复用的能力**。

### Skill 是什么

学生编写的 C++ 经过**编译、测试、认证与激活**后形成的世界行动能力。

它不是提示词，不是一次性答案，也不是可以直接改写世界的脚本：

- 从学生视角：自己创造并保留在技能树里的数字工具
- 从 Agent 视角：具有明确输入、输出和权限边界的可调用 Tool
- 从系统视角：带来源、哈希、认证状态和不可变版本的软件产物

### 为什么不选另外两条路

| 路径 | 关键代价 | 结论 |
|---|---|---|
| 独立聊天式 AI 助教 | 看不到权威世界结果，容易直接给答案 | 不作核心 |
| 固定脚本提示 | 不能按错误历史、边界案例和学生选择动态分层 | 仅作兜底 |
| **证据驱动的 Agent + C++ Skill** | 需要沙箱、合同、事件证据和多端联调 | **已实现核心链路** |

## 可信边界

这是整个系统的地基，也是所有设计取舍的来源：

> **AI 决定「建议做什么」，孩子的 C++ 决定「具体怎么做」，网关负责「谁能调用哪一个版本」，WorldEngine 决定「事情是否真正发生」。**
>
> 任何角色都不能绕过世界回执宣布成功。

| 关键对象 | AI 可以做 | AI 不可以做 | 最终权威 |
|---|---|---|---|
| 世界任务结果 | 解释运行轨迹，指出可观察事实 | 无运行结果时声称完成，或改写世界事实 | 编译器、运行时、WorldEngine |
| 孩子的学习状态 | 给出带 Evidence 与置信度的推断候选 | 凭一次对话判定「已掌握」 | LearnerProjector 与可回放规则 |
| 实时教学介入 | 选择角色、提示层级、表达方式 | 绕过教学策略直接给出整题答案 | PedagogyPolicy 与安全规则 |
| 新关卡与新内容 | 分析数据并生成候选版本 | 未经审核直接上线 | 教研、关卡、叙事、质量保障流程 |

### 五个角色不是预设对白，而是五种权威

| 角色 | 何时出现 | 依据 | 绝对不能做 |
|---|---|---|---|
| 芽芽 | 任务开始、世界状态变化 | 剧情、经营目标、任务规则 | 评价代码或宣布掌握 |
| 小核桃 | 构建、激活并真实调用 Skill | 精确 Skill 版本、Run 和世界回执 | 无运行结果时声称完成 |
| 叮当师傅 | 编译失败、运行失败或主动求助 | 真实错误、变量轨迹和世界结果 | 第一次失败直接贴完整答案 |
| Bug 先生 | 连续三次同类失败 | 真实公开边界案例 | 为剧情编造 Bug |
| 书书 | 世界目标客观完成后 | 代码版本、提示与运行历史 | 提前结算，或把一次成功说成永久掌握 |

### AI 分层教学

| 层级 | AI 做什么 | 学生控制权 |
|---|---|---|
| L0 | 只展示事实（「1 号番茄仍差 5」） | 自己观察并修改 |
| L1 | 定位代码区域，不给写法 | 接受或拒绝提示 |
| L2 | 解释一个概念 | 继续独立修改 |
| L3 | 提供局部支架，不完成整题 | 完成剩余代码 |
| L4 | 生成结构化修改提案（前后对比、理由、证据、影响范围） | **显式接受或拒绝；接受后仍需手动构建、激活、运行** |

前端进一步收紧 L4：同类失败累计 4 次、且学生主动看完 L3 之后，才显示「AI 修改提案」入口。

## 架构

```text
                    agent/
              合同 + 运行时库（Python）
                 │  不可变 Wire 合同 / Ports / Runtime
                 ▼
          walnut-world-backend/   ◀── 唯一生产 HTTP Gateway
            唯一 PostgreSQL 写入 + Alembic 迁移权威
                 │  只读投影
        ┌────────┴────────┐
        ▼                 ▼
walnut-world-frontend/    miaoda-teacher-workbench/
   Godot 4.5.2 学生端          NestJS + React 教师工作台
```

| 目录 | 负责 |
|---|---|
| `agent/` | 定义「怎么通信」：不可变 Wire 合同（v0.6.0，147 个文件）、Ports、provider-neutral Runtime / Build / Sandbox |
| `walnut-world-backend/` | 定义「事实」：唯一 HTTP 网关、全部持久化、客观世界引擎、耐久 worker |
| `walnut-world-frontend/` | 定义「学生体验」：Godot 客户端，只消费后端，不本地编译 C++ |
| `miaoda-teacher-workbench/` | 定义「教师体验」：只读消费后端投影，不直接写业务数据 |
| `tools/` | 本地工具运行时（Godot、Node），不纳入版本控制 |

### 一次完整闭环

1. **提出目标** —— 芽芽说明剧情与客观完成条件
2. **创造并认证** —— 孩子编写 C++；后端完成构建、测试、认证与激活
3. **真实执行** —— 小核桃调用精确 Skill 版本，Sandbox 输出结构化动作
4. **世界验证** —— WorldEngine 原子提交合法变化，生成 Run / WorldEvent / Evidence / Receipt
5. **教学回流** —— 失败触发分层提示，成功后由书书总结；脱敏证据进入飞书

每一步都由真实版本和 Evidence 串联，任何一环缺失都会 fail closed。

### Demo 关卡：《作物适配浇水器》

8 块土地种着不同作物，旧浇水器统一浇到湿度 60，结果番茄仍缺水、土豆却被浇多。

| 土地 | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 |
|---|---|---|---|---|---|---|---|---|
| 作物 | 胡萝卜 | 番茄 | 土豆 | 玉米 | 胡萝卜 | 番茄 | 土豆 | 玉米 |
| 当前湿度 | 20 | 65 | 45 | 90 | 60 | 35 | 55 | 50 |
| 目标湿度 | 60 | 70 | 50 | 65 | 60 | 70 | 50 | 65 |
| 缺口 | 40 | 5 | 5 | -25 | 0 | 35 | -5 | 15 |
| 正确水量 | 2 | 1 | 1 | 0 | 0 | 2 | 0 | 1 |

规则：`缺口 = 目标 - 当前`；`≥ 30` 浇 2 份，`0 < 缺口 < 30` 浇 1 份，`≤ 0` 不浇（**不输出任何内容**）。1 份 = 250 毫升。

难度边界刻意压在**一维数组 + 下标配对 + 差值计算 + if / else if**，却能暴露写死目标、差值方向、下标错位、分支顺序、循环边界等多种真实错误——既能展示 AI 分层教学，又保持在学 C++ 一年左右可理解的范围内。

关卡初始代码是**填空模板**（`/*目标*/`、`/*当前*/`、`/*边界*/`、`/*份数*/`），让孩子一打开就知道要做什么。

## 跑起来

### 前置

- Windows + Docker Desktop
- Python 3.12（后端 `.venv`）
- Godot 4.5.2（`tools/godot-4.5.2/`）
- 一个 LLM Provider Key（DeepSeek），存成纯文本文件

### 数据库迁移

```bash
cd walnut-world-backend && .venv/Scripts/python.exe -m alembic upgrade head
```

当前 head 是 `020_skill_artifact_per_build`。**换机器或拉新代码后必须先跑**，否则读写权威链会失败。

### 启动整套（含游戏窗口）

```bash
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $env:WALNUT_LLM_UPSTREAM_API_KEY_FILE='C:\path\to\deepseek.key'; & '.\walnut-world-backend\scripts\start-persistent-play.ps1' -Action Start"
```

`-Action Status` 查看状态，`-Action Stop` 停止（数据卷保留）。

脚本会依次拉起：PostgreSQL 容器 → 迁移与 seed → 私有 LLM relay → Gateway → workflow worker → learner worker → Godot 客户端。

**两个必须知道的坑：**

1. **必须用 Windows PowerShell 5.1**（`powershell.exe`），不能用 PowerShell 7（`pwsh`）。PS7 的 `Start-Process` 会把已删除的环境变量当作空字符串传给子进程，relay 读到空 key 会拒绝启动。
2. **自己写包装脚本时要存成带 BOM 的 UTF-8**。5.1 按 ANSI 解码无 BOM 文件，含中文的路径会被解析成乱码。

### 跑测试

```bash
cd walnut-world-backend && .venv/Scripts/python.exe -m pytest tests/unit -q
```

前端（62 个用例，逐个跑）：

```bash
tools/godot-4.5.2/Godot_v4.5.2-stable_win64.exe --headless --path walnut-world-frontend --script res://tests/client/<name>_test.gd
```

后端集成测试需要一个**全新的** PostgreSQL，并设置 `WALNUT_TEST_DATABASE_URL`。注意：集成套件对共享库状态敏感，在复用过的库上跑会出现互不重合的浮动失败——判断回归时请用全新库。

## 权威与指路

新协作者按顺序读：

1. **接口合同权威** —— `agent/contracts/manifest.json`（v0.6.0，147 文件）及其引用的 OpenAPI / AsyncAPI / JSON Schema
2. **五份顶层设计文档** —— `agent/01_~05_核桃代码世界_*.md`
3. **运行 / 部署手册** —— `walnut-world-backend/docs/operations/runbook.md`
4. **数据库迁移权威** —— `walnut-world-backend/migrations/`
5. **验证证据账本** —— `agent/docs/INT2_CROSS_REPO_VALIDATION_REPORT.md`

## 里程碑

| 里程碑 | 内容 | 状态 |
|---|---|---|
| INT1 | 耐久单一 Gateway 权威 + 学生全链 | 已交付验收 |
| INT2 | World 呈现 + 学生确认的 Skill Patch | 已交付（受控真实 Provider M2 PASS，run `868a`） |
| INT3 | 飞书教师只读 MCP + 教师工作台 | 已交付 |

## 当前边界与已知未完成项

如实列出，不计入通过项：

| 边界 | 现状 |
|---|---|
| 第一关 WATER 权威演出 | 基础链路已接入，但发布的 world-presentation 合同仍是 HARVEST-only；前端默认关闭演出，不冒充权威浇水结果 |
| 合同 v0.6 | 147 entries 的 candidate 已进入前端描述，但 tag 尚未发布，release identity 为 `NOT_PROVEN` |
| 公开 Gateway 写响应丢失 | 私有 Provider relay 的恢复已验证；公开 Gateway pending write response-loss 仍为 `NOT_PROVEN` |
| 飞书生产接入 | 接口合同与角色边界已设计，当前交付明确排除生产接入 |
| 儿童真实用户验证 | 现有证据以工程回归和内部评审为主，尚无课堂试点数据 |
| 集成套件稳定性 | 部分并发/时序用例在复用库上浮动失败，单独跑均通过；需要全新库才能取得干净信号 |
| Bug 先生 / Patch 对编译失败 | 编译失败已成为教学证据，但两者的判定阈值仍定义在 Run 失败上，编译失败上报封顶在 2 次 |

## 一类值得记录的缺陷

系统曾反复出现同一种故障：**一次已经结算的事（成功或失败），让下一次做不成。**

最典型的两个只惩罚做对了的孩子：

- 把题解对 → 产出与历史完全相同的编译产物 → 撞上产物表主键 → 构建死信，此后再也无法构建那份正确代码
- 成功运行 → 世界前进一个版本 → 客户端游标落后 → 之后每个 Turn（含问叮当）被 409 拒绝

根因是一条贯穿的设计倾向：任何权威值对不上就 fail closed，而没有任何机制回头收拾它。对**执行副作用**这是对的；但它被用作**每个动作的总闸**，于是任何一次漂移都变成永久死亡——而「不重复执行」这个真正要保的安全性，服务端的幂等键早已保证。

现行规则：

1. 无法校验的未结算信封 → **隔离**，不阻塞新操作
2. 恢复出来的失败结局 → **报告**，不判死
3. 被拒的请求 → 重读它声明的**全部**游标，重试一次
4. 失败必须说出**卡在哪一步**，而不是所有失败长一个样

## 协作

私有仓库，协作者：`wu-jiqi`、`diamondYe`。

上传时已排除嵌套 `.git`、`node_modules`、`.venv`、`.godot` 缓存、`dist` / `logs`、`.env` 等依赖与密钥文件；各子项目的 `.gitignore` 保留在对应目录中。
