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

## 发布检查

分发 wheel 前，按 [干净构建与安装验证](docs/releases.zh-CN.md) 执行 `python scripts/build_release.py`。它从当前工作区建立新快照并验证安装副本，不调用模型。构建工具只用于开发，核心运行时仍保持零第三方 Python 依赖。

## 提交说明

提交不要太碎。一个好的提交说明应该讲清楚：

- 改了什么
- 为什么改
- 如何验证
