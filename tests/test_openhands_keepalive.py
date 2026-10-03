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


if __name__ == "__main__":
    unittest.main()
