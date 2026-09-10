# Bug 军团与书书：前端完整接口参考

版本：2026-09-10。以本仓库后端实现为准。本协议为单题 Demo 扩展，与现有主关 HTTP 合同并行；前端练习界面需自行接入。

配套：[全产品接口入口](README.md)、[调用顺序与请求示例](../bug-practice-frontend-handoff.md)、[后端实现逻辑](../architecture/bug-practice.md)。

## 一、约定与接口目录

每局流程：进入 → start → 主关正确 → prepare → Bug 一题（可重试多次）→ answer 正确 → summary → 同时展示总结并播放音频。

这里的“一局”由 `entry_id` 标识，一个 entry 最多生成一道挑战；不是一次点击提交，也不是长期使用的 session。服务端以 entry 隔离本轮错题上下文，前端以 entry/challenge 去重登场动画。

基础地址由 `API_BASE_URL` 配置，本地默认为 `http://127.0.0.1:8790`。所有请求使用现有游戏 JWT；不能使用模型供应商密钥。请求头格式沿用[公共约定](README.md#0-所有模块共用的约定)。本组接口全部为 POST，成功 HTTP 200，JSON 无 `data` 外壳，响应 `Cache-Control: no-store`。

下表中的 `P` 为 `/product-experience/v1/sessions/{session_id}/practice-entries/{entry_id}`。

| 完整操作 | 用途 | 请求 body | 返回 |
| --- | --- | --- | --- |
| POST P/start | 开始本局；重复调用恢复进度 | `{}` | EntryStatus |
| POST P/status | 查询已创建局的进度 | `{}` | EntryStatus |
| POST P/prepare | 主关已正确后生成新题 | `{run_id}` | Challenge |
| POST P/answer | 提交挑战 C++ 并获取最终判题 | AnswerRequest | AnswerResult |
| POST P/summary | 挑战通过后生成总结和音频 | `{challenge_id}` | PracticeSummary |
| POST /product-experience/v1/sessions/{session_id}/agent-interactions/{interaction_id}/speech | 为旧版已发布 Book 总结合成音频 | `{}` | LegacySpeech |

路径参数：`session_id` 来自 Bootstrap/Workspace，必须为当前身份的 ACTIVE 会话。`entry_id` 为前端创建的 UUID 去连字符后 **32 位小写十六进制**；新局新 ID，网络重试保留 ID。`challenge_id` 由 prepare 返回，不自行构造。

start 必须先于本轮第一次主关提交；本轮起点使用服务端时间。不要每次 answer 或断网重试都调用新 entry 的 start。

## 二、请求字段

### prepare

| 字段 | 类型 | 必填 | 来源及约束 |
| --- | --- | --- | --- |
| run_id | string | 是 | 使用主关接口实际返回的 Run ID；最长 128 字符、字母数字/下划线/连字符；服务端查验本人、本 session、本局创建后的 SUCCEEDED Run |

服务端不接受前端声称成功的布尔值、失败次数或世界结果。主关标准流程见[编译与激活](README.md#3-代码编译与技能激活)和[正式执行](README.md#4-技能执行与游戏世界)。主关 `Command.links.run` 已就绪时即可读取 Run，不必等旧 Book 交互结束后才准备 Bug。

### answer：推荐与正式编辑器复用源码包

| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| challenge_id | string | 是 | 本 entry 的挑战 ID |
| answer_id | string | 条件 | 32 位小写十六进制；可改用 Idempotency-Key 请求头，两处都有时必须相同 |
| source_bundle | object | 条件 | 推荐格式；与 source 二选一 |
| source_bundle.language | string | 是 | `CPP20` |
| source_bundle.entrypoint | string | 是 | `main.cpp` |
| source_bundle.files | array | 是 | 此 Demo 恰好 1 个文件 |
| files[0].path | string | 是 | `main.cpp` |
| files[0].content | string | 是 | 学生编辑后的完整源码 |
| files[0].content_sha256 | string | 是 | content 的 UTF-8 字节 SHA-256，小写 64 位十六进制 |
| compiler_profile | string | 否 | 省略默认 `YAYA_CPP20_SAFE_V1`；源码包提交若传其他值则拒绝 |
| test_suite_version | string | 否 | 省略默认 `bug-practice-v1`；源码包提交若传其他值则拒绝 |
| source | string | 条件 | 简化提交时直接传完整源码；不需要前端提供哈希 |

推荐请求/响应 JSON 已在[接入说明](../bug-practice-frontend-handoff.md#3-学生提交练习answer)列出。source 与 source_bundle 同时传入时，服务端要求内容一致；前端应只传一种，避免不同步。

整份原始请求 body 最多 **40000 字节**，源码内容最多 **32000 UTF-8 字节**，每题最多 100 次完成判题。JSON 转义也计入 body 大小，因此源码未达上限时，经过大量转义的请求仍可能超出 body 限制。body 必须为严格 UTF-8 JSON 对象，拒绝重复键、NaN 等非法常量。

同次提交重试必须复用 answer_id 和源码。编辑源码后点击提交是新一次尝试，生成新 answer_id。按钮等待期间应防止重复点击；即使同 ID 并发到达，服务端也只判一次。编译或测试不通过属于 HTTP 200 的业务结果，不应显示“连接失败”。

### summary

唯一必需业务字段为 `challenge_id`。无需传文字、音色、答案或“通过”标志，后端自行读取已通过的挑战与代码。只有正式 summary 完整返回后才能展示最终总结。

## 三、完整响应字段

### EntryStatus（start/status）

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| entry_id | string | 当前局 ID |
| trigger | string | 固定 `main_run_succeeded` |
| phase | enum | 下表中的状态 |
| run_id | string/null | 当前局已绑定的主关 Run，未绑定为 null |
| challenge | Challenge/null | 成功生成后返回完整题目；没有答案或总结音频 |
| attempts | integer | 当前挑战已完成判题次数；基础设施故障不计数 |
| passed | boolean | 挑战是否已正确通过 |

| phase | 准确含义 | 前端操作 |
| --- | --- | --- |
| WAITING_MAIN | 未绑定有效主关 Run | 继续主关编辑/提交 |
| CHALLENGE_PENDING | 已绑定 Run，题目未成功发布；可能在生成或上次失败 | 等待原请求，必要时同 run 重试 prepare |
| CHALLENGE_READY | 题目已生成，尚未通过 | 展示题目与练习编辑器 |
| SUMMARY_PENDING | 挑战通过，完整总结音频尚未成功返回 | 调用/重试 summary |
| COMPLETED | 总结和音频曾完整生成成功 | 需要恢复内容时再次 summary |

status 是快照，不会启动新任务。判题进行中仍可能是 CHALLENGE_READY，attempts 只计已结束的判题；UI 自己保留提交中的等待状态。COMPLETED 不包含“用户已播放完”含义，音频缓存淘汰后再次请求仍可能需要合成。

### Challenge（prepare、EntryStatus.challenge）

| 字段 | 类型 | 含义/约束 |
| --- | --- | --- |
| challenge_id | string | 后端生成 `challenge_` 加 UUID |
| run_id | string | 关联的正确主关 Run |
| source | string | 固定 `provider`，表示模型真实出题 |
| failure_count | integer | 本局用于出题的错误证据条数，最多 40；不是触发阈值 |
| title | string | 2–35 字符，题目名称 |
| brief | string | 10–150 字符，变式情境 |
| focus | string | 2–100 字符，本次练习关注点；无错误时为巩固 |
| moisture | integer[8] | 0–100 的当前湿度 |
| target | integer[8] | 0–100 的目标湿度 |
| starter_source | string | 预置数组、输入读取与待写逻辑的完整 C++ 起始代码 |
| rules | object | 确定性的判题规则，不以模型 brief 覆盖 |
| rules.language | string | `cpp`，展示标签；源码包仍用 `CPP20` |
| rules.plot_count | integer | 8 |
| rules.gap | string | `target[i] - moisture[i]` |
| rules.actions | string[3] | gap>=30 输出2份，0<gap<30 输出1份，gap<=0 不输出 |
| rules.output | string | 下标从0递增，每行 WATER i units，以换行结束；保留输入代码 |
| starter_skill | object | 与正式编辑器起始技能相同的字段结构，见下段 |

starter_skill 含 `skill_id="skill_bug_practice"`、`display_name=title`、`source_bundle`、`compiler_profile="YAYA_CPP20_SAFE_V1"`、`test_suite_version="bug-practice-v1"`。source_bundle 与 answer 的结构一致，已包含正确哈希。前端编辑后重新计算哈希。

请为挑战保留独立源码缓冲区，不能覆盖主关 Draft。挑战不调用主关 Build→Activate→Run，不改变世界，不发布可激活技能。起始代码的输入读取已提供，学生无需额外学习输入语法，但应保留，以便隐藏数据替换数组。

### AnswerResult

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| correct | boolean | 判题结果权威字段 |
| stage | string | 成功 PASSED；失败常见 VALIDATE_SOURCE / COMPILE / PUBLIC_TEST / HIDDEN_TEST |
| message | string | 可给学生展示的概括反馈 |
| build_id | string | 本次真实沙箱构建标识，用于排查；不是正式 Skill Build 查询资源 |
| status | enum | SUCCEEDED / REJECTED |
| compiler_profile | string | YAYA_CPP20_SAFE_V1 |
| test_suite_version | string | bug-practice-v1 |
| diagnostics | array | `{code:string,message:string}`；成功为空，隐藏测试诊断不泄露测试数据 |
| attempts | integer | 包含本次的有效尝试次数；重放返回原结果中的次数 |
| challenge_id | string | 当前挑战 |

本接口 HTTP 200 一次返回最终判题结果，不是正式 Build 的 HTTP 202 Command 模式。不能将这里的 build_id 交给 `/v1/skill-builds/{id}` 或激活接口。

### PracticeSummary

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| challenge_id | string | 当前已通过挑战 |
| message | string | Book 生成的完整总结，后端 schema 限制30–420字符，提示目标120–220中文字 |
| source | string | provider |
| speaker | string | ICL_uranus_zh_male_bujiqingnian_tob（不羁青年） |
| text_sha256 | string | message UTF-8 字节 SHA-256 |
| format | string | pcm_s16le |
| sample_rate | integer | 24000 |
| audio_base64 | string | 完整音频 Base64；解码为单声道16-bit小端 PCM，无 WAV 文件头 |

前端校验 text_sha256、解码音频成功后，一起展示全文并播放。不要把原始 PCM 当 MP3/WAV 文件读取。Godot 可使用 `AudioStreamWAV`，设置 FORMAT_16_BITS、mix_rate=24000、stereo=false、data=解码字节；浏览器需要将有符号16位样本归一化后写 AudioBuffer。保留重播时去重，防止同时开启两路播放。

## 四、恢复、超时与错误码

建议 prepare/answer/summary 总请求超时至少300秒，期间显示等待状态。仅对可重试依赖故障进行有限重试并保留相同业务 ID；每个网络 attempt 的请求头 ID 可以更新。不要无限循环重试配置错误。

操作已启动后取消 HTTP 等待不会取消网关内部任务；重试等待同局串行任务并复用结果。summary 模型生成失败可重新生成；已获得合规文字后，音频失败只补合成，不再重写总结。语音有缓存，缓存命中不排队等待其他合成。

本组练习接口业务错误：`{"code":"PRACTICE_...","retryable":false}`。没有旧合同的统一 error 外壳；认证失败仍沿用公共认证错误格式。

| HTTP | code | 处理 |
| --- | --- | --- |
| 400 | PRACTICE_ENTRY_INVALID / PRACTICE_RUN_INVALID / PRACTICE_ANSWER_INVALID | 修正业务 ID |
| 400 | PRACTICE_REQUEST_INVALID / PRACTICE_ACTION_INVALID | 修正 body 或操作路径 |
| 400 | PRACTICE_REQUEST_TOO_LARGE / PRACTICE_SOURCE_INVALID | 修正大小、源码包、哈希或配置 |
| 404 | PRACTICE_SESSION_UNAVAILABLE | 会话不存在、非当前身份或已失活；恢复会话 |
| 409 | PRACTICE_MAIN_NOT_PASSED | Run 无权访问、不属于会话或未正确通过；核对正式 Run |
| 409 | PRACTICE_PREVIOUS_ENTRY_RUN | Run 早于本局开始，不能拿旧通关触发新局 |
| 409 | PRACTICE_RUN_CONFLICT | 本局已绑定另一个有效 Run；恢复原挑战 |
| 409 | PRACTICE_CHALLENGE_MISMATCH | 挑战尚未生成或 ID 不匹配 |
| 409 | PRACTICE_ANSWER_CONFLICT | 同答题 ID 换了源码，或头/body 中 ID 不一致 |
| 409 | PRACTICE_ALREADY_PASSED | 通过后不能提交新答案；转 summary |
| 409 | PRACTICE_ATTEMPT_LIMIT | 本题完成判题达到100次 |
| 409 | PRACTICE_NOT_PASSED | 不能提前请求总结，或 challenge ID 不匹配 |
| 410 | PRACTICE_ENTRY_EXPIRED | entry 不存在、超过6小时或被淘汰；新局从 start 和主关开始 |
| 503 | PRACTICE_MODEL_UNAVAILABLE / PRACTICE_PROBLEM_INVALID | 出题/总结模型失败或题目不合规；保留 ID 重试 |
| 503 | PRACTICE_JUDGE_UNAVAILABLE / PRACTICE_UNAVAILABLE | 依赖或判题服务不可用；保留源码和 ID 重试 |
| 503 | BOOK_SPEECH_PROVIDER_UNAVAILABLE / BOOK_SPEECH_PROVIDER_REJECTED | 语音供应商超时或拒绝，重试或联系后端 |
| 503 | BOOK_SPEECH_INVALID_RESPONSE / BOOK_SPEECH_INCOMPLETE / BOOK_SPEECH_AUDIO_TOO_LARGE | 音频校验失败，不播放部分音频 |
| 503 | BOOK_SPEECH_DISABLED / BOOK_SPEECH_CONFIGURATION_INVALID / BOOK_SPEECH_RESOURCE_NOT_GRANTED / BOOK_SPEECH_TEXT_INVALID | 后端配置、权限或文字限制问题；保留进度，待修复再试 |

练习接口对上述503返回 retryable=true，但配置问题需要后端修复后才有重试意义。服务端不会把密钥或供应商原始错误返回前端。

Demo 使用单网关进程内存，最多128局，6小时有效；重启或淘汰后丢失练习进度。新局只重置练习上下文，**不清空历史主关数据库、旧提示等级或持久化学习档案**。多进程共享与长期恢复尚未实现。

## 五、旧 Book 音频与固定角色台词

旧版 `/agent-interactions/{interaction_id}/speech` 只读取本人 session 下、role=book_agent 且 response_type=growth_summary 的持久交互。请求体传 `{}`，不能替换文字和音色。成功返回 `interaction_id`、speaker、text_sha256、format、sample_rate、audio_base64；不含 message，原文来自该 Interaction 的 feedback.message。服务错误返回503和 `{code:"BOOK_SPEECH_..."}`，没有 retryable 字段；资源/认证错误沿用旧合同。

新 Bug 流程必须使用 P/summary，不能用旧 speech 端点绕过挑战门槛。旧 speech 保留给旧版界面和已归档交互，不是新一局完成信号。

固定角色台词已打包在 `walnut-world-frontend/assets/audio/dialogue/`，无需每次调用语音接口：芽芽开场使用用户提供的3段WAV，叮当固定台词为胡子叔叔，书书为不羁青年。系统台词无配音。现有对白交互是整句显示并同步播放；点击停止当前句音频并前进一句；音频结束等待点击，不自动下一句。

新流程接入时，已有 `yaya_complete` 固定语音仍提及“现在请书书归档”，属于旧流程台词；前端应调整播放时机或更新该句并重新生成配音，避免 Bug 挑战之前宣布归档。

## 六、前端交付验收清单

1. start 成功后再允许主关提交，entry_id 不因网络重试改变。
2. 正式 Run SUCCEEDED 后自动 prepare，与失败阈值无关；主动提示始终展示叮当。
3. 新界面忽略旧管线自动 bug_agent 提问及主关自动 Book 展示，但仍消费列表并推进游标；不要停止整个事件同步。
4. 一局只有一个挑战和一次登场；刷新恢复 CHALLENGE_READY 时不重复演登场。
5. 不覆盖主关代码；从 starter_skill 初始化独立编辑器，按新的源码计算哈希。
6. 答错保留原题、原代码；同一次重试复用 ID，新修改后提交换 ID。
7. 验证0/30边界、编译错误、硬编码答案、网络取消和并发同ID提交。
8. correct=true 后关闭继续答题入口，再 summary；总结等待/失败不撤销通过状态。
9. 文本和完整音频就绪后一起展示；重试或恢复不叠加播放。
10. 重新进入生成新 entry；410 明确提示重开本局，不能反复用同一个失效 entry 重试。
