# Bug 军团变式题：后端接口与前端接入说明

更新：2026-09-11。`all` 分支已接入正式前端，验证范围及真实配音待办见[前端联调记录](../../walnut-world-frontend/docs/testing/bug-practice-integration.md)。完整字段、错误码和验收清单见[前端接口参考](frontend-api/Bug军团与书书接口.md)，后端设计见[实现逻辑](architecture/bug-practice.md)。

## 产品流程

进入游戏 → 建立本轮 entry → 正常编译、提交主关 C++ → 主关 Run 为 `SUCCEEDED` → Bug 军团生成一道同难度 C++ 变式题 → 编译、判题通过 → 书书生成本轮总结和完整语音 → 前端统一展示。

Bug 军团不再由“给我提示”入口召唤。主动提示和语音求助均使用叮当师傅。新练习流程不依赖累计答错次数；本轮有错误时依据错误代码和反馈出题，没有错误时做同知识点巩固。

**每局只出现一次，即每个 entry 最多一题。**答错继续同一道题，不重新召唤或换题；通过后进入书书阶段。重复 prepare 返回原题；同局另传一个 run 不会创建第二题，而是返回 RUN_CONFLICT。恢复进度时，前端按 phase 恢复，不重新播放已播放的登场动画。

**兼容边界：**原有主关 AgentTurn / AgentInteraction 流程保留旧版自动失败教学、完成总结事件，以兼容旧客户端和历史回放。接入本协议的前端需要忽略这些事件中的旧版自动 Bug 提问和主关完成 Book 展示，统一使用本协议的 `prepare` / `summary` 结果。不要将旧 `task_completed`、旧 Book interaction 或主关 `ok: true` 当作新流程结束。`ok` 只说明一次工作流闭环，主关正确性以服务端 Run 为准。本协议的 `summary` 会在服务端拒绝未通过变式挑战的请求。

## 地址与认证

本地 API：`http://127.0.0.1:8790`。全部为 `POST`，前缀：

```text
/product-experience/v1/sessions/{session_id}/practice-entries/{entry_id}/
```

使用现有登录 token 和请求头：

```text
Authorization: Bearer <现有 token>
Content-Type: application/json
X-Schema-Version: 1.0.0
X-Request-Id: req_<每次请求的新标识>
X-Trace-Id: trace_<标识>
X-Correlation-Id: corr_<标识>
```

`entry_id` 为 32 位小写十六进制（UUID 去掉连字符）。每次真正进入/重新开始游戏创建一个新值；网络重试保留原值。`session_id` 使用现有游戏会话。所有读取均验证 tenant、actor 和活动 session，服务端不接受前端传入的成功次数或失败次数。

## 调用顺序与响应

### 1. 进入：`start`

请求 `{}`。必须在本轮第一次主关提交前完成。

```json
{
  "entry_id": "0123456789abcdef0123456789abcdef",
  "trigger": "main_run_succeeded",
  "phase": "WAITING_MAIN",
  "run_id": null,
  "challenge": null,
  "attempts": 0,
  "passed": false
}
```

重复 `start` 返回当前进度，不会清空本轮答题次数。新 entry 只读取它创建之后的错误记录，不删除持久化世界或历史学习数据。

### 2. 主关成功后出题：`prepare`

请求 `{"run_id":"run_..."}`。后端验证该 Run 属于本人和当前 session、产生于 entry 创建之后且状态为 `SUCCEEDED`。旧通关记录和失败 Run 均不能触发新题。

响应示例（具体文案和数组由出题 Agent 生成）：

```json
{
  "challenge_id": "challenge_...",
  "run_id": "run_...",
  "source": "provider",
  "failure_count": 1,
  "title": "雨后的新试验田",
  "brief": "另一片农田也需要按各自的目标湿度照顾。",
  "focus": "同下标数组配对与缺口边界",
  "moisture": [10, 50, 70, 30, 60, 20, 55, 65],
  "target": [50, 65, 60, 60, 60, 70, 70, 70],
  "starter_source": "完整 C++ 起始代码……",
  "rules": {
    "language": "cpp",
    "plot_count": 8,
    "gap": "target[i] - moisture[i]",
    "actions": [
      "gap >= 30: WATER i 2",
      "0 < gap < 30: WATER i 1",
      "gap <= 0: 不输出"
    ],
    "output": "按下标从0到7，每个动作单独一行，以换行结束。保留预置的输入读取代码。"
  }
}
```

前端展示题目、数组、规则并将 `starter_source` 放入独立练习编辑器。保留主关原代码，不用主关 Build/Activate/Run 接口提交这道练习，也不修改世界状态。

