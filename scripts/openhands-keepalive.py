#!/usr/bin/env python3
"""OpenHands Cloud keepalive / one-canonical-conversation-per-repository.

Normal operation selects the newest conversation that still has a sandbox for
each repository. A conversation whose sandbox is genuinely gone (MISSING / not
found) stops being canonical, so a dead conversation cannot keep a repository
stuck in a permanent recovery loop; only when every candidate for a repository
is MISSING does loss recovery run.

Loop mode (--nudge-mode loop) never stops: there is no stop token. When an
agent runs out of work it is instructed to pick an important topic itself,
research it, and open a new pull request with the findings for a human to
review instead of ending the loop.

GitHub Actions logs and step summaries of a public repository are public, so
repository names, conversation titles and full conversation UUIDs are redacted
by default. Set OPENHANDS_VERBOSE=1 for full local output.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import time

try:
    import requests
except ImportError:
    print("requests puuttuu. Asenna:\n  python -m pip install requests", file=sys.stderr)
    sys.exit(1)


class ConversationNotFound(RuntimeError):
    """Raised only when OpenHands confirms that a conversation no longer exists."""


# outcome-luokat: mik\u00e4 vaatii palautuksen, mik\u00e4 n\u00e4kyy ihmiselle
RECOVERY_OUTCOMES = ("sandbox-missing", "not-found", "stalled", "nudge-failed", "starting-stuck", "not-running")
TERMINAL_OUTCOMES = (
    "done",
    "stalled",
    "budget-exhausted",
    "sandbox-replaced",
    "replacement-failed",
    "replacement-start-skipped",
)
NEEDS_HUMAN_OUTCOMES = {"confirmation"}
AT_RISK_OUTCOMES = {
    "stalled",
    "nudge-failed",
    "replacement-failed",
    "http-error",
    "net-error",
    "error",
    "sandbox-missing",
    "not-found",
    "budget-exhausted",
    "idle-unknown",
    "resume-failed",
    "resuming",
    "replacement-start-skipped",
    "starting-stuck",
    "not-running",
}
TRANSIENT_STATUSES = (429, 500, 502, 503, 504)
# 404/410 send-messagesta tarkoittaa ett\u00e4 conversation on arkistoitu tai
# poistettu. Sandboxia ei ole en\u00e4\u00e4 olemassa, joten nudge ei voi koskaan menn\u00e4
# l\u00e4pi: conversation pit\u00e4\u00e4 korvata uudella.
GONE_STATUSES = (404, 410)

# --- Julkisen repon suojaus -------------------------------------------------
# Actions-logit ja step summaryt ovat julkisessa repossa julkisia, joten
# oletuksena repositorion nimi\u00e4, conversation-title\u00e4 eik\u00e4 t\u00e4ytt\u00e4 UUID:t\u00e4
# ei tulosteta. OPENHANDS_VERBOSE=1 palauttaa t\u00e4ydet tiedot paikalliseen ajoon.
VERBOSE = False
_REPO_LABELS: dict = {}


def label_repository(repository: str) -> str:
    """Vakaa, ei-ihmistunnistettava tunniste repositoriolle."""
    if VERBOSE or not repository:
        return repository or "-"
    if repository not in _REPO_LABELS:
        _REPO_LABELS[repository] = f"repo#{len(_REPO_LABELS) + 1}"
    return _REPO_LABELS[repository]


def register_repository_labels(repositories) -> None:
    """Kiinnit\u00e4 tunnisteet deterministisess\u00e4 j\u00e4rjestyksess\u00e4 ennen lokitusta."""
    for repository in sorted(repositories or []):
        label_repository(repository)


def label_conversation(conversation_id: str) -> str:
    if VERBOSE:
        return conversation_id or "-"
    return (conversation_id or "-")[:8]


def outcome_parts(outcome: str):
    """Jaa yhdistetty outcome ("stalled->sandbox-replaced") osiin."""
    return [part for part in str(outcome or "").split("->") if part]


def outcome_matches(outcome: str, candidates) -> bool:
    """Osuuko yksikin outcome-osa annettuun joukkoon?"""
    return any(part in candidates for part in outcome_parts(outcome))


def final_outcome(outcome: str) -> str:
    """Return the final state after any attempted recovery chain."""
    parts = outcome_parts(outcome)
    return parts[-1] if parts else ""


def safe_error_label(exc: Exception) -> str:
    """Describe an error without exposing API bodies, URLs, IDs, or user data."""
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code is not None:
        return f"HTTP {status_code}"
    return type(exc).__name__


def label_title(title) -> str:
    if VERBOSE:
        return repr(title)
    return "'<redacted>'" if title else "''"


# --- Conversation-otsikot ---------------------------------------------------
# Jokainen hallittu conversation nimetään alkamaan repositorion nimellä, jotta
# OpenHands UI:sta näkee yhdellä silmäyksellä mikä repo on työstössä. Tämä on
# tarpeen myös siksi, että API:n kautta luodut conversationit saavat huonot
# automaattiset otsikot (OpenHands issue #13125).
TITLE_SEPARATOR = ": "
DEFAULT_TITLE_SUFFIX = "keepalive"
LEGACY_STOP_TOKENS = ("LOOP-STOP", "DONE")


def repository_short_name(repository: str) -> str:
    """"org/repo" -> "repo"."""
    return (repository or "").rsplit("/", 1)[-1]


def _clean_title_text(title: str, repository: str) -> str:
    """Pudota emojit, erottimet ja toistuva repo-nimi otsikon alusta."""
    text = (title or "").strip()
    text = re.sub(r"^[^\w]+", "", text, flags=re.UNICODE).strip()
    for candidate in (repository, repository_short_name(repository)):
        if candidate and text.lower().startswith(candidate.lower()):
            text = text[len(candidate):].lstrip(" :-\u2013\u2014").strip()
    return text


def desired_conversation_title(title: str, repository: str):
    """Uusi otsikko, joka alkaa repositorion nimell\u00e4; None jos jo kunnossa.

    Omistaja j\u00e4tet\u00e4\u00e4n pois: "owner/repo" -> "repo", koska se on UI:ssa
    pelkk\u00e4\u00e4 kohinaa. Vanha "owner/repo: ..." -otsikko migroituu t\u00e4ll\u00e4
    kertaalleen muotoon "repo: ...".

    None tarkoittaa ett\u00e4 API-kutsua ei tarvita, joten jokainen ajo ei
    kirjoita conversationin otsikkoa uudelleen.
    """
    repository = (repository or "").strip()
    if not repository:
        return None
    prefix = repository_short_name(repository)
    current = (title or "").strip()
    if current.startswith(prefix + TITLE_SEPARATOR):
        return None
    rest = _clean_title_text(current, repository)
    if not rest:
        rest = DEFAULT_TITLE_SUFFIX
    return f"{prefix}{TITLE_SEPARATOR}{rest}"


def sync_conversation_title(
    base_url, headers, conversation_id, repository, current_title, args
):
    """Pid\u00e4 huoli ett\u00e4 otsikko alkaa repositorion nimell\u00e4.

    Palauttaa True vain jos otsikko oikeasti p\u00e4ivitettiin. Dry-runissa ei
    koskaan kirjoiteta mit\u00e4\u00e4n.
    """
    if not getattr(args, "title_sync", True):
        return False
    desired = desired_conversation_title(current_title, repository)
    if not desired:
        return False
    if args.dry_run:
        print(
            "  DRY-RUN: otsikko alkaisi repositorion nimell\u00e4 "
            f"({label_conversation(conversation_id)})"
        )
        return False
    try:
        response = requests.patch(
            f"{base_url}/api/v1/app-conversations/{conversation_id}",
            headers=headers,
            timeout=30,
            json={"title": desired},
        )
        if response.status_code < 300:
            # Otsikko sis\u00e4lt\u00e4\u00e4 repositorion nimen, joten sit\u00e4 ei koskaan
            # tulosteta ilman VERBOSE-tilaa.
            print(
                "  otsikko synkronoitu -> "
                f"{label_conversation(conversation_id)} "
                f"title={label_title(desired)}"
            )
            return True
        print(f"  otsikon päivitys -> HTTP {response.status_code}")
    except Exception as exc:
        print(f"  otsikon päivitys ep\u00e4onnistui: {safe_error_label(exc)}")
    return False


# --- Ajo-budjetti -----------------------------------------------------------
# Vain --once-ajossa (GitHub Actions) k\u00e4yt\u00f6ss\u00e4: pit\u00e4\u00e4 huolen ett\u00e4 ajo
# ehtii kirjoittaa yhteenvedon ennen jobin timeoutia.
_BUDGET_DEADLINE = 0.0


def set_budget_deadline(deadline: float) -> None:
    global _BUDGET_DEADLINE
    _BUDGET_DEADLINE = deadline


def budget_exhausted() -> bool:
    return bool(_BUDGET_DEADLINE) and time.monotonic() >= _BUDGET_DEADLINE


def budget_remaining() -> float:
    if not _BUDGET_DEADLINE:
        return float("inf")
    return max(0.0, _BUDGET_DEADLINE - time.monotonic())


DEFAULT_NUDGE_LOOP = (
    "Jatka autonomista looppia ty\u00f6nkulkusi mukaan.\n\n"
    "1) Tarkista nykyinen tila (git, avoimet ty\u00f6t, roadmap).\n"
    "2) Jatka kesken olevaa ty\u00f6t\u00e4 ilman turhaa selittely\u00e4.\n"
    "3) Ennen uuden ty\u00f6n aloittamista tarkista git, avoimet PR:t ja issue-ty\u00f6 "
    "sek\u00e4 varmista, ettei toinen agentti tee samaa ty\u00f6t\u00e4. V\u00e4lt\u00e4 duplikaatit.\n"
    "4) Jos nykyiset teht\u00e4v\u00e4t loppuvat: ota lis\u00e4\u00e4 t\u00f6it\u00e4 roadmapilta "
    "tai avoimista issueista (kun s\u00e4\u00e4nn\u00f6t sen sallivat).\n"
    "5) Jos roadmap ja issuet ovat tyhjät: aloita itse uusi feature tai "
    "merkittävä parannus, joka vie sovellusta eteenpäin. Valitse se, mikä on "
    "käyttäjälle arvokkain, ja perustele lyhyesti miksi.\n"
    "6) Vasta jos sovellus on mielestäsi käytännössä valmis eikä järkevää "
    "uutta tekemistä ole: valitse itse aihe, jonka koet tärkeäksi ja jota ei "
    "ole vielä käsitelty, tutki se huolellisesti ja kirjaa tulokset sekä "
    "suositukset uuteen PR:ään. Älä mergaa sitä: jätä PR ihmisen "
    "tutkittavaksi ja arvioitavaksi.\n"
    "7) Pushaa muutokset normaalilla kadenssilla. Älä mergaa suojattuihin "
    "haaroihin ilman erillistä ohjetta.\n\n"
    "Looppi ei pysähdy. Älä vastaa pelkällä lopetusmerkillä: jos nykyinen työ "
    "loppuu, siirry kohtaan 5 ja aloita uusi feature."
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
    parser.add_argument(
        "--min-nudge-interval",
        type=int,
        default=int(os.getenv("OPENHANDS_MIN_NUDGE_INTERVAL", "1800")),
        help="Nudgea ei lähetetä samalle conversationille tätä tiheämmin (s)",
    )
    parser.add_argument(
        "--max-stalled-nudges",
        type=int,
        default=int(os.getenv("OPENHANDS_MAX_STALLED_NUDGES", "4")),
        help="Kuinka monen vastaamattoman nuden jälkeen conversation korvataan",
    )
    parser.add_argument(
        "--run-budget",
        type=int,
        default=int(os.getenv("OPENHANDS_RUN_BUDGET_SECONDS", "420")),
        help="--once-ajon aikabudjetti sekunteina (0 = ei rajaa)",
    )
    parser.add_argument("--resume-wait-seconds", type=int, default=int(os.getenv("OPENHANDS_RESUME_WAIT_SECONDS", "90")))
    parser.add_argument("--resume-poll-interval", type=int, default=int(os.getenv("OPENHANDS_RESUME_POLL_INTERVAL", "5")))
    parser.add_argument(
        "--fail-on-attention",
        action="store_true",
        default=_env_bool("OPENHANDS_FAIL_ON_ATTENTION"),
        help="Palauta virhe jos jokin conversation vaatii ihmisen",
    )
    parser.add_argument(
        "--fail-on-risk",
        action="store_true",
        default=_env_bool("OPENHANDS_FAIL_ON_RISK"),
        help="Fail the run if any repository remains in an unresolved risk state",
    )
    parser.add_argument(
        "--title-sync",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("OPENHANDS_TITLE_SYNC", True),
        help="Pidä conversation-otsikot repositorion nimellä alkavina",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=_env_bool("OPENHANDS_VERBOSE"),
        help="Näytä repositoriot, titlet ja täydet ID:t (vain paikalliseen ajoon)",
    )
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


def normalize_repository_name(repository: str) -> str:
    """Normalize owner/repo names for case-insensitive skip-list matching."""
    return repository.strip().rstrip("/").casefold() if isinstance(repository, str) else ""


def resolve_skip_repositories():
    """Read the optional comma-separated repository denylist from the environment."""
    return {
        normalized
        for raw in os.getenv("OPENHANDS_SKIP_REPOSITORIES", "").split(",")
        if (normalized := normalize_repository_name(raw))
    }


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
    for page in range(1, max_pages + 1):
        if budget_exhausted():
            print("  discover: run budget ylitetty -> k\u00e4ytet\u00e4\u00e4n l\u00f6ydetyt")
            break
        params = {"limit": limit}
        if page_id:
            params["page_id"] = page_id
        try:
            r = requests.get(
                f"{base_url}/api/v1/app-conversations/search",
                headers=headers,
                params=params,
                timeout=45,
            )
            r.raise_for_status()
            payload = r.json()
        except Exception as exc:
            # Osittainen tulos on paljon parempi kuin tyhj\u00e4: yksi hidas tai
            # ep\u00e4onnistunut sivu ei saa pudottaa koko fleeti\u00e4.
            print(
                f"  discover: sivu {page} ep\u00e4onnistui ({safe_error_label(exc)}) -> jatketaan "
                f"{len(all_items)} l\u00f6ydetyll\u00e4 conversationilla"
            )
            break

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


def _repository_from_conversation(conversation):
    if not isinstance(conversation, dict):
        return ""
    repository = conversation.get("selected_repository")
    return repository.strip() if isinstance(repository, str) else ""


def group_conversations_by_repository(
    items, skip_ids=None, skip_repositories=None, excluded_repositories=None
):
    groups = {}
    skip_ids = skip_ids or set()
    excluded_repositories = (
        excluded_repositories if excluded_repositories is not None else set()
    )
    skip_repositories = {
        normalized
        for repository in (skip_repositories or set())
        if (normalized := normalize_repository_name(repository))
    }
    for item in items:
        if not isinstance(item, dict):
            continue
        cid = (item.get("id") or "").strip()
        repository = _repository_from_conversation(item)
        normalized_repository = normalize_repository_name(repository)
        if not cid or not repository:
            continue
        if normalized_repository in skip_repositories:
            excluded_repositories.add(normalized_repository)
            continue
        if cid in skip_ids:
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
    """Choose the newest usable conversation, falling back to recoverable errors.

    A newer ERROR sandbox must not mask an older RUNNING/PAUSED/STARTING one.
    If no usable candidate exists, retain the newest non-missing candidate so
    the regular recovery path can attempt repair or replace it.
    """
    selected = {}
    for repository, candidates in groups.items():
        if not candidates:
            continue
        # Prefer the newest operational conversation over a newer ERROR session.
        usable = [
            c for c in candidates
            if c.get("sandbox_status") in ("RUNNING", "PAUSED", "STARTING")
        ]
        present = [c for c in candidates if c.get("sandbox_status") != "MISSING"]
        selected[repository] = (usable or present or candidates)[0]
    return selected


def order_candidates(canonical, candidates):
    """Canonical ensin, muut alkuper\u00e4isess\u00e4 (uusin ensin) j\u00e4rjestyksess\u00e4.

    recover_repository_after_loss() olettaa ett\u00e4 candidates[0] on se
    conversation, joka juuri menetettiin, ja kokeilee sen j\u00e4lkeen muita.
    """
    canonical_id = (canonical.get("id") or "").strip()
    ordered = [canonical]
    for candidate in candidates:
        if (candidate.get("id") or "").strip() != canonical_id:
            ordered.append(candidate)
    return ordered


def nudge_threshold(args) -> int:
    """Nudge vasta kun idle ylitt\u00e4\u00e4 sek\u00e4 idle-timeoutin ett\u00e4 min-v\u00e4lin."""
    return max(args.idle_timeout, args.min_nudge_interval)


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


def sorted_messages(events):
    """Viestit uusin ensin: timestampilla jos saatavilla, muuten lista käännettynä."""
    messages = [
        e for e in (events or [])
        if isinstance(e, dict)
        and e.get("source") in ("agent", "user")
        and _event_text(e).strip()
    ]
    if not messages:
        return []
    if any(_event_timestamp(e) > 0 for e in messages):
        messages.sort(key=_event_timestamp, reverse=True)
    else:
        messages = messages[::-1]
    return messages


def latest_agent_is_stop_token(events) -> bool:
    """Onko uusin agentin viesti lopetusmerkki (LOOP-STOP / DONE)?

    Loop-moodissa lopetusmerkki ei enää pysäytä valvontaa. Tämä on vain
    näkyvyyttä varten: lokiin jää merkintä siitä että agentti ilmoitti
    lopettavansa, jolloin se ohjataan aloittamaan uusi feature.
    """
    try:
        messages = sorted_messages(events)
        if not messages or messages[0].get("source") != "agent":
            return False
        return _event_text(messages[0]).strip().upper() in LEGACY_STOP_TOKENS
    except Exception:
        return False


def is_stop_message(events, nudge_mode: str) -> bool:
    """Onko conversation tarkoituksella lopetettu?

    Vain task-moodissa on lopetustoken (t\u00e4sm\u00e4lleen "DONE"). Loop-moodissa
    lopetustokenia ei ole lainkaan: ty\u00f6n loppuessa agentti siirtyy
    tutkimusty\u00f6h\u00f6n, joten mik\u00e4\u00e4n viesti ei pys\u00e4yt\u00e4 valvontaa. N\u00e4in agentin
    vahingossa l\u00e4hett\u00e4m\u00e4 lopetusmerkki ei tapa conversationia pysyv\u00e4sti.
    """
    if nudge_mode != "task":
        return False
    try:
        messages = sorted_messages(events)
        if not messages:
            return False
        latest = messages[0]
        if latest.get("source") != "agent":
            return False
        return _event_text(latest).strip().upper() == "DONE"
    except Exception:
        return False


def count_trailing_user_messages(events) -> int:
    """Montako vastaamatonta k\u00e4ytt\u00e4j\u00e4n viesti\u00e4 conversationin lopussa on?

    Jokainen nudge on k\u00e4ytt\u00e4j\u00e4n viesti. Jos agentti ei vastaa niihin,
    m\u00e4\u00e4r\u00e4 kasvaa. N\u00e4in n\u00e4hd\u00e4\u00e4n ilman jaettua tilaa ett\u00e4 conversation on
    jumissa (agentti ei reagoi nudgeihin) ja se on aika korvata.
    """
    try:
        messages = sorted_messages(events)
        if not messages:
            return 0
        count = 0
        for event in messages:
            if event.get("source") == "user":
                count += 1
            else:
                break
        return count
    except Exception:
        return 0


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
        print(f"  event-haku ep\u00e4onnistui: {safe_error_label(exc)}")
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
        print(f"  resume -> HTTP {r.status_code}")
        return r.status_code < 300
    except Exception as exc:
        print(f"  resume ep\u00e4onnistui: {safe_error_label(exc)}")
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
            print(f"  resume wait -haku ep\u00e4onnistui: {safe_error_label(exc)}")
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


def parse_retry_after(headers, default=2.0, cap=30.0) -> float:
    """Kuuntele Retry-After -otsake, mutta \u00e4l\u00e4 koskaan odota liian kauan."""
    if not headers:
        return default
    try:
        raw = headers.get("Retry-After") or headers.get("retry-after")
    except Exception:
        return default
    if raw is None:
        return default
    try:
        return max(0.0, min(float(str(raw).strip()), cap))
    except Exception:
        return default


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
    last_status = None
    for attempt in (1, 2):
        try:
            r = requests.post(
                f"{base_url}/api/v1/app-conversations/{conversation_id}/send-message",
                headers=headers,
                timeout=30,
                json=payload,
            )
            if r.status_code < 300:
                print(f"  send-message OK (HTTP {r.status_code})")
                return True
            print(f"  send-message -> HTTP {r.status_code}")
            last_status = r.status_code
            if attempt == 1 and r.status_code in TRANSIENT_STATUSES:
                delay = parse_retry_after(getattr(r, "headers", None))
                if delay <= min(30.0, budget_remaining()):
                    print(
                        f"  send-message transient virhe -> "
                        f"uusi yritys {delay:.1f}s kuluttua"
                    )
                    time.sleep(delay)
                    continue
        except Exception as exc:
            print(f"  send-message virhe: {safe_error_label(exc)}")
        break

    # Arkistoitu tai poistettu conversation: sandboxia ei ole en\u00e4\u00e4 olemassa,
    # joten nudge ei voi koskaan menn\u00e4 l\u00e4pi. T\u00e4m\u00e4 ei ole tilap\u00e4inen virhe
    # vaan menetetty conversation -> kutsuja hoitaa korvauksen.
    if last_status in GONE_STATUSES:
        raise ConversationNotFound(conversation_id)

    # 409 tarkoittaa ett\u00e4 sandbox ei ole valmis vastaanottamaan viesti\u00e4:
    # kokeillaan viel\u00e4 runtime-fallbackia ennen kuin todetaan ett\u00e4 nudge
    # ei mennyt l\u00e4pi.
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
            print(f"  runtime events OK (HTTP {r.status_code})")
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
        print(f"  runtime events -> HTTP {r.status_code}")
        return False
    except Exception as exc:
        print(f"  runtime fallback virhe: {safe_error_label(exc)}")
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
        print(f"  DRY-RUN: uusi conversation -> {label_repository(repository)}")
        return "DRY-RUN"

    # Myös uusi conversation nimetään repositorion nimellä, jotta otsikosta
    # näkee heti mikä repo on työstössä.
    title = desired_conversation_title("", repository) or repository_short_name(
        repository
    )
    payload = {
        "initial_message": {"content": [{"type": "text", "text": text}]},
        "selected_repository": repository,
        "title": title,
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
        print(f"  start -> status={status} task={label_conversation(task_id)} conversation={label_conversation(conversation_id)}")

        if conversation_id:
            return conversation_id
        if not task_id:
            print("  start: response ei sisältänyt start-task ID:tä")
            return None

        for attempt in range(poll_attempts):
            if budget_exhausted():
                print("  start-task: run budget ylitetty -> lopetetaan odotus")
                return None
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
                print(f"  uusi conversation valmis: {label_conversation(conversation_id)}")
                return conversation_id
            if status == "ERROR":
                print("  start-task ERROR (details redacted)")
                return None
        print("  start-task timeout; uusi conversation valmistuu mahdollisesti myöhemmin")
        return None
    except requests.RequestException as exc:
        print(f"  start ep\u00e4onnistui: {safe_error_label(exc)}")
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
                    f"  start-task: aktiivinen/recent l\u00f6ytyy jo: "
                    f"{label_repository(repository)} ({status})"
                )
                return True

        return False
    except Exception as exc:
        print(
            f"  start-task-haku epäonnistui: {safe_error_label(exc)} -> "
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
        repository = conversation.get("selected_repository", "")
        sandbox_id = conversation.get("sandbox_id", "")
        now = time.time()
        updated_ts = parse_updated_at(updated_at)
        activity_ts = updated_ts
        if execution_status != "running" and sandbox_status in ("RUNNING", "PAUSED", "ERROR"):
            activity_ts = latest_activity_ts(base_url, headers, conv_id, updated_ts)
        idle_for = max(0, int(now - activity_ts)) if activity_ts else None
        nudge_after = nudge_threshold(args)
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        idle_text = f"{idle_for}s idle" if idle_for is not None else "idle unknown"
        print(
            f"[{ts}] [{label_conversation(conv_id)}] sandbox={sandbox_status} "
            f"exec={execution_status} {idle_text} title={label_title(title)}"
        )

        # Otsikko kertoo mikä repo on työstössä; ei kirjoiteta mitään dry-runissa
        # eikä silloin kun otsikko on jo kunnossa.
        sync_conversation_title(
            base_url, headers, conv_id, repository, title, args
        )

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
            # Jos resume vahvistetaan tällä kierroksella RUNNING-tilaan, nudge
            # saa lähteä heti: outcome on silloin "resumed->nudged".
            resume_prefix = ""
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
                    return "resume-failed"
                if dry_run:
                    print("  DRY-RUN: sandboxin palautumista ei odotettu")
                    return "resumed"
                if not wait_for_resumed_sandbox(
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
                # Sandbox on nyt vahvistettu RUNNING-tilaan, joten send-message
                # ei enää palauta 409:ää ja nudge voidaan lähettää heti.
                # Tämä on tarpeen siksi, että resume nollaa activity-aikaleiman:
                # ilman tätä herätetty conversation näyttää "juuri aktiiviselta"
                # ja odottaisi nudgea turhaan koko nudge-rajan (30 min) verran.
                if idle_for is not None and idle_for < nudge_after:
                    print(
                        f"  Resume valmis, mutta idle {idle_for}s < "
                        f"{nudge_after}s -> nudge jää seuraavalle kierrokselle"
                    )
                    return "resumed"
                print(
                    "  Resume valmis ja idle-raja ylitetty -> "
                    "nudge samalla kierroksella"
                )
                resume_prefix = "resumed->"

            if idle_for is None:
                print(
                    "  Idle-aikaa ei voitu mitata (PAUSED) -> "
                    "käsitellään idle-timeoutina"
                )
                idle_for = nudge_after

            if idle_for >= nudge_after:
                if not args.no_done_check:
                    recent = fetch_recent_events(base_url, headers, conv_id)
                    if recent is not None and is_stop_message(recent, args.nudge_mode):
                        return resume_prefix + "done"
                    if count_trailing_user_messages(recent) >= args.max_stalled_nudges:
                        print(
                            "  PAUSED: agentti ei vastaa nudgeihin -> "
                            "conversation on jumissa, vaaditaan palautus."
                        )
                        return resume_prefix + "stalled"
                if send_nudge(base_url, headers, conv_id, args.nudge, dry_run, conversation):
                    state["nudges"][conv_id] = nudges + 1
                    return resume_prefix + ("dry-nudge" if dry_run else "nudged")
                return resume_prefix + "nudge-failed"
            return resume_prefix + "paused"

        if sandbox_status == "STARTING":
            # Conversation age is not sandbox age: an old conversation can
            # legitimately have a newly resumed sandbox. Use updated_at as the
            # best available proxy for a recent state transition.
            starting_since = parse_updated_at(conversation.get("updated_at", ""))
            startup_timeout = max(600, getattr(args, "resume_wait_seconds", 90) * 2)
            if starting_since and now - starting_since >= startup_timeout:
                print(
                    f"  Sandbox STARTING yli {startup_timeout}s -> "
                    "recoveroidaan jumittunut käynnistys"
                )
                return "starting-stuck"
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
                idle_for = nudge_after
            elif idle_for < nudge_after:
                print(f"  Ei viel\u00e4 idle: {idle_for}s / {nudge_after}s")
                return "idle-wait"
            if not args.no_done_check:
                recent = fetch_recent_events(base_url, headers, conv_id)
                if recent is not None and is_stop_message(recent, args.nudge_mode):
                    return "done"
                if recent is not None and latest_agent_is_stop_token(recent):
                    print(
                        "  Viimeisin agentin viesti on lopetusmerkki -> "
                        "ohjataan aloittamaan uusi feature."
                    )
                if count_trailing_user_messages(recent) >= args.max_stalled_nudges:
                    print(
                        f"  Agentti ei ole vastannut "
                        f"{count_trailing_user_messages(recent)} nudgeen -> "
                        "conversation on jumissa, vaaditaan palautus."
                    )
                    return "stalled"
            print(f"  Pys\u00e4htynyt ({execution_status}), {idle_for}s idle -> nudge")
            if send_nudge(base_url, headers, conv_id, args.nudge, dry_run, conversation):
                state["nudges"][conv_id] = nudges + 1
                return "dry-nudge" if dry_run else "nudged"
            return "nudge-failed"
        return "unknown"
    except ConversationNotFound:
        print(f"  Conversation {label_conversation(conv_id)} ei en\u00e4\u00e4 l\u00f6ydy -> fallback sallittu")
        return "not-found"
    except requests.HTTPError as exc:
        print(f"  HTTP-virhe: {safe_error_label(exc)}")
        return "http-error"
    except requests.RequestException as exc:
        print(f"  Network-virhe: {safe_error_label(exc)}")
        return "net-error"
    except Exception as exc:
        print(f"  Virhe: {safe_error_label(exc)}")
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
            f"conversationia {label_conversation(cid)} "
            f"repo={label_repository(repository)}"
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
        if outcome in RECOVERY_OUTCOMES:
            continue

        print(
            f"  fallback conversation valittu: "
            f"{label_conversation(cid)} ({outcome})"
        )
        return cid, outcome

    if has_recent_start_task(base_url, headers, repository, limit=args.discover_limit):
        return "", "replacement-start-skipped"

    replacement = start_conversation(
        base_url, headers, repository, args.nudge, args.dry_run
    )
    if replacement and replacement != "DRY-RUN":
        state["new_conversations"].add(replacement)
    return replacement or "", "sandbox-replaced" if replacement else "replacement-failed"


def collect_conversation_groups(
    base_url,
    headers,
    args,
    seed_ids,
    skip_ids,
    skip_repositories=None,
    excluded_repositories=None,
):
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
            print(
                f"  seed conversation ei en\u00e4\u00e4 l\u00f6ydy: "
                f"{label_conversation(cid)}"
            )
        except requests.RequestException as exc:
            print(
                f"  seed conversation -haku ep\u00e4onnistui "
                f"{label_conversation(cid)}: {safe_error_label(exc)}"
            )

    return group_conversations_by_repository(
        items,
        skip_ids=skip_ids,
        skip_repositories=skip_repositories,
        excluded_repositories=excluded_repositories,
    )


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
                f"| `{label_repository(repository)}` | "
                f"`{label_conversation(cid) if cid else '-'}` | `{outcome}` |"
            )
        counts = {}
        for _, _, outcome in results:
            counts[outcome] = counts.get(outcome, 0) + 1
        lines.append("")
        lines.append(
            "Summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        )

        at_risk = [
            (r, c, o) for r, c, o in results if final_outcome(o) in AT_RISK_OUTCOMES
        ]
        needs_human = [
            (r, c, o) for r, c, o in results if final_outcome(o) in NEEDS_HUMAN_OUTCOMES
        ]

        for title, entries in (
            (f"At risk ({len(at_risk)})", at_risk),
            (f"Needs human ({len(needs_human)})", needs_human),
        ):
            lines.append("")
            lines.append(f"**{title}**")
            if not entries:
                lines.append("- none")
                continue
            for repository, cid, outcome in entries:
                lines.append(
                    f"- `{label_repository(repository)}` / "
                    f"`{label_conversation(cid) if cid else '-'}` -> `{outcome}`"
                )

        recovered = [
            (r, c, o) for r, c, o in results
            if len(outcome_parts(o)) > 1 and final_outcome(o) not in AT_RISK_OUTCOMES
        ]
        lines.append("")
        lines.append(f"**Recovered ({len(recovered)})**")
        if not recovered:
            lines.append("- none")
        else:
            for repository, cid, outcome in recovered:
                lines.append(
                    f"- `{label_repository(repository)}` / "
                    f"`{label_conversation(cid) if cid else '-'}` -> `{outcome}`"
                )

        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception as exc:
        print(f"  step summary ep\u00e4onnistui: {safe_error_label(exc)}")


def main():
    global VERBOSE
    args = parse_args()
    # GitHub Actions logs are public; never allow verbose output there.
    VERBOSE = bool(args.verbose) and os.getenv("GITHUB_ACTIONS", "").lower() != "true"
    api_key = os.getenv("OPENHANDS_API_KEY", "").strip()
    if not api_key:
        print("OPENHANDS_API_KEY puuttuu", file=sys.stderr)
        sys.exit(2)
    if args.idle_timeout <= 0:
        print("--idle-timeout pit\u00e4\u00e4 olla > 0", file=sys.stderr)
        sys.exit(2)
    if args.min_nudge_interval <= 0:
        print("--min-nudge-interval pit\u00e4\u00e4 olla > 0", file=sys.stderr)
        sys.exit(2)
    if args.max_stalled_nudges <= 0:
        print("--max-stalled-nudges pit\u00e4\u00e4 olla > 0", file=sys.stderr)
        sys.exit(2)

    base_url = args.base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    state = {"nudges": {}, "last_resume": {}, "new_conversations": set()}
    seed_ids = resolve_conversation_ids(args)
    skip_ids = resolve_skip_ids()
    skip_repositories = resolve_skip_repositories()
    excluded_repositories = set()

    # Aikabudjetti vain --once-ajoon: pit\u00e4\u00e4 huolen ett\u00e4 yhteenveto ehtii
    # synty\u00e4 ennen jobin timeoutia.
    if args.once and args.run_budget > 0:
        set_budget_deadline(time.monotonic() + args.run_budget)

    groups = collect_conversation_groups(
        base_url,
        headers,
        args,
        seed_ids=seed_ids,
        skip_ids=skip_ids,
        skip_repositories=skip_repositories,
        excluded_repositories=excluded_repositories,
    )
    latest = select_latest_per_repository(groups)

    if not latest:
        if excluded_repositories:
            print(
                "No keepalive conversations remain after repository exclusions "
                f"({len(excluded_repositories)} excluded repositories)."
            )
            if args.once:
                write_step_summary([])
            return
        print("Ei hallittavia conversationeita l\u00f6ydetty.", file=sys.stderr)
        sys.exit(2)

    register_repository_labels(latest)

    print("--- OpenHands keepalive ---")
    print(f"nudge-mode: {args.nudge_mode}")
    print(f"discover: {bool(args.discover)}")
    print(f"repositories: {len(latest)}")
    for repository, candidate in sorted(latest.items()):
        print(
            f"  - {label_repository(repository)}: "
            f"{label_conversation(candidate.get('id', ''))} "
            f"created={candidate.get('created_at') or '-'} "
            f"sandbox={candidate.get('sandbox_status')}"
        )
    print(f"idle timeout: {args.idle_timeout}s")
    print(f"nudge threshold: {nudge_threshold(args)}s")
    print(f"run budget: {args.run_budget}s")
    if args.dry_run:
        print("DRY-RUN")
    print()

    results = []
    for repository in sorted(groups):
        if budget_exhausted():
            print(
                f"  run budget ({args.run_budget}s) ylitetty -> "
                "loput repositoriot j\u00e4\u00e4v\u00e4t seuraavalle ajolle"
            )
            results.append((repository, "", "budget-exhausted"))
            continue

        candidates = groups[repository]
        canonical = latest.get(repository) or candidates[0]
        ordered = order_candidates(canonical, candidates)
        cid = (canonical.get("id") or "").strip()
        outcome = check_conversation(base_url, headers, cid, args, state)

        if outcome_matches(outcome, RECOVERY_OUTCOMES):
            fallback_cid, fallback_outcome = recover_repository_after_loss(
                base_url, headers, repository, ordered, args, state
            )
            cid = fallback_cid or cid
            # Pid\u00e4 molemmat n\u00e4kyviss\u00e4: "stalled->sandbox-replaced" kertoo
            # ett\u00e4 agentti ei vastannut nudgeihin ja conversation korvattiin.
            outcome = f"{outcome}->{fallback_outcome}"

        results.append((repository, cid, outcome))
        print()

    if args.once:
        write_step_summary(results)
        print("--- Yhteenveto ---")
        for repository, cid, outcome in results:
            print(
                f"  {label_repository(repository)}: "
                f"{label_conversation(cid) if cid else '-'} -> {outcome}"
            )
        attention = [
            row for row in results if final_outcome(row[2]) in NEEDS_HUMAN_OUTCOMES
        ]
        if attention and args.fail_on_attention:
            print(
                f"  {len(attention)} repositoriota tarvitsee ihmisen -> "
                "fail-on-attention p\u00e4\u00e4ll\u00e4, palautetaan virhe.",
                file=sys.stderr,
            )
            sys.exit(3)
        at_risk = [
            row for row in results if final_outcome(row[2]) in AT_RISK_OUTCOMES
        ]
        if at_risk and args.fail_on_risk:
            print(
                f"  {len(at_risk)} repositoriota jäi riskitilaan -> "
                "keepalive-ajo epäonnistuu, jotta valvonta hälyttää.",
                file=sys.stderr,
            )
            sys.exit(4)
        return

    active = [
        cid for _, cid, outcome in results
        if cid and not outcome_matches(outcome, TERMINAL_OUTCOMES)
    ]
    while active:
        # Continuous local mode keeps the already-selected canonical/fallback
        # conversations alive. It never re-discovers a second conversation for
        # the same repository during the same run.
        for cid in list(active):
            outcome = check_conversation(base_url, headers, cid, args, state)
            # Sama terminaalilogiikka kuin --once-ajossa: my\u00f6s stalled ja
            # budget-exhausted poistuvat seurannasta sen sijaan ett\u00e4 niit\u00e4
            # t\u00f6n\u00e4ist\u00e4isiin loputtomiin.
            if outcome_matches(outcome, TERMINAL_OUTCOMES + ("sandbox-missing", "not-found")):
                active.remove(cid)
        if not active:
            break
        time.sleep(args.interval)




if __name__ == "__main__":
    main()
