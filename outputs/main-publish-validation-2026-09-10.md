# main 整合发布验证（2026-09-10）

本次从已集成前后端的 all 分支整合到 main。远端 main `00052e9` 是整合基线 `0b74899` 的祖先，包含23个已有提交；新增提交包含Bug练习、Book与固定台词音频、可靠性修复、完整接口文档及实现说明。采用普通快进推送，保留分支历史。

## 文档交付

- `walnut-world-backend/docs/frontend-api/README.md`：全产品前端接入入口。
- `walnut-world-backend/docs/frontend-api/HTTP接口参考.md`：28个合同操作的生成式字段参考，并索引6个Demo扩展。
- `walnut-world-backend/docs/frontend-api/Bug军团与书书接口.md`：完整扩展字段、错误码、音频、重试、兼容与验收清单。
- `walnut-world-backend/docs/bug-practice-frontend-handoff.md`：调用顺序与请求/响应示例。
- `walnut-world-backend/docs/architecture/bug-practice.md`：状态机、每局一次、上下文读取、模型、判题及恢复逻辑。

## 已通过

- 后端unit目录与持久启动脚本合同测试：433项通过。
- 本次涉及的Agent上下文、角色、Book、Provider失败及语音教学回归：59项通过，106个子测试通过。
- Godot固定对白音频测试通过：整句显示、鼠标/键盘单句前进、播放结束不自动下一句、系统对白静音。
- Godot crop_agent_bridge测试通过：正式提示及Book文字/语音交付链路。
- 文档重新生成成功：28个合同操作、6个Demo扩展、30份schema校验示例。新入口及配套文档56个本地链接无断链。
- 相关Python文件Ruff检查、Git diff空白检查通过。
- 待发布内容检查未发现本地实际密钥、运行时秘密或超大单文件；本地日志、临时工具和运行状态保留在忽略目录。
- 新Bug流程的真实主关→出题→编译错误/公开错误/隐藏拒绝→正确→总结音频，见 `practice-bug-recheck-2026-09-10.md` 及对应JSON/WAV。

## 未计入通过的旧套件

Agent全量非live runner的测试计数已与新增用例同步：发现641项、排除原有2个显式live项，目标639项。但本机运行旧的 `test_agent_backend_book_outcome_matrix` 时，主关命令在VALIDATING阶段45秒未终态；全套运行已中止，并单独复现该用例失败。

为核对是否由本次修改引入，另从未修改的 `0b74899` 提取Agent基线，在独立测试数据库重跑 `test_book_provider_permanent_mastery_learner_reason_is_never_published`，同样因命令VALIDATING未结束而失败；基线还记录AGENT_TURN_CLAIM_FAILED。该旧套件的本机根因未定位，不能声称全量639项通过。以上433项后端测试、59项Agent定向测试和真实单网关练习验证均独立完成。

## 交付边界

新Bug练习正式前端尚需接入；保留此前已经确认的语音与对白交互。旧Agent自动Bug/Book事件、旧完成台词的兼容处理均在前端文档列明。练习仍为单网关内存Demo，重启/过期需重新进入；不声称生产多实例持久化已实现。