响应同时提供 `starter_skill`，字段与正式编码界面的起始技能一致，推荐复用现有编辑器读取这部分：

```json
{
  "skill_id": "skill_bug_practice",
  "display_name": "本次题目名称",
  "source_bundle": {
    "language": "CPP20",
    "entrypoint": "main.cpp",
    "files": [{"path":"main.cpp","content":"完整起始代码","content_sha256":"实际内容的64位SHA-256"}]
  },
  "compiler_profile": "YAYA_CPP20_SAFE_V1",
  "test_suite_version": "bug-practice-v1"
}
```

题目保持 8 块土地、循环、同下标数组及 0/30 分级，不引入新知识。起始代码已写好输入读取，学生只需完成浇水逻辑。判题器会替换输入数组，防止硬编码答案通过；输出按下标递增，严格为 `WATER i units` 加换行。后端不返回标准答案或隐藏用例。

`failure_count` 是本次用于出题的错误证据条数（最多最近 20 个失败 Run 加 20 个失败 Build），不是触发阈值。重复 `prepare` 返回同一挑战；同 entry 换 run 会返回冲突。生成失败可对原 entry/run 重试，不需重新提交主关。模型草稿需通过结构和边界校验，最多自动修正两次后才返回可用题目。

### 3. 学生提交练习：`answer`

推荐沿用正式代码提交的数据格式：把编辑后的完整代码放进 `source_bundle.files[0].content`，重新计算其 UTF-8 SHA-256。此 Demo 支持单文件 `main.cpp`。提交示例：

```json
{
  "challenge_id": "challenge_...",
  "answer_id": "abcdef0123456789abcdef0123456789",
  "source_bundle": {
    "language": "CPP20",
    "entrypoint": "main.cpp",
    "files": [{"path":"main.cpp","content":"学生完整代码","content_sha256":"实际内容的64位SHA-256"}]
  },
  "compiler_profile": "YAYA_CPP20_SAFE_V1",
  "test_suite_version": "bug-practice-v1"
}
```

支持用 `Idempotency-Key` 请求头提供同一个 32 位答题 ID，省略 body 的 `answer_id`；两处同时提供时必须相同。源码包使用正式 Build 的哈希校验函数，哈希不匹配会在编译前拒绝。

也兼容简化的纯源码请求（`source` 与 `source_bundle` 二选一）：

```json
{
  "challenge_id": "challenge_...",
  "answer_id": "abcdef0123456789abcdef0123456789",
  "source": "完整 C++ 源码"
}
```

`answer_id` 同样为 32 位小写十六进制。一次用户提交一个新值；该次网络重试必须保留同值和同源码。换源码却复用 ID 会返回冲突。

请求等待被取消时，已开始的出题、判题、总结仍在网关进程内执行并保存结果。客户端用原 entry / run / challenge / answer ID 重试，会等待同一局的操作完成并复用结果；不会因取消请求再次编译或重新生成一题。网关关闭或重启仍遵循下述 Demo 状态失效规则。

```json
{
  "correct": false,
  "stage": "PUBLIC_TEST",
  "message": "输出还没有符合全部规则。检查同一下标、缺口为 0 和 30 时的动作，以及每行 WATER 的格式。",
  "attempts": 1,
  "challenge_id": "challenge_..."
}
```

答错返回 HTTP 200、`correct: false`，保留编辑器并允许重试。成功为 `correct: true`、`stage: "PASSED"`。重复请求返回原判题结果，不重复累计次数。通过后不接受新答案。源码最多 32000 UTF-8 字节，每题最多 100 次已完成判题。

响应还包含 `build_id`、`status`（`SUCCEEDED` / `REJECTED`）、`compiler_profile`、`test_suite_version` 和 `diagnostics: [{"code":"COMPILE_ERROR","message":"具体编译诊断"}]`，可复用正式界面的诊断展示。隐藏测试只返回脱敏诊断。这里 HTTP 200 返回最终判题结果；不是正式 Build 的 202 异步任务，不要把练习 build_id 传给正式 `/v1/skill-builds/{id}` 查询或激活。前端需增加薄接口适配，编辑器与源码结构可以复用。

实际使用现有固定镜像的 C++20 Docker 编译器，运行公开和隐藏边界测试，关闭网络，并限制编译、运行时间、内存与输出。基础设施故障返回可重试错误，不计作一次错误答案。

### 4. 挑战通过后：`summary`

请求 `{"challenge_id":"challenge_..."}`。没通过则返回 `PRACTICE_NOT_PASSED`，不会调用总结或语音服务。

