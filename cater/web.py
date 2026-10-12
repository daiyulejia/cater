"""Minimal local web surface for Cater replay."""

from __future__ import annotations

import html
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .events import Event
from .fold import TaskSnapshot
from .ladder import decision_ladder
from .runtime import Runtime


def serve(
    db_path: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    drivers_path: str | Path | None = None,
    roles_path: str | Path | None = None,
    audit_key: str | bytes | None = None,
    audit_key_id: str = "local",
) -> None:
    handler = make_handler(
        db_path,
        drivers_path=drivers_path,
        roles_path=roles_path,
        audit_key=audit_key,
        audit_key_id=audit_key_id,
    )
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Cater web listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def make_handler(
    db_path: str | Path,
    *,
    drivers_path: str | Path | None = None,
    roles_path: str | Path | None = None,
    audit_key: str | bytes | None = None,
    audit_key_id: str = "local",
):
    class CaterHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            if path == "/":
                return self._send_html(render_index(self._runtime()))
            if path == "/api/tasks":
                with self._runtime() as rt:
                    return self._send_json([task_to_dict(task) for task in rt.tasks()])
            if path.startswith("/api/tasks/"):
                task_id = path.split("/", 3)[3]
                with self._runtime() as rt:
                    task = rt.task(task_id)
                    if task is None:
                        return self._not_found()
                    events = rt.log.read(task_id=task_id)
                    return self._send_json(
                        {
                            "task": task_to_dict(task),
                            "events": [event_to_dict(e) for e in events],
                            "decision_ladder": decision_ladder(events),
                        }
                    )
            if path.startswith("/tasks/"):
                task_id = path.split("/", 2)[2]
                with self._runtime() as rt:
                    task = rt.task(task_id)
                    if task is None:
                        return self._not_found()
                    events = rt.log.read(task_id=task_id)
                    return self._send_html(render_task(task, events))
            return self._not_found()

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/")
            if path == "/tasks":
                data = self._read_form()
                goal = data.get("goal", [""])[0].strip()
                if not goal:
                    self.send_response(HTTPStatus.SEE_OTHER)
                    self.send_header("Location", "/")
                    self.end_headers()
                    return
                acceptance = [
                    line.strip()
                    for line in data.get("acceptance", [""])[0].splitlines()
                    if line.strip()
                ]
                with self._runtime() as rt:
                    task_id = rt.create_task(goal, acceptance)
                    if data.get("battle", [""])[0] == "on":
                        rt.collaborate(task_id)
                self._redirect(f"/tasks/{task_id}")
                return
            if path.startswith("/tasks/") and path.endswith("/run"):
                task_id = path.split("/")[2]
                with self._runtime() as rt:
                    if rt.task(task_id) is None:
                        return self._not_found()
                    rt.collaborate(task_id)
                self._redirect(f"/tasks/{task_id}")
                return
            if path.startswith("/tasks/") and path.endswith("/battle"):
                task_id = path.split("/")[2]
                with self._runtime() as rt:
                    if rt.task(task_id) is None:
                        return self._not_found()
                    rt.battle(task_id)
                self._redirect(f"/tasks/{task_id}")
                return
            if path.startswith("/tasks/") and path.endswith("/command"):
                task_id = path.split("/")[2]
                data = self._read_form()
                instruction = data.get("instruction", [""])[0].strip()
                goal = data.get("goal", [""])[0].strip() or None
                acceptance_raw = data.get("acceptance", [""])[0]
                acceptance = [line.strip() for line in acceptance_raw.splitlines() if line.strip()]
                with self._runtime() as rt:
                    if rt.task(task_id) is None:
                        return self._not_found()
                    rt.command(
                        task_id,
                        instruction or "Updated from Cater web",
                        goal=goal,
                        acceptance=acceptance if acceptance_raw.strip() else None,
                    )
                self._redirect(f"/tasks/{task_id}")
                return
            if path.startswith("/tasks/") and path.endswith("/decide"):
                task_id = path.split("/")[2]
                data = self._read_form()
                resolution = data.get("resolution", ["done"])[0]
                reason = data.get("reason", [""])[0].strip() or "Decided from Cater web"
                with self._runtime() as rt:
                    if rt.task(task_id) is None:
                        return self._not_found()
                    rt.decide(task_id, resolution, reason)
                self._redirect(f"/tasks/{task_id}")
                return
            return self._not_found()

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _runtime(self) -> Runtime:
            return Runtime(
                db_path,
                drivers_path=drivers_path,
                roles_path=roles_path,
                audit_key=audit_key,
                audit_key_id=audit_key_id,
            )

        def _send_html(self, body: str) -> None:
            raw = body.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _send_json(self, body: Any) -> None:
            raw = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _not_found(self) -> None:
            raw = b"not found"
            self.send_response(HTTPStatus.NOT_FOUND)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _read_form(self) -> dict[str, list[str]]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length).decode("utf-8")
            return parse_qs(raw)

        def _redirect(self, location: str) -> None:
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", location)
            self.end_headers()

    return CaterHandler


