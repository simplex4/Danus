# Danus — Operations Runbook

Day-to-day operation of a Danus deployment: the persistent services, health checks,
recovery after a restart, and unattended-operation helpers.

> In normal operation you do not run these commands yourself — you talk to the
> **main agent** (codex), and it runs them for you. This page documents what
> happens underneath, and doubles as your fallback for the moments the main agent
> is not there to act (a fresh host restart, a session that will not start,
> debugging the stack by hand).

## The persistent services

Two services must be managed via `scripts/services.sh`, which detaches each into its own session
so it **survives your shell / SSH session ending** (a bare `&` would die with the
shell). Start them only this way.

```bash
bash scripts/services.sh up verify            # REQUIRED — no verify ⇒ fact_submit fails ⇒ no facts
bash scripts/services.sh up dashboard <p>     # optional read-only view of project <p>
bash scripts/services.sh status               # what's up (+ a verify /health probe)
bash scripts/services.sh logs <svc> [-f]      # tail a service log
bash scripts/services.sh down <svc> | all     # stop
```

- **verify** — `127.0.0.1:8091`. The correctness gate. Must be up before starting
  any workers.
- **dashboard** — `127.0.0.1:8099`, read-only. View it via an SSH port-forward
  (do not expose it to a network).

> **Shared-host caveat.** These ports are per-host, not per-deployment. If a second
> Danus deployment (another user/checkout) is already bound to `8091`, your
> `services.sh up verify` will **fail to bind** (`address already in use`). A bare
> health probe cannot tell your verify from the other one, so `/health` now
> **self-identifies with the serving process pid**: `doctor.sh` and `services.sh
> status`/`test` match that pid against your `runtime/run/verify.pid` and report the
> port as **`FAIL … answered by a FOREIGN process`** instead of a false `ok` when
> another deployment holds it. On a shared host, give each deployment its own
> `VERIFY_PORT` / `DASHBOARD_PORT` (`config/danus.env`).

`services.sh` keeps a pid registry under `runtime/run/` and an `autostart` manifest
of `up` invocations, so a restart can replay them (see recovery).

## Health checks

```bash
bash scripts/doctor.sh          # green / FAIL / warn across the whole stack
bash scripts/check-codex.sh     # one live codex ping + scan recent logs for API errors
```

- `doctor.sh` is read-only: config files, python + verify deps, node, the codex
  wrapper + backend, a live verify `/health`, and soft checks for `pdflatex` /
  Chrome. Run it whenever something looks off. A real healthy run (chatgpt backend,
  no TeX installed):

  ```
  == Danus doctor ==
  DANUS_ROOT=/home/you/Danus
    ok   config/danus.env present
    ok   config/codex.env present
    ok   python: .../runtime/venv/bin/python
    ok   python dep: mcp
    ok   python pkg: danus (importable from any cwd)
    ok   python deps: fastapi/uvicorn/pydantic
    ok   node: .../runtime/node22/bin/node
    ok   codex: codex-cli 0.142.5
    ok   codex login ok (/home/you/codex-home)
    ok   verify service up :8091 (ours)
    warn no pdflatex on PATH (write-paper PDF render needs it; set TEX_ENGINE or install TeX)
    ok   chrome: /usr/bin/chromium-browser (human-summary PDF)
  done.
  ```

  (`warn` lines are soft/optional deps, not failures; on the api backend the codex
  lines read `codex backend: api provider configured` + `codex API live ping ok`.)
- `check-codex.sh` exits `0` if the backend answered; history in
  `runtime/logs/codex-health.jsonl`. Use it when workers/verify show API errors.

## Recovery after a host restart

```bash
bash scripts/recover.sh
```

One command: re-runs `bootstrap.sh` (rebuilds the possibly-dangling venv + codex
provider), clears stale pidfiles, **replays the `runtime/run/autostart` manifest**
(brings the services back up), and prints codex + services health. Idempotent.

> Note: after a restart, worker loops are **not** auto-resumed by `recover.sh` — it
> restores the services. Before the main agent restarts workers with
> `danus start <project>` (which resumes persisted worker memory), it must pass
> the Goal check below.

### Main-agent Goal check on start and resume

Before research starts or resumes, the main agent calls `get_goal`. It may
restart workers or scouts only after a fresh result reports `active` for the
current project's authorized objective. This also applies after an interrupted
turn or session; saying "continue" and restoring services are not evidence that
the Goal is active.

If no Goal exists, the agent creates one and checks again. A completed Goal may
be replaced for newly authorized research. A paused Goal must instead be resumed
through the mechanism supported by the current Codex client. The agent must
verify that mechanism rather than guess a command: `update_goal` may not support
activation, and creating a replacement Goal is not a resume operation. If an
operator action is required, the agent explains it and waits for an `active`
result before dispatching research.

Unavailable Goal tools, status errors, other inactive states, and an objective
for a different project all leave research stopped. Status inspection and
checkpoint recovery may continue. If a later check finds research running with
an inactive Goal, the agent reports the mismatch, stops the project's workers,
interrupts its scouts, and preserves saved work before resolving the mismatch.
It checks again at each control beat and records the observed state in
pause/resume checkpoints.

This is a main-agent operating rule, not a Goal-state check implemented in the
`danus start` CLI. It detects a failed Codex Goal resume; it does not repair the
client's Goal state or guarantee that Desktop will reactivate it automatically.

## Worker lifecycle (operational view)

```bash
danus status <project>          # per-worker liveness + round + last activity
danus start  <project>          # (re)launch the worker loop(s); resumes from memory
danus stop   <project>          # graceful: finish the round, then exit
danus stop   <project> --force  # kill the process group now
```

- Workers run detached in their own process groups, so they outlive your session and
  a graceful stop lets an in-flight round finish (no lost verified work). `--force`
  kills a live codex child.
- `status` shows a `stuck?` soft signal when a running round exceeds ~1.5× the hard
  timeout — investigate (often a flaky backend); decide stop/restart.

## Unattended operation (examples, not core)

Under `examples/ops/` (parameterized; nothing in the engine depends on them):

- `main-agent-tmux.sh` — run codex (the main agent) detached in a tmux
  session, so strategic beats continue while you are away. **The only unattended
  mode.**
- `watchdog.sh <project>` — probe verify `/health` + parse `danus status`; alarm via
  a generic `DANUS_NOTIFY` hook on a `stuck?`/`dead`/`error` worker or a down verify.

## Common issues

| symptom | check |
|---|---|
| no facts appearing | is `verify` up? `services.sh status`; `doctor.sh` |
| workers erroring in rounds | `check-codex.sh`; `runtime/logs/codex-health.jsonl`; the worker's `logs/round_*.log` |
| a `paper_*` tool came back non-`ok` | read the returned `log_path` (`<project>/paper/.runs/<utc>-<tool>/log.md`) |
| dashboard blank | port-forward `:8099`; is the dashboard service up for that project? |
| after reboot, nothing runs | `recover.sh`, then `danus start <project>` |

See `configuration.md` for the variables that tune all of the above, and
`security-and-trust.md` for the trust assumptions behind the sandbox-bypassed codex
sessions.
