# walnut-world-new — 核桃代码世界 · 聚合快照仓库

> 本仓库是「核桃代码世界」（Walnut World）四端工程的聚合快照（sub-repo），集中存放各端源码与配套工具，用于整体浏览与分发。**日常开发不在此进行**，改动请到对应的子仓库（见下）。

## 这是什么

「核桃代码世界」是一款游戏化 C++ 编程教育产品：

- 学生在 **Godot 客户端** 中进入俯视角 3D 农场，接到农作任务（浇水、种菜）；
- 在代码面板编写 **C++** Skill，提交到云端编译、在真实 Docker Sandbox 中运行；
- 程序驱动游戏角色执行任务，世界结果由确定性 WorldEngine 客观计算；
- **AI 角色基于真实的 Run / Evidence 开展教学**（芽芽 / 叮当发布任务、小核桃调用学生 Skill、教学角色讲解、Bug 角色包装反例、书书总结成长）；
- 教师在 **飞书教师工作台** 查看班级学习概览、学生档案与脱敏证据。

核心设计原则：**程序决定客观事实（世界结果、教学策略、学习画像），Agent 只做受约束的教学表达**。AI 无法直接改写学生代码或世界状态，任何代码修改候选都必须经学生显式确认。

## 架构总览

```text
                    agent/
              合同 + 运行时库（Python）
                 │  不可变 Wire 合同 / Ports
                 ▼
          walnut-world-backend/   ◀── 唯一生产 HTTP Gateway
            唯一 PostgreSQL 写入 + Alembic 迁移权威
                 │  只读投影缓存
        ┌────────┴────────┐
        ▼                 ▼
walnut-world-frontend/    miaoda-teacher-workbench/
   Godot 4.5.2 学生端          NestJS + React 教师工作台
```

四端职责一句话：

| 目录 | 负责 |
|---|---|
| `agent/` | 定义「怎么通信」：不可变 Wire 合同、Ports、provider-neutral Runtime / Build / Sandbox 库 |
| `walnut-world-backend/` | 定义「事实」：唯一 HTTP 网关、全部持久化、客观世界引擎、耐久 worker |
| `walnut-world-frontend/` | 定义「学生体验」：Godot 客户端，只消费后端，不本地编译 C++ |
| `miaoda-teacher-workbench/` | 定义「教师体验」：只读消费后端投影，不直接写业务数据 |

## 目录结构

| 目录 | 说明 |
|---|---|
| `agent/` | Agent 端：Python 服务与 Godot 客户端（`clients/godot`），含系统架构、前端/后端/Agent 开发文档 |
| `walnut-world-frontend/` | 前端（Godot 4.5.2）：场景、脚本、资源、测试 |
| `walnut-world-backend/` | 后端（Python）：API、Alembic 数据迁移、Docker 部署 |
| `miaoda-teacher-workbench/` | 教师工作台（NestJS + Vite）：server / client / shared |
| `tools/` | 本地工具运行时（Godot、Node），未纳入本快照版本控制 |
| `.codex-miaoda-npm-shim/` | npm shim 工具 |
| `.claude/` | Claude Code 本地权限配置（`settings.local.json`） |

## 权威与指路

新协作者按顺序阅读：

1. **接口合同权威**：`agent/contracts/manifest.json` 及其引用的 OpenAPI / AsyncAPI / JSON Schema（当前 v0.6.0，147 文件）
2. **五份顶层设计文档**：`agent/01_~05_核桃代码世界_*.md`（系统架构 / 前端 / 后端 / Agent / 联调规范）
3. **运行 / 部署手册**：`walnut-world-backend/docs/operations/runbook.md`
4. **数据库迁移权威**：`walnut-world-backend/migrations/`，当前 head `019_int2_skill_patch_authority`
5. **验证证据账本**：`agent/docs/INT2_CROSS_REPO_VALIDATION_REPORT.md` 等

## 里程碑状态

| 里程碑 | 内容 | 状态 |
|---|---|---|
| INT1 | 耐久单一 Gateway 权威 + 学生全链 | 已交付验收 |
| INT2 | World 呈现 + 学生确认的 Skill Patch | 已交付（受控真实 Provider M2 PASS，run `868a`） |
| INT3 | 飞书教师只读 MCP + 教师工作台 | 已交付 |

**明确未证明 / 排除项**：production private DinD、公开 Gateway pending write response-loss、v0.6 合同 tag 尚未发布（release identity `NOT_PROVEN`）；WSS、Client Event Batch、自动 / 多文件 Patch 默认排除。

## ⚠️ 快照说明

本仓库是各子仓库的**打包快照**，此处的代码可能落后于上游。请作为导航与分发使用，不要以此作为修改目标或验收证据来源；一切以对应子仓库为准。

## 协作

- 私有仓库，协作者：`wu-jiqi`、`diamondYe`
- 上传时已排除：嵌套 `.git`、`node_modules`、`.venv`、`.godot` 缓存、`dist` / `logs`、`.env` 等依赖与密钥文件；各子项目的 `.gitignore` 保留在对应目录中。