def render_index(rt: Runtime) -> str:
    with rt:
        tasks = rt.tasks()
        inbox = {item["task_id"] for item in rt.inbox()}
        profiles = rt.profiles()
        chain = rt.verify_chain()
    rows = "\n".join(_task_row(task, task.id in inbox) for task in tasks)
    if not rows:
        rows = '<tr><td colspan="5" class="empty">No tasks yet. Run <code>cater demo</code>.</td></tr>'
    return page(
        "Cater",
        f"""
        <section class="topbar">
          <div>
            <p class="eyebrow">Cater Replay</p>
            <h1>Autonomous agent workbench</h1>
          </div>
          <span class="chain">{escape(chain.describe())}</span>
        </section>
        <section class="grid">
          <article class="panel">
            <div class="panel-head">
              <h2>New Task</h2>
              <span>create</span>
            </div>
            <form method="post" action="/tasks">
              <label>Goal
                <textarea name="goal" rows="3" required></textarea>
              </label>
              <label>Acceptance
                <textarea name="acceptance" rows="4"></textarea>
              </label>
              <label class="checkline">
                <input type="checkbox" name="battle" checked>
                <span>Run collaboration round now</span>
              </label>
              <button type="submit">Create task</button>
            </form>
          </article>
          <article class="panel">
            <div class="panel-head">
              <h2>Human Input</h2>
              <span>{len(inbox)} pending</span>
            </div>
            {_inbox_list(tasks, inbox)}
          </article>
        </section>
        <section class="panel">
          <div class="panel-head">
            <h2>Agent Profiles</h2>
            <span>{len(profiles)} active</span>
          </div>
          {_profiles_table(profiles)}
        </section>
        <section class="panel">
          <div class="panel-head">
            <h2>Tasks</h2>
            <span>{len(tasks)} total</span>
          </div>
          <table>
            <thead>
              <tr><th>Task</th><th>Status</th><th>Cabinet</th><th>Messages</th><th>Decision</th></tr>
            </thead>
            <tbody>{rows}</tbody>
          </table>
        </section>
        """,
    )


