import importlib.util
import pathlib
import unittest


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "openhands-keepalive.py"
SPEC = importlib.util.spec_from_file_location("openhands_keepalive", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeRequests:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error

    def get(self, *args, **kwargs):
        if self.error:
            raise self.error
        return FakeResponse(self.payload)


class RecoveryIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.original_requests = MODULE.requests

    def tearDown(self):
        MODULE.requests = self.original_requests

    def test_recent_start_task_for_repository_blocks_replacement(self):
        MODULE.requests = FakeRequests({
            "items": [{
                "status": "WAITING_FOR_SANDBOX",
                "request": {"selected_repository": "akukuusisto/tradefoundry"},
            }]
        })
        self.assertTrue(
            MODULE.has_recent_start_task(
                "https://app.all-hands.dev",
                {},
                "akukuusisto/tradefoundry",
            )
        )

    def test_ready_start_task_is_also_treated_as_existing(self):
        MODULE.requests = FakeRequests({
            "items": [{
                "status": "READY",
                "selected_repository": "akukuusisto/accounter",
            }]
        })
        self.assertTrue(
            MODULE.has_recent_start_task(
                "https://app.all-hands.dev",
                {},
                "akukuusisto/accounter",
            )
        )

    def test_start_task_search_failure_fails_closed(self):
        MODULE.requests = FakeRequests(error=RuntimeError("network down"))
        self.assertTrue(
            MODULE.has_recent_start_task(
                "https://app.all-hands.dev",
                {},
                "akukuusisto/build-horizon-pilot",
            )
        )


class RepositorySelectionTests(unittest.TestCase):
    def test_only_newest_conversation_is_canonical_per_repository(self):
        groups = MODULE.group_conversations_by_repository([
            {"id":"old","selected_repository":"akukuusisto/tradefoundry","created_at":"2026-10-01T10:00:00Z","sandbox_status":"RUNNING"},
            {"id":"new","selected_repository":"akukuusisto/tradefoundry","created_at":"2026-10-03T10:00:00Z","sandbox_status":"RUNNING"},
            {"id":"other","selected_repository":"akukuusisto/accounter","created_at":"2026-10-02T10:00:00Z","sandbox_status":"PAUSED"},
        ])
        latest = MODULE.select_latest_per_repository(groups)
        self.assertEqual(latest["akukuusisto/tradefoundry"]["id"], "new")
        self.assertEqual(latest["akukuusisto/accounter"]["id"], "other")

    def test_created_at_not_updated_at_defines_newest(self):
        groups = MODULE.group_conversations_by_repository([
            {"id":"older-created","selected_repository":"akukuusisto/accounter","created_at":"2026-10-01T10:00:00Z","updated_at":"2026-10-03T10:00:00Z","sandbox_status":"RUNNING"},
            {"id":"newer-created","selected_repository":"akukuusisto/accounter","created_at":"2026-10-02T10:00:00Z","updated_at":"2026-10-02T10:30:00Z","sandbox_status":"RUNNING"},
        ])
        latest = MODULE.select_latest_per_repository(groups)
        self.assertEqual(latest["akukuusisto/accounter"]["id"], "newer-created")

    def test_missing_latest_keeps_older_candidate_available_for_fallback(self):
        groups = MODULE.group_conversations_by_repository([
            {"id":"old","selected_repository":"akukuusisto/build-horizon-pilot","created_at":"2026-10-01T10:00:00Z","sandbox_status":"RUNNING"},
            {"id":"new","selected_repository":"akukuusisto/build-horizon-pilot","created_at":"2026-10-03T10:00:00Z","sandbox_status":"MISSING"},
        ])
        candidates = groups["akukuusisto/build-horizon-pilot"]
        self.assertEqual(candidates[0]["id"], "new")
        self.assertEqual(candidates[1]["id"], "old")

    def test_known_missing_older_conversations_are_skipped(self):
        groups = MODULE.group_conversations_by_repository([
            {"id":"oldest","selected_repository":"akukuusisto/ridekernel-explore","created_at":"2026-10-01T10:00:00Z","sandbox_status":"MISSING"},
            {"id":"older","selected_repository":"akukuusisto/ridekernel-explore","created_at":"2026-10-02T10:00:00Z","sandbox_status":"PAUSED"},
            {"id":"new","selected_repository":"akukuusisto/ridekernel-explore","created_at":"2026-10-03T10:00:00Z","sandbox_status":"MISSING"},
        ])
        candidates = groups["akukuusisto/ridekernel-explore"]
        reusable = next(c["id"] for c in candidates[1:] if c["sandbox_status"] != "MISSING")
        self.assertEqual(reusable, "older")


class NudgeRecoveryTests(unittest.TestCase):
    def _args(self):
        from types import SimpleNamespace
        return SimpleNamespace(
            dry_run=False,
            nudge="continue",
            nudge_mode="loop",
            no_done_check=True,
            resume_cooldown=900,
            resume_wait_seconds=90,
            resume_poll_interval=5,
            idle_timeout=900,
        )

    def test_send_nudge_function_exists_and_supports_dry_run(self):
        self.assertTrue(callable(MODULE.send_nudge))
        self.assertTrue(
            MODULE.send_nudge(
                "https://app.all-hands.dev",
                {},
                "conversation-1",
                "continue",
                True,
            )
        )

    def test_sandbox_error_still_gets_nudged(self):
        original_get = MODULE.get_conversation
        original_activity = MODULE.latest_activity_ts
        original_nudge = MODULE.send_nudge
        try:
            MODULE.get_conversation = lambda *args: {
                "sandbox_status": "ERROR",
                "execution_status": "error",
                "updated_at": "2026-10-01T10:00:00Z",
                "title": "failed agent",
                "sandbox_id": "sandbox-1",
            }
            MODULE.latest_activity_ts = lambda *args: MODULE.time.time() - 3600
            calls = []
            MODULE.send_nudge = lambda *args: calls.append(args) or True

            outcome = MODULE.check_conversation(
                "https://app.all-hands.dev",
                {},
                "conversation-1",
                self._args(),
                {"nudges": {}, "last_resume": {}, "new_conversations": set()},
            )

            self.assertEqual(outcome, "nudged")
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][2], "conversation-1")
        finally:
            MODULE.get_conversation = original_get
            MODULE.latest_activity_ts = original_activity
            MODULE.send_nudge = original_nudge

    def test_sandbox_error_nudged_when_idle_time_unknown(self):
        """ERROR/error with unmeasurable idle must still be nudged (not idle-unknown)."""
        original_get = MODULE.get_conversation
        original_activity = MODULE.latest_activity_ts
        original_nudge = MODULE.send_nudge
        try:
            MODULE.get_conversation = lambda *args: {
                "sandbox_status": "ERROR",
                "execution_status": "error",
                "updated_at": "",
                "title": "failed agent",
                "sandbox_id": "sandbox-1",
            }
            MODULE.latest_activity_ts = lambda *args: 0.0
            calls = []
            MODULE.send_nudge = lambda *args: calls.append(args) or True

            outcome = MODULE.check_conversation(
                "https://app.all-hands.dev",
                {},
                "conversation-1",
                self._args(),
                {"nudges": {}, "last_resume": {}, "new_conversations": set()},
            )

            self.assertEqual(outcome, "nudged")
            self.assertEqual(len(calls), 1)
        finally:
            MODULE.get_conversation = original_get
            MODULE.latest_activity_ts = original_activity
            MODULE.send_nudge = original_nudge

    def test_finished_nudged_when_idle_time_unknown(self):
        original_get = MODULE.get_conversation
        original_activity = MODULE.latest_activity_ts
        original_nudge = MODULE.send_nudge
        try:
            MODULE.get_conversation = lambda *args: {
                "sandbox_status": "RUNNING",
                "execution_status": "finished",
                "updated_at": "",
                "title": "finished agent",
                "sandbox_id": "sandbox-2",
            }
            MODULE.latest_activity_ts = lambda *args: 0.0
            calls = []
            MODULE.send_nudge = lambda *args: calls.append(args) or True

            outcome = MODULE.check_conversation(
                "https://app.all-hands.dev",
                {},
                "conversation-2",
                self._args(),
                {"nudges": {}, "last_resume": {}, "new_conversations": set()},
            )

            self.assertEqual(outcome, "nudged")
            self.assertEqual(len(calls), 1)
        finally:
            MODULE.get_conversation = original_get
            MODULE.latest_activity_ts = original_activity
            MODULE.send_nudge = original_nudge

    def test_sandbox_missing_does_not_nudge(self):
        original_get = MODULE.get_conversation
        original_events = MODULE.fetch_recent_events
        original_nudge = MODULE.send_nudge
        try:
            MODULE.get_conversation = lambda *args: {
                "sandbox_status": "MISSING",
                "execution_status": "error",
            }
            MODULE.fetch_recent_events = lambda *args, **kwargs: []
            def fail_nudge(*args, **kwargs):
                self.fail("MISSING sandbox must not receive a nudge")
            MODULE.send_nudge = fail_nudge

            outcome = MODULE.check_conversation(
                "https://app.all-hands.dev",
                {},
                "conversation-1",
                self._args(),
                {"nudges": {}, "last_resume": {}, "new_conversations": set()},
            )

            self.assertEqual(outcome, "sandbox-missing")
        finally:
            MODULE.get_conversation = original_get
            MODULE.fetch_recent_events = original_events
            MODULE.send_nudge = original_nudge

    def test_missing_sandbox_fallback_waits_for_resumed_paused_conversation(self):
        original_resume = MODULE.try_resume
        original_wait = MODULE.wait_for_resumed_sandbox
        original_check = MODULE.check_conversation
        try:
            calls = []
            MODULE.try_resume = lambda *args: calls.append("resume") or True
            MODULE.wait_for_resumed_sandbox = (
                lambda *args, **kwargs: calls.append("wait") or True
            )
            MODULE.check_conversation = lambda *args: calls.append("check") or "nudged"

            from types import SimpleNamespace
            args = SimpleNamespace(
                dry_run=False,
                discover_limit=50,
                nudge="continue",
                resume_wait_seconds=90,
                resume_poll_interval=5,
            )
            state = {"nudges": {}, "last_resume": {}, "new_conversations": set()}
            candidates = [
                {"id": "canonical", "sandbox_status": "MISSING"},
                {"id": "older", "sandbox_status": "PAUSED", "sandbox_id": "sandbox-older"},
            ]

            cid, outcome = MODULE.recover_repository_after_loss(
                "https://app.all-hands.dev", {}, "akukuusisto/battleweave",
                candidates, args, state,
            )

            self.assertEqual(cid, "older")
            self.assertEqual(outcome, "nudged")
            self.assertEqual(calls, ["resume", "wait", "check"])
        finally:
            MODULE.try_resume = original_resume
            MODULE.wait_for_resumed_sandbox = original_wait
            MODULE.check_conversation = original_check

