"""Small HTTP server exposing the web UI and a JSON status API.

Uses only the standard library. The server reads task/record data through a
:class:`Daemon`, triggers manual runs, and resets containers. The UI is
server-rendered HTML that polls ``/api/status`` for live updates (no
websocket, no live-tail).
"""

from __future__ import annotations

import html
import json
import logging
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any, cast
from urllib.parse import unquote, urlsplit

from .cron import CronError, CronSchedule
from .scheduler import Daemon, TaskNotFound
from .tasks import utcnow

log = logging.getLogger("supercron.http")


class NotFound(Exception):
    """Raised by the view layer for unknown tasks/paths."""


class App:
    """Renders the UI and provides the JSON status API over a Daemon."""

    def __init__(self, daemon: Daemon):
        self.daemon = daemon

    # ------------------------------------------------------------ views

    def task_view(self, name: str) -> dict[str, Any]:
        task = self.daemon.task_by_name(name)
        if task is None:
            raise NotFound(name)
        records = self.daemon.store.list_records(name)
        status = "never_run"
        if records:
            last = records[-1]
            status = "running" if last.status == "running" else last.status
        next_run = None
        if task.schedule:
            try:
                next_run = CronSchedule.parse(task.schedule).next_after(utcnow())
            except CronError:
                next_run = None
        return {
            "name": task.name,
            "title": task.title,
            "schedule": task.schedule,
            "timeout": task.timeout,
            "status": status,
            "next_run": next_run.isoformat() if next_run else None,
        }

    def tasks_view(self) -> list[dict[str, Any]]:
        return [self.task_view(t.name) for t in self.daemon.tasks()]

    def history_view(self, name: str) -> list[dict[str, Any]]:
        task = self.daemon.task_by_name(name)
        if task is None:
            raise NotFound(name)
        return [rec.to_dict() for rec in self.daemon.store.list_records(name)]

    def log_view(self, name: str, eid: int) -> str:
        task = self.daemon.task_by_name(name)
        if task is None:
            raise NotFound(name)
        path = self.daemon.store.task_dir(name) / f"{eid}.log"
        if not path.is_file():
            raise NotFound(name)
        return path.read_text()

    # ------------------------------------------------------------ actions

    def trigger(self, name: str) -> None:
        self.daemon.trigger_task(name)

    def reset(self, name: str) -> None:
        self.daemon.reset_task(name)

    def reload_tasks(self) -> None:
        self.daemon.refresh()

    # ------------------------------------------------------------ html

    def render_index(self) -> str:
        rows = []
        for view in self.tasks_view():
            esc = html.escape
            name = esc(view["name"], quote=True)
            row = (
                f'<td id="status-{name}" class="status">'
                f"{_status_emoji(view['status'])} {esc(view['status'])}</td>"
                f"<td>{esc(view['title'])}</td>"
                f"<td>{esc(view['schedule'] or '')}</td>"
                f'<td id="next-{name}">{esc(view["next_run"] or "")}</td>'
                f'<td><a href="/task/{name}">history</a></td>'
            )
            row += (
                f"<td><button onclick=\"action('/task/{name}/run')\">Run</button></td>"
            )
            row += (
                f"<td><button onclick=\"action('/task/{name}/reset')\">Reset"
                "</button></td>"
            )
            rows.append(f"<tr>{row}</tr>")
        body = "<h1>supercron</h1>"
        body += "<p><button onclick=\"action('/reload')\">Reload tasks</button></p>"
        if rows:
            body += _table(("Status", "Task", "Schedule", "Next run", "", "", ""), rows)
        else:
            body += "<p>No tasks found.</p>"
        return _page(body, action_script() + index_script())

    def render_task(self, name: str) -> str:
        view = self.task_view(name)
        records = self.daemon.store.list_records(name)
        rows = []
        esc = html.escape
        safe_name = esc(view["name"], quote=True)
        url = f"/task/{safe_name}"
        for rec in reversed(records):
            exit_code = "" if rec.return_code is None else str(rec.return_code)
            rows.append(
                f"<tr><td>{rec.id}</td>"
                f'<td class="status">{esc(rec.status)}</td>'
                f"<td>{esc(rec.trigger)}</td>"
                f"<td>{exit_code}</td>"
                f"<td>{_fmt(rec.started_at)}</td>"
                f"<td>{_fmt(rec.ended_at)}</td>"
                f'<td><a href="{url}/log/{rec.id}">log</a></td></tr>'
            )
        body = (
            f"<h1>{esc(view['title'])}</h1>"
            + meta_html(view)
            + f"<button onclick=\"action('{url}/run')\">Run</button> "
            + f"<button onclick=\"action('{url}/reset')\">Reset</button> "
            + '<a href="/">back</a>'
        )
        if rows:
            body += _table(
                ("ID", "Status", "Trigger", "Exit", "Started", "Ended", ""), rows
            )
        else:
            body += "<p>No executions yet.</p>"
        return _page(body, action_script())

    def render_log(self, name: str, eid: int) -> str:
        view = self.task_view(name)
        esc = html.escape
        safe_name = esc(view["name"], quote=True)
        body = (
            f"<h1>{esc(view['title'])} · run #{eid}</h1>"
            f'<p><a href="/task/{safe_name}">back</a></p>'
            f"<pre>{esc(self.log_view(name, eid))}</pre>"
        )
        return _page(body, "")