def render_task(task: TaskSnapshot, events: list[Event]) -> str:
    message_cards = "\n".join(_message_card(item) for item in task.messages)
    timeline = "\n".join(_event_row(event) for event in events)
    ladder = "\n".join(_ladder_step(step) for step in decision_ladder(events))
    governance = _governance_panel(task)
    decision = _decision_panel(task)
    command = _command_panel(task)
    failures = _failure_panel(task)
    return page(
        f"Cater / {task.id}",
        f"""
        <section class="topbar">
          <div>
            <p class="eyebrow"><a href="/">Cater Replay</a> / {escape(task.id)}</p>
            <h1>{escape(task.goal)}</h1>
          </div>
          <span class="status">{escape(task.status)}</span>
        </section>
        <section class="grid">
          {governance}
          {decision}
        </section>
        <section class="panel">
          <div class="panel-head"><h2>Decision Ladder</h2><span>autonomy path</span></div>
          <ol class="ladder">{ladder}</ol>
        </section>
        {command}
        {failures}
        <section class="panel">
          <div class="panel-head"><h2>Messages</h2><span>{len(task.messages)} entries</span></div>
          <div class="messages">{message_cards}</div>
        </section>
        <section class="panel">
          <div class="panel-head"><h2>Timeline</h2><span>{len(events)} events</span></div>
          <ol class="timeline">{timeline}</ol>
        </section>
        """,
    )


def page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>{CSS}</style>
</head>
<body>
  <main>{body}</main>
