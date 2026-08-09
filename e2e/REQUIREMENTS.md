# supercron — End-to-End Integration Test Requirements

## 1. Goal
A black-box integration test that exercises **real Docker, real scheduling,
real timing, and the real web API/UI endpoints** of a running supercron
daemon. It drives the UI exactly the way a user does (HTTP), and cross-checks
the outcome through three independent channels: the UI API, the on-disk
`results/` store, and live Docker state.

This targets the gap that real-world testing exposed: the unit tests use fake
runners and never drive Docker with real wall-clock behavior.

It is **not** part of `make check` (currently seconds). It is an opt-in
**`make e2e`** run (~10 minutes).

## 2. Scope and non-goals
In scope:
- Spawn the **real CLI** (`python -m supercron --cron-dir <dir> --host
  127.0.0.1 --port <port>`) as a subprocess; terminate it with a signal at the
  end.
- Real Docker containers (`supercron-<task>`), real cron dispatch, real
  timeout/kill, real HTTP.
- Exactly 4 tasks (section 4). "Mock UI" = direct HTTP against the exact
  endpoints the UI uses. No browser automation.

Non-goals (deferred deliberately):
- Selenium/Playwright; HTTP lifecycle callbacks; crash + restart recovery;
  retention pressure; container-reset button; live log tailing; overlap-kill
  (covered by `tests/test_scheduler.py`).

## 3. Environment and prerequisites
- Docker CLI must be available and usable. The test **skips with a clear
  message** if `docker info` fails.
- Image: `busybox:latest` (small). The test pre-pulls/verifies it and fails
  fast if the pull fails.
- At setup, assert **no pre-existing `supercron-*` containers**; fail fast with
  a hint if any exist.
- The test owns a temp cron root; every `supercron-*` container it creates is
  force-removed in teardown, and teardown asserts none remain.
- Total wall-clock budget: ~10 minutes + daemon shutdown. Each run is fully
  deterministic (section 5), so it does not depend on the wall-clock phase at
  which the test happens to start. `E2E_WINDOW` may be set to shrink the soak
  window for debugging.

## 4. Task layout
`config.toml`:
```toml
image      = "busybox:latest"
mount_path = "/work"
timeout    = 300
kill_grace = 5
```
Retention limits are omitted, so the defaults apply (`max_executions = 100`,
`max_age_days = 30`); that is far above the ~2 runs per task this test
produces, so count assertions are never affected by pruning.

> Finding surfaced by this test: the README/example/Requirements claim a
> retention limit can be disabled with `null` (e.g. `max_executions = null`),
> but TOML has no `null`, so `tomllib` rejects such a config and the daemon
> fails to start; a limit can only be disabled by omitting the key, which
> keeps the dataclass default instead. This documentation/config mismatch is
> tracked as a separate fix.

| # | Task name | `start.sh` | schedule | timeout | purpose |
|---|-----------|------------|----------|---------|---------|
| A | `every3` | `echo; sleep 60` | computed at run time (section 5) | default (300) | scheduled task; must produce exactly 2 success runs |
| B | `manual2` | `echo; sleep 30` | none | default | manually run 2x via mock UI; never by cron |
| C | `alwaysfail` | `echo; exit 1` | none | default | manual run 1x; always fails |
| D | `timeouttask` | `echo; sleep 3600`, later replaced by `echo; exit 0` | none | 20 (per-task in `cron.toml`) | manual run; must be killed on timeout and disposed of |

Design constraints:
- Cron and manual dispatches run on **background threads** (see
  `supercron/scheduler.py::_spawn_run`), so no task can stall another; the
  overlap policy kills a still-running task at its next tick.
- **Manual runs are async**: `POST /task/<name>/run` returns `{"ok": true}`
  before the run finishes. The test must poll the record until it leaves
  `running`.
- **Timeout exit code is not exact**: on timeout the daemon runs
  `docker stop -t kill_grace`; busybox `sleep` dies via SIGTERM (143) or
  SIGKILL (137). The test asserts only: status `failure`, non-zero non-null
  `return_code`, duration in `[timeout, timeout + kill_grace + slack]`,
  container in `exited` state, and container **reusability** (a follow-up run
  succeeds).

## 5. Deterministic schedule for task A
A fixed `*/3` is nondeterministic (fires on wall-clock minutes; a 10-min
window started arbitrarily yields 3-4 runs). Instead, **derive the cron
minutes from the actual test start instant**:
1. `base` = the next minute boundary after test start.
2. Offsets `(2, 6)` -> cron expression `"M2,M6 * * * *"` where
   `Mk = (base.minute + k) % 60`.
   - Example: wall time 10:23 -> `25,29 * * * *` -> fires at 10:25 and 10:29.
   - Hour rollover (start minute >= 54, e.g. 10:58 -> minutes `0,4`) is handled
     transparently by `CronSchedule.next_after`, which scans across
     hour/day boundaries.