def _status_emoji(status: str) -> str:
    return {
        "success": "✅",
        "failure": "🔴",
        "running": "🟠",
        "never_run": "⚪",
    }.get(status, "❔")


def _fmt(value: datetime | None) -> str:
    return value.isoformat(timespec="seconds") if value else ""


def meta_html(view: dict[str, Any]) -> str:
    esc = html.escape
    parts = []
    if view["schedule"]:
        parts.append(f"schedule: {esc(view['schedule'])}")
    parts.append(f'status: <span class="status">{esc(view["status"])}</span>')
    if view["next_run"]:
        parts.append(f"next run: {esc(view['next_run'])}")
    return "<p>" + " · ".join(parts) + "</p>"


def _table(headers: tuple[str, ...], rows: list[str]) -> str:
    head = "".join(f"<th>{h}</th>" for h in headers)
    return (
        f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


PAGE_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>supercron</title>
<style>
body { font-family: sans-serif; margin: 2rem; color: #222; }
table { border-collapse: collapse; margin-top: 1rem; }
th, td { border: 1px solid #ccc; padding: 0.4rem 0.7rem; text-align: left; }
pre { background: #f5f5f5; padding: 1rem; overflow-x: auto; }
button { margin-right: 0.4rem; }
.status.running { color: #b36b00; }
.status.success { color: #1a7f1a; }
.status.failure { color: #b00000; }
.status.never_run { color: #777; }
</style>
</head>
<body>
{BODY}
</body>
</html>
"""


def _page(body: str, script: str) -> str:
    return PAGE_TEMPLATE.replace("{BODY}", body + script)


def action_script() -> str:
    return (
        "<script>"
        "function action(url){"
        "fetch(url,{method:'POST'}).then(function(){location.reload();});"
        "}"
        "</script>"
    )


def index_script() -> str:
    return (
        "<script>"
        "var emojis={'success':'✅','failure':'🔴','running':'🟠','never_run':'⚪'};"
        "function poll(){fetch('/api/status').then(function(r){return r.json();})"
        ".then(function(data){data.forEach(function(t){"
        "var s=document.getElementById('status-'+t.name);"
        "if(s){"
        "s.textContent=(emojis[t.status]||'❔')+' '+t.status;"
        "s.className='status '+t.status;"
        "}"
        "var n=document.getElementById('next-'+t.name);"
        "if(n){n.textContent=t.next_run||'';}"
        "});});}"
        "setInterval(poll,2000);poll();"
        "</script>"
    )


class SupercronHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self, addr: tuple[str, int], handler: type[BaseHTTPRequestHandler], app: App
    ):
        super().__init__(addr, handler)
        self.app = app


class _Handler(BaseHTTPRequestHandler):
    server_version = "supercron/0.1"

    @property
    def app(self) -> App:
        return cast(SupercronHTTPServer, self.server).app

    # ------------------------------------------------------------ routing

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        try:
            if path in ("/", "/index.html", "/index"):
                self._send_html(self.app.render_index())
            elif path == "/api/status":
                self._send_json(self.app.tasks_view())
            elif path.startswith("/api/task/"):
                name = unquote(path[len("/api/task/") :])
                self._send_json(self.app.history_view(name))
            elif path.startswith("/task/"):
                self._send_html(self._render_task_path(path[len("/task/") :]))
            elif path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
            else:
                raise NotFound(path)
        except NotFound:
            self._send_text(404, "not found")

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        try:
            if path == "/reload":
                self.app.reload_tasks()
                self._send_json({"ok": True})
                return
            if path.startswith("/task/"):
                rest = path[len("/task/") :].split("/")
                name = unquote(rest[0])
                if len(rest) == 2 and rest[1] == "run":
                    self.app.trigger(name)
                    self._send_json({"ok": True})
                    return
                if len(rest) == 2 and rest[1] == "reset":
                    self.app.reset(name)
                    self._send_json({"ok": True})
                    return
            raise NotFound(path)
        except (NotFound, TaskNotFound):
            self._send_text(404, "not found")

    def _render_task_path(self, rest: str) -> str:
        parts = rest.split("/")
        name = unquote(parts[0])
        if len(parts) == 1:
            return self.app.render_task(name)
        if len(parts) == 3 and parts[1] == "log":
            try:
                eid = int(parts[2])
            except ValueError:
                raise NotFound(rest) from None
            return self.app.render_log(name, eid)
        raise NotFound(rest)

    # ------------------------------------------------------------ helpers

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, body: str) -> None:
        self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")

    def _send_json(self, data: object) -> None:
        self._send(200, json.dumps(data).encode("utf-8"), "application/json")

    def _send_text(self, code: int, text: str) -> None:
        self._send(code, text.encode("utf-8"), "text/plain; charset=utf-8")

    def log_message(self, message: str, *args: object) -> None:
        log.info("ui %s", message % args)


class Server:
    """Runs the web UI + JSON API on a background thread."""

    def __init__(self, daemon: Daemon, host: str = "127.0.0.1", port: int = 0):
        self.app = App(daemon)
        self._httpd = SupercronHTTPServer((host, port), _Handler, self.app)
        self._thread: Thread | None = None

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def start(self) -> None:
        self._thread = Thread(
            target=self._httpd.serve_forever, daemon=True, name="supercron-web"
        )
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread:
            self._thread.join(timeout=5)