class ResumeRecoveryTests(unittest.TestCase):
    """Resume on asynkroninen: nudgea ei laheteta ennen kuin sandbox on RUNNING."""

    def _args(self, **overrides):
        from types import SimpleNamespace
        values = dict(
            dry_run=False,
            nudge="continue",
            nudge_mode="loop",
            no_done_check=True,
            resume_cooldown=900,
            resume_wait_seconds=90,
            resume_poll_interval=1,
            idle_timeout=900,
        )
        values.update(overrides)
        return SimpleNamespace(**values)

    def _conversation(self, status):
        return {
            "sandbox_status": status,
            "execution_status": None,
            "updated_at": "2026-10-01T10:00:00Z",
            "title": "resumed agent",
            "sandbox_id": "sandbox-1",
        }

    def _run(self, args, statuses, resume=True):
        original_get = MODULE.get_conversation
        original_activity = MODULE.latest_activity_ts
        original_resume = MODULE.try_resume
        original_nudge = MODULE.send_nudge
        original_sleep = MODULE.time.sleep
        try:
            statuses_iter = iter(statuses)
            MODULE.get_conversation = lambda *a, **k: self._conversation(
                next(statuses_iter)
            )
            # raskas idle, jotta testi todistaa ettei nudgea laheteta
            MODULE.latest_activity_ts = lambda *a: MODULE.time.time() - 3600
            MODULE.try_resume = lambda *a: resume
            calls = []
            MODULE.send_nudge = lambda *a: calls.append(a) or True
            MODULE.time.sleep = lambda *a: None
            outcome = MODULE.check_conversation(
                "https://app.all-hands.dev",
                {},
                "conversation-1",
                args,
                {"nudges": {}, "last_resume": {}, "new_conversations": set()},
            )
            return outcome, calls
        finally:
            MODULE.get_conversation = original_get
            MODULE.latest_activity_ts = original_activity
            MODULE.try_resume = original_resume
            MODULE.send_nudge = original_nudge
            MODULE.time.sleep = original_sleep

    def test_paused_sandbox_waits_until_running_before_nudging(self):
        outcome, calls = self._run(
            self._args(), ["PAUSED", "STARTING", "RUNNING"]
        )
        self.assertEqual(outcome, "resumed")
        self.assertEqual(calls, [])

    def test_paused_sandbox_that_never_becomes_ready_reports_resuming(self):
        outcome, calls = self._run(
            self._args(resume_wait_seconds=0), ["PAUSED", "STARTING"]
        )
        self.assertEqual(outcome, "resuming")
        self.assertEqual(calls, [])

    def test_failed_resume_stays_paused_without_nudging(self):
        outcome, calls = self._run(self._args(), ["PAUSED"], resume=False)
        self.assertEqual(outcome, "paused")
        self.assertEqual(calls, [])

    def test_dry_run_resume_is_not_waited_on(self):
        outcome, calls = self._run(self._args(dry_run=True), ["PAUSED"])
        self.assertEqual(outcome, "resumed")
        self.assertEqual(calls, [])

    def test_wait_returns_false_when_the_sandbox_errors(self):
        original_get = MODULE.get_conversation
        original_sleep = MODULE.time.sleep
        try:
            MODULE.get_conversation = lambda *a, **k: self._conversation("ERROR")
            MODULE.time.sleep = lambda *a: None
            ready = MODULE.wait_for_resumed_sandbox(
                "https://app.all-hands.dev", {}, "conversation-1", "sandbox-1"
            )
        finally:
            MODULE.get_conversation = original_get
            MODULE.time.sleep = original_sleep
        self.assertFalse(ready)

    def test_wait_returns_true_once_running(self):
        original_get = MODULE.get_conversation
        original_sleep = MODULE.time.sleep
        statuses = iter(["STARTING", "RUNNING"])
        try:
            MODULE.get_conversation = lambda *a, **k: self._conversation(next(statuses))
            MODULE.time.sleep = lambda *a: None
            ready = MODULE.wait_for_resumed_sandbox(
                "https://app.all-hands.dev", {}, "conversation-1", "sandbox-1"
            )
        finally:
            MODULE.get_conversation = original_get
            MODULE.time.sleep = original_sleep
        self.assertTrue(ready)


if __name__ == "__main__":
    unittest.main()
