# supercron

A cron replacement written in Python: a daemon that runs tasks in isolated
persistent Docker containers, records every execution as a TOML file, fires
HTTP callbacks, and exposes a web UI. No database.

## How it works

Each task owns one long-lived container (named `supercron-<task>`), created
once with `docker run -d` and reused across executions via `docker start` /
`docker stop`. The task directory is mounted into the container, so
environment and dependencies persist between runs. The process exit code is
the source of truth: `0` = success, anything else (or a kill/timeout) =
failure.

Tasks are triggered on a 5-field cron schedule and/or manually from the web
UI. If an execution is still running when a new one is due, the previous
container run is killed first (overlap policy).

## Directory layout

```
cron/
  config.toml          # docker image, mount path, timeouts, retention
  tasks/
    <task>/
      start.sh         # entry point (required; must be executable)
      cron.toml        # title, schedule, callback urls (optional)
      <project files>  # the task's own code/data
  results/
    <task>/
      <id>.toml        # execution metadata
      <id>.log         # merged stdout+stderr for that run
```

## Installation

```sh
pip install .            # installs the `supercron` command and package
# or, for development:
pip install -e '.[dev]'
```

The daemon calls the `docker` CLI, so Docker must be installed and the user
must have permission to run it.

## Usage

Create a cron root with `config.toml` and at least one task that has a
`start.sh`:

```sh
mkdir -p /cron/tasks/mytask
cat > /cron/config.toml <<'EOF'
image = "python:3.12"
mount_path = "/work"
timeout = 300
kill_grace = 15
EOF
cat > /cron/tasks/mytask/start.sh <<'EOF'
#!/bin/sh
echo "hello from mytask"
EOF
chmod +x /cron/tasks/mytask/start.sh
```

Optionally add `cron.toml` for scheduling, per-task timeout, and HTTP
callbacks:

```toml
title = "My Task"
schedule = "*/5 * * * *"
timeout = 60

[callbacks]
start       = "https://monitoring.example/start"
end_success = "https://monitoring.example/ok"
end_failure = "https://monitoring.example/fail"
```

Then run the daemon:

```sh
supercron --cron-dir /cron --host 127.0.0.1 --port 8080
```

Open the web UI at <http://127.0.0.1:8080> to view history/status/logs, trigger
manual runs, and reset containers. A JSON status endpoint is available at
`/api/status`.

## Configuration reference

`config.toml` fields (all optional, defaults shown):

| Key             | Default        | Meaning                                    |
|-----------------|----------------|--------------------------------------------|
| `image`         | `python:3.12`  | Docker image for task containers           |
| `mount_path`    | `/work`        | Directory the task dir is mounted to       |
| `tasks_dir`     | `tasks`        | Subdirectory under the cron root           |
| `results_dir`   | `results`      | Subdirectory for records/logs              |
| `timeout`       | `300`          | Default per-run timeout in seconds         |
| `kill_grace`    | `15`           | Grace period before SIGKILL on stop        |
| `retention.max_executions` | `100` | Max records kept per task            |
| `retention.max_age_days`   | `30`   | Max age of kept records in days      |

Retention pruning runs after every execution: a record (and its log) is
removed when it falls outside the newest `max_executions` records or is older
than `max_age_days`. Set either to `null` to disable that constraint.

## HTTP callbacks

Up to three optional URLs per task fire `POST` requests with a JSON payload of
`{task, execution_id, status, return_code, started_at, ended_at}`:
`start`, `end_success` (exit 0), and `end_failure` (non-zero / killed /
timed out). Delivery is best-effort: a short timeout, a single attempt, and
failures are reported to the error handler rather than retried.

## Running as a systemd service

A unit file is provided in `supercron.service`. Adjust the `ExecStart` path
and `--cron-dir`, then:

```sh
sudo cp supercron.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now supercron
```

## Development

```sh
make format   # auto-format with ruff
make check    # ruff lint + mypy (strict) + pytest
```

Run the test suite with `make check` before committing.

See `Requirements.md` for the full specification and `TODO.md` for phase
status.