```json
{
  "challenge_id": "challenge_...",
  "message": "本轮生成的书书总结……",
  "source": "provider",
  "speaker": "ICL_uranus_zh_male_bujiqingnian_tob",
  "text_sha256": "总结 UTF-8 字节的 SHA-256",
  "format": "pcm_s16le",
  "sample_rate": 24000,
  "audio_base64": "完整音频的 Base64"
}
```

书书读取主关代码、本轮错误证据、变式题、练习通过的代码及实际尝试记录，生成新的总结。使用豆包语音合成“不羁青年”。返回音频为单声道、24kHz、16-bit signed little-endian 原始 PCM，不含 WAV 文件头。

**前端收到完整响应后，再一起展示文字并播放音频。**语音失败时不会提前返回半份成功结果；重试只补合成，已生成总结不会改写，代码也不会重新判题。同一文本音频由现有合成服务缓存。建议客户端总请求超时至少 300 秒，期间展示等待状态并防止重复点击。

如果模型返回的 JSON 不合规或模型服务失败，保留挑战通过状态，summary 重试使用新的生成请求，避免反复读取同一份失败响应；一旦合规文字生成成功，后续只补语音。总结上下文包含最近 10 次练习的源码与诊断，并单独传入总尝试次数。

### 5. 恢复进度：`status`

请求 `{}`，响应结构与 `start` 一致，`challenge` 有题目后返回完整题目，但不返回学生答案或总结音频。

| phase | 前端动作 |
| --- | --- |
| WAITING_MAIN | 继续主关 |
| CHALLENGE_PENDING | 出题正在进行或上次中断，对同一 run 重试 prepare |
| CHALLENGE_READY | 展示 challenge，继续答题 |
| SUMMARY_PENDING | 已答对，调用/重试 summary |
| COMPLETED | 总结与音频至少完整生成过一次，可调用 summary 重新获取 |

`status` 本身不会启动生成或判题，也不能取代首次 `start`。

## 错误和运行范围

本接口业务错误响应为 `{"code":"PRACTICE_...","retryable":false}`；认证错误沿用项目现有认证响应。

| HTTP | 示例 | 处理 |
| --- | --- | --- |
| 400 | REQUEST_INVALID、RUN_INVALID、SOURCE_INVALID、ANSWER_INVALID | 检查请求和大小 |
| 401 | 现有认证错误 | 恢复登录 |
| 404 | PRACTICE_SESSION_UNAVAILABLE | 检查会话归属及状态 |
| 409 | MAIN_NOT_PASSED、PREVIOUS_ENTRY_RUN、RUN_CONFLICT、ANSWER_CONFLICT、NOT_PASSED | 按流程修正，不盲目重试 |
| 410 | PRACTICE_ENTRY_EXPIRED | 重新进入，生成新 entry 并从主关开始 |
| 503 | MODEL_UNAVAILABLE、JUDGE_UNAVAILABLE、PROBLEM_INVALID、BOOK_SPEECH_* | 保留 ID 与源码，按 retryable 重试；配置类语音错误需后端恢复配置 |

表中省略的业务错误前缀为 `PRACTICE_`。响应禁止缓存，不返回服务密钥或供应商原始错误。

Demo 练习状态存于单个网关进程内，保留 6 小时，最多 128 个 entry，超额淘汰最早创建项。网关重启后需重新进入；当前不支持多网关进程共享进度。历史主关记录和世界数据不受影响。生产持久化、多实例部署需另做数据库接入。

## 验证与交付文件

追加故障复查后：64 项测试、42 个子测试通过；修改模块通过 Ruff 检查。

最终真实联调通过：正式主关提交成功 → Agent 新题 → 源码包答错 → 同 ID 重试仍计一次 → 源码包答对（公开、隐藏测试均通过）→ 总结模型一次失败后重试成功 → 返回 2,149,804 字节完整 PCM。通关后再次 prepare 返回原 challenge，start 返回 COMPLETED，没有创建第二题。

- `tests/unit/test_bug_practice.py`：主关门槛、总结门槛、错误计数隔离、并发只生成一题、答题 ID 冲突、源码包哈希校验、语音失败重试、模型失败后重新生成、HTTP 认证与过期。
- `tests/unit/test_practice_reliability.py`：取消出题/判题/总结请求后的任务复用、缓存语音不被其他生成阻塞、非法 run_id 提前拒绝。
- `agent/tests/test_hint_role_independence.py`：主动提示不因错误次数改成 Bug。
- 本地真实调用证据：仓库 `outputs/practice-api-check.json`（生成题目、错误和正确判题、真实总结，已去掉音频 Base64）。
- 本地真实总结音频：仓库 `outputs/bug-practice-book.wav`。
- 已保留之前确认的角色固定音频、整句展示、点击跳过当前句且不自动下一句的改动；本次未交付新的正式前端练习组件。
