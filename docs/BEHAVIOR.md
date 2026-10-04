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
| `PAUSED` | any | Resume (cooldown), then nudge if still idle |
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
