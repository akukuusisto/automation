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
        print(f"  discover ep\u00e4onnistui: {exc}")
        return []
