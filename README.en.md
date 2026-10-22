# Cater

[中文](README.md) | English

**An AI cat cafe that collaborates through conversation.**

Tell the manager what you want to make and work with the team in one conversation. Add requirements, `@` a teammate, and inspect contributions and deliverables without switching between chat and task forms.

Cater is under active development. It offers a Chinese-language local workroom, replayable collaboration, and configurable model connections. **The default is a local demo: it makes no real model calls and does not modify your files or perform engineering work.**

## Quick Start

Python 3.11+ is required. Run directly from the source checkout, with no third-party Python runtime dependencies. The repository includes a prebuilt web interface.

```bash
git clone https://github.com/daiyulejia/cater.git
cd cater
python -m cater --db .cater/cater.db web --port 8766
```

Open the [local workroom](http://127.0.0.1:8766/). Task records are stored in `.cater/cater.db` under the current directory. Keep the same database and port for everyday use; when starting elsewhere, pass the original database's absolute path to `--db`.

Already have the source? Run only the last command. Node.js and pnpm are needed only when changing the frontend; see [frontend development](web/README.md).

If the port is occupied, first check whether Cater is already running. Updating source files does not upgrade a running service. Before upgrading, confirm that tasks are idle, back up the database, then stop the old service and start it with the same command. Add `--no-startup-recovery` to prevent automatic continuation on startup; you can then continue work through the conversation. A different port is useful for isolated testing, but does not upgrade the original entry.

## Work in Conversation

1. Send a goal, such as "Help me design a personal portfolio website," or mention a teammate with `@罐头` to discuss the layout first.
2. Read assignments and replies, then add requirements or change direction in the same conversation.
3. Open published deliverables from the chat or workboard, and inspect diagnoses, handoff notes, and the replay.

Available today:

- **Chat collaboration**: create tasks, mention teammates, add directions, and follow progress.
- **Related conversations**: start separate work from a frozen background snapshot while the original task continues.
- **Readable deliverables**: open, copy, or download published text, Markdown, and code bodies.
- **Inspectable records**: explore subtask dependencies, replay collaboration events, and distinguish agent reports from actual check receipts.
- **Versioned acceptance**: execute work in dependency order and download artifact bodies, a manifest, and handoff notes after whole-task acceptance.
- **Configurable teammates**: customize names, responsibilities, and model connections through local CLI drivers or Chat Completions-compatible APIs.

## Connect Real Models

Try the demo first, then follow the [integration examples](examples/README.md) to configure your model or local CLI. Roles can share a connection or use separate ones.

Keep API keys in environment variables, not role cards, chat messages, or Git. File access and command execution depend on the tools you connect and their permissions. Connecting a chat API alone does not provide those capabilities.

## Current Limits

- A model's conclusion, vote, or "done" message does not prove correctness. Dependency scheduling and stage/whole-task acceptance gates are implemented; real-model long-task quality, automatic tool execution, and uncertain-result recovery still need work.
- Cater does not provide a general filesystem or network sandbox. Role definitions, prompts, and process management are not permission isolation. Run only trusted driver configurations and commands.
- The workroom binds to loopback by default. Remote access requires explicit opt-in, an access token, and HTTPS or an equivalent controlled network boundary that you provide.

## Docs and Development

The guides below are primarily in Chinese.

- [Integration and configuration](examples/README.md)
- [Deliverables and replay](docs/artifacts.zh-CN.md)
- [Work items and whole-task acceptance](docs/work-items.zh-CN.md)
- [Local verification and safety](docs/local-verification.zh-CN.md)
- [Optional automated browser checks](docs/browser-checks.zh-CN.md)
- [Project workspaces and host tools](docs/workspaces.zh-CN.md)
- [Frontend development and remote access](web/README.md)
- [Clean builds and installed-package checks](docs/releases.zh-CN.md)
- [Contributing](CONTRIBUTING.md)

Run the local checks from the repository root:

```bash
python scripts/check.py
```

On Windows, if `python` points to the Microsoft Store placeholder, use `./scripts/check.ps1` to locate an installed interpreter. See `python -m cater --help` for other commands.

License: [MIT](LICENSE).
