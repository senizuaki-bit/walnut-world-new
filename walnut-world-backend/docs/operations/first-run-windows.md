# Windows 首次安装、启动与更新

适用：本仓库 `main`，Windows 10/11 x64，Windows PowerShell 5.1。游戏前端、后端、Agent 合同均在仓库里；教师工作台是独立可选应用，不影响学生游戏。不要分别下载不同分支的前后端拼装。

完整体验需要三类服务：DeepSeek 文本模型（提示、出题、总结），豆包书书配音，以及可选的豆包实时语音。凭据和供应商权限必须由使用者配置，仓库不会包含他人的 Key。脚本负责安装固定依赖、检查本地配置、启动数据库和服务；供应商的网络、余额与服务权限仍需实际调用验证。

## 1. 安装基础软件并拉取 main

先安装 Git、uv、Docker Desktop。已安装的不用重装。可在 PowerShell 中运行：

```powershell
winget install --id Git.Git --exact
winget install --id astral-sh.uv --exact
winget install --id Docker.DockerDesktop --exact
```

安装后重新打开 **Windows PowerShell**（`powershell.exe`，不要用 `pwsh`）。打开 Docker Desktop，按其提示启用 WSL 2/虚拟化并完成必要的系统重启；等待 Docker Engine 启动，选择 Linux containers。验证：

```powershell
git --version
uv --version
docker info --format '{{.OSType}}'
```

最后一条必须输出 `linux`。首次下载需要访问 GitHub、Python 包源和 Docker 镜像仓库。项目资源、Godot、Python 环境和镜像会占用数 GB 空间。

```powershell
git clone --branch main https://github.com/senizuaki-bit/walnut-world-new.git
cd walnut-world-new
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\walnut-world-backend\scripts\setup-play.ps1
```

私有仓库需要先用有权限的 GitHub 账号完成 Git 登录。初始化脚本执行以下工作：

1. 通过 uv 安装 Python **3.12.13**，创建后端 `.venv`。
2. 安装 `requirements-play-win-py312.txt` 锁定的版本，再以 editable 方式安装仓库内 `agent/` 和后端。无需另行查找 `yaya_agent_contracts` 包。
3. 下载启动器指定 digest 的 PostgreSQL 16.9 和 GCC 沙箱镜像。C++ 在 Docker 内编译，本机不用安装 Visual Studio 或 g++。
4. 下载 Godot **4.7.1 stable Windows x64**，核验固定 SHA-256，并首次导入游戏资源和脚本索引。

看到 `SETUP_PLAY_READY` 表示安装完成。脚本可重复运行；不会创建游戏数据库、重置存档或启动游戏。已有 Godot 可传 `-GodotExe 'C:\Godot\Godot_v4.7.1-stable_win64.exe'`，以后检查和启动也须传同一参数，或设置 `GODOT_EXE`。

## 2. 配置仓库外的密钥文件

| 服务 | 默认文件（每个文件只有一行 Key） | 用途和必要权限 |
|---|---|---|
| DeepSeek，必需 | `%USERPROFILE%\.walnut-secrets\deepseek-v4-flash.key` | 默认 `deepseek-v4-flash`，HTTPS Chat Completions；需要可用余额和模型权限 |
| 书书配音，完整通关必需 | `%USERPROFILE%\.walnut-secrets\book-tts.key` | 豆包语音合成 `seed-tts-2.0`；须能使用音色 `ICL_uranus_zh_male_bujiqingnian_tob` |
| 问叮当实时语音，可选 | `%USERPROFILE%\.walnut-secrets\doubao-voice.key` | 豆包 3.0 / Seeduplex JSON WebSocket 实时对话权限；默认音色 `ICL_uranus_zh_male_youmodaye_tob` |

书书与实时对话是不同接口。只有供应商账号明确具备两种权限时才可使用同一 Key；不能仅凭其中一种接口成功就认定另一种可用。这里使用的是 API Key，不是 App ID、旧版 Access Token 或临时 JWT。

在 PowerShell 建立仅当前用户和 SYSTEM 可访问的目录。下面不包含任何密钥值：

```powershell
$privateDir = Join-Path $env:USERPROFILE '.walnut-secrets'
New-Item -ItemType Directory -Path $privateDir -Force | Out-Null
$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
icacls $privateDir /inheritance:r /grant:r "*${currentSid}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F'
notepad (Join-Path $privateDir 'deepseek-v4-flash.key')
notepad (Join-Path $privateDir 'book-tts.key')
```

