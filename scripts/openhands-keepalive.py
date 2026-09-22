#!/usr/bin/env python3
"""
OpenHands Cloud keepalive / auto-nudge.

Valvoo yhtä tai useampaa OpenHands-conversationia ja lähettää
jatkoviestin, jos agentti on ollut pitkään pysähtyneenä.

Kaksi ajotapaa:

1. Jatkuva vahti (kotikone / palvelin):
   python scripts/openhands-keepalive.py --conversation-id <uuid>

2. Kertatarkistus (GitHub Actions, cron hoitaa toiston):
   python scripts/openhands-keepalive.py --once

Useampi conversation:
   --conversation-id <uuid1> --conversation-id <uuid2>
   tai OPENHANDS_CONVERSATION_IDS="uuid1,uuid2,uuid3"
   (vanha OPENHANDS_CONVERSATION_ID toimii yhä yhdelle)

Ympäristömuuttujat:
  OPENHANDS_API_KEY            (pakollinen)
  OPENHANDS_CONVERSATION_ID    (yksi, legacy)
  OPENHANDS_CONVERSATION_IDS   (pilkulla eroteltu lista)
  OPENHANDS_NUDGE
  OPENHANDS_BASE_URL
  OPENHANDS_POLL_INTERVAL
  OPENHANDS_IDLE_TIMEOUT
  OPENHANDS_RESUME_COOLDOWN
  OPENHANDS_DRY_RUN=1          (ei lähetä nudgeja/resumeja, vain logittaa)

Suositellut oletukset:
  jatkuva vahti: poll interval = 120 s, idle timeout = 1200 s (20 min)
  GitHub Actions: cron 5 min välein + --once + idle timeout 900 s (15 min)

DONE-pysäytys: ennen nudgea haetaan tuoreimmat MessageEventit
(GET /api/v1/conversation/{id}/events/search). Jos uusin viesti
kokonaisuudessaan on agentin täsmällinen "DONE", nudgea ei lähetetä
(outcome "done"). Jos käyttäjä on puhunut sen jälkeen, valvonta
jatkuu normaalisti. Tarkistus on fail-open: jos event-haku
epäonnistuu, toimitaan kuten ennenkin (tönäistään).

Logiikka per conversation:
  - GET conversation -> sandbox_status + execution_status + updated_at
  - sandbox PAUSED -> yritä resumea (cooldownilla), nudge jos liian kauan idle
  - sandbox STARTING / muu ei-RUNNING -> odota, ei tönäistä
    (execution_status on None kun sandbox ei ole RUNNING)
  - running -> odota
  - waiting_for_confirmation -> älä tönäise, ihminen tarvitaan
  - finished / idle / stuck -> jos updated_at on yli idle-timeoutin
    vanha, tarkista DONE ja lähetä nudge
  - uusin viesti on agentin "DONE" -> ei tönäistä (done)
  - sandbox ERROR/MISSING -> kyseisen conversationin valvonta lopetetaan
"""

import argparse
import datetime
import os
import sys
import time

try:
    import requests
except ImportError:
    print(
        "requests puuttuu. Asenna:\n"
        "  python -m pip install requests",
        file=sys.stderr,
    )
    sys.exit(1)


