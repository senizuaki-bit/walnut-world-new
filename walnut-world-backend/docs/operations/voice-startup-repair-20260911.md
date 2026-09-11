# 本地配音启动修复（2026-09-11）

本次用户挑战已通过，原网关 `summary` 返回 HTTP 503 / `BOOK_SPEECH_CONFIGURATION_INVALID`。本机仓库外的 `book-tts.key` 存在，但原启动器没有加载该路径；它无条件启用 doubao 模式，而新 checkout 不含旧的仓库内凭据文件。错误发生在供应商请求之前。此前的进程/端口就绪检查没有覆盖配音配置。

## 修复

- 启动时保留显式配置，其次读取用户目录 `.walnut-secrets/book-tts.key`；实时语音独立读取 `.walnut-secrets/doubao-voice.key`，兼容旧 Agent 目录文件。未配置实时语音时保持关闭。
- 相对路径以启动者目录解析为绝对路径；缺失文件、无效文件、来源冲突不静默回退。
- `-Action Check` 和 `Start` 共用本地配置验证，在运行态文件、Docker、迁移和 Worker 操作前执行。检查不会调用供应商，不能用 CONFIGURED 推导远端权限或余额正常。
- 保存配置摘要，检测已有网关仍持有旧环境，明确拒绝误复用；不会自动终止用户游戏。
- 配置、权限和认证错误标为不可自动重试，前端提示主关与挑战已通过、无需改答案、配音配置修复后再继续。网络类错误仍可重试。
- 回归证明：配音失败后修复同一路径文件，只重新合成配音，判题与总结生成均不重复。

## 验证与边界

- Backend 单元与合同测试：521 通过，含独立 PostgreSQL 数据库的两项合同门禁。测试容器及其临时卷已精确清理。
- 后续独立 speech 接口错误语义测试：9 通过；改动 Python Ruff 通过。
- 新 PowerShell 进程覆盖稳定路径、新 checkout、显式覆盖、关闭模式、无效文件、失效盘符、配置变更摘要与启动前阻断。
- 本机真实书书凭据生成完整 24kHz PCM 音频（86,758 字节）。未输出凭据；该结果不验证独立的实时语音权限。
- Godot 最终完整离线套件 90/90 通过，日志为仓库 `tools/frontend-offline-verified.log`。前两轮均为 89/90，固定录音计时用例失败。定向探针实测：3.036 秒录音在场景计时器结束时只播放至 2.699 秒，循环关闭。测试改为等待真实播放停止和完成事件，并保留独立墙钟超时；连续两轮通过，刻意开启循环的反向探针被拒绝。两个真实网关 opt-in 场景不在离线套件中执行。

当前已运行的旧网关不会热加载新环境。重启仍会使单进程内存中的 Bug entry 过期；此修复没有将练习状态改成持久化，也没有回写用户的主关结果。重启前的本地代码与进度备份位于 `%LOCALAPPDATA%/WalnutWorld/persistent-play/voice-repair-backup-20260911/`。不能把本地备份当作已实现跨网关重启恢复。

## 同日交互链路复查

用户再次反馈后，真实 WebSocket 确认返回 VOICE_DISABLED。客户端在同一帧收到错误和关闭时先处理关闭，丢失具体错误。现在先消费最后的数据包，再判断传输关闭。现有书书密钥已单独验证具有实时权限（VOICE_CONNECTED，163726 字节完整音频），并配置到用户目录的实时语音文件；不将一类服务权限推断为另一类服务权限。

真实 Relay 记录显示：08:52 UTC 的三次出题都缺少大于30的缺口；08:55 UTC 的总结多出 type 字段。前者改为服务端构造数值边界、模型只写针对实际代码的文案；后者增加已确定格式错误的有限修复。严格 schema 和判题门禁均保留。

提示按钮问题可通过“编译拒绝已经显示、后台教学反馈仍未结束时点击”稳定复现。现在立即显示等待并排队一个请求，上一操作结束后自动请求；重复点击不会增加请求。

新增故障回归覆盖错误帧与关闭同帧、编译反馈期间点击提示、256组数据边界、真实模型解析器拒绝额外字段后的自动修复、连接丢失时保持原答案身份，以及持续失败时有限重试。

最终验证：

- 后端完整 unit + contract：524项通过；此后新增停止进程竞态与非格式错误不重复派发检查，相关13项全部通过。实际重启遇到 launcher 在子进程停止后自行退出，停止器已改为持有原 Process 对象，只容忍已退出对象，仍传播真实停止失败。
- Godot 离线套件：90/90。完整日志在忽略目录 `tools/interaction-offline-tests.log`；后端日志为 `tools/interaction-backend-tests.log`。
- 新启动进程：书书和实时语音均 CONFIGURED，网关实际返回 voice.ready。
- 真实语音往返：用合成测试语音作为 PCM 输入，经正式鉴权 WebSocket → 豆包 ASR → 回答文字 → 回答音频，返回102个识别字符（含增量）、58个回答字符、551728字节 base64 音频。该探针验证软件收发链路，不代替物理麦克风验收。
- Godot 显式在线验收：真实 Build/Activation/Run、模型出题、真实 C++ 编译拒绝、正确答案及隐藏边界判题、总结全文和完整PCM全部通过；练习未修改主关草稿与世界快照。模型一次格式拒绝被自动修复，无人工重试。结果见当前任务09:17～09:18 UTC的在线验收输出；原日志路径后来被隔离拦截探针覆盖，不能再当作通过日志引用。
- 独立 PostgreSQL 测试容器及其临时卷已按精确ID删除；用户数据库和主服务保留运行。重启前的原代码、练习状态和已完成总结保存在 `%LOCALAPPDATA%/WalnutWorld/persistent-play/interaction-repair-backup-20260911/`。

