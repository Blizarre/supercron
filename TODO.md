# TODO

Development phases for supercron. Items marked **done** are implemented and
tested; run `make check` before committing.

## Phase 1 — Bootstrap: config, task discovery, records — DONE
- [x] Python project skeleton (`pyproject.toml`, package layout)
- [x] `config.toml` schema + loading with defaults (`supercron/config.py`)
- [x] Task discovery from `cron/tasks/*/` and `cron.toml` parsing
      (`supercron/tasks.py`)
- [x] Per-execution TOML records + log files, atomic writes + per-task locking
      (`supercron/records.py`)

## Phase 2 — Persistent-container Docker runner — DONE
- [x] Create/reuse a long-lived container per task (`docker start`/`stop`
      analog) (`supercron/runner.py`)
- [x] Stream execution logs into the per-execution log file
- [x] Capture exit code as the source of truth for success/failure
- [x] Timeout/kill (SIGTERM -> grace -> SIGKILL)
- [x] Overlap policy: kill previous execution before starting a new one
- [x] Reset/destroy container; integration tests against the docker daemon

## Phase 3 — Scheduling engine — DONE
- [x] Parse 5-field cron expressions (per task `schedule` in `cron.toml`)
      (`supercron/cron.py`)
- [x] Next-run timer loop in the daemon (`supercron/scheduler.py`)
- [x] Manual-trigger entry point (`Daemon.run_task`)
- [x] Overlap handling on scheduled ticks (previous execution killed)
- [x] Daemon startup recovery: stale `running` records marked failed, orphan
      containers stopped (`Daemon.recover`)

## Phase 4 — HTTP callbacks
- [ ] `start` / `end_success` / `end_failure` POSTs (optional URLs in
      `cron.toml`)
- [ ] Standard JSON payload: task, id, status, return code, timestamps
- [ ] Best-effort send with timeout; log failures (no retries)

## Phase 5 — Web UI + server
- [ ] Serve history, status, and post-run logs per task
- [ ] Manual run trigger
- [ ] Container reset button
- [ ] Poll-based refresh (no live-tail)

## Phase 6 — Ops & polish
- [ ] Retention/pruning of `results/` (count/age) from `config.toml`
- [ ] Daemon service (systemd unit)
- [ ] Orphaned-container detection and cleanup on startup
- [ ] README documenting layout, config, and usage
- [ ] End-to-end tests
