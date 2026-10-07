# OpenHands keepalive — expected behavior

This workflow keeps exactly **one canonical conversation per repository** alive.

> Tämä repositorio on julkinen, joten Actions-logit ja step summaryt ovat julkisia.
> Tässä dokumentissa ei ole conversation-UUID:ita, salaisuuksia eikä yksityisten
> repositorioiden nimiä. Skripti peittää ne myös lokituksesta oletuksena.

## Selection rules

1. Discover available conversations (or use seed IDs from secrets).
2. Group by `selected_repository`.
3. The **newest** conversation (by `created_at`) is canonical.
4. If the newest conversation's sandbox is `MISSING`, the **newest conversation
   that still has a sandbox** becomes canonical instead. A dead conversation must
   not keep a repository stuck in a permanent recovery loop.
5. Loss recovery runs only when **every** candidate for that repository is
   `MISSING` (or not found): reuse the newest older conversation, or start a new
   one when no reusable conversation exists and no recent start-task is active.
6. A conversation that is resumed or replaced automatically becomes canonical on
   the next run, because it is the newest candidate with a live sandbox.

## Nudge rules

| Sandbox | Execution | Action |
|---------|-----------|--------|
| `RUNNING` | `running` | Leave alone |
| `RUNNING` / `ERROR` | `finished` / `idle` / `stuck` / `error` / unknown | Nudge once idle passes the threshold |
| `PAUSED` | any | Resume (cooldown). Never nudge in the same pass as a resume; nudge on a later pass if still idle |
| `MISSING` | any | Loss recovery (older conversation, or a new one) |
| any | agent stopped in `task` mode with exactly `DONE` | Stop managing that conversation |

### Loop mode never stops

In `loop` mode there is **no stop token**. An agent cannot end the loop by
replying with a single word, so a conversation can no longer die silently.

When the agent runs out of ordinary work, the nudge instructs it to:

1. pick a topic **it considers genuinely important** and not yet covered,
2. research it thoroughly,
3. open a new pull request with the findings and recommendations, and
4. **not merge it** — the PR is left for a human to review.

`DONE` still stops a conversation in `task` mode, where a conversation represents
one bounded task.

### Idle measurement and nudge rate

Idle time is `now - max(updated_at, latest event timestamp)`.

A nudge is only sent when idle exceeds **`max(idle-timeout, min-nudge-interval)`**.
With the defaults (900s / 1800s) the same conversation is nudged at most once per
30 minutes, even though the workflow runs roughly every 15 minutes. This is what
stops an unmeasurable-idle conversation from being nudged on every single run.

### Stalled escalation

Every nudge is a user message. If the agent never answers, unanswered user
messages accumulate at the end of the conversation. When that count reaches
`OPENHANDS_MAX_STALLED_NUDGES`, the conversation is reported as `stalled` and
treated as lost: recovery is triggered instead of nudging it forever.

### Idle that cannot be measured

If idle time cannot be measured (missing timestamps / event lookup failure) but
the agent is in a recoverable non-running state (`ERROR`, `PAUSED`, `finished`,
`idle`, `stuck`, `error`), the conversation is treated as **past the threshold**
and still nudged. This prevents agents from getting stuck as `idle-unknown`.
The check is applied to every recoverable sandbox state, including `PAUSED`.

## Resume handling

`PAUSED -> resume` is asynchronous: the sandbox reports `STARTING` before it is
`RUNNING`, and OpenHands rejects `send-message` with HTTP 409 while it is not
ready. Therefore:

- after sending a resume, the keepalive does **not** nudge in the same pass — the
  next pass evaluates the fresh state;
- a recovery path that resumes an older conversation waits for `RUNNING` before
  using it (see `wait_for_resumed_sandbox`).

## Message delivery

1. Prefer the app `send-message` endpoint.
2. Fall back to runtime `/events` + `/run` with the session key when the app
   endpoint fails — including HTTP 409/410 (sandbox not ready).
3. A transient HTTP status (429/5xx) is retried once, respecting `Retry-After`.

## Scheduling and reliability

- GitHub's own `schedule` is unreliable in practice: in a sample of 100 runs only
  4 were schedule-triggered and 96 came from an external `workflow_dispatch`.
  The real cadence comes from an external timer (cron-job.org) hitting the
  dispatch API. That timer is **not** stored in this repository.
- `keepalive-deadman.yml` is the dead-man's switch: it fails when no successful
  keepalive run completed within the last ~45 minutes. Because the dead-man also
  relies on GitHub's schedule, cron-job.org should additionally alert when a
  dispatch fails.
- `OPENHANDS_RUN_BUDGET_SECONDS` bounds a `--once` run. Discovery pagination,
  start-task polling and the recovery waits all respect it, so the job still
  writes its summary instead of dying on `timeout-minutes`.

## Observability

The step summary contains the per-repository outcome table plus two explicit
sections:

- **At risk** — `stalled`, `nudge-failed`, `replacement-failed`, `http-error`,
  `net-error`, `error`, `sandbox-missing`, `not-found`, `budget-exhausted`.
- **Needs human** — `confirmation` (`waiting_for_confirmation`). This state was
  previously ignored silently, so a conversation blocked on a confirmation looked
  alive while nothing could progress.

Set `OPENHANDS_FAIL_ON_ATTENTION=true` to make the run fail when something needs a
human.

## Public repository

Actions logs and step summaries of a public repository are public. By default the
script redacts repository names (`repo#1`, `repo#2`, ...), conversation titles
(`'<redacted>'`) and prints only the first 8 characters of a conversation UUID.
`OPENHANDS_VERBOSE=1` disables redaction and is meant for local runs only.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `OPENHANDS_API_KEY` | — | required |
| `OPENHANDS_CONVERSATION_IDS` | — | seed IDs / bootstrap scope |
| `OPENHANDS_SKIP_IDS` | — | conversations to ignore |
| `OPENHANDS_NUDGE` | built-in | override nudge text |
| `OPENHANDS_NUDGE_MODE` | `loop` | `loop` (never stops) or `task` (`DONE` stops) |
| `OPENHANDS_IDLE_TIMEOUT` | `900` | idle seconds before a nudge is considered |
| `OPENHANDS_MIN_NUDGE_INTERVAL` | `1800` | minimum seconds between nudges of the same conversation |
| `OPENHANDS_MAX_STALLED_NUDGES` | `4` | unanswered nudges before a conversation is treated as stalled |
| `OPENHANDS_RESUME_COOLDOWN` | `900` | how often a `PAUSED` resume may be retried |
| `OPENHANDS_RUN_BUDGET_SECONDS` | `420` | wall-clock budget for one `--once` run (`0` disables) |
| `OPENHANDS_FAIL_ON_ATTENTION` | `false` | fail the run when a conversation needs a human |
| `OPENHANDS_VERBOSE` | `false` | local only: disable redaction |
| `OPENHANDS_AUTO_DISCOVER` | `false` | discover conversations instead of using seeds |

Secrets are configured in repository settings, never in this file.
