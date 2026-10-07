"""End-to-end test for scripts/openhands-keepalive.py --once.

The script is run exactly the way GitHub Actions runs it, against a local fake
OpenHands API. This covers the behavior that unit tests cannot: canonical
selection, stalled escalation, dry-run decisions, redaction and the step
summary. No network access outside 127.0.0.1 and no API key are required.
"""

import datetime
import json
import os
import pathlib
import socket
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "openhands-keepalive.py"
OLD = "2026-10-01T10:00:00Z"


def iso_now():
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def conversation(cid, repository, created, sandbox, execution, updated, title="agent"):
    return {
        "id": cid,
        "selected_repository": repository,
        "created_at": created,
        "sandbox_status": sandbox,
        "execution_status": execution,
        "updated_at": updated,
        "title": title,
        "sandbox_id": "sandbox-" + cid[:8],
    }


ALPHA_DEAD = "dead0001-1111-2222-3333-444444444444"
ALPHA_ALIVE = "alive001-1111-2222-3333-444444444444"
BETA_STALLED = "busy0001-1111-2222-3333-444444444444"
GAMMA_FRESH = "gamma001-1111-2222-3333-444444444444"
DELTA_HUMAN = "delta001-1111-2222-3333-444444444444"

CONVERSATIONS = {
    # newest conversation lost its sandbox -> an older live one must be canonical
    ALPHA_DEAD: conversation(ALPHA_DEAD, "org/alpha", "2026-10-03T10:00:00Z", "MISSING", None, OLD),
    ALPHA_ALIVE: conversation(ALPHA_ALIVE, "org/alpha", "2026-10-01T10:00:00Z", "RUNNING", "finished", OLD),
    # agent never answered four nudges -> stalled -> recovery
    BETA_STALLED: conversation(BETA_STALLED, "org/beta", "2026-10-02T10:00:00Z", "RUNNING", "finished", OLD),
    # recently active -> still inside the nudge threshold
    GAMMA_FRESH: conversation(GAMMA_FRESH, "org/gamma", "2026-10-02T11:00:00Z", "RUNNING", "finished", None),
    # blocked on a human confirmation
    DELTA_HUMAN: conversation(DELTA_HUMAN, "org/delta", "2026-10-02T12:00:00Z", "RUNNING", "waiting_for_confirmation", OLD),
}

_posted_paths = []


class _FakeOpenHands(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def _send(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        path, query = url.path, parse_qs(url.query)
        if path == "/api/v1/app-conversations/search":
            self._send({"items": list(CONVERSATIONS.values()), "next_page_id": None})
        elif path == "/api/v1/app-conversations":
            cid = (query.get("ids") or [""])[0]
            item = CONVERSATIONS.get(cid)
            self._send([item] if item else [])
        elif path.endswith("/events/search"):
            cid = path.split("/api/v1/conversation/")[1].split("/")[0]
            if cid == BETA_STALLED:
                self._send({
                    "items": [
                        {
                            "source": "user",
                            "llm_message": {"content": [{"type": "text", "text": "nudge"}]},
                            "timestamp": "2026-10-01T10:0%d:00Z" % minute,
                        }
                        for minute in (1, 2, 3, 4)
                    ]
                })
            else:
                self._send({"items": []})
        elif path == "/api/v1/app-conversations/start-tasks/search":
            self._send({"items": []})
        else:
            self._send({}, 404)

    def do_POST(self):
        _posted_paths.append(self.path)
        self._send({"ok": True})


class KeepaliveEndToEndTests(unittest.TestCase):
    def setUp(self):
        handle = socket.socket()
        handle.bind(("127.0.0.1", 0))
        self.port = handle.getsockname()[1]
        handle.close()
        self.server = HTTPServer(("127.0.0.1", self.port), _FakeOpenHands)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        _posted_paths.clear()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _run_once(self, tmp_dir):
        summary = str(pathlib.Path(tmp_dir) / "summary.md")
        env = dict(os.environ)
        env.update({
            "OPENHANDS_API_KEY": "test-key",
            "OPENHANDS_BASE_URL": "http://127.0.0.1:%d" % self.port,
            "OPENHANDS_AUTO_DISCOVER": "1",
            "OPENHANDS_DRY_RUN": "1",
            "OPENHANDS_NUDGE_MODE": "loop",
            "OPENHANDS_IDLE_TIMEOUT": "900",
            "OPENHANDS_MIN_NUDGE_INTERVAL": "1800",
            "OPENHANDS_MAX_STALLED_NUDGES": "4",
            "OPENHANDS_RUN_BUDGET_SECONDS": "420",
            "GITHUB_STEP_SUMMARY": summary,
        })
        for name in ("OPENHANDS_VERBOSE", "OPENHANDS_CONVERSATION_IDS",
                     "OPENHANDS_FAIL_ON_ATTENTION", "OPENHANDS_NO_DONE_CHECK"):
            env.pop(name, None)
        # a freshness relative to the real clock keeps the idle assertion stable
        CONVERSATIONS[GAMMA_FRESH] = conversation(
            GAMMA_FRESH, "org/gamma", "2026-10-02T11:00:00Z", "RUNNING", "finished", iso_now()
        )

        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--once"],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        with open(summary, encoding="utf-8") as handle:
            return proc, handle.read()

    def test_once_run_behaves_as_documented(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            proc, summary = self._run_once(tmp_dir)
        out = proc.stdout

        failures = []
        for name, ok in (
            ("exit code 0", proc.returncode == 0),
            ("stderr empty", proc.stderr.strip() == ""),
            ("lost newest conversation is not canonical",
             "alive001" in out and "dead0001" not in out),
            ("stalled agent is escalated and replaced",
             "sandbox-replaced" in out),
            ("recent activity only waits",
             "idle-wait" in out),
            ("human confirmation is reported",
             "confirmation" in out),
            ("repository names are redacted from stdout",
             "org/alpha" not in out and "org/beta" not in out
             and "org/gamma" not in out and "org/delta" not in out),
            ("repository names are redacted from the summary",
             "org/" not in summary),
            ("titles are redacted", "'<redacted>'" in out),
            ("full conversation UUIDs are never printed",
             "-1111-2222-3333-444444444444" not in out),
            ("summary marks needs human",
             "Needs human (1)" in summary),
            ("summary keeps the stall visible at risk",
             "At risk (1)" in summary and "stalled->sandbox-replaced" in summary),
            ("dry run sends nothing", _posted_paths == []),
        ):
            if not ok:
                failures.append(name)

        if failures:
            self.fail(
                "failed checks: %s\n--- stdout ---\n%s\n--- summary ---\n%s"
                % (", ".join(failures), out, summary)
            )


if __name__ == "__main__":
    unittest.main()