DEFAULT_NUDGE = (
    "Jatka tehtävää siitä mihin jäit.\n\n"
    "Tarkista ensin nykyinen tila ja jatka itse tehtävän suorittamista "
    "ilman turhaa selittelyä.\n\n"
    "Jos tehtävä on täysin valmis, vastaa täsmälleen:\n"
    "DONE\n\n"
    "Jos tehtävä ei ole vielä valmis, jatka työskentelyä ja vie tehtävä "
    "mahdollisimman pitkälle."
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="OpenHands conversation keepalive"
    )

    parser.add_argument(
        "--conversation-id",
        action="append",
        default=None,
        help="Conversation UUID (toistettavissa, tai "
        "OPENHANDS_CONVERSATION_IDS pilkulla eroteltuna)",
    )

    parser.add_argument(
        "--base-url",
        default=os.getenv(
            "OPENHANDS_BASE_URL",
            "https://app.all-hands.dev",
        ),
        help="OpenHands base URL",
    )

    parser.add_argument(
        "--nudge",
        default=os.getenv("OPENHANDS_NUDGE", DEFAULT_NUDGE),
        help="Viesti pysähtyneelle agentille",
    )

    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.getenv("OPENHANDS_POLL_INTERVAL", "120")),
        help="Pollausväli sekunteina jatkuvassa vahdissa (default 120)",
    )

    parser.add_argument(
        "--idle-timeout",
        type=int,
        default=int(os.getenv("OPENHANDS_IDLE_TIMEOUT", "1200")),
        help="Idle-aika ennen nudgea sekunteina (default 1200 = 20 min)",
    )

    parser.add_argument(
        "--resume-cooldown",
        type=int,
        default=int(os.getenv("OPENHANDS_RESUME_COOLDOWN", "900")),
        help="Kuinka usein PAUSED-resumea saa yrittää uudelleen",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=os.getenv("OPENHANDS_DRY_RUN", "").lower()
        in ("1", "true", "yes", "on"),
        help="Älä lähetä nudgeja/resumeja, vain logittaa",
    )

    parser.add_argument(
        "--once",
        action="store_true",
        help="Tarkista jokainen conversation kerran ja lopeta "
        "(GitHub Actions -moodi)",
    )

    parser.add_argument(
        "--no-done-check",
        action="store_true",
        default=os.getenv("OPENHANDS_NO_DONE_CHECK", "").lower()
        in ("1", "true", "yes", "on"),
        help="Älä tarkista DONE-viestiä ennen nudgea "
        "(hätäkatkaisin, jos event-haku oireilee)",
    )

    return parser.parse_args()


def resolve_conversation_ids(args):
    """Kerää ID:t lipuista + env-muuttujista, duplikaatit pois."""
    ids = []
    if args.conversation_id:
        ids.extend(args.conversation_id)
    plural = os.getenv("OPENHANDS_CONVERSATION_IDS", "")
    if plural:
        ids.extend(plural.split(","))
    singular = os.getenv("OPENHANDS_CONVERSATION_ID", "")
    if singular:
        ids.append(singular)
    seen = set()
    unique = []
    for raw in ids:
        cleaned = raw.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            unique.append(cleaned)
    return unique


def parse_updated_at(value: str) -> float:
    """Muuntaa ISO8601 timestampin Unix-timeksi."""
    if not value:
        return 0.0

    try:
        dt = datetime.datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
        return dt.timestamp()
    except Exception:
        return 0.0


def get_conversation(base_url, headers, conversation_id):
    response = requests.get(
        f"{base_url}/api/v1/app-conversations",
        headers=headers,
        params={"ids": conversation_id},
        timeout=30,
    )

    response.raise_for_status()

    items = response.json()

    if not items or not items[0]:
        raise RuntimeError("Conversation not found")

    return items[0]


def _event_text(event):
    """Poimi viestin teksti MessageEvent-muodoista puolustautuen.

    Palauttaa tekstin tai tyhjän merkkijonon, ei koskaan heitä.
    """
    try:
        if not isinstance(event, dict):
            return ""
        for key in ("llm_message", "message", "content", "text"):
            value = event.get(key)
            text = _coerce_text(value)
            if text:
                return text
        return ""
    except Exception:
        return ""


def _coerce_text(value):
    """Muunna merkkijono / content-lista / dict tekstiksi."""
    try:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts = [_coerce_text(item) for item in value]
            return "".join(p for p in parts if p)
        if isinstance(value, dict):
            # OpenAI-tyyli: {"type": "text", "text": "..."}
            # tai {"role": ..., "content": ...}
            if isinstance(value.get("text"), str):
                return value["text"]
            if "content" in value:
                return _coerce_text(value["content"])
            return ""
        return ""
    except Exception:
        return ""


def _event_timestamp(event):
    """Eventin timestamp Unix-timena (0.0 jos puuttuu/virheellinen)."""
    try:
        if isinstance(event, dict):
            return parse_updated_at(event.get("timestamp", ""))
        return 0.0
    except Exception:
        return 0.0


def is_done(events):
    """Onko uusin viesti kokonaisuudessaan agentin täsmällinen DONE?

    Sääntö: uusin viesti overall ratkaisee. Jos agentti vastasi DONE
    mutta käyttäjä puhui sen jälkeen, kyseessä on uusi tehtävä eikä
    DONEa huomioida. Täsmää vain tasan "DONE" (whitespace + case
    sallitaan), jotta "DONE!" tai lauseen sisäinen maininta ei
    pysäytä valvontaa vahingossa.
    """
    try:
        if not events:
            return False
        messages = [
            e for e in events
            if isinstance(e, dict)
            and e.get("source") in ("agent", "user")
            and _event_text(e).strip()
        ]
        if not messages:
            return False
        # Uusin ensin: timestampilla jos saatavilla, muuten listajärjestys.
        if any(_event_timestamp(e) > 0 for e in messages):
            messages.sort(key=_event_timestamp, reverse=True)
        else:
            messages = messages[::-1]
        latest = messages[0]
        if latest.get("source") != "agent":
            return False
        return _event_text(latest).strip().upper() == "DONE"
    except Exception:
        return False