在记事本中分别粘贴对应 Key，保存为 UTF-8 纯文本，确认文件名是 `.key`，没有多出来的 `.txt`。不要加引号、`Bearer `、变量名或 JSON。不要把 Key 写进启动命令、截图、README 或 Git。模型密钥文件如果已有单独的宽泛权限，也需移除 Users/Everyone 的读取权限；启动检查会拒绝不安全的模型密钥文件。

需要实时语音时，再建立 `doubao-voice.key`：

```powershell
notepad (Join-Path $privateDir 'doubao-voice.key')
```

缺少实时语音 Key 时保持关闭，其他游戏功能可用。Windows 设置中还需允许桌面应用使用麦克风，并选对输入设备。

需要自定义文件位置时，在**当前 PowerShell** 设置后启动；环境会传给脚本子进程：

```powershell
$env:WALNUT_LLM_UPSTREAM_API_KEY_FILE = 'C:\my-private\deepseek.key'
$env:YAYA_BOOK_TTS_API_KEY_FILE = 'C:\my-private\book.key'
$env:YAYA_DOUBAO_VOICE_API_KEY_FILE = 'C:\my-private\voice.key'
# 明确关闭实时对话时使用：
# $env:YAYA_VOICE_MODE = 'disabled'
```

显式变量优先于默认路径；错误路径不会回退。不要同时设置同一服务的 `*_API_KEY` 与 `*_API_KEY_FILE`。`.env` 文件不会由这个启动器自动加载。默认路径方案无需每次设置环境变量。

## 3. 检查和启动

以下命令均在仓库根目录执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\walnut-world-backend\scripts\start-persistent-play.ps1 -Action Check
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\walnut-world-backend\scripts\start-persistent-play.ps1 -Action Start
```

`Check` 不写数据库、不启动服务、不调用供应商。它验证模型与语音密钥配置、模型密钥文件权限、后端导入、Python/Godot 版本、Docker Linux 引擎和两张固定镜像。应看到两份 `CONFIGURED` 和 `PERSISTENT_PLAY_DEPENDENCIES_READY`。`provider_access: NOT_CHECKED` 表示尚未在线验证权限，不是错误，也不是“供应商已可用”的证明。

`Start` 自动完成：数据库容器 → `alembic upgrade head` → 首次 seed 及既有数据权威核验 → 私有模型 relay → Gateway → workflow worker → learner worker → 带本机学生凭证的 Godot 窗口。**首次使用无需手动设置数据库 URL、JWT 或执行迁移。** 不要只打开 Godot 工程然后点击运行，否则缺少启动器注入的正式会话配置。

启动成功标志是 `PERSISTENT_PLAY_READY`，随后状态中的服务为 `true`。可以提交代码、点击“给我提示”，主关通过后完成 Bug 挑战和书书总结；启用实时语音后再点击“问叮当”验证实际收发音频。

默认端口都只监听本机：Gateway **8790**、模型 relay **20999**、PostgreSQL **55433**。不要让其他程序占用，也不要把这些本地开发端口开放到公网。默认学生登录有效期为 2 小时；过期后关闭游戏窗口、重新执行 `Start` 可在后台仍运行时获得新凭证。

```powershell
# 查看状态
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\walnut-world-backend\scripts\start-persistent-play.ps1 -Action Status
# 停止整套服务，保留 PostgreSQL 数据卷
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\walnut-world-backend\scripts\start-persistent-play.ps1 -Action Stop
```

关闭游戏窗口不会停止后台；再次 `Start` 可复用同一后台。源码、模型设置、语音设置或密钥发生变化时，启动器拒绝静默复用旧配置，须有计划地 `Stop` 后 `Start`。

## 4. 更新与数据保留

先完成当前挑战并保存想保留的代码，再更新：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\walnut-world-backend\scripts\start-persistent-play.ps1 -Action Stop
git switch main
git pull --ff-only origin main
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\walnut-world-backend\scripts\setup-play.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\walnut-world-backend\scripts\start-persistent-play.ps1 -Action Check
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\walnut-world-backend\scripts\start-persistent-play.ps1 -Action Start
```

主关数据在 `walnut-play-pgdata` 数据卷，启动信息和日志在 `%LOCALAPPDATA%\WalnutWorld\persistent-play`，客户端代码缓存位于 `%APPDATA%\Godot\app_userdata\核桃代码世界`。不要删除 `state.json` 后继续复用原数据库卷：里面包含与该数据库配对的本机凭据。该文件不能上传到 GitHub。

