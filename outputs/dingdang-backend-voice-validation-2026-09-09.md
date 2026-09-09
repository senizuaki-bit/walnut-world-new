# 问叮当后端实时语音验证

日期：2026-09-09。范围：仅后端及语音适配器、接口文档；本次未修改前端文件。

## 结果

- 后端单元测试 8 项：鉴权、连续语音、主动更新上下文、工具读取、音频转发、打断、关闭、错误处理。
- PostgreSQL 语音集成测试 1 项：当前关卡和代码读取、Run 归属过滤、无额外游戏写入。
- 原有 HTTP／世界 WSS 协议测试 24 项。
- Agent 语音适配器测试 19 项，含新增的 `session.update` 配置保留测试。
- 上述 52 项合并通过，另有 4 个子测试通过。
- 额外回归：原有 DS 提示链路 5 项、世界实时事件 1 项通过。
- 涉及 Python 文件的 Ruff 检查通过，启动脚本 PowerShell 语法解析通过。

## 真实豆包验证

使用豆包合成的一句测试提问作为音频输入，没有录制麦克风。验证链路为真实本地 WebSocket → 主后端鉴权／PostgreSQL 上下文 → 豆包实时服务 → 转写、回答文字、PCM 音频。

同一个会话连续两轮：

| 轮次 | 当前代码 | 豆包回答 | 返回 PCM 字节 |
| --- | --- | --- | --- |
| 1 | `int apple = 4;` | `apple` | 57,602 |
| 2 | `int banana = 5;` | `banana` | 67,486 |

上下文读取 5 次，连接正常关闭。测试确认只靠工具调用提示词可能沿用旧代码，最终实现改为收到前端 `context` 后主动发送豆包 `session.update`，并保留只读工具用于按需查询。

真实探针：[check_dingdang_voice_live.py](check_dingdang_voice_live.py)。该脚本需要已迁移的可丢弃测试数据库及语音环境变量，会调用真实豆包服务；不属于默认离线测试集。

本次使用独立 Docker PostgreSQL 容器 `walnut-voice-check-20260909`，测试结束后删除该容器及其匿名数据卷。未启动或修改用户正式演示进程。

接口交接：[DINGDANG_VOICE_FRONTEND_BACKEND_API.md](../agent/docs/DINGDANG_VOICE_FRONTEND_BACKEND_API.md)。前端尚未接入麦克风、播放及按钮。