def fetch_recent_messages(base_url, headers, conversation_id, limit=20):
    """Hae tuoreimmat MessageEventit. Palauttaa listan tai None.

    None = haku epäonnistui -> kutsujan kuuluu jatkaa vanhalla
    logiikalla (fail-open), ei koskaan pysäyttää valvontaa.
    """
    try:
        response = requests.get(
            f"{base_url}/api/v1/conversation/"
            f"{conversation_id}/events/search",
            headers=headers,
            params={"kind__eq": "MessageEvent", "limit": limit},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            for key in ("items", "results", "events"):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
            return []
        if isinstance(payload, list):
            return payload
        return []
    except Exception as exc:
        print(f"  DONE-tarkistus epäonnistui: {exc} -> jatketaan normaalisti.")
        return None


def try_resume(base_url, headers, sandbox_id, dry_run):
    if not sandbox_id:
        print("  resume: sandbox_id puuttuu")
        return False

    if dry_run:
        print("  DRY-RUN: resumea ei lähetetty")
        return True

    try:
        response = requests.post(
            f"{base_url}/api/v1/sandboxes/{sandbox_id}/resume",
            headers=headers,
            timeout=30,
        )

        print(
            f"  resume -> {response.status_code} "
            f"{response.text[:200]}"
        )

        return response.status_code < 300

    except Exception as exc:
        print(f"  resume epäonnistui: {exc}")
        return False


def send_nudge(base_url, headers, conversation_id, text, dry_run):
    if dry_run:
        print("  DRY-RUN: nudgea ei lähetetty")
        return True

    response = requests.post(
        f"{base_url}/api/v1/app-conversations/"
        f"{conversation_id}/send-message",
        headers=headers,
        timeout=30,
        json={
            "role": "user",
            "run": True,
            "content": [
                {
                    "type": "text",
                    "text": text,
                }
            ],
        },
    )

    if response.status_code in (409, 410, 503):
        print(
            f"  send-message hylätty "
            f"{response.status_code}: "
            f"{response.text[:300]}"
        )
        return False

    response.raise_for_status()

    print(
        f"  send-message OK: "
        f"{response.text[:300]}"
    )

    return True


def check_conversation(base_url, headers, conv_id, args, state):
    """Yksi tarkistus yhdelle conversatiolle.

    Palauttaa outcome-merkkijonon yhteenvedolle. Päivittää
    state-sanakirjoja (nudges, last_resume per conversation).
    """
    dry_run = args.dry_run
    nudges = state["nudges"].get(conv_id, 0)

    try:
        conversation = get_conversation(
            base_url,
            headers,
            conv_id,
        )

        sandbox_status = conversation.get("sandbox_status")
        execution_status = conversation.get("execution_status")
        updated_at = conversation.get("updated_at", "")
        title = conversation.get("title", "")
        sandbox_id = conversation.get("sandbox_id", "")

        now = time.time()
        updated_ts = parse_updated_at(updated_at)

        if updated_ts:
            idle_for = max(0, int(now - updated_ts))
        else:
            idle_for = None

        timestamp = datetime.datetime.now().isoformat(
            timespec="seconds"
        )

        if idle_for is not None:
            idle_text = f"{idle_for}s idle"
        else:
            idle_text = "idle unknown"

        print(
            f"[{timestamp}] [{conv_id[:8]}] "
            f"sandbox={sandbox_status} "
            f"exec={execution_status} "
            f"{idle_text} "
            f"title={title!r}"
        )

        # --------------------------------------------------
        # Sandbox terminaalitila -> valvonta loppuu tältä osin
        # --------------------------------------------------
        if sandbox_status in ("ERROR", "MISSING"):
            print(
                f"  Sandbox {sandbox_status}: "
                "tämän conversationin valvonta lopetetaan."
            )
            return "retire-sandbox"

        # --------------------------------------------------
        # Sandbox PAUSED -> resume (cooldownilla) + nudge jos liian kauan idle
        # --------------------------------------------------
        if sandbox_status == "PAUSED":
            last_resume = state["last_resume"].get(conv_id, 0.0)
            if now - last_resume >= args.resume_cooldown:
                print(
                    "  Sandbox PAUSED -> yritetään resumea..."
                )
                try_resume(
                    base_url,
                    headers,
                    sandbox_id,
                    dry_run,
                )
                state["last_resume"][conv_id] = now
            else:
                remaining = int(
                    args.resume_cooldown - (now - last_resume)
                )
                print(
                    f"  Sandbox PAUSED -> "
                    f"resume cooldown, {remaining}s jäljellä."
                )

            # Tarkista onko conversation ollut liian kauan idle PAUSED-tilassa
            # Jos on, lähetä nudge vaikka resume on cooldownissa
            if idle_for is not None and idle_for >= args.idle_timeout:
                # DONE-tarkistus myös PAUSED-tilassa
                if not args.no_done_check:
                    recent = fetch_recent_messages(
                        base_url, headers, conv_id
                    )
                    if recent is not None and is_done(recent):
                        print(
                            "  Uusin viesti on agentin DONE -> "
                            "tehtävä valmis, ei tönäistä."
                        )
                        return "done"

                next_nudge = nudges + 1
                print(
                    f"  PAUSED ja {idle_for}s idle -> "
                    f"nudge {next_nudge}"
                )

                if send_nudge(
                    base_url,
                    headers,
                    conv_id,
                    args.nudge,
                    dry_run,
                ):
                    state["nudges"][conv_id] = next_nudge
                    print(
                        "  Nudge lähetetty -> "
                        "odotetaan seuraavaa pollia."
                        if not dry_run
                        else "  (dry-run, ei lasketa)"
                    )
                    return "dry-nudge" if dry_run else "nudged"

                print(
                    "  Nudge ei mennyt läpi -> "
                    "yritetään myöhemmin uudelleen."
                )
                return "nudge-failed"

            return "paused"

        # --------------------------------------------------
        # Sandbox ei valmis -> ei tönäistä (execution_status on
        # None kun sandbox ei ole RUNNING, joten arvaaminen
        # johtaisi vain hylättyihin send-message-kutsuihin).
        # --------------------------------------------------
        if sandbox_status == "STARTING":
            print(
                "  Sandbox STARTING -> odotetaan "
                "käynnistymistä, ei tönäistä."
            )
            return "starting"

        if sandbox_status != "RUNNING":
            print(
                f"  Sandbox {sandbox_status} ei RUNNING -> "
                "ei tönäistä, odotetaan."
            )
            return "not-running"

        # --------------------------------------------------
        # Running
        # --------------------------------------------------
        if execution_status == "running":
            print("  Agentti on running -> ei tehdä mitään.")
            return "running"

        # --------------------------------------------------
        # Human confirmation required
        # --------------------------------------------------
        if execution_status == "waiting_for_confirmation":
            print(
                "  VAATII VAHVISTUKSEN UI:ssa -> "
                "ei lähetetä automaattista nudgea."
            )
            return "confirmation"

        # --------------------------------------------------
        # Potentially stalled
        # --------------------------------------------------
        if execution_status in (
            "finished",
            "idle",
            "stuck",
            "error",
            None,
        ):
            if idle_for is None:
                print(
                    "  updated_at puuttuu / ei voitu tulkita -> "
                    "odotetaan."
                )
                return "idle-unknown"

            if idle_for < args.idle_timeout:
                print(
                    f"  Ei vielä tarpeeksi idle: "
                    f"{idle_for}s / {args.idle_timeout}s."
                )
                return "idle-wait"

            # --------------------------------------------------
            # DONE-tarkistus: vain kun nudge olisi muuten lähdössä,
            # jotta event-haku ei kuormita joka pollia. Fail-open:
            # epäonnistunut haku ei estä nudgea.
            # --------------------------------------------------
            if not args.no_done_check:
                recent = fetch_recent_messages(
                    base_url, headers, conv_id
                )
                if recent is not None and is_done(recent):
                    print(
                        "  Uusin viesti on agentin DONE -> "
                        "tehtävä valmis, ei tönäistä."
                    )
                    return "done"

            next_nudge = nudges + 1
            print(
                f"  Agentti pysähtynyt "
                f"({execution_status}), "
                f"{idle_for}s idle -> "
                f"nudge {next_nudge}"
            )

            if send_nudge(
                base_url,
                headers,
                conv_id,
                args.nudge,
                dry_run,
            ):
                state["nudges"][conv_id] = next_nudge
                print(
                    "  Nudge lähetetty -> "
                    "odotetaan seuraavaa pollia."
                    if not dry_run
                    else "  (dry-run, ei lasketa)"
                )
                return "dry-nudge" if dry_run else "nudged"

            print(
                "  Nudge ei mennyt läpi -> "
                "yritetään myöhemmin uudelleen."
            )
            return "nudge-failed"

        print(
            f"  Tuntematon execution_status="
            f"{execution_status!r} -> odotetaan."
        )
        return "unknown"

    except requests.HTTPError as exc:
        response_text = ""
        if exc.response is not None:
            response_text = exc.response.text[:300]
        print(
            f"  HTTP-virhe: {exc}"
            + (f" | {response_text}" if response_text else "")
        )
        return "http-error"

    except requests.RequestException as exc:
        print(f"  Network-virhe: {exc}")
        return "net-error"

    except Exception as exc:
        print(f"  Virhe: {exc}")
        return "error"


def write_step_summary(results):
    """Kirjoita Actions-yhteenveto jos GITHUB_STEP_SUMMARY on asetettu."""
    path = os.getenv("GITHUB_STEP_SUMMARY", "")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("## OpenHands keepalive\n\n")
            fh.write("| Conversation | Tulos |\n")
            fh.write("|---|---|\n")
            for conv_id, outcome in results:
                fh.write(f"| `{conv_id[:8]}` | {outcome} |\n")
    except Exception as exc:
        print(f"  Step summary epäonnistui: {exc}")


def main():
    args = parse_args()

    api_key = os.getenv("OPENHANDS_API_KEY", "")
    if not api_key:
        print(
            "OPENHANDS_API_KEY puuttuu.\n"
            "Luo API key OpenHands Cloudin asetuksista.",
            file=sys.stderr,
        )
        sys.exit(2)

    conv_ids = resolve_conversation_ids(args)
    if not conv_ids:
        print(
            "--conversation-id puuttuu "
            "(tai OPENHANDS_CONVERSATION_ID / "
            "OPENHANDS_CONVERSATION_IDS)",
            file=sys.stderr,
        )
        sys.exit(2)

    if not args.once and args.interval <= 0:
        print("--interval pitää olla > 0", file=sys.stderr)
        sys.exit(2)

    if args.idle_timeout <= 0:
        print("--idle-timeout pitää olla > 0", file=sys.stderr)
        sys.exit(2)

    base_url = args.base_url.rstrip("/")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    state = {"nudges": {}, "last_resume": {}}

    print("--- OpenHands keepalive ---")
    print(f"conversations: {len(conv_ids)}")
    for cid in conv_ids:
        print(f"  - {cid}")
    if not args.once:
        print(
            f"poll interval: {args.interval}s "
            f"({args.interval / 60:.1f} min)"
        )
    print(
        f"idle timeout: {args.idle_timeout}s "
        f"({args.idle_timeout / 60:.1f} min)"
    )
    if args.dry_run:
        print("DRY-RUN: ei lähetetä nudgeja/resumeja")
    print()

    # --------------------------------------------------
    # Kertatarkistus (GitHub Actions)
    # --------------------------------------------------
    if args.once:
        results = []
        for cid in conv_ids:
            try:
                outcome = check_conversation(
                    base_url, headers, cid, args, state
                )
            except KeyboardInterrupt:
                print("\nLopetetaan käyttäjän pyynnöstä.")
                break
            results.append((cid, outcome))
        print()
        print("--- Yhteenveto ---")
        for cid, outcome in results:
            print(f"  {cid[:8]}: {outcome}")
        write_step_summary(results)
        return

    # --------------------------------------------------
    # Jatkuva vahti (kotikone / palvelin)
    # --------------------------------------------------
    active = list(conv_ids)
    while active:
        for cid in list(active):
            try:
                outcome = check_conversation(
                    base_url, headers, cid, args, state
                )
            except KeyboardInterrupt:
                print("\nLopetetaan käyttäjän pyynnöstä.")
                return
            if outcome in ("retire-sandbox", "done"):
                active.remove(cid)
        if not active:
            print("Kaikki conversationit eläköity -> lopetetaan.")
            break
        print(
            f"  Seuraava kierros "
            f"{args.interval}s kuluttua..."
        )
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nLopetetaan käyttäjän pyynnöstä.")
            break


if __name__ == "__main__":
    main()
