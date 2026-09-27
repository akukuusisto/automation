#!/usr/bin/env python3
"""OpenHands Cloud keepalive / auto-nudge.

Monitors conversations and sends a continuity nudge when idle too long.

Modes: --once (cron/Actions), continuous poll, --discover (API auto-list).
Nudge modes: loop (default) or task. See --help and workflow comments.
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys
import time

try:
    import requests
except ImportError:
    print("requests puuttuu. Asenna:\n  python -m pip install requests", file=sys.stderr)
    sys.exit(1)

DEFAULT_NUDGE_LOOP = (
    "Jatka autonomista looppia ty\u00f6nkulkusi mukaan.\n\n"
    "1) Tarkista nykyinen tila (git, avoimet ty\u00f6t, roadmap).\n"
    "2) Jatka kesken olevaa ty\u00f6t\u00e4 ilman turhaa selittely\u00e4.\n"
    "3) Jos nykyiset teht\u00e4v\u00e4t loppuvat: ota lis\u00e4\u00e4 t\u00f6it\u00e4 roadmapilta "
    "tai avoimista issueista (kun s\u00e4\u00e4nn\u00f6t sen sallivat).\n"
    "4) Jos roadmapkin on tyhj\u00e4: tutki mit\u00e4 sovelluksesta puuttuu "
    "isommana kokonaisuutena, lis\u00e4\u00e4 suosituksesi roadmapille ja "
    "ota se ty\u00f6st\u00f6\u00f6n.\n"
    "5) Pushaa muutokset normaalilla kadenssilla. \u00c4l\u00e4 mergaa "
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
    parser.add_argument("--dry-run", action="store_true", default=_env_bool("OPENHANDS_DRY_RUN"))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--discover", action="store_true", default=_env_bool("OPENHANDS_AUTO_DISCOVER"))
    parser.add_argument("--discover-limit", type=int, default=int(os.getenv("OPENHANDS_DISCOVER_LIMIT", "50")))
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
    r = requests.get(f"{base_url}/api/v1/app-conversations", headers=headers, params={"ids": conversation_id}, timeout=30)
    r.raise_for_status()
    items = r.json()
    if not items or not items[0]:
        raise RuntimeError("Conversation not found")
    return items[0]


def discover_conversation_ids(base_url, headers, limit=50):
    try:
        r = requests.get(f"{base_url}/api/v1/app-conversations/search", headers=headers, params={"limit": limit}, timeout=45)
        r.raise_for_status()
        payload = r.json()
        items = payload.get("items") or payload.get("results") or ([] if not isinstance(payload, list) else payload)
        active = {"RUNNING", "PAUSED", "ERROR"}
        found, seen = [], set()
        for item in items:
            if not isinstance(item, dict):
                continue
            cid = (item.get("id") or "").strip()
            if cid and cid not in seen and item.get("sandbox_status") in active:
                seen.add(cid)
                found.append(cid)
        print(f"  discover: {len(found)} aktiivista")
        return found
    except Exception as exc:
        print(f"  discover ep\u00e4onnistui: {exc}")
        return []


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


def send_nudge(base_url, headers, conversation_id, text, dry_run, conversation=None):
    if dry_run:
        print("  DRY-RUN: nudgea ei l\u00e4hetetty")
        return True
    payload = {"role": "user", "run": True, "content": [{"type": "text", "text": text}]}
    try:
        r = requests.post(
            f"{base_url}/api/v1/app-conversations/{conversation_id}/send-message",
            headers=headers, timeout=30, json=payload,
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
        r = requests.post(f"{conv_url}/events", headers=rh, timeout=30, json=payload)
        if r.status_code < 300:
            print(f"  runtime events OK: {r.text[:200]}")
            try:
                rr = requests.post(f"{conv_url}/run", headers=rh, timeout=15)
                print(f"  runtime run -> {rr.status_code}")
            except Exception:
                pass
            return True
        print(f"  runtime events -> {r.status_code}: {r.text[:200]}")
        return False
    except Exception as exc:
        print(f"  runtime fallback virhe: {exc}")
        return False


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
            return "retire-sandbox"

        if sandbox_status == "ERROR":
            if not args.no_done_check:
                recent = fetch_recent_events(base_url, headers, conv_id)
                if recent is not None and is_stop_message(recent, args.nudge_mode):
                    return "done"
            if idle_for is not None and idle_for >= args.idle_timeout:
                if send_nudge(base_url, headers, conv_id, args.nudge, dry_run, conversation):
                    state["nudges"][conv_id] = nudges + 1
                    return "dry-nudge" if dry_run else "nudged"
                return "nudge-failed"
            return "error-wait"

        if sandbox_status == "PAUSED":
            last_resume = state["last_resume"].get(conv_id, 0.0)
            if now - last_resume >= args.resume_cooldown:
                print("  Sandbox PAUSED -> resume...")
                try_resume(base_url, headers, sandbox_id, dry_run)
                state["last_resume"][conv_id] = now
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
        if sandbox_status != "RUNNING":
            return "not-running"
        if execution_status == "running":
            return "running"
        if execution_status == "waiting_for_confirmation":
            return "confirmation"

        if execution_status in ("finished", "idle", "stuck", "error", None):
            if idle_for is None:
                return "idle-unknown"
            if idle_for < args.idle_timeout:
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
    except requests.HTTPError as exc:
        print(f"  HTTP-virhe: {exc}")
        return "http-error"
    except requests.RequestException as exc:
        print(f"  Network-virhe: {exc}")
        return "net-error"
    except Exception as exc:
        print(f"  Virhe: {exc}")
        return "error"


def write_step_summary(results):
    path = os.getenv("GITHUB_STEP_SUMMARY", "")
    if not path:
        return
    try:
        lines = ["### OpenHands keepalive", "", "| Conversation | Outcome |", "|---|---|"]
        for cid, outcome in results:
            lines.append(f"| `{cid[:8]}` | `{outcome}` |")
        counts = {}
        for _, o in results:
            counts[o] = counts.get(o, 0) + 1
        lines.append("")
        lines.append("Summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception as exc:
        print(f"  step summary ep\u00e4onnistui: {exc}")


def main():
    args = parse_args()
    api_key = os.getenv("OPENHANDS_API_KEY", "").strip()
    if not api_key:
        print("OPENHANDS_API_KEY puuttuu", file=sys.stderr)
        sys.exit(2)
    if args.idle_timeout <= 0:
        print("--idle-timeout pit\u00e4\u00e4 olla > 0", file=sys.stderr)
        sys.exit(2)
    base_url = args.base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    state = {"nudges": {}, "last_resume": {}}
    conv_ids = resolve_conversation_ids(args)
    skip_ids = resolve_skip_ids()
    if args.discover:
        for cid in discover_conversation_ids(base_url, headers, limit=args.discover_limit):
            if cid not in conv_ids:
                conv_ids.append(cid)
    if skip_ids:
        conv_ids = [c for c in conv_ids if c not in skip_ids]
    if not conv_ids:
        print("Ei conversation-ID:it\u00e4. Aseta lista tai --discover.", file=sys.stderr)
        sys.exit(2)
    print("--- OpenHands keepalive ---")
    print(f"nudge-mode: {args.nudge_mode}")
    print(f"discover: {bool(args.discover)}")
    print(f"conversations: {len(conv_ids)}")
    for cid in conv_ids:
        print(f"  - {cid}")
    print(f"idle timeout: {args.idle_timeout}s")
    if args.dry_run:
        print("DRY-RUN")
    print()
    if args.once:
        results = []
        for i, cid in enumerate(conv_ids):
            if i:
                time.sleep(0.5)
            results.append((cid, check_conversation(base_url, headers, cid, args, state)))
        print("--- Yhteenveto ---")
        for cid, outcome in results:
            print(f"  {cid[:8]}: {outcome}")
        write_step_summary(results)
        return
    active = list(conv_ids)
    while active:
        for cid in list(active):
            outcome = check_conversation(base_url, headers, cid, args, state)
            if outcome in ("retire-sandbox", "done"):
                active.remove(cid)
        if not active:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
