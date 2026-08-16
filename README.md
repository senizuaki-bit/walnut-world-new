# walnut-world-new

核桃世界（Walnut World）项目的聚合子仓库，集中存放各端源码与配套工具。

## 目录结构

| 目录 | 说明 |
|---|---|
| `agent/` | Agent 端：Python 服务与 Godot 客户端（`clients/godot`），含系统架构、前端/后端/Agent 开发文档 |
| `walnut-world-frontend/` | 前端（Godot 4）：场景、脚本、资源、测试 |
| `walnut-world-backend/` | 后端（Python）：API、数据迁移（Alembic）、Docker 部署 |
| `miaoda-teacher-workbench/` | 教师工作台（NestJS + Vite）：server / client / shared |
| `tools/` | 本地工具运行时（Godot、Node），未纳入版本控制，目录为空 |
| `.codex-miaoda-npm-shim/` | npm shim 工具 |
| `.claude/` | Claude Code 本地权限配置（`settings.local.json`） |

## 说明

- 本仓库是父仓库 [`walnut-world`](https://github.com/senizuaki-bit/walnut-world) 的 `new` 子模块。
- 上传时已排除：嵌套 `.git`、`node_modules`、`.venv`、`.godot` 缓存、`dist`/`logs`、`.env` 等依赖与密钥文件；各子项目的 `.gitignore` 保留在对应目录中。

## 协作

- 私有仓库，协作者：`wu-jiuqi`、`diamondYe`。
