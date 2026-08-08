# supercron — Requirements

A replacement for cron written in Python: a daemon that runs tasks in isolated
containers, records every execution as a TOML file, fires HTTP callbacks, and
exposes a web UI to view history/status and trigger manual runs. No database.

## Trigger model
- **Both**: scheduled (5-field cron expression in `cron.toml`) **and** manual
  (button in the UI).
- **Overlap policy**: if an execution is still running when a new run is due
  (cron or manual), **kill the previous container execution and start a new
  one**. Kill = SIGTERM -> grace period -> SIGKILL. The new run takes over;
  the skipped tick is not recorded as a separate execution.

## Directory layout
```
cron/
  config.toml          # docker image, mount path, timeouts, retention, defaults
  tasks/
    <task>/
      start.sh         # entry point (required)
      cron.toml        # title, schedule, callback urls (optional w/ defaults)
      <project files>  # the task's own code/data
  results/
    <task>/
      <id>.toml        # execution metadata
      <id>.log         # execution log (separate file)
```

## Container execution (Docker)
- Runtime: **Docker**.
- **Persistent container**: each task owns one long-lived container (named
  `supercron-<task>`). It is created once, then each execution is a
  `docker start` / `docker stop` on that same container — the analog of
  `docker start`. The container does **not** stop when idle; it persists and
  is reused. Environment and any dependency installation persist across runs.
  To recreate it from scratch there must be a way to destroy/recreate the
  container (e.g. a reset button or a version bump).
- The task directory (`cron/tasks/<task>`) is **mounted as a volume** at a
  fixed path (e.g. `/work`), and the working directory is set there. Project
  files live on the host and are shared into the container.
- Entrypoint executes `start.sh` (must be present and executable; validated on
  install). `start.sh` is responsible for environment setup *inside* the
  container.
- **Return code is the source of truth for status**: exit code `0` =
  success, any non-zero (or kill/timeout) = failure.
- **Timeout/kill**: if an execution does not stop by itself, the daemon kills
  it (SIGTERM, grace period, SIGKILL). Timeout configurable in `config.toml`.

## Container lifecycle states
- A container is only ever created or mutated by the daemon.
- Creating one must be idempotent: if the named container already exists,
  reuse it; otherwise `docker run` to create it. The schedule intentionally
  uses `docker start`/`docker stop` on the persistent container.

## Data model (per execution)
- **ID**: per-task self-incrementing counter
  (`results/<task>/1.toml`, `2.toml`, ...). Concurrency-safe via a per-task
  lockfile and atomic writes (write temp file, then `os.replace`; next id =
  max existing id + 1 while holding the lock).
- **`<id>.toml` fields**: `id`, `task`, `status`
  (`running|success|failure`), `started_at`, `ended_at`, `return_code`,
  `log_file`, `trigger` (`cron|manual|overridden`), and `previous_killed`
  flag when an overlap-kill occurred.
- **`<id>.log`**: merged stdout+stderr, timestamped, written during the run.
  Shown post-run only; no live-tail in the UI.
- **Current task status** derived from latest record:
  `running` (record without `ended_at`) -> `success` / `failure` /
  `never_run`.

## HTTP callbacks (3 URLs)
`cron.toml` defines up to three optional URLs:
```toml
[callbacks]
start        = "https://monitoring.example/start"
end_success  = "https://monitoring.example/ok"
end_failure  = "https://monitoring.example/fail"
```
- A callback only fires if its URL is present.
- `POST` JSON payload (same shape for all three; the URL selects the event):
  `{task, execution_id, status, return_code, started_at, ended_at}`.
- `start` fires when the execution begins. Exactly **one** terminal callback
  fires at the end: `end_success` (exit 0) or `end_failure` (non-zero /
  killed / timeout).
- Black box: no response parsing; best-effort with a small timeout; failures
  are logged, not retried.

## Web server + UI (Python)
- View per-task and per-execution **history**, **status**, and **post-run
  logs**.
- **Trigger a manual run** for any task.
- **Reset a task's container** (destroy + recreate) so environments can be
  rebuilt.
- Poll-based refresh (no websocket needed; no live logs).
- Daemon and web server run together, started via the `supercron` CLI
  (`supercron --cron-dir /cron --host 0.0.0.0 --port 8080`); a systemd unit is
  provided in `supercron.service`.

## Daemon startup recovery
- On startup the daemon marks records left `running` by a crashed run as
  `failure`, stops orphaned still-running containers, and removes any
  `supercron-*` container that no longer corresponds to a known task.

## Concurrency & atomicity
- Per-task execution lock: at most one active execution per task.
- Per-task id lockfile serializes id assignment; atomic file writes for
  records and logs.
- Daemon discovers tasks by scanning `cron/tasks/*/` and validating each has a
  `start.sh`.

## Logging, retention, cleanup
- daemon logs to stdout (not part of execution data).
- Configurable retention in `config.toml` (`retention.max_executions` and
  `retention.max_age_days`): `results/<task>/` is pruned when execution count
  or age exceeds the limit, so the results dir does not grow unbounded.
  Pruning runs after every execution; either limit may be disabled with
  `null`.

## config.toml
```toml
image      = "python:3.12"   # docker image for task containers
mount_path = "/work"         # path the task dir is mounted to in the container
tasks_dir  = "tasks"         # subdirectory under the cron root
results_dir = "results"      # subdirectory for records/logs
timeout    = 300             # default per-run timeout (seconds)
kill_grace = 15              # SIGKILL grace period (seconds)

[retention]
max_executions = 100         # max records kept per task (null = unlimited)
max_age_days   = 30          # max age kept (null = unlimited)
```

## Implementation
All phases are implemented and covered by tests (`make format` / `make check`):
1. **Bootstrap**: Python project, `config.toml` schema, task discovery,
   execution-record TOML + log writers with atomic writes and per-task locks.
2. **Container runner**: persistent-container create/reuse, `docker start`/
   `stop` lifecycle, mount, exit-code capture, timeout/kill, overlap-kill.
3. **Scheduling engine**: parse cron expressions, next-run timer, manual-trigger
   endpoint, overlap handling, startup recovery.
4. **Callbacks**: `start` / `end_success` / `end_failure` POSTs.
5. **Web UI**: history/status/log views, manual trigger, container reset
   (poll-based).
6. **Ops**: retention/pruning, `supercron` CLI + systemd unit, orphaned-container
   cleanup, README, end-to-end tests.