</body>
</html>
"""


def task_to_dict(task: TaskSnapshot) -> dict[str, Any]:
    return {
        "id": task.id,
        "goal": task.goal,
        "acceptance": task.acceptance,
        "status": task.status,
        "messages": task.messages,
        "driver_failures": task.driver_failures,
        "cabinet": task.cabinet,
        "proposal": task.proposal,
        "brief": task.brief,
        "decision": task.decision,
        "commands": task.commands,
        "tokens": task.tokens,
    }


def _profiles_table(profiles: dict[str, dict[str, Any]]) -> str:
    if not profiles:
        return '<p class="muted">No agent activity yet</p>'
    rows = "\n".join(
        f"""
        <tr>
          <td>{escape(profile['id'])}<small>{escape(profile['role'])}</small></td>
          <td>{escape(profile['messages'])}</td>
          <td>{escape(profile['supports'])}</td>
          <td>{escape(profile['challenges'])}</td>
          <td>{escape(profile['driver_failures'])}</td>
          <td>{escape(profile['last_claim'])}</td>
        </tr>
        """
        for profile in profiles.values()
    )
    return f"""
    <table>
      <thead>
        <tr><th>Agent</th><th>Messages</th><th>Support</th><th>Challenge</th><th>Failures</th><th>Last Claim</th></tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
    """


def event_to_dict(event: Event) -> dict[str, Any]:
    return {
        "seq": event.seq,
        "id": event.id,
        "ts": event.ts,
        "task_id": event.task_id,
        "actor": event.actor.to_dict(),
        "type": event.type,
        "category": event.category,
        "payload": event.payload,
        "prev_hash": event.prev_hash,
        "hash": event.hash,
        "key_id": event.key_id,
        "signature": event.signature,
    }


def _task_row(task: TaskSnapshot, needs_decision: bool) -> str:
    cabinet = task.cabinet or {}
    governance = (task.brief or {}).get("governance", {}) if isinstance(task.brief, dict) else {}
    governance_status = cabinet.get("status") or governance.get("status", "-")
    decision = "input needed" if needs_decision else (task.decision or {}).get("resolution", "-")
    return f"""
    <tr>
      <td><a href="/tasks/{escape(task.id)}">{escape(task.goal)}</a><small>{escape(task.id)}</small></td>
      <td><span class="pill">{escape(task.status)}</span></td>
      <td>{escape(governance_status)}</td>
      <td>{len(task.messages)}</td>
      <td>{escape(decision)}</td>
    </tr>
    """


def _message_card(item: dict[str, Any]) -> str:
    evidence = ", ".join(str(value) for value in item.get("evidence", []))
    return f"""
    <article class="message">
      <header><strong>{escape(item.get("actor", "agent"))}</strong><span>{escape(item.get("stance", ""))}</span></header>
      <p>{escape(item.get("claim", ""))}</p>
      <small>{escape(evidence)}</small>
    </article>
    """


def _governance_panel(task: TaskSnapshot) -> str:
    if task.cabinet:
        cabinet = task.cabinet
        options = "\n".join(_option_card(item) for item in cabinet.get("options", []))
        return f"""
        <article class="panel">
          <div class="panel-head"><h2>Cabinet</h2><span>{escape(cabinet.get("status", "pending"))}</span></div>
          <dl class="metrics">
            <div><dt>threshold</dt><dd>{escape(cabinet.get("approval_threshold", "-"))}</dd></div>
            <div><dt>recommendation</dt><dd>{escape(cabinet.get("recommendation", "none"))}</dd></div>
            <div><dt>votes</dt><dd>{escape(len(cabinet.get("votes", [])))}</dd></div>
          </dl>
          <p class="muted">{escape(cabinet.get("reason", ""))}</p>
          <div class="options">{options}</div>
        </article>
        """

    brief = task.brief or {}
    governance = brief.get("governance", {}) if isinstance(brief, dict) else {}
    options = "\n".join(_option_card(item) for item in brief.get("options", [])) if isinstance(brief, dict) else ""
    status = governance.get("status") or "pending"
    reason = governance.get("reason") or "No governance record yet."
    return f"""
    <article class="panel">
      <div class="panel-head"><h2>Governance</h2><span>{escape(status)}</span></div>
      <p class="muted">{escape(reason)}</p>
      <div class="options">{options}</div>
    </article>
    """


def _option_card(item: dict[str, Any]) -> str:
    supporters = ", ".join(item.get("supporters", [])) or "none"
    concerns = ", ".join(item.get("concerns", [])) or "none"
    return f"""
    <article class="option">
      <strong>{escape(item.get("id", ""))}: {escape(item.get("summary", ""))}</strong>
      <span>support {escape(item.get("support", 0))} / concern {escape(item.get("concern", 0))}</span>
      <small>supporters: {escape(supporters)} | concerns: {escape(concerns)}</small>
    </article>
    """


def _event_row(event: Event) -> str:
    text = event.payload.get("claim") or event.payload.get("reason") or event.payload.get("goal") or ""
    return f"""
    <li>
      <span>#{event.seq}</span>
      <strong>{escape(event.type)}</strong>
      <em>{escape(event.actor.id)}</em>
      <p>{escape(text)}</p>
    </li>
    """


def _ladder_step(step: dict[str, Any]) -> str:
    return f"""
    <li class="ladder-step {escape(step.get("status"))}">
      <strong>{escape(step.get("label"))}</strong>
      <span>{escape(step.get("status"))}</span>
      <p>{escape(step.get("detail"))}</p>
    </li>
    """


def _decision_panel(task: TaskSnapshot) -> str:
    if task.decision:
        return f"""
        <article class="panel">
          <div class="panel-head"><h2>Agent Decision</h2><span>{escape(task.decision.get("resolution"))}</span></div>
          <p>{escape(task.decision.get("reason", ""))}</p>
        </article>
        """
    proposal = ""
    if task.proposal:
        proposal = f"""
        <div class="proposal">
          <strong>Proposed option {escape(task.proposal.get("option_id"))}</strong>
          <p>{escape(task.proposal.get("reason", ""))}</p>
        </div>
        """
    if task.status != "escalated":
        return f"""
        <article class="panel">
          <div class="panel-head"><h2>Agent Decision</h2><span>{escape(task.status)}</span></div>
          <p class="muted">Agents are expected to continue autonomously unless you send a command.</p>
          <div class="button-row">
            <form method="post" action="/tasks/{escape(task.id)}/run">
              <button type="submit">Run next round</button>
            </form>
            <form method="post" action="/tasks/{escape(task.id)}/battle">
              <button class="secondary" type="submit">Force governance</button>
            </form>
          </div>
        </article>
        """
    return f"""
    <article class="panel">
      <div class="panel-head"><h2>Human Input</h2><span>exception</span></div>
      {proposal}
      <form method="post" action="/tasks/{escape(task.id)}/decide">
        <label>Resolution
          <select name="resolution">
            <option value="done">done</option>
            <option value="rework">rework</option>
          </select>
        </label>
        <label>Reason
          <textarea name="reason" rows="4" placeholder="Why is this the right call?"></textarea>
        </label>
        <button type="submit">Record override</button>
      </form>
    </article>
    """


def _command_panel(task: TaskSnapshot) -> str:
    history = ""
    if task.commands:
        rows = "\n".join(
            f"<li><strong>#{escape(item.get('seq'))}</strong> {escape(item.get('instruction'))}</li>"
            for item in task.commands[-5:]
        )
        history = f'<ul class="inbox-list">{rows}</ul>'
    return f"""
    <section class="panel">
      <div class="panel-head"><h2>Human Command</h2><span>optional</span></div>
      <form method="post" action="/tasks/{escape(task.id)}/command">
        <label>Instruction
          <textarea name="instruction" rows="3" placeholder="Change direction, add a constraint, or override the goal."></textarea>
        </label>
        <label>New goal
          <textarea name="goal" rows="2"></textarea>
        </label>
        <label>Acceptance
          <textarea name="acceptance" rows="3"></textarea>
        </label>
        <button type="submit">Send command</button>
      </form>
      {history}
    </section>
    """


def _failure_panel(task: TaskSnapshot) -> str:
    if not task.driver_failures:
        return ""
    rows = "\n".join(
        f"""
        <li>
          <strong>{escape(item.get("driver"))}</strong>
          <span>{escape(item.get("error_type"))}</span>
          <p>{escape(item.get("message"))}</p>
        </li>
        """
        for item in task.driver_failures
    )
    return f"""
    <section class="panel">
      <div class="panel-head"><h2>Driver Failures</h2><span>{len(task.driver_failures)} contained</span></div>
      <ol class="failure-list">{rows}</ol>
    </section>
    """


def _inbox_list(tasks: list[TaskSnapshot], inbox: set[str]) -> str:
    pending = [task for task in tasks if task.id in inbox]
    if not pending:
        return '<p class="muted">Inbox empty</p>'
    rows = "\n".join(
        f'<li><a href="/tasks/{escape(task.id)}">{escape(task.goal)}</a></li>' for task in pending
    )
    return f'<ul class="inbox-list">{rows}</ul>'


def escape(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


CSS = """
:root {
  color-scheme: light;
  --bg: #f6f7f9;
  --panel: #ffffff;
  --ink: #1e242c;
  --muted: #687282;
  --line: #d8dee8;
  --accent: #216869;
  --accent-weak: #e8f2f1;
  --warn: #8f4f00;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
main {
  width: min(1120px, calc(100vw - 32px));
  margin: 0 auto;
  padding: 28px 0 48px;
}
a { color: var(--accent); text-decoration: none; }
h1, h2, p { margin: 0; }
h1 { font-size: 28px; line-height: 1.15; font-weight: 720; letter-spacing: 0; }
h2 { font-size: 16px; }
.topbar {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 20px;
  margin-bottom: 20px;
}
.eyebrow {
  color: var(--muted);
  font-size: 13px;
  margin-bottom: 8px;
}
.chain, .status, .pill {
  border: 1px solid var(--line);
  background: var(--panel);
  border-radius: 999px;
  padding: 7px 10px;
  color: var(--muted);
  font-size: 13px;
  white-space: nowrap;
}
.panel {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 18px;
  margin-bottom: 16px;
}
.panel-head {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: center;
  margin-bottom: 14px;
}
.panel-head span, .muted, small {
  color: var(--muted);
  font-size: 13px;
}
.grid {
  display: grid;
  grid-template-columns: minmax(0, 1.35fr) minmax(280px, .65fr);
  gap: 16px;
}
table {
  border-collapse: collapse;
  width: 100%;
}
th, td {
  border-top: 1px solid var(--line);
  padding: 12px 8px;
  text-align: left;
  vertical-align: top;
}
th {
  color: var(--muted);
  font-size: 12px;
  text-transform: uppercase;
}
td small {
  display: block;
  margin-top: 4px;
}
.empty {
  color: var(--muted);
  text-align: center;
}
.metrics {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
  margin: 0 0 14px;
}
.metrics div {
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 10px;
}
dt {
  color: var(--muted);
  font-size: 12px;
}
dd {
  margin: 4px 0 0;
  font-weight: 700;
}
.messages, .options {
  display: grid;
  gap: 10px;
}
.message, .option {
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 12px;
  background: #fbfcfd;
}
.message header {
  display: flex;
  justify-content: space-between;
  margin-bottom: 8px;
}
.message header span, .option span {
  color: var(--warn);
  font-size: 13px;
}
.message p, .option strong {
  display: block;
  margin-bottom: 8px;
}
.failure-list {
  list-style: none;
  padding: 0;
  margin: 0;
}
.inbox-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: grid;
  gap: 8px;
}
.inbox-list li {
  border-top: 1px solid var(--line);
  padding-top: 8px;
}
.failure-list li {
  border-top: 1px solid var(--line);
  padding: 10px 0;
}
.failure-list strong {
  display: inline-block;
  min-width: 120px;
}
.failure-list span {
  color: var(--warn);
  font-size: 13px;
}
.failure-list p {
  color: var(--muted);
  margin-top: 4px;
}
.proposal {
  border: 1px solid var(--line);
  background: var(--accent-weak);
  border-radius: 8px;
  padding: 10px;
  margin-bottom: 12px;
}
.proposal p {
  color: var(--muted);
  margin-top: 6px;
}
.timeline {
  list-style: none;
  padding: 0;
  margin: 0;
}
.ladder {
  list-style: none;
  padding: 0;
  margin: 0;
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}
.ladder-step {
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 10px;
  background: #fbfcfd;
}
.ladder-step strong, .ladder-step span {
  display: block;
}
.ladder-step span {
  color: var(--accent);
  font-size: 13px;
  margin-top: 4px;
}
.ladder-step p {
  color: var(--muted);
  margin-top: 8px;
  font-size: 13px;
}
.timeline li {
  display: grid;
  grid-template-columns: 56px 180px 120px minmax(0, 1fr);
  gap: 12px;
  padding: 10px 0;
  border-top: 1px solid var(--line);
}
.timeline span, .timeline em {
  color: var(--muted);
  font-style: normal;
}
form {
  display: grid;
  gap: 12px;
}
label {
  display: grid;
  gap: 6px;
  color: var(--muted);
  font-size: 13px;
}
label.checkline {
  display: flex;
  align-items: center;
  color: var(--ink);
}
input[type="checkbox"] {
  width: 16px;
  height: 16px;
}
select, textarea {
  width: 100%;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 10px;
  font: inherit;
  color: var(--ink);
  background: #fff;
}
button {
  border: 0;
  border-radius: 8px;
  padding: 10px 12px;
  background: var(--accent);
  color: #fff;
  font-weight: 700;
  cursor: pointer;
}
.button-row {
  display: flex;
  gap: 10px;
  align-items: center;
}
.button-row form {
  display: block;
}
button.secondary {
  background: #eef3f3;
  color: var(--accent);
  border: 1px solid var(--line);
}
@media (max-width: 760px) {
  main { width: min(100vw - 20px, 1120px); padding-top: 18px; }
  .topbar, .grid { display: block; }
  .chain, .status { display: inline-block; margin-top: 12px; }
  table, thead, tbody, tr, th, td { display: block; }
  thead { display: none; }
  tr { border-top: 1px solid var(--line); padding: 10px 0; }
  td { border: 0; padding: 6px 0; }
  .timeline li { grid-template-columns: 48px minmax(0, 1fr); }
  .timeline em, .timeline p { grid-column: 2; }
  .ladder { grid-template-columns: 1fr; }
}
"""
