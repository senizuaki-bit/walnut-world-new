# INT2 focused 红测诊断账本

> 目的：保留实现前的最小、可复跑失败证据。后续绿测追加记录，不覆盖本页红灯。
> 日期：2026-08-14（Asia/Shanghai）
> 证据分类：deterministic local；不是 real Provider evidence。

## R1 — Backend reducer 没有权威逐步投影

命令：

```powershell
Set-Location C:\Users\HP\Desktop\核桃编程\walnut-world-backend
.venv\Scripts\python.exe -m pytest tests\unit\test_world_presentation_projection.py -q
```

结果：`2 failed`。两个用例都在读取 `WorldTransition.reducer_steps` 时得到 `AttributeError`。

最小诊断：当前 reducer 只返回最终 state/hash，无法从已提交 reducer 结果生成逐 HARVEST 的 before/after hash chain 和闭合展示参数。测试文件：`tests/unit/test_world_presentation_projection.py`。

## R2 — Agent 合同没有 v0.4 baseline lock 和 v0.5 presentation Wire

命令：

```powershell
Set-Location C:\Users\HP\Desktop\核桃编程\agent
$env:YAYA_PYTHON_EXE = '.venv\Scripts\python.exe'
node --test tests\contract-manifest.test.mjs tests\world-presentation-contract.test.mjs tests\contracts.test.mjs
```

结果：`exit 1`。合同红灯为：

- Manifest generator 没有通用 `inspectReleaseBaselines`；
- `contracts/releases/agent-contracts-v0.4.lock.json` 不存在；
- v0.5 presentation event/page/example/OpenAPI 不存在；
- 当前 OpenAPI 汇总为 31 operations，红测要求 additive 第 32 个只读 operation。

最小诊断：v0.4 generic EventEnvelope 无法机器闭合逐动作身份、参数和最终 Snapshot；在不改 v0.4 字节的前提下必须追加 v0.5 Wire。

首次运行还观测到系统 Python 缺 `jsonschema` 的环境噪声；它不是上述合同红灯。复跑必须显式使用仓库 `.venv`，避免把工具环境错误混入产品诊断。

## R3 — Frontend Player 没有可等待的正式演出 API

命令：

```powershell
$godot = 'C:\Users\HP\Desktop\核桃编程\tools\godot-4.5.2\Godot_v4.5.2-stable_win64_console.exe'
Set-Location C:\Users\HP\Desktop\核桃编程\walnut-world-frontend
& $godot --headless --path . --script res://tests/client/world_event_player_test.gd
& $godot --headless --path . --script res://tests/client/world_presentation_gateway_test.gd
```

结果：红灯。现有 `WorldEventPlayer.play(events)` 返回 `void`，没有 renderer handshake、2x、skip 或 replay API；测试出现 `Too many arguments` / coroutine return parse error。第二个红测所需 additive `world_presentation_gateway.gd` 不存在。

最小诊断：现有孤立 player 只能按一帧发信号，正式 AppRoot 无法等待权威动作完成，也无法证明 controls、腐败回退或最终 Snapshot 收口。

## R4 — 正式跨进程脚本尚未启用或指纹化 World 演出

命令：

```powershell
Set-Location C:\Users\HP\Desktop\核桃编程\walnut-world-backend
.venv\Scripts\python.exe -m pytest tests\contract\test_int1_local_diagnostic_harness.py::test_harness_has_fresh_authority_recovery_and_official_godot_chain -q
```

结果：`1 failed`。首个缺失断言为 `WALNUT_ENABLE_WORLD_PRESENTATION`；同一红测还要求正式 Godot runner 的显式 `-EnableWorldPresentation`、presentation 数据库副作用集合以及播放高水位/事件身份指纹。

最小诊断：focused Player/route 绿灯不能证明正式 Gateway + Godot 路径已使用它们。跨进程脚本仍按 INT1 默认关闭能力运行，因而必须显式 opt-in，并在进程重启前后比较 presentation 权威指纹；恢复阶段仍只能 GET。

## R5 — Backend 仓库总门禁缺少 compileall 且硬依赖不存在的 `py -3.12`

命令：

```powershell
Set-Location C:\Users\HP\Desktop\核桃编程\walnut-world-backend
.venv\Scripts\python.exe -m pytest tests\contract\test_verify_all_script.py -q
```

首次红灯一：`verify_all.ps1` 只有 6 次 Python 门禁调用，缺少用户要求的 `compileall`。补入该断言后测试得到 `1 failed`。

首次红灯二：本机仓库 `.venv` 是 Python 3.12.13，但 Python launcher 没有注册 `-3.12`；原脚本硬编码 `py -3.12`，因此即便项目运行时存在也无法执行总门禁。第二个 focused 红测要求脚本默认解析仓库 `.venv\\Scripts\\python.exe`，同时允许显式 `-PythonExe` 覆盖。

最小诊断：静态工具或 `pytest` 的局部 PASS 不能替代仓库自带总门禁可运行性。总门禁必须使用明确的 repository runtime，并把 bytecode compilation 纳入 native exit 检查。

## 待追加

- Frontend formal AppRoot 状态迁移红测；
- Backend PostgreSQL 原子写和腐败读取红测；
- 三仓 focused 绿测及其命令/指纹。
