# Contributing to Cater

> 中文优先。保持改动成组、可观察、可验证。

## Project Shape

Cater's core runtime must stay dependency-free and event-first.

- Keep third-party model APIs, MCP clients, skills, LangChain, LangGraph, and CLIs behind the driver boundary.
- Do not infer third-party behavior from names alone. Inspect the real contract, failure modes, state, and cost before integration.
- Register new behavior as events before projecting it into views.
- Prefer collaboration-first flows; use governance only when it improves reliability or explains a decision.
- Keep Web/CLI surfaces thin. They should call the same runtime APIs.
- A completed feature is not a stopping condition: after each grouped change, review product value, user flow, tests, UI, reliability, cost, adoption, and the initiator's evolving goal.
- When evidence shows a concrete gap, fix and verify it in the current iteration when possible. Do not create churn merely to appear autonomous.

## Local Checks

```bash
python scripts/check.py
```

On Windows, if `python` is the Microsoft Store placeholder:

```powershell
.\scripts\check.ps1
```

For a deterministic module shard, use `python scripts/check.py --shard 1/2` and `--shard 2/2`. `--list-tests` prints selected IDs without running cases, but still imports modules for discovery. Both wrappers forward these options. Default execution remains the complete original suite. Discovery errors, empty selections and custom suite hooks that cannot safely be sharded fail explicitly. CI keeps Linux full runs and both Windows shards for Python 3.11/3.13; a shard alone is not full validation. The runner reports up to ten slowest tests, excluding discovery and module/class fixtures.

With `GITHUB_ACTIONS=true`, bounded progress and stall observation emits at most eight `Test progress` checkpoints, at most once per 120 seconds. Runs shorter than the first period emit none. Each JSON message includes the current case or discovery/fixture phase, monotonic elapsed seconds, seconds since the last discovery/execution or test start/stop boundary, `stalled` (true only after a full period without a boundary), `completed_cases` and `selected_total` (null before selection). Completed cases count `stopTest`, including skips; fixture errors without a completed case do not increase it. Thus continuing slow work can produce a normal checkpoint without being labelled stalled. Up to eight main-thread frames contain only basename, function and line. Fixture labels are inferred from the sampled unittest stack; the boundary timer is not exact fixture duration. No locals, source lines, exception contents, environment or arguments are sampled. Ordinary diagnostic exceptions cannot replace the suite result or exception. On exit, including interrupts, stop is requested and the worker is joined for at most one second; normal streams leave no worker or late output. A write already blocked in the sink may outlive this budget as a daemon and complete later; once it returns, stop prevents further flush or notices. The thread is not forcibly killed. `--list-tests` disables observation even during discovery. Diagnostics do not retry, cancel or change test decisions. Hard kills, unavailable Python scheduling/GIL or blocked output can prevent evidence. GitHub documents [notice commands](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-commands#setting-a-notice-message) and [annotation constraints](https://docs.github.com/en/rest/checks/runs#update-a-check-run); the eight-notice budget is our policy, not a guarantee of public retention.

## Release Checks

Before distributing a wheel, follow [clean builds and installed-package checks](docs/releases.zh-CN.md). The public `python scripts/build_release.py` entry uses a fresh working-tree snapshot and checks the installed package without model calls. Build tools are development-only; the runtime still has no third-party Python dependencies.

## Commit Notes

Use meaningful commits. A good commit explains:

- what changed
- why it changed
- how it was verified

## 贡献说明

Cater 的核心运行时必须保持零第三方依赖，并坚持事件优先。

- 第三方模型 API、MCP client、skills、LangChain、LangGraph、CLI 都应该放在 driver 边界之后。
- 不要只看变量名、函数名或项目口号就推断第三方行为；接入前要检查真实协议、失败模式、状态和成本。
- 新行为先沉淀为事件，再投影到视图。
- 默认协作优先；治理层只在能提升可靠性或解释决策时出现。
- Web/CLI 保持轻薄，复用同一套 runtime API。
- 功能完成不是停止条件：每次成组改动后，都要复盘产品价值、用户路径、测试、UI、可靠性、成本、采用门槛，以及发起人正在演化的目标。
- 有证据表明存在明确缺口时，能在本轮修正就自行修正并验证；不要为了表现“自主”而制造无价值的改动。

## 本地验证

```bash
python scripts/check.py
```

如果 Windows 上的 `python` 是 Microsoft Store 占位符：

```powershell
.\scripts\check.ps1
```

测试可用 `python scripts/check.py --shard 1/2` 和 `--shard 2/2` 按模块确定性分片；`--list-tests` 只列出所选 ID，不执行测试用例，但仍会导入模块完成发现。Python/PowerShell 入口均转发这些参数；默认仍执行完整原始测试树。发现错误、空分片或无法安全拆分的自定义测试套件会明确失败。CI 保留 Linux 全量和 Windows 两片的 Python 3.11/3.13 检查，单片通过不等于完整验证。执行后最多列出十项慢测试，耗时不含发现和模块/类级夹具。

`GITHUB_ACTIONS=true` 时启用有界进度与停滞观测，每 120 秒至多输出一条 `Test progress` checkpoint，全程最多八条；不足首个周期的短作业没有 notice。JSON 包含当前用例或发现/夹具阶段、单调经过秒、距上次发现/执行切换或测试开始/结束边界秒数、`stalled`（仅连续一个周期没有边界时为 true）、`completed_cases` 和 `selected_total`（选集确定前为 null）。完成数仅在 `stopTest` 增加，包含 skip；没有完成用例的 fixture 错误不增加计数。持续缓慢前进也会留下正常 checkpoint，不称为停滞。最多八层主线程栈仅含文件 basename、函数名、行号；夹具标签根据 unittest 栈推断，边界计时不是精确夹具耗时。不采样局部变量、源码行、异常正文、环境或参数。普通诊断异常不替换 suite 原返回或异常。退出及键盘中断时请求停止，最多等待线程一秒；正常输出路径回收线程且无晚写。已阻塞的 write 可作为 daemon 超出预算、稍后完成，但恢复后不再 flush 或发下一条 notice，不声称硬杀线程。`--list-tests` 连发现阶段也不启用观测。不重试、取消或改变测试判定。硬终止、Python/GIL 无法调度或输出阻塞可能使证据缺失。协议依据见上方 GitHub 官方 notice 与注解约束；八条上限是本地策略，不保证公共注解保留或 API 可读。

## 发布检查

分发 wheel 前，按 [干净构建与安装验证](docs/releases.zh-CN.md) 执行 `python scripts/build_release.py`。它从当前工作区建立新快照并验证安装副本，不调用模型。构建工具只用于开发，核心运行时仍保持零第三方 Python 依赖。

## 提交说明

提交不要太碎。一个好的提交说明应该讲清楚：

- 改了什么
- 为什么改
- 如何验证