## 运行准备冲突与防回归

用户随后遇到“运行准备暂未完成”。只读检查确认真实注册版本31、窗口缓存30；09:17 UTC的在线验收确实使用了玩家相同账号并更新了注册版本。验收隔离不足是本次触发因素。服务按正确的并发控制返回409；客户端恢复代码却读取 `submission.error.code`，真实网关保留的路径是 `submission.error.error.code`。既有测试直接返回扁平错误，因此没有发现这条分支从未执行。UI同样读取扁平错误，连具体错误编号也未显示。

修复与约束：

- 激活恢复识别真实 ErrorResponse，也识别已接受命令在后台被 REJECTED 的版本冲突；至多读一次最新权威并重试一次，新版本使用新幂等键。
- 必须仍是同一 actor、content、activation scope，且版本确实前进；身份、内容、范围不一致、鉴权失败或连续冲突均停止，不伪造 ACTIVE。
- 未知提交状态继续查询原命令，不按“冲突”重新执行。
- 场景读取 ErrorResponse 内的 ContractError 后再显示错误分类，保留原代码与可查询的错误编号。
- 回归中409经过真实 `YayaAgentApiGateway` 的错误结构及响应头校验；另覆盖提交后冲突、连续冲突、版本不变、账号/关卡/范围变化、鉴权失败。
- 显式在线练习脚本在创建场景或写请求前拒绝玩家常用8790端口、已保存的玩家服务地址及外部地址。必须声明专用本地测试网关，配套数据库也必须单独准备。实际对玩家地址的反向探针已被拦截。

新增后前端全套91/91通过，日志 `tools/activation-offline-tests.log`。刷新仅停止并重新打开玩家窗口，网关监听进程ID保持不变；本局练习服务器记录仍在。最近一次通过编译的源码另存于 `%LOCALAPPDATA%/WalnutWorld/persistent-play/activation-repair-backup-20260911/latest-submitted.cpp`。

复查结论：测试替身必须保留真实协议层次；等待、错误、重试和终态必须一起测试；精确规则由程序构造校验；真实依赖故障须有限恢复；在线验收不能共享正在玩的账号和状态。外部供应商的可用性不由本项目保证，持续不可用仍应显示明确反馈并保留结果。

## main 发布前的首次安装验收

按用户要求，将正在使用的 all 分支历史和上述修复完整推进 main，不覆盖主线历史。新增 [首次安装指南](first-run-windows.md)、`setup-play.ps1`、Windows Python 3.12.13 的固定依赖清单和根目录 GitHub Actions 回归门禁。

启动前新增模型真实配置解析、密钥文件 ACL、Python/Godot 版本、Docker Linux 引擎和固定镜像检查。配置摘要包含密钥文件实际内容的摘要，模型/语音轮换及后端源码变更后拒绝静默复用旧服务。检查不会连接供应商或数据库。

以全新工作目录 `walnut-first-run-validation` 验证，未复制旧 `.venv` 或 `.godot`：

- 自动安装 Python 3.12.13、固定的第三方依赖、本仓库 Agent 与后端；依赖一致性检查通过。
- 从官方 GitHub Release 下载 Godot 4.7.1，SHA-256 与固定发布摘要一致。首次资源导入暴露 PowerShell 对图形 exe 不等待的问题；改为显式等待进程退出，再重新加载已导入工程检查错误，最终 setup 通过。
- 专用实例使用独立容器/卷、55439/21999/18790 端口和独立 Godot 用户数据目录，从空库自动迁移到 head、首次 seed、权威验证、relay、Gateway 和两类 Worker 全部就绪。玩家服务未被验收脚本写入。
- 后端全量 unit + contract **539项通过**（1项依赖弃用警告），数据库合同使用该实例内另外创建的回归数据库；Agent public-copy **11项及24个 subtests通过**。
- 最终 Godot 离线全套 **91/91通过**。在线测试的专用端口最初被固定传输策略阻止，现仅在两个显式验收开关与精确本地 origin 匹配时开放，保留 URL、Token 和请求约束；固定合同文件未修改。另验证普通启动不能使用该例外，畸形地址、伪装玩家端口和远端地址均被拒绝。
- 真实在线闭环退出码0：main-build-activation-run、provider-challenge、compile-rejection、correct-answer-main-state-isolation、summary-text-audio全部PASS，无人工重试。测试题 `challenge_2cee765a39b7415fbf798ac161e4ea21`，运行 `run_ad9b45f945d05f28148ad768`。

本地原始验证日志位于忽略目录 `tools/first-run-setup-final.log`、`tools/first-run-backend.log`、`tools/first-run-frontend-final.log` 和 `tools/first-run-live.log`；不把私有运行态、真实 Key 或数据库凭据纳入发布。GitHub Actions 分 Windows 离线回归和 Linux 真实 PostgreSQL 合同两项作业，无需配置供应商 Key。
