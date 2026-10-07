# OpenHands keepalive — expected behavior

This workflow keeps exactly **one canonical conversation per repository** alive.

## Selection rules

1. Discover available conversations (or use seed IDs from secrets).
2. Group by `selected_repository`.
3. The **newest** conversation (by `created_at`) is canonical for that repository.
4. Older same-repo conversations are used only if the canonical sandbox is truly gone (`MISSING` / not found).
5. A **new** conversation is started only when no reusable older conversation exists and no recent start-task is already in progress for that repository.

## Nudge rules

| Sandbox | Execution | Action |
|---------|-----------|--------|
| `RUNNING` | `running` | Leave alone |
| `RUNNING` / `ERROR` | `finished` / `idle` / `stuck` / `error` | Nudge after idle timeout |
| `PAUSED` | any | Resume (cooldown), wait for `RUNNING`, never nudge in the same pass |
| `MISSING` | any | Fallback to older conversation or start a new one |
| any | agent replied `LOOP-STOP` (loop mode) or `DONE` (task mode) | Stop managing that conversation |

### Idle measurement

Idle time is `now - max(updated_at, latest event timestamp)`.

If idle time **cannot** be measured (missing timestamps / event lookup failure) but the agent is in a recoverable non-running state (`ERROR`, `finished`, `idle`, `stuck`, `error`), the conversation is treated as **past the idle timeout** and still receives a nudge. This prevents agents from getting stuck forever as `idle-unknown`.

## Continuity (loop mode)

Default nudge text instructs the agent to:

1. Continue current work
2. Pull more work from the roadmap when current tasks finish
3. Research larger missing pieces if the roadmap is empty, add recommendations, and take them into work
4. Never answer with bare `DONE` unless explicitly ending the loop with `LOOP-STOP`

## Message delivery

1. Prefer app `send-message` endpoint
2. Fall back to runtime `/events` + `/run` with session key when the app endpoint fails

## Operational notes

- Schedule runs about every 15 minutes (GitHub cron is a backup; external dispatch is preferred for reliability).
- `workflow_dispatch` inputs: `dry-run`, `discover`.
- Unit tests run before the live check; a failing test blocks nudges for that run.
- `OPENHANDS_RESUME_WAIT_SECONDS` (default `90`) and `OPENHANDS_RESUME_POLL_INTERVAL`
  (default `5`) control how long a resumed sandbox is given to reach `RUNNING`.

## Resume is asynchronous

`PAUSED -> resume` does not make a sandbox usable immediately: it reports
`STARTING` first, and OpenHands rejects `send-message` with HTTP 409 until the
sandbox is `RUNNING`. The keepalive therefore never nudges in the same pass in
which it sent a resume. It waits up to `OPENHANDS_RESUME_WAIT_SECONDS` for
`RUNNING`; if the sandbox is not ready in time the conversation is reported as
`resuming` and the next keepalive check evaluates the fresh state. The same wait
applies when an older conversation is resumed during loss recovery.

## Incident log

### 2026-10-04 — transient sandbox loss during keepalive

- Keepalive saw a canonical conversation as `sandbox=MISSING`.
- Recovery selected an older same-repository conversation whose sandbox was `PAUSED`.
- `POST .../resume` returned HTTP 200, but the sandbox was still starting.
- The immediate `send-message` then failed with HTTP 409 (`Sandbox is STARTING`).
- The conversation recovered on its own; the next keepalive run reported every
  managed repository as `RUNNING` again.

Conclusion: `MISSING` / `PAUSED -> STARTING` can be a transient recovery state. A
resume that has not become ready yet must not be treated as permanent sandbox
loss, and a nudge must never be sent in the same pass as a resume.

Repository names and conversation identifiers are intentionally not recorded here:
this repository is public.
