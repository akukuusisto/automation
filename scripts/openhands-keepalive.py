#!/usr/bin/env python3
"""OpenHands Cloud keepalive / one-canonical-conversation-per-repository.

Normal operation selects only the newest conversation for each repository.
An older conversation is considered only after the newest conversation's
sandbox is genuinely gone (MISSING/not found). This prevents parallel agents
from the same repository while still allowing recovery to an older conversation.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time

try:
    import requests
except ImportError:
    print("requests puuttuu. Asenna:\n  python -m pip install requests", file=sys.stderr)
    sys.exit(1)


class ConversationNotFound(RuntimeError):
    """Raised only when OpenHands confirms that a conversation no longer exists."""


DEFAULT_NUDGE_LOOP = (
    "Jatka autonomista looppia ty\u00f6nkulkusi mukaan.\n\n"
    "1) Tarkista nykyinen tila (git, avoimet ty\u00f6t, roadmap).\n"
    "2) Jatka kesken olevaa ty\u00f6t\u00e4 ilman turhaa selittely\u00e4.\n"
    "3) Ennen uuden ty\u00f6n aloittamista tarkista git, avoimet PR:t ja issue-ty\u00f6 "
    "sek\u00e4 varmista, ettei toinen agentti tee samaa ty\u00f6t\u00e4. V\u00e4lt\u00e4 duplikaatit.\n"
    "4) Jos nykyiset teht\u00e4v\u00e4t loppuvat: ota lis\u00e4\u00e4 t\u00f6it\u00e4 roadmapilta "
    "tai avoimista issueista (kun s\u00e4\u00e4nn\u00f6t sen sallivat).\n"
    "5) Jos roadmapkin on tyhj\u00e4: tutki mit\u00e4 sovelluksesta puuttuu "
    "isommana kokonaisuutena, lis\u00e4\u00e4 suosituksesi roadmapille ja "
    "ota se ty\u00f6st\u00f6\u00f6n.\n"
    "6) Pushaa muutokset normaalilla kadenssilla. \u00c4l\u00e4 mergaa "
    "suojattuihin haaroihin ilman erillist\u00e4 ohjetta.\n\n"
    "\u00c4l\u00e4 vastaa pelk\u00e4ll\u00e4 DONE. Jos looppi on tietoisesti lopetettava, "
    "vastaa t\u00e4sm\u00e4lleen:\n"
    "LOOP-STOP"
)

DEFAULT_NUDGE_TASK = (
    "Jatka teht\u00e4v\u00e4\u00e4 siit\u00e4 mihin j\u00e4it.\n\n"
    "Tarkista ensin nykyinen tila ja jatka itse teht\u00e4v\u00e4n suorittamista "
    "ilman turhaa selittely\u00e4.\n\n"
    "Jos teht\u00e4v\u00e4 on t\u00e4ysin valmis, vastaa t\u00e4sm\u00e4lleen:\n"
    "DONE\n\n"
    "Jos teht\u00e4v\u00e4 ei ole viel\u00e4 valmis, jatka ty\u00f6skentely\u00e4 ja vie teht\u00e4v\u00e4 "
    "mahdollisimman pitk\u00e4lle."
)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "")
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def parse_args():
    parser = argparse.ArgumentParser(description="OpenHands conversation keepalive")
    parser.add_argument("--conversation-id", action="append", default=None)
    parser.add_argument("--base-url", default=os.getenv("OPENHANDS_BASE_URL", "https://app.all-hands.dev"))
    mode = os.getenv("OPENHANDS_NUDGE_MODE", "loop").lower()
    if mode not in ("loop", "task"):
        mode = "loop"
    parser.add_argument("--nudge-mode", choices=("loop", "task"), default=mode)
    parser.add_argument("--nudge", default=None)
    parser.add_argument("--interval", type=int, default=int(os.getenv("OPENHANDS_POLL_INTERVAL", "120")))
    parser.add_argument("--idle-timeout", type=int, default=int(os.getenv("OPENHANDS_IDLE_TIMEOUT", "900")))
    parser.add_argument("--resume-cooldown", type=int, default=int(os.getenv("OPENHANDS_RESUME_COOLDOWN", "900")))
    parser.add_argument("--resume-wait-seconds", type=int, default=int(os.getenv("OPENHANDS_RESUME_WAIT_SECONDS", "90")))
    parser.add_argument("--resume-poll-interval", type=int, default=int(os.getenv("OPENHANDS_RESUME_POLL_INTERVAL", "5")))
    parser.add_argument("--dry-run", action="store_true", default=_env_bool("OPENHANDS_DRY_RUN"))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--discover", action="store_true", default=_env_bool("OPENHANDS_AUTO_DISCOVER"))
    parser.add_argument("--discover-limit", type=int, default=int(os.getenv("OPENHANDS_DISCOVER_LIMIT", "50")))
    parser.add_argument("--discover-max-pages", type=int, default=int(os.getenv("OPENHANDS_DISCOVER_MAX_PAGES", "100")))
    parser.add_argument("--no-done-check", action="store_true", default=_env_bool("OPENHANDS_NO_DONE_CHECK"))
    args = parser.parse_args()
    if args.nudge is None:
        args.nudge = DEFAULT_NUDGE_LOOP if args.nudge_mode == "loop" else DEFAULT_NUDGE_TASK
    env_nudge = os.getenv("OPENHANDS_NUDGE", "").strip()
    if env_nudge:
        args.nudge = env_nudge
    return args


def resolve_conversation_ids(args):
    ids = []
    if args.conversation_id:
        ids.extend(args.conversation_id)
    for raw in os.getenv("OPENHANDS_CONVERSATION_IDS", "").split(","):
        if raw.strip():
            ids.append(raw.strip())
    singular = os.getenv("OPENHANDS_CONVERSATION_ID", "").strip()
    if singular:
        ids.append(singular)
    seen, unique = set(), []
    for x in ids:
        if x and x not in seen:
            seen.add(x)
            unique.append(x)
    return unique


def resolve_skip_ids():
    return {p.strip() for p in os.getenv("OPENHANDS_SKIP_IDS", "").split(",") if p.strip()}


def parse_updated_at(value: str) -> float:
    if not value:
        return 0.0
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def get_conversation(base_url, headers, conversation_id):
    r = requests.get(
        f"{base_url}/api/v1/app-conversations",
        headers=headers,
        params={"ids": conversation_id},
        timeout=30,
    )
    if r.status_code == 404:
        raise ConversationNotFound(conversation_id)
    r.raise_for_status()
    items = r.json()
    if not items or not items[0]:
        raise ConversationNotFound(conversation_id)
    return items[0]


def conversation_created_ts(conversation: dict) -> float:
    """Use creation time for canonical ordering, with updated_at as a fallback."""
    return parse_updated_at(conversation.get("created_at", "")) or parse_updated_at(
        conversation.get("updated_at", "")
    )


def discover_conversations(base_url, headers, limit=50, max_pages=100):
    """Return all available conversations, paginating through the V1 search API."""
    all_items = []
    seen = set()
    page_id = None
    try:
        for page in range(1, max_pages + 1):
            params = {"limit": limit}
            if page_id:
                params["page_id"] = page_id
            r = requests.get(
                f"{base_url}/api/v1/app-conversations/search",
                headers=headers,
                params=params,
                timeout=45,
            )
            r.raise_for_status()
            payload = r.json()
            if isinstance(payload, dict):
                items = payload.get("items") or payload.get("results") or []
                next_page_id = payload.get("next_page_id")
            elif isinstance(payload, list):
                items = payload
                next_page_id = None
            else:
                items = []
                next_page_id = None

            for item in items:
                if not isinstance(item, dict):
                    continue
                cid = (item.get("id") or "").strip()
                if cid and cid not in seen:
                    seen.add(cid)
                    all_items.append(item)

            if not next_page_id or next_page_id == page_id:
                break
            page_id = next_page_id
        else:
            print(f"  discover: max pages {max_pages} reached")
        print(f"  discover: {len(all_items)} conversations")
        return all_items
    except Exception as exc:
        print(f"  discover epäonnistui: {exc}")
        return []


def _repository_from_conversation(conversation):
    if not isinstance(conversation, dict):
        return ""
    repository = conversation.get("selected_repository")
    return repository.strip() if isinstance(repository, str) else ""


def group_conversations_by_repository(items, skip_ids=None):
    groups = {}
    skip_ids = skip_ids or set()
    for item in items:
        if not isinstance(item, dict):
            continue
        cid = (item.get("id") or "").strip()
        repository = _repository_from_conversation(item)
        if not cid or not repository or cid in skip_ids:
            continue
        groups.setdefault(repository, []).append(item)

    for repository, candidates in groups.items():
        candidates.sort(
            key=lambda item: (conversation_created_ts(item), str(item.get("id") or "")),
            reverse=True,
        )
        groups[repository] = candidates
    return groups


def select_latest_per_repository(groups):
    """Return exactly one candidate—the newest conversation—for each repository."""
    return {
        repository: candidates[0]
        for repository, candidates in groups.items()
        if candidates
    }


def _coerce_text(value):
    try:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "".join(_coerce_text(i) for i in value if _coerce_text(i))
        if isinstance(value, dict):
            if isinstance(value.get("text"), str):
                return value["text"]
            if "content" in value:
                return _coerce_text(value["content"])
        return ""
    except Exception:
        return ""


def _event_text(event):
    try:
        if not isinstance(event, dict):
            return ""
        for key in ("llm_message", "message", "content", "text"):
            t = _coerce_text(event.get(key))
            if t:
                return t
        return ""
    except Exception:
        return ""


def _event_timestamp(event):
    try:
        return parse_updated_at(event.get("timestamp", "")) if isinstance(event, dict) else 0.0
    except Exception:
        return 0.0


def is_stop_message(events, nudge_mode: str) -> bool:
    try:
        messages = [e for e in (events or []) if isinstance(e, dict) and e.get("source") in ("agent", "user") and _event_text(e).strip()]
        if not messages:
            return False
        if any(_event_timestamp(e) > 0 for e in messages):
            messages.sort(key=_event_timestamp, reverse=True)
        else:
            messages = messages[::-1]
        latest = messages[0]
        if latest.get("source") != "agent":
            return False
        text = _event_text(latest).strip().upper()
        return text == "LOOP-STOP" if nudge_mode == "loop" else text == "DONE"
    except Exception:
        return False


def fetch_recent_events(base_url, headers, conversation_id, limit=20):
    try:
        r = requests.get(
            f"{base_url}/api/v1/conversation/{conversation_id}/events/search",
            headers=headers,
            params={"limit": limit, "sort_order": "TIMESTAMP_DESC"},
            timeout=30,
        )
        r.raise_for_status()
        payload = r.json()
        if isinstance(payload, dict):
            for key in ("items", "results", "events"):
                if isinstance(payload.get(key), list):
                    return payload[key]
            return []
        return payload if isinstance(payload, list) else []
    except Exception as exc:
        print(f"  event-haku ep\u00e4onnistui: {exc}")
        return None


def latest_activity_ts(base_url, headers, conversation_id, updated_ts: float):
    events = fetch_recent_events(base_url, headers, conversation_id, limit=10)
    if events is None:
        return updated_ts
    best = updated_ts or 0.0
    for event in events:
        ts = _event_timestamp(event)
        if ts > best:
            best = ts
    return best


def try_resume(base_url, headers, sandbox_id, dry_run):
    if not sandbox_id:
        print("  resume: sandbox_id puuttuu")
        return False
    if dry_run:
        print("  DRY-RUN: resumea ei l\u00e4hetetty")
        return True
    try:
        r = requests.post(f"{base_url}/api/v1/sandboxes/{sandbox_id}/resume", headers=headers, timeout=30)
        print(f"  resume -> {r.status_code} {r.text[:200]}")
        return r.status_code < 300
    except Exception as exc:
        print(f"  resume ep\u00e4onnistui: {exc}")
        return False



def wait_for_resumed_sandbox(base_url, headers, conversation_id, sandbox_id, timeout=90, interval=5):
    """Wait for a resumed sandbox to become RUNNING before using the conversation.

    Resuming is asynchronous: the sandbox reports STARTING first and OpenHands
    rejects send-message with HTTP 409 until it is RUNNING.
    """
    timeout = max(0, int(timeout))
    interval = max(1, int(interval))
    deadline = time.time() + timeout
    attempt = 0
    while True:
        attempt += 1
        try:
            conversation = get_conversation(base_url, headers, conversation_id)
        except ConversationNotFound:
            print(f"  resume wait: conversation {conversation_id[:8]} disappeared")
            return False
        except requests.RequestException as exc:
            print(f"  resume wait -haku ep\u00e4onnistui: {exc}")
            return False

        sandbox_status = conversation.get("sandbox_status")
        print(f"  resume poll {attempt}: sandbox={sandbox_status}")
        if sandbox_status == "RUNNING":
            return True
        if sandbox_status in ("MISSING", "ERROR"):
            return False
        if time.time() >= deadline:
            print(f"  resume wait timeout after {timeout}s (sandbox={sandbox_status})")
            return False
        time.sleep(min(interval, max(1, deadline - time.time())))


def send_nudge(base_url, headers, conversation_id, text, dry_run, conversation=None):
    """Send a user nudge to an existing conversation, with runtime fallback."""
    if dry_run:
        print("  DRY-RUN: nudgea ei lähetetty")
        return True

    payload = {
        "role": "user",
        "run": True,
        "content": [{"type": "text", "text": text}],
    }
    try:
        r = requests.post(
            f"{base_url}/api/v1/app-conversations/{conversation_id}/send-message",
            headers=headers,
            timeout=30,
            json=payload,
        )
        if r.status_code < 300:
            print(f"  send-message OK: {r.text[:200]}")
            return True
        print(f"  send-message -> {r.status_code}: {r.text[:200]}")
        if r.status_code in (409, 410):
            return False
    except Exception as exc:
        print(f"  send-message virhe: {exc}")

    conv = conversation or {}
    conv_url = (conv.get("conversation_url") or "").rstrip("/")
    session_key = conv.get("session_api_key") or ""
    if not conv_url or not session_key:
        print("  fallback: conversation_url/session_api_key puuttuu")
        return False

    try:
        rh = {"X-Session-API-Key": session_key, "Content-Type": "application/json"}
        r = requests.post(
            f"{conv_url}/events",
            headers=rh,
            timeout=30,
            json=payload,
        )
        if r.status_code < 300:
            print(f"  runtime events OK: {r.text[:200]}")
            try:
                rr = requests.post(
                    f"{conv_url}/run",
                    headers=rh,
                    timeout=15,
                )
                print(f"  runtime run -> {rr.status_code}")
            except Exception:
                pass
            return True
        print(f"  runtime events -> {r.status_code}: {r.text[:200]}")
        return False
    except Exception as exc:
        print(f"  runtime fallback virhe: {exc}")
        return False


def start_conversation(base_url, headers, repository, text, dry_run, poll_attempts=12):
    """Start a replacement conversation through the OpenHands V1 API.

    V1 creation is asynchronous: POST creates a start task and the
    conversation ID becomes available when that task reaches READY.
    """
    if not repository:
        print("  start: selected_repository puuttuu")
        return None
    if dry_run:
        print(f"  DRY-RUN: uusi conversation -> {repository}")
        return "DRY-RUN"

    payload = {
        "initial_message": {"content": [{"type": "text", "text": text}]},
        "selected_repository": repository,
    }
    try:
        r = requests.post(
            f"{base_url}/api/v1/app-conversations",
            headers=headers,
            timeout=45,
            json=payload,
        )
        r.raise_for_status()
        task = r.json()
        task_id = task.get("id")
        conversation_id = task.get("app_conversation_id")
        status = task.get("status")
        print(f"  start -> status={status} task={task_id or '-'} conversation={conversation_id or '-'}")

        if conversation_id:
            return conversation_id
        if not task_id:
            print("  start: response ei sisältänyt start-task ID:tä")
            return None

        for attempt in range(poll_attempts):
            time.sleep(5)
            r = requests.get(
                f"{base_url}/api/v1/app-conversations/start-tasks",
                headers=headers,
                params={"ids": task_id},
                timeout=30,
            )
            r.raise_for_status()
            tasks = r.json()
            item = tasks[0] if isinstance(tasks, list) and tasks else tasks
            if not isinstance(item, dict):
                continue
            status = item.get("status")
            conversation_id = item.get("app_conversation_id")
            print(f"  start-task poll {attempt + 1}/{poll_attempts}: {status}")
            if status == "READY" and conversation_id:
                print(f"  uusi conversation valmis: {conversation_id}")
                return conversation_id
            if status == "ERROR":
                print(f"  start-task ERROR: {item.get('error', 'Unknown error')}")
                return None
        print("  start-task timeout; uusi conversation valmistuu mahdollisesti myöhemmin")
        return None
    except requests.RequestException as exc:
        print(f"  start epäonnistui: {exc}")
        return None



def _repository_from_start_task(task):
    """Extract the selected repository from an OpenHands start-task payload."""
    if not isinstance(task, dict):
        return ""
    repository = task.get("selected_repository")
    if isinstance(repository, str):
        return repository.strip()

    request = task.get("request")
    if isinstance(request, str):
        try:
            request = json.loads(request)
        except json.JSONDecodeError:
            request = None

    if isinstance(request, dict):
        repository = request.get("selected_repository") or request.get("repository")
        if isinstance(repository, str):
            return repository.strip()
    return ""


def has_recent_start_task(
    base_url, headers, repository, lookback_seconds=1800, limit=50
):
    """Return whether a recent non-terminal start task targets this repository.

    A successful conversation-start request can temporarily exist only as a
    start-task before the conversation is visible to discovery. Checking these
    tasks prevents a scheduled retry from creating a duplicate conversation.

    Search failures fail closed: a replacement is skipped rather than risking
    another concurrent OpenHands conversation for the same repository.
    """
    if not repository:
        return False

    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        seconds=lookback_seconds
    )
    try:
        response = requests.get(
            f"{base_url}/api/v1/app-conversations/start-tasks/search",
            headers=headers,
            params={
                "limit": limit,
                "created_at__gte": since.isoformat(timespec="milliseconds").replace(
                    "+00:00", "Z"
                ),
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()

        if isinstance(payload, dict):
            items = payload.get("items") or payload.get("results") or []
        elif isinstance(payload, list):
            items = payload
        else:
            items = []

        active_statuses = {
            "WORKING",
            "WAITING_FOR_SANDBOX",
            "PREPARING_REPOSITORY",
            "SETTING_UP_SKILLS",
            "READY",
        }
        for task in items:
            if not isinstance(task, dict):
                continue
            status = str(task.get("status", "")).upper()
            if (
                status in active_statuses
                and _repository_from_start_task(task) == repository
            ):
                print(
                    f"  start-task: aktiivinen/recent löytyy jo: "
                    f"{repository} ({status})"
                )
                return True

        return False
    except Exception as exc:
        print(
            f"  start-task-haku epäonnistui: {exc} -> "
            "oletetaan start olevan mahdollinen ja estetään uusi"
        )
        return True


def check_conversation(base_url, headers, conv_id, args, state):
    dry_run = args.dry_run
    nudges = state["nudges"].get(conv_id, 0)
    try:
        conversation = get_conversation(base_url, headers, conv_id)
        sandbox_status = conversation.get("sandbox_status")
        execution_status = conversation.get("execution_status")
        updated_at = conversation.get("updated_at", "")
        title = conversation.get("title", "")
        sandbox_id = conversation.get("sandbox_id", "")
        now = time.time()
        updated_ts = parse_updated_at(updated_at)
        activity_ts = updated_ts
        if execution_status != "running" and sandbox_status in ("RUNNING", "PAUSED", "ERROR"):
            activity_ts = latest_activity_ts(base_url, headers, conv_id, updated_ts)
        idle_for = max(0, int(now - activity_ts)) if activity_ts else None
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        idle_text = f"{idle_for}s idle" if idle_for is not None else "idle unknown"
        print(f"[{ts}] [{conv_id[:8]}] sandbox={sandbox_status} exec={execution_status} {idle_text} title={title!r}")

        if sandbox_status == "MISSING":
            if not args.no_done_check:
                recent = fetch_recent_events(base_url, headers, conv_id)
                if recent is not None and is_stop_message(recent, args.nudge_mode):
                    return "done"
            print("  Sandbox MISSING: canonical conversation is lost")
            return "sandbox-missing"

        if sandbox_status == "ERROR":
            # Sandbox ERROR is recoverable in practice: keep the same conversation
            # and use the normal execution-idle nudge path below.
            pass

        if sandbox_status == "PAUSED":
            last_resume = state["last_resume"].get(conv_id, 0.0)
            if now - last_resume >= args.resume_cooldown:
                print("  Sandbox PAUSED -> resume...")
                resumed = try_resume(base_url, headers, sandbox_id, dry_run)
                state["last_resume"][conv_id] = now
                if not resumed:
                    print(
                        "  Resume ei onnistunut -> "
                        "odotetaan seuraavaa keepalive-kierrosta"
                    )
                    return "paused"
                if dry_run:
                    print("  DRY-RUN: sandboxin palautumista ei odotettu")
                elif not wait_for_resumed_sandbox(
                    base_url,
                    headers,
                    conv_id,
                    sandbox_id,
                    timeout=args.resume_wait_seconds,
                    interval=args.resume_poll_interval,
                ):
                    print(
                        "  Sandbox ei ehtinyt RUNNING-tilaan -> "
                        "odotetaan seuraavaa keepalive-kierrosta"
                    )
                    return "resuming"
                # Resume on asynkroninen. Nudgea ei l\u00e4hetet\u00e4 samalla
                # kierroksella vanhan PAUSED-tilan perusteella: seuraava
                # keepalive-kierros arvioi tuoreen execution-tilan.
                return "resumed"

            if idle_for is not None and idle_for >= args.idle_timeout:
                if not args.no_done_check:
                    recent = fetch_recent_events(base_url, headers, conv_id)
                    if recent is not None and is_stop_message(recent, args.nudge_mode):
                        return "done"
                if send_nudge(base_url, headers, conv_id, args.nudge, dry_run, conversation):
                    state["nudges"][conv_id] = nudges + 1
                    return "dry-nudge" if dry_run else "nudged"
                return "nudge-failed"
            return "paused"

        if sandbox_status == "STARTING":
            return "starting"
        if sandbox_status not in ("RUNNING", "ERROR"):
            return "not-running"
        if execution_status == "running":
            return "running"
        if execution_status == "waiting_for_confirmation":
            return "confirmation"

        if execution_status in ("finished", "idle", "stuck", "error", None):
            if idle_for is None:
                print(
                    f"  Idle-aikaa ei voitu mitata "
                    f"(sandbox={sandbox_status} exec={execution_status}) "
                    f"-> käsitellään idle-timeoutina"
                )
                idle_for = args.idle_timeout
            elif idle_for < args.idle_timeout:
                print(f"  Ei viel\u00e4 idle: {idle_for}s / {args.idle_timeout}s")
                return "idle-wait"
            if not args.no_done_check:
                recent = fetch_recent_events(base_url, headers, conv_id)
                if recent is not None and is_stop_message(recent, args.nudge_mode):
                    return "done"
            print(f"  Pys\u00e4htynyt ({execution_status}), {idle_for}s idle -> nudge")
            if send_nudge(base_url, headers, conv_id, args.nudge, dry_run, conversation):
                state["nudges"][conv_id] = nudges + 1
                return "dry-nudge" if dry_run else "nudged"
            return "nudge-failed"
        return "unknown"
    except ConversationNotFound:
        print(f"  Conversation {conv_id} ei enää löydy -> fallback sallittu")
        return "not-found"
    except requests.HTTPError as exc:
        print(f"  HTTP-virhe: {exc}")
        return "http-error"
    except requests.RequestException as exc:
        print(f"  Network-virhe: {exc}")
        return "net-error"
    except Exception as exc:
        print(f"  Virhe: {exc}")
        return "error"


def recover_repository_after_loss(
    base_url, headers, repository, candidates, args, state
):
    """After canonical loss, reuse the newest older conversation if possible."""
    for candidate in candidates[1:]:
        cid = (candidate.get("id") or "").strip()
        if not cid or candidate.get("sandbox_status") == "MISSING":
            continue

        print(
            f"  fallback: canonical menetetty -> kokeillaan vanhempaa "
            f"conversationia {cid[:8]} repo={repository}"
        )
        if candidate.get("sandbox_status") == "PAUSED":
            # This path is reached specifically because the canonical
            # conversation lost its sandbox. Resume the older conversation,
            # then wait for STARTING -> RUNNING before attempting a nudge.
            # Without this wait OpenHands can return 409 while the sandbox is
            # still starting, causing a false recovery failure.
            sandbox_id = candidate.get("sandbox_id") or ""
            if not try_resume(base_url, headers, sandbox_id, args.dry_run):
                continue
            if not args.dry_run and not wait_for_resumed_sandbox(
                base_url,
                headers,
                cid,
                sandbox_id,
                timeout=args.resume_wait_seconds,
                interval=args.resume_poll_interval,
            ):
                continue

        outcome = check_conversation(base_url, headers, cid, args, state)
        if outcome in ("sandbox-missing", "not-found"):
            continue

        print(f"  fallback conversation valittu: {cid[:8]} ({outcome})")
        return cid, outcome

    if has_recent_start_task(base_url, headers, repository, limit=args.discover_limit):
        return "", "replacement-start-skipped"

    replacement = start_conversation(
        base_url, headers, repository, args.nudge, args.dry_run
    )
    if replacement and replacement != "DRY-RUN":
        state["new_conversations"].add(replacement)
    return replacement or "", "sandbox-replaced" if replacement else "replacement-failed"


def collect_conversation_groups(base_url, headers, args, seed_ids, skip_ids):
    items = (
        discover_conversations(
            base_url,
            headers,
            limit=args.discover_limit,
            max_pages=args.discover_max_pages,
        )
        if args.discover
        else []
    )

    known_ids = {
        (item.get("id") or "").strip()
        for item in items
        if isinstance(item, dict)
    }

    for cid in seed_ids:
        if cid in known_ids:
            continue
        try:
            item = get_conversation(base_url, headers, cid)
            items.append(item)
            known_ids.add(cid)
        except ConversationNotFound:
            print(f"  seed conversation ei enää löydy: {cid}")
        except requests.RequestException as exc:
            print(f"  seed conversation -haku epäonnistui {cid[:8]}: {exc}")

    return group_conversations_by_repository(items, skip_ids=skip_ids)


def write_step_summary(results):
    path = os.getenv("GITHUB_STEP_SUMMARY", "")
    if not path:
        return
    try:
        lines = [
            "### OpenHands keepalive",
            "",
            "| Repository | Conversation | Outcome |",
            "|---|---|---|",
        ]
        for repository, cid, outcome in results:
            lines.append(
                f"| `{repository}` | `{cid[:8] if cid else '-'}` | `{outcome}` |"
            )
        counts = {}
        for _, _, outcome in results:
            counts[outcome] = counts.get(outcome, 0) + 1
        lines.append("")
        lines.append(
            "Summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        )
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception as exc:
        print(f"  step summary epäonnistui: {exc}")


def main():
    args = parse_args()
    api_key = os.getenv("OPENHANDS_API_KEY", "").strip()
    if not api_key:
        print("OPENHANDS_API_KEY puuttuu", file=sys.stderr)
        sys.exit(2)
    if args.idle_timeout <= 0:
        print("--idle-timeout pitää olla > 0", file=sys.stderr)
        sys.exit(2)

    base_url = args.base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    state = {"nudges": {}, "last_resume": {}, "new_conversations": set()}
    seed_ids = resolve_conversation_ids(args)
    skip_ids = resolve_skip_ids()

    groups = collect_conversation_groups(
        base_url, headers, args, seed_ids=seed_ids, skip_ids=skip_ids
    )
    latest = select_latest_per_repository(groups)

    if not latest:
        print("Ei hallittavia conversationeita löydetty.", file=sys.stderr)
        sys.exit(2)

    print("--- OpenHands keepalive ---")
    print(f"nudge-mode: {args.nudge_mode}")
    print(f"discover: {bool(args.discover)}")
    print(f"repositories: {len(latest)}")
    for repository, candidate in sorted(latest.items()):
        print(
            f"  - {repository}: {candidate.get('id', '')[:8]} "
            f"created={candidate.get('created_at') or '-'} "
            f"sandbox={candidate.get('sandbox_status')}"
        )
    print(f"idle timeout: {args.idle_timeout}s")
    if args.dry_run:
        print("DRY-RUN")
    print()

    results = []
    for repository in sorted(groups):
        candidates = groups[repository]
        canonical = candidates[0]
        cid = (canonical.get("id") or "").strip()
        outcome = check_conversation(base_url, headers, cid, args, state)

        if outcome in ("sandbox-missing", "not-found"):
            fallback_cid, fallback_outcome = recover_repository_after_loss(
                base_url, headers, repository, candidates, args, state
            )
            cid = fallback_cid or cid
            outcome = fallback_outcome

        results.append((repository, cid, outcome))
        print()

    if args.once:
        write_step_summary(
            [(repo, cid, outcome) for repo, cid, outcome in results]
        )
        print("--- Yhteenveto ---")
        for repository, cid, outcome in results:
            print(f"  {repository}: {cid[:8] if cid else '-'} -> {outcome}")
        return

    active = [cid for _, cid, outcome in results if cid and outcome not in (
        "done", "sandbox-replaced", "replacement-failed", "replacement-start-skipped"
    )]
    while active:
        # Continuous local mode keeps the already-selected canonical/fallback
        # conversations alive. It never re-discovers a second conversation for
        # the same repository during the same run.
        for cid in list(active):
            outcome = check_conversation(base_url, headers, cid, args, state)
            if outcome in ("done", "sandbox-missing", "not-found"):
                active.remove(cid)
        if not active:
            break
        time.sleep(args.interval)




if __name__ == "__main__":
    main()
