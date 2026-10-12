# Contributing to Cater

> English first, 中文随后。Keep changes small, observable, and testable.

## Project Shape

Cater's core runtime must stay dependency-free and event-first.

- Keep third-party model APIs, MCP clients, skills, LangChain, LangGraph, and CLIs behind the driver boundary.
- Register new behavior as events before projecting it into views.
- Prefer collaboration-first flows; use governance only when it improves reliability or explains a decision.
- Keep Web/CLI surfaces thin. They should call the same runtime APIs.

## Local Checks

```bash
python scripts/check.py
```

On Windows, if `python` is the Microsoft Store placeholder:

```powershell
.\scripts\check.ps1
```

## Commit Notes

Use meaningful commits. A good commit explains:

- what changed
- why it changed
- how it was verified

## 贡献说明

Cater 的核心运行时必须保持零第三方依赖，并坚持事件优先。

- 第三方模型 API、MCP client、skills、LangChain、LangGraph、CLI 都应该放在 driver 边界之后。
- 新行为先沉淀为事件，再投影到视图。
- 默认协作优先；治理层只在能提升可靠性或解释决策时出现。
- Web/CLI 保持轻薄，复用同一套 runtime API。

## 本地验证

```bash
python scripts/check.py
```

如果 Windows 上的 `python` 是 Microsoft Store 占位符：

```powershell
.\scripts\check.ps1
```

## 提交说明

提交不要太碎。一个好的提交说明应该讲清楚：

- 改了什么
- 为什么改
- 如何验证