3. **Expected fire instants** are taken from the daemon itself: once the 4
   tasks appear in `/api/status`, `every3`'s `next_run` field is the first
   fire; the second fire is `CronSchedule.parse(expr).next_after(first)`.
   Taking the snapshot right after startup avoids any race with a minute
   boundary crossing between spawn and read.
4. Assertion: exactly **2** cron-triggered records for A, their `started_at`
   within `FIRE_TOLERANCE` (~90 s) of the predicted instants, ids 1 and 2,
   status `success`, and the runs **never overlap** (run 2 starts after run 1
   ends).

## 6. Timeline (~10 min)
1. **t≈0** - Set up cron root + `config.toml` + 4 tasks; verify image; assert
   no stray containers; compute A's schedule; pick a free HTTP port.
2. **t≈0** - Spawn `python -m supercron --cron-dir <dir> --host 127.0.0.1
   --port <port>`; read **stderr** until `supercron ready; web UI at
   http://...:PORT`; assert the port matches.
3. **t≈+5 s** - Poll `GET /api/status` until exactly the 4 task names appear;
   record `every3`'s `next_run` as the first predicted fire.
4. **t≈+0:30** - Mock-UI `POST /task/manual2/run` (B #1); poll until
   `success`.
5. **t≈+2-3 min** - A's first cron tick: poll for record #1, assert
   `trigger == "cron"`, `started_at` near the predicted instant, `success`,
   and `GET /task/every3/log/1` contains the task output.
6. **t≈+3 min** - Mock-UI `POST /task/alwaysfail/run`; assert `failure`,
   `return_code == 1` (C #1).
7. **t≈+3:30** - Mock-UI `POST /task/timeouttask/run`; poll terminal
   (~30 s). Assert D #1: `failure`, non-zero `return_code`, duration in
   `[20, 40]` s, `docker inspect` shows the container `exited`.
8. **t≈+4:30** - Disposal/reuse check: overwrite D's host `start.sh` with
   `exit 0` and re-trigger; assert D #2 `success` and the container returns to
   `exited` (proves the killed container is reusable).
9. **t≈+5:00** - Mock-UI B #2; assert `success`.
10. **t≈+6-7 min** - A's second cron tick; poll for record #2; assert
    `trigger == "cron"`, `started_at` near the predicted instant, `success`,
    and non-overlap with record #1.
11. **t≈+RUN_WINDOW (10 min)** - Final observation (daemon still up):
    - `GET /` serves HTML mentioning all four tasks.
    - `GET /api/status` shows the final status per task (A/B success, C
      failure, D success).
    - `GET /api/task/<name>` counts match: A 2, B 2, C 1, D 2; triggers and
      statuses as designed.
    - On-disk `results/<name>/<id>.toml` ids equal the API ids; every
      `results/<name>/<id>.log` is non-empty.
    - No leaked `docker logs|start|stop supercron-*` subprocesses.
12. **t≈+10:15** - `SIGTERM` the daemon; assert clean exit (rc 0).
13. **teardown** - `docker rm -f` every `supercron-*` container; assert none
    remain.

## 7. Final assertions (UI API + disk + docker)
- **A**: exactly 2 records, `trigger == "cron"`, `status == "success"`, ids
  1->2, `started_at` near predicted instants, non-empty logs, non-overlapping.
- **B**: exactly 2 records, all `trigger == "manual"`, all `success`.
- **C**: exactly 1 record, `trigger == "manual"`, `failure`, `return_code
  == 1`.
- **D**: exactly 2 records - #1 `failure` with non-zero `return_code` and
  duration in `[20, 40]` s; #2 `success` with `return_code == 0`; container
  `exited` after each run.
- **Cross-check**: API record ids/counts equal on-disk `results/<task>/N.toml`
  files and each `N.log` is non-empty.
- **Status endpoint**: `/api/status` reports A/B `success`, C `failure`,
  D `success`.

## 8. Robustness / determinism rules
- Every wait is a poll helper with an explicit deadline and a descriptive
  failure message - never a bare `sleep` + assert.
- All expected-count math derives from the actual daemon start (section 5);
  no hardcoded clock values.
- Task D cannot self-exit within the window (`sleep 3600` >> window), so only
  the daemon can stop it.
- The daemon stderr tail is included in failure messages so a CI failure can
  be diagnosed without re-running.
- On failure, the daemon is still killed in `finally` and containers are still
  removed by the fixture teardown.

## 9. Implementation / CI integration
- New `e2e/` directory (repo root): `conftest.py` (docker skip, image check,
  stray-container guard + teardown) and `test_real_docker_e2e.py` (implements
  sections 5-8).
- `Makefile`: add `make e2e` -> `python3 -m pytest e2e -q`; extend
  `lint`/`format`/`typecheck`/`check` to cover `e2e`.
- `pyproject.toml`: set `pytest.testpaths = ["tests"]` so the default `pytest
  -q` (used by `make check`) never collects the 10-minute e2e suite; add mypy
  relaxations for the e2e modules.
- `make check` stays as-is: fast, no Docker.
- ci.yml: a separate `e2e` job is deliberately **not** added yet (it would
  extend every PR by ~10 min); to be introduced on demand.