**当前 Bug 练习保存在网关内存，最长 6 小时，重启网关会过期。** 客户端缓存不等于服务端挑战已持久化；启动器不会声称旧挑战可以跨重启恢复。关闭并重开游戏窗口本身不清除后台练习。

## 5. 错误定位

| 现象或错误 | 检查与处理 |
|---|---|
| 找不到 Python、Godot、依赖或 Docker 镜像 | 先完成 `setup-play.ps1`；确认 Docker 已启动且为 Linux containers |
| `LLM_CONFIGURATION_INVALID` | 检查模型 Key 路径、UTF-8 单行内容、权限及模型参数；不会输出密钥内容 |
| `BOOK_SPEECH_CONFIGURATION_INVALID` / `BOOK_SPEECH_DISABLED` | 配置独立书书 Key，运行 `Check`；已通过的代码不需要修改 |
| `BOOK_SPEECH_AUTH_FAILED` / `BOOK_SPEECH_RESOURCE_NOT_GRANTED` | 检查供应商账号鉴权、语音合成服务和指定音色授权，单纯重试不会授予权限 |
| `VOICE_DISABLED` / `VOICE_AUTH_FAILED` | 前者需配置实时对话 Key 并重启后台；后者检查实时对话权限。书书配音成功不能替代此验证 |
| 给我提示正在等待 | 编译/教学请求尚在收尾，已接受的点击排队一次；不需要连续点击 |
| `CONTENT_VERSION_MISMATCH` / 运行准备暂未完成 | 已覆盖真实 HTTP 错误格式，读取同账号、同关卡的新版本后有限重试。持续冲突时停止同时操作同一账号的另一个窗口或测试进程 |
| `PRACTICE_PROBLEM_INVALID` / 总结格式错误 | 数值边界由服务端保证；确定的模型格式错误最多 3 次内部修复。持续供应商失败仍会显示反馈，不会伪造题目或通过结果 |
| `Backend source or model configuration changed` / `Voice configuration changed` | 旧后台仍使用旧源码或配置；保存当前成果，`Stop` 后重新 `Start` |
| `runtime is incomplete` | 查看后台日志，`Stop` 清理本实例已记录进程后重新 `Start`；不要手工杀全部 Python 进程 |
| `PRACTICE_LIVE_ISOLATION_REQUIRED` | 在线测试禁止访问玩家服务；使用下节专用实例，不能去掉保护开关 |

错误排查先看状态，再看运行目录中的 `gateway.stderr.log`、`worker.stderr.log`、`relay.stderr.log`。提交问题时提供时间、界面提示和脱敏错误码；不要提供 `state.json`、JWT、完整环境或密钥文件。

## 6. 开发回归与在线测试隔离

后端单元测试和前端离线套件无需真实供应商 Key：

```powershell
Push-Location .\walnut-world-backend
.\.venv\Scripts\python.exe -m pytest .\tests\unit -q
Pop-Location
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\walnut-world-frontend\scripts\run-offline-tests.ps1
```

后端全量 `unit + contract` 还需独立、已迁移的 PostgreSQL，通过 `WALNUT_TEST_DATABASE_URL` 指定；不得指向玩家数据库。仓库根 `.github/workflows/play-regression.yml` 在 main 推送和 PR 上运行离线回归及两项真实数据库合同检查；不依赖供应商密钥。

真实闯关测试需要单独的 checkout、用户数据目录和服务实例。例如在验收 checkout 初始化后运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\walnut-world-backend\scripts\start-persistent-play.ps1 -Action Start -InstanceName acceptance -PostgresPort 55439 -RelayPort 21999 -GatewayPort 18790 -NoGame
```

该实例使用独立容器、卷和 `%LOCALAPPDATA%\WalnutWorld\acceptance`，不共享玩家状态。`Status` / `Stop` 同样传这四个实例参数。正式在线测试另外需要注入**专用实例签发**的学生 Token、`YAYA_API_BASE_URL=http://127.0.0.1:18790`、`WALNUT_PRACTICE_LIVE=1` 和 `WALNUT_PRACTICE_ISOLATED_GATEWAY=1`；并把 Godot 的 `APPDATA` 定位到独立测试目录。不要把玩家的登录 Token 复制过去。

固定回归覆盖：真实错误信封、激活版本冲突、提示等待、错误帧与语音关闭同帧、题目边界、模型格式修复、答案身份不变的有限重试、持续失败反馈，以及在线验收与玩家状态隔离。测试能防止这些已定位缺陷回归；外部供应商断网、无额度或未授权仍需按明确错误处理。
