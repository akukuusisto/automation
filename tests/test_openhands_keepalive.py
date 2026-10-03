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


if __name__ == "__main__":
    unittest.main()
