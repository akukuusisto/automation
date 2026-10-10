import importlib.util
import pathlib
import unittest
from unittest.mock import patch


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
    def test_skip_repositories_resolves_optional_environment_secret(self):
        with patch.dict(
            "os.environ",
            {
                "OPENHANDS_SKIP_REPOSITORIES":
                    " example-org/skip-this-repo , ORG/example/ , , "
            },
        ):
            self.assertEqual(
                MODULE.resolve_skip_repositories(),
                {"example-org/skip-this-repo", "org/example"},
            )

    def test_skip_repository_excludes_all_conversations_case_insensitively(self):
        excluded = set()
        groups = MODULE.group_conversations_by_repository(
            [
                {
                    "id": "excluded-old",
                    "selected_repository": "example-org/skip-this-repo",
                    "created_at": "2026-10-01T10:00:00Z",
                    "sandbox_status": "RUNNING",
                },
                {
                    "id": "excluded-new",
                    "selected_repository": "example-org/skip-this-repo",
                    "created_at": "2026-10-03T10:00:00Z",
                    "sandbox_status": "RUNNING",
                },
                {
                    "id": "kept",
                    "selected_repository": "example-org/keep-this-repo",
                    "created_at": "2026-10-02T10:00:00Z",
                    "sandbox_status": "PAUSED",
                },
            ],
            skip_repositories={" EXAMPLE-ORG/SKIP-THIS-REPO/ "},
            excluded_repositories=excluded,
        )
        self.assertEqual(excluded, {"example-org/skip-this-repo"})
        self.assertNotIn("example-org/skip-this-repo", groups)
        self.assertEqual(
            groups["example-org/keep-this-repo"][0]["id"], "kept"
        )

    def test_main_succeeds_when_all_discovered_repositories_are_excluded(self):
        from types import SimpleNamespace

        args = SimpleNamespace(
            once=True,
            run_budget=0,
            base_url="https://app.all-hands.dev",
            idle_timeout=900,
            min_nudge_interval=1800,
            max_stalled_nudges=4,
            verbose=False,
        )

        def collect_groups(*_args, excluded_repositories=None, **_kwargs):
            excluded_repositories.add("example-org/skip-this-repo")
            return {}

        with patch.dict("os.environ", {"OPENHANDS_API_KEY": "test-key"}):
            with patch.object(MODULE, "parse_args", return_value=args):
                with patch.object(MODULE, "resolve_conversation_ids", return_value=[]):
                    with patch.object(MODULE, "resolve_skip_ids", return_value=set()):
                        with patch.object(
                            MODULE,
                            "resolve_skip_repositories",
                            return_value={"example-org/skip-this-repo"},
                        ):
                            with patch.object(
                                MODULE,
                                "collect_conversation_groups",
                                side_effect=collect_groups,
                            ):
                                with patch.object(MODULE, "write_step_summary") as summary:
                                    MODULE.main()
        summary.assert_called_once_with([])

    def test_only_newest_conversation_is_canonical_per_repository(self):
        groups = MODULE.group_conversations_by_repository([
            {"id":"old","selected_repository":"akukuusisto/tradefoundry","created_at":"2026-10-01T10:00:00Z","sandbox_status":"RUNNING"},
            {"id":"new","selected_repository":"akukuusisto/tradefoundry","created_at":"2026-10-03T10:00:00Z","sandbox_status":"RUNNING"},
            {"id":"other","selected_repository":"akukuusisto/accounter","created_at":"2026-10-02T10:00:00Z","sandbox_status":"PAUSED"},
        ])
        latest = MODULE.select_latest_per_repository(groups)
        self.assertEqual(latest["akukuusisto/tradefoundry"]["id"], "new")
        self.assertEqual(latest["akukuusisto/accounter"]["id"], "other")

    def test_newest_error_prefers_older_usable_conversation(self):
        groups = MODULE.group_conversations_by_repository([
            {"id": "older-running", "selected_repository": "org/example",
             "created_at": "2026-10-01T10:00:00Z", "sandbox_status": "RUNNING"},
            {"id": "newer-error", "selected_repository": "org/example",
             "created_at": "2026-10-03T10:00:00Z", "sandbox_status": "ERROR"},
        ])
        selected = MODULE.select_latest_per_repository(groups)
        self.assertEqual(selected["org/example"]["id"], "older-running")

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
    def _args(self, **overrides):
        from types import SimpleNamespace
        values = dict(
            dry_run=False,
            nudge="continue",
            nudge_mode="loop",
            no_done_check=True,
            resume_cooldown=900,
            resume_wait_seconds=90,
            resume_poll_interval=5,
            idle_timeout=900,
            min_nudge_interval=1800,
            max_stalled_nudges=4,
            run_budget=420,
            fail_on_attention=False,
            verbose=False,
        )
        values.update(overrides)
        return SimpleNamespace(**values)

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

def keepalive_args(**overrides):
    from types import SimpleNamespace
    values = dict(
        dry_run=False,
        nudge="continue",
        nudge_mode="loop",
        no_done_check=False,
        resume_cooldown=900,
        resume_wait_seconds=90,
        resume_poll_interval=5,
        idle_timeout=900,
        min_nudge_interval=1800,
        max_stalled_nudges=4,
        run_budget=420,
        fail_on_attention=False,
        verbose=False,
        title_sync=True,
        discover_limit=50,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def user_event(text="nudge", timestamp="2026-10-07T10:00:00Z"):
    return {
        "source": "user",
        "llm_message": {"content": [{"type": "text", "text": text}]},
        "timestamp": timestamp,
    }


def agent_event(text="tyo jatkuu", timestamp="2026-10-07T10:00:00Z"):
    return {
        "source": "agent",
        "llm_message": {"content": [{"type": "text", "text": text}]},
        "timestamp": timestamp,
    }


class LoopContinuityTests(unittest.TestCase):
    """Loop-moodissa ei ole lopetustokenia: conversation ei kuole hiljaa."""

    def test_loop_mode_never_stops_on_loop_stop_message(self):
        self.assertFalse(MODULE.is_stop_message([agent_event("LOOP-STOP")], "loop"))

    def test_loop_mode_does_not_stop_on_done_either(self):
        self.assertFalse(MODULE.is_stop_message([agent_event("DONE")], "loop"))

    def test_task_mode_still_stops_on_done(self):
        self.assertTrue(MODULE.is_stop_message([agent_event("DONE")], "task"))

    def test_task_mode_stops_only_on_exact_done(self):
        self.assertFalse(MODULE.is_stop_message([agent_event("DONE!")], "task"))

    def test_loop_nudge_prefers_a_new_feature_before_research(self):
        text = MODULE.DEFAULT_NUDGE_LOOP
        lowered = text.lower()
        self.assertNotIn("LOOP-STOP", text)
        self.assertIn("PR", text)
        self.assertIn("mergaa", text)
        self.assertIn("uusi feature", lowered)
        self.assertIn("tutki se", lowered)
        # uusi feature -ohje on ennen tutkimusohjetta
        self.assertLess(lowered.index("uusi feature"), lowered.index("tutki se"))

    def test_legacy_stop_token_is_logged_but_not_treated_as_stop(self):
        events = [agent_event("LOOP-STOP")]
        self.assertTrue(MODULE.latest_agent_is_stop_token(events))
        self.assertFalse(MODULE.is_stop_message(events, "loop"))

    def test_latest_agent_is_stop_token_ignores_user_messages(self):
        events = [
            agent_event("tyo jatkuu", timestamp="2026-10-07T10:00:00Z"),
            user_event(timestamp="2026-10-07T10:01:00Z"),
        ]
        self.assertFalse(MODULE.latest_agent_is_stop_token(events))

    def test_trailing_user_messages_counts_unanswered_nudges(self):
        events = [
            agent_event("vastaus", timestamp="2026-10-07T10:00:00Z"),
            user_event(timestamp="2026-10-07T10:01:00Z"),
            user_event(timestamp="2026-10-07T10:02:00Z"),
        ]
        self.assertEqual(MODULE.count_trailing_user_messages(events), 2)

    def test_trailing_user_messages_is_zero_after_agent_reply(self):
        self.assertEqual(MODULE.count_trailing_user_messages([agent_event()]), 0)


class CanonicalSelectionTests(unittest.TestCase):
    """Kanoninen valinta konvergoi: kuollut sandbox ei jumita repositoriota."""

    def _groups(self, items):
        return MODULE.group_conversations_by_repository(items)

    def test_missing_newest_is_not_canonical(self):
        groups = self._groups([
            {"id": "dead", "selected_repository": "org/one", "created_at": "2026-10-03T10:00:00Z", "sandbox_status": "MISSING"},
            {"id": "alive", "selected_repository": "org/one", "created_at": "2026-10-01T10:00:00Z", "sandbox_status": "RUNNING"},
        ])
        selected = MODULE.select_latest_per_repository(groups)
        self.assertEqual(selected["org/one"]["id"], "alive")

    def test_newest_is_canonical_when_every_sandbox_is_missing(self):
        groups = self._groups([
            {"id": "dead-new", "selected_repository": "org/two", "created_at": "2026-10-03T10:00:00Z", "sandbox_status": "MISSING"},
            {"id": "dead-old", "selected_repository": "org/two", "created_at": "2026-10-01T10:00:00Z", "sandbox_status": "MISSING"},
        ])
        selected = MODULE.select_latest_per_repository(groups)
        self.assertEqual(selected["org/two"]["id"], "dead-new")

    def test_order_candidates_puts_canonical_first(self):
        canonical = {"id": "alive", "sandbox_status": "RUNNING"}
        candidates = [
            {"id": "dead", "sandbox_status": "MISSING"},
            {"id": "alive", "sandbox_status": "RUNNING"},
            {"id": "older", "sandbox_status": "PAUSED"},
        ]
        ordered = MODULE.order_candidates(canonical, candidates)
        self.assertEqual([c["id"] for c in ordered], ["alive", "dead", "older"])


class NudgeHygieneTests(unittest.TestCase):
    """Nudgeja ei laheteta liian tiheaan eika jumiutuneelle conversationille."""

    def _run(
        self,
        conversation,
        args=None,
        events=None,
        resume=False,
        activity_offset=3600,
        last_resume_offset=None,
    ):
        original_get = MODULE.get_conversation
        original_activity = MODULE.latest_activity_ts
        original_nudge = MODULE.send_nudge
        original_events = MODULE.fetch_recent_events
        original_resume = MODULE.try_resume
        original_wait = MODULE.wait_for_resumed_sandbox
        try:
            MODULE.get_conversation = lambda *a: conversation
            if activity_offset is None:
                # updated_at ja event-timestampit puuttuvat kokonaan
                MODULE.latest_activity_ts = lambda *a: 0.0
            else:
                MODULE.latest_activity_ts = (
                    lambda *a: MODULE.time.time() - activity_offset
                )
            calls = []
            MODULE.send_nudge = lambda *a: calls.append(a) or True
            MODULE.fetch_recent_events = lambda *a, **k: events
            MODULE.try_resume = lambda *a: resume
            # Oikea odotus kest\u00e4\u00e4 resume_wait_seconds; sen semantiikka on
            # katettu ResumeRecoveryTests-luokassa. T\u00e4ss\u00e4 riitt\u00e4\u00e4 ett\u00e4
            # resume ei vaadi nudgea samalla kierroksella.
            MODULE.wait_for_resumed_sandbox = lambda *a, **k: True
            state = {"nudges": {}, "last_resume": {}, "new_conversations": set()}
            if last_resume_offset is not None:
                state["last_resume"]["conversation-1"] = (
                    MODULE.time.time() - last_resume_offset
                )
            outcome = MODULE.check_conversation(
                "https://app.all-hands.dev",
                {},
                "conversation-1",
                args or keepalive_args(),
                state,
            )
            return outcome, calls
        finally:
            MODULE.get_conversation = original_get
            MODULE.latest_activity_ts = original_activity
            MODULE.send_nudge = original_nudge
            MODULE.fetch_recent_events = original_events
            MODULE.try_resume = original_resume
            MODULE.wait_for_resumed_sandbox = original_wait

    def _conversation(self, **overrides):
        values = {
            "sandbox_status": "RUNNING",
            "execution_status": "finished",
            "updated_at": "2026-10-01T10:00:00Z",
            "title": "agent",
            "sandbox_id": "sandbox-1",
        }
        values.update(overrides)
        return values

    def test_nudge_threshold_respects_min_nudge_interval(self):
        outcome, calls = self._run(
            self._conversation(),
            args=keepalive_args(idle_timeout=900, min_nudge_interval=1800),
            events=[],
            activity_offset=1000,
        )
        self.assertEqual(outcome, "idle-wait")
        self.assertEqual(calls, [])

    def test_idle_above_min_interval_still_nudges(self):
        outcome, calls = self._run(self._conversation(), events=[agent_event()])
        self.assertEqual(outcome, "nudged")
        self.assertEqual(len(calls), 1)

    def test_unanswered_nudges_escalate_to_stalled(self):
        events = [
            user_event(timestamp=f"2026-10-07T10:0{minute}:00Z")
            for minute in (1, 2, 3, 4)
        ]
        outcome, calls = self._run(self._conversation(), events=events)
        self.assertEqual(outcome, "stalled")
        self.assertEqual(calls, [])

    def test_few_unanswered_nudges_still_nudge(self):
        events = [
            user_event(timestamp=f"2026-10-07T10:0{minute}:00Z")
            for minute in (1, 2)
        ]
        outcome, calls = self._run(self._conversation(), events=events)
        self.assertEqual(outcome, "nudged")
        self.assertEqual(len(calls), 1)

    def test_paused_resume_nudges_right_away_when_already_past_the_threshold(self):
        # Resume nollaa activity-aikaleiman, mutta conversation oli jo yli
        # idle-rajan: nudge lähtee samalla kierroksella.
        outcome, calls = self._run(
            self._conversation(sandbox_status="PAUSED", execution_status=None),
            events=[agent_event()],
            resume=True,
        )
        self.assertEqual(outcome, "resumed->nudged")
        self.assertEqual(len(calls), 1)

    def test_paused_resume_below_the_threshold_waits_for_the_next_pass(self):
        outcome, calls = self._run(
            self._conversation(sandbox_status="PAUSED", execution_status=None),
            events=[agent_event()],
            resume=True,
            activity_offset=100,
        )
        self.assertEqual(outcome, "resumed")
        self.assertEqual(calls, [])

    def test_paused_resume_that_never_answers_is_still_replaced(self):
        events = [
            user_event(timestamp=f"2026-10-07T10:0{minute}:00Z")
            for minute in (1, 2, 3, 4)
        ]
        outcome, calls = self._run(
            self._conversation(sandbox_status="PAUSED", execution_status=None),
            events=events,
            resume=True,
        )
        self.assertEqual(outcome, "resumed->stalled")
        self.assertEqual(calls, [])

    def test_paused_resume_failure_is_reported_without_nudging(self):
        outcome, calls = self._run(
            self._conversation(sandbox_status="PAUSED", execution_status=None),
            events=[agent_event()],
            resume=False,
        )
        self.assertEqual(outcome, "resume-failed")
        self.assertEqual(calls, [])

    def test_old_starting_sandbox_is_a_recovery_outcome(self):
        conversation = self._conversation(
            sandbox_status="STARTING",
            execution_status=None,
            created_at="2026-10-01T10:00:00Z",
            updated_at="2026-10-01T10:00:00Z",
        )
        outcome, calls = self._run(conversation, events=[])
        self.assertEqual(outcome, "starting-stuck")
        self.assertTrue(MODULE.outcome_matches(outcome, MODULE.RECOVERY_OUTCOMES))
        self.assertEqual(calls, [])

    def test_old_conversation_with_recent_starting_transition_is_not_replaced(self):
        conversation = self._conversation(
            sandbox_status="STARTING",
            execution_status=None,
            created_at="2026-10-01T10:00:00Z",
            updated_at=MODULE.datetime.datetime.now(MODULE.datetime.timezone.utc).isoformat(),
        )
        outcome, calls = self._run(conversation, events=[])
        self.assertEqual(outcome, "starting")
        self.assertEqual(calls, [])

    def test_paused_with_unmeasurable_idle_is_still_nudged(self):
        # Resume on juuri tehty (cooldown voimassa), joten PAUSED-tilaa ei
        # yriteta resumeta uudelleen vaan se kasitellaan idle-timeoutina.
        outcome, calls = self._run(
            self._conversation(
                sandbox_status="PAUSED",
                execution_status=None,
                updated_at="",
            ),
            events=[agent_event()],
            resume=False,
            activity_offset=None,
            last_resume_offset=0,
        )
        self.assertEqual(outcome, "nudged")
        self.assertEqual(len(calls), 1)

    def test_paused_dry_run_still_reports_what_would_happen(self):
        # Resume-cooldown voimassa -> dry-run nayttaa minka nudgen lahettaisi.
        outcome, calls = self._run(
            self._conversation(sandbox_status="PAUSED", execution_status=None),
            args=keepalive_args(dry_run=True),
            events=[agent_event()],
            resume=True,
            last_resume_offset=0,
        )
        self.assertEqual(outcome, "dry-nudge")
        self.assertEqual(len(calls), 1)


class _FailingPageRequests:
    """Palauttaa ensimm\u00e4isen sivun ja kaatuu sen j\u00e4lkeen."""

    def __init__(self, first_page, fail_on_call=2):
        self.first_page = first_page
        self.fail_on_call = fail_on_call
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        if self.calls >= self.fail_on_call:
            raise RuntimeError("page fetch failed")
        return FakeResponse(self.first_page)


class DiscoveryRobustnessTests(unittest.TestCase):
    def tearDown(self):
        MODULE.set_budget_deadline(0.0)

    def test_partial_results_survive_a_failing_page(self):
        original = MODULE.requests
        try:
            MODULE.requests = _FailingPageRequests({
                "items": [{"id": "keep-1", "selected_repository": "org/one"}],
                "next_page_id": "page-2",
            })
            items = MODULE.discover_conversations(
                "https://app.all-hands.dev", {}, limit=1, max_pages=5
            )
        finally:
            MODULE.requests = original
        self.assertEqual([item["id"] for item in items], ["keep-1"])

    def test_discovery_stops_when_budget_is_exhausted(self):
        original = MODULE.requests
        try:
            MODULE.requests = _FailingPageRequests({}, fail_on_call=1)
            MODULE.set_budget_deadline(MODULE.time.monotonic() - 1)
            items = MODULE.discover_conversations(
                "https://app.all-hands.dev", {}, limit=1, max_pages=5
            )
        finally:
            MODULE.requests = original
        self.assertEqual(items, [])


class BudgetTests(unittest.TestCase):
    def tearDown(self):
        MODULE.set_budget_deadline(0.0)

    def test_expired_budget_is_detected(self):
        MODULE.set_budget_deadline(MODULE.time.monotonic() - 1)
        self.assertTrue(MODULE.budget_exhausted())
        self.assertEqual(MODULE.budget_remaining(), 0.0)

    def test_without_budget_nothing_expires(self):
        MODULE.set_budget_deadline(0.0)
        self.assertFalse(MODULE.budget_exhausted())


class StepSummaryRedactionTests(unittest.TestCase):
    """Julkinen repo: summary ei sisalla repositorion nimea eika koko UUID:ta."""

    def test_summary_hides_repository_names_and_full_ids(self):
        import os
        import tempfile

        MODULE._REPO_LABELS.clear()
        handle, path = tempfile.mkstemp(suffix=".md")
        os.close(handle)
        original = os.environ.get("GITHUB_STEP_SUMMARY")
        os.environ["GITHUB_STEP_SUMMARY"] = path
        try:
            MODULE.write_step_summary([
                ("org/secret-project", "12345678-aaaa-bbbb-cccc-dddddddddddd", "nudged"),
                ("org/other", "abcdef01-aaaa-bbbb-cccc-dddddddddddd", "confirmation"),
            ])
        finally:
            if original is None:
                os.environ.pop("GITHUB_STEP_SUMMARY", None)
            else:
                os.environ["GITHUB_STEP_SUMMARY"] = original
        with open(path, encoding="utf-8") as fh:
            summary = fh.read()
        os.unlink(path)

        self.assertNotIn("secret-project", summary)
        self.assertNotIn("12345678-aaaa", summary)
        self.assertIn("repo#1", summary)
        self.assertIn("12345678", summary)
        self.assertIn("Needs human (1)", summary)
        self.assertIn("At risk (0)", summary)

    def test_successfully_recovered_outcome_is_not_currently_at_risk(self):
        import os
        import tempfile

        MODULE._REPO_LABELS.clear()
        handle, path = tempfile.mkstemp(suffix=".md")
        os.close(handle)
        original = os.environ.get("GITHUB_STEP_SUMMARY")
        os.environ["GITHUB_STEP_SUMMARY"] = path
        try:
            MODULE.write_step_summary(
                [("org/one", "deadbeef", "stalled->sandbox-replaced")]
            )
        finally:
            if original is None:
                os.environ.pop("GITHUB_STEP_SUMMARY", None)
            else:
                os.environ["GITHUB_STEP_SUMMARY"] = original
        with open(path, encoding="utf-8") as fh:
            summary = fh.read()
        os.unlink(path)
        self.assertIn("At risk (0)", summary)
        self.assertIn("Recovered (1)", summary)
        self.assertIn("stalled->sandbox-replaced", summary)

    def test_stalled_conversation_shows_up_at_risk(self):
        import os
        import tempfile

        MODULE._REPO_LABELS.clear()
        handle, path = tempfile.mkstemp(suffix=".md")
        os.close(handle)
        original = os.environ.get("GITHUB_STEP_SUMMARY")
        os.environ["GITHUB_STEP_SUMMARY"] = path
        try:
            MODULE.write_step_summary([("org/one", "deadbeef", "stalled")])
        finally:
            if original is None:
                os.environ.pop("GITHUB_STEP_SUMMARY", None)
            else:
                os.environ["GITHUB_STEP_SUMMARY"] = original
        with open(path, encoding="utf-8") as fh:
            summary = fh.read()
        os.unlink(path)
        self.assertIn("At risk (1)", summary)


class OutcomeMatchingTests(unittest.TestCase):
    def test_nudge_failure_is_recoverable_and_final_state_is_used(self):
        self.assertTrue(MODULE.outcome_matches("nudge-failed", MODULE.RECOVERY_OUTCOMES))
        self.assertEqual(MODULE.final_outcome("nudge-failed->running"), "running")
        self.assertEqual(MODULE.final_outcome("nudge-failed->replacement-failed"),
                         "replacement-failed")

    def test_safe_error_label_never_exposes_exception_text(self):
        error = RuntimeError("private repository name, URL, and response body")
        self.assertEqual(MODULE.safe_error_label(error), "RuntimeError")
        response = type("Response", (), {"status_code": 503, "text": "private payload"})()
        http_error = type("HTTPError", (RuntimeError,), {"response": response})(
            "private URL and payload"
        )
        self.assertEqual(MODULE.safe_error_label(http_error), "HTTP 503")

    def test_combined_outcome_matches_each_part(self):
        self.assertTrue(
            MODULE.outcome_matches("stalled->sandbox-replaced", MODULE.AT_RISK_OUTCOMES)
        )
        self.assertTrue(
            MODULE.outcome_matches("stalled", MODULE.RECOVERY_OUTCOMES)
        )
        self.assertFalse(MODULE.outcome_matches("nudged", MODULE.AT_RISK_OUTCOMES))

    def test_running_fallback_is_not_terminal(self):
        self.assertFalse(
            MODULE.outcome_matches("sandbox-missing->running", MODULE.TERMINAL_OUTCOMES)
        )

    def test_replaced_fallback_is_terminal(self):
        self.assertTrue(
            MODULE.outcome_matches(
                "stalled->sandbox-replaced", MODULE.TERMINAL_OUTCOMES
            )
        )


class _StubResponse:
    def __init__(self, status_code, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _NudgeRequests:
    """app send-message -> app_status, runtime /events ja /run -> 204."""

    def __init__(self, app_status, app_headers=None):
        self.app_status = app_status
        self.app_headers = app_headers or {}
        self.urls = []

    def post(self, url, **kwargs):
        self.urls.append(url)
        if url.endswith("/send-message"):
            return _StubResponse(self.app_status, "Sandbox is STARTING", self.app_headers)
        return _StubResponse(204, "ok")

    def get(self, *args, **kwargs):
        return _StubResponse(200, "{}")


class SendNudgeFallbackTests(unittest.TestCase):
    conversation = {
        "conversation_url": "https://runtime.example/api",
        "session_api_key": "session-key",
    }

    def _send(self, app_status, app_headers=None):
        original = MODULE.requests
        try:
            fake = _NudgeRequests(app_status, app_headers)
            MODULE.requests = fake
            ok = MODULE.send_nudge(
                "https://app.all-hands.dev",
                {},
                "conversation-1",
                "text",
                False,
                self.conversation,
            )
            return ok, fake.urls
        finally:
            MODULE.requests = original

    def test_conflict_falls_back_to_runtime_events(self):
        ok, urls = self._send(409)
        self.assertTrue(ok)
        self.assertTrue(any(url.endswith("/events") for url in urls))

    def test_success_does_not_call_the_runtime_api(self):
        original = MODULE.requests
        try:
            fake = _NudgeRequests(200)
            MODULE.requests = fake
            ok = MODULE.send_nudge(
                "https://app.all-hands.dev",
                {},
                "conversation-1",
                "text",
                False,
                self.conversation,
            )
        finally:
            MODULE.requests = original
        self.assertTrue(ok)
        self.assertFalse(any(url.endswith("/events") for url in fake.urls))

    def test_retry_after_header_is_respected(self):
        self.assertEqual(MODULE.parse_retry_after({"Retry-After": "5"}), 5.0)
        self.assertEqual(MODULE.parse_retry_after({"Retry-After": "999"}), 30.0)
        self.assertEqual(MODULE.parse_retry_after(None), 2.0)

    def test_transient_failure_is_retried_once(self):
        original = MODULE.requests
        original_sleep = MODULE.time.sleep
        try:
            fake = _NudgeRequests(503)
            MODULE.requests = fake
            MODULE.time.sleep = lambda *a: None
            ok = MODULE.send_nudge(
                "https://app.all-hands.dev",
                {},
                "conversation-1",
                "text",
                False,
                self.conversation,
            )
        finally:
            MODULE.requests = original
            MODULE.time.sleep = original_sleep
        apps = [url for url in fake.urls if url.endswith("/send-message")]
        self.assertEqual(len(apps), 2)
        self.assertTrue(ok)


class ArchivedConversationTests(unittest.TestCase):
    """Arkistoitu conversation (HTTP 410/404) on menetetty, ei tilap\u00e4inen virhe.

    OpenHands vastaa arkistoituun conversationiin
    {"detail": "Conversation is archived. The sandbox no longer exists."}
    eik\u00e4 nudge voi koskaan menn\u00e4 l\u00e4pi. Repositorio pit\u00e4\u00e4 saada uusi
    conversation sen sijaan ett\u00e4 se j\u00e4isi ikuisesti kuolleeksi.
    """

    conversation = {
        "conversation_url": "",
        "session_api_key": "",
        "sandbox_status": "ERROR",
        "execution_status": None,
        "updated_at": "2026-10-01T10:00:00Z",
        "title": "archived agent",
        "sandbox_id": "sandbox-1",
    }

    def _send_with(self, status_code):
        original = MODULE.requests
        fake = _NudgeRequests(status_code)
        MODULE.requests = fake
        try:
            with self.assertRaises(MODULE.ConversationNotFound):
                MODULE.send_nudge(
                    "https://app.all-hands.dev",
                    {},
                    "conversation-1",
                    "text",
                    False,
                    self.conversation,
                )
        finally:
            MODULE.requests = original
        return fake

    def test_archived_conversation_is_reported_as_gone(self):
        fake = self._send_with(410)
        # Arkistoituun conversationiin ei yritet\u00e4 runtime-fallbackia.
        self.assertFalse(any(url.endswith("/events") for url in fake.urls))

    def test_deleted_conversation_is_reported_as_gone(self):
        self._send_with(404)

    def test_a_starting_sandbox_is_still_a_transient_failure(self):
        # 409 ei ole menetetty conversation: nudgea yritet\u00e4\u00e4n runtime-API:n kautta.
        original = MODULE.requests
        fake = _NudgeRequests(409)
        MODULE.requests = fake
        try:
            conversation = dict(self.conversation)
            conversation["conversation_url"] = "https://runtime.example/api"
            conversation["session_api_key"] = "session-key"
            ok = MODULE.send_nudge(
                "https://app.all-hands.dev",
                {},
                "conversation-1",
                "text",
                False,
                conversation,
            )
        finally:
            MODULE.requests = original
        self.assertTrue(ok)

    def test_check_conversation_turns_a_gone_conversation_into_recovery(self):
        original_get = MODULE.get_conversation
        original_activity = MODULE.latest_activity_ts
        original_events = MODULE.fetch_recent_events
        original_nudge = MODULE.send_nudge
        try:
            MODULE.get_conversation = lambda *a, **k: dict(self.conversation)
            MODULE.latest_activity_ts = lambda *a: MODULE.time.time() - 3600
            MODULE.fetch_recent_events = lambda *a, **k: []

            def raise_gone(*a, **k):
                raise MODULE.ConversationNotFound("conversation-1")

            MODULE.send_nudge = raise_gone
            outcome = MODULE.check_conversation(
                "https://app.all-hands.dev",
                {},
                "conversation-1",
                keepalive_args(),
                {"nudges": {}, "last_resume": {}, "new_conversations": set()},
            )
        finally:
            MODULE.get_conversation = original_get
            MODULE.latest_activity_ts = original_activity
            MODULE.fetch_recent_events = original_events
            MODULE.send_nudge = original_nudge
        # "not-found" kuuluu palautettaviin outcomeihin, joten main() korvaa
        # conversationin uudella sen sijaan ett\u00e4 repo j\u00e4isi kuolleeksi.
        self.assertEqual(outcome, "not-found")
        self.assertTrue(MODULE.outcome_matches(outcome, MODULE.RECOVERY_OUTCOMES))


class _TitleRequests:
    """Tallentaa PATCH-payloadit otsikon synkronointia varten."""

    def __init__(self, status_code=200, error=None):
        self.status_code = status_code
        self.error = error
        self.payloads = []

    def patch(self, url, **kwargs):
        if self.error:
            raise self.error
        self.payloads.append(kwargs.get("json"))
        return _StubResponse(self.status_code, "ok")


class TitleSyncTests(unittest.TestCase):
    """Jokainen hallittu conversation nimetään repositorion nimellä.

    Omistaja ("akukuusisto/") jätetään pois, koska se on UI:ssa pelkkää
    kohinaa: otsikko alkaa aina pelkällä repositorion nimellä.
    """

    def test_desired_title_prefixes_the_repository_without_the_owner(self):
        self.assertEqual(
            MODULE.desired_conversation_title(
                "BuildHorizon", "org/build-horizon-pilot"
            ),
            "build-horizon-pilot: BuildHorizon",
        )

    def test_desired_title_never_contains_the_owner(self):
        title = MODULE.desired_conversation_title("agent", "akukuusisto/toolbox")
        self.assertEqual(title, "toolbox: agent")
        self.assertNotIn("/", title)

    def test_legacy_title_with_owner_is_migrated(self):
        # Vanha "owner/repo: ..." -otsikko siivotaan kertaalleen puhtaaksi.
        self.assertEqual(
            MODULE.desired_conversation_title(
                "org/toolbox: jatka looppia", "org/toolbox"
            ),
            "toolbox: jatka looppia",
        )

    def test_desired_title_is_none_when_already_prefixed(self):
        self.assertIsNone(
            MODULE.desired_conversation_title(
                "toolbox: jatka looppia", "org/toolbox"
            )
        )

    def test_desired_title_strips_emoji_and_repeated_repo_name(self):
        self.assertEqual(
            MODULE.desired_conversation_title(
                "\U0001f527 Toolbox: Continue the loop", "org/toolbox"
            ),
            "toolbox: Continue the loop",
        )

    def test_desired_title_uses_default_suffix_for_empty_title(self):
        self.assertEqual(
            MODULE.desired_conversation_title("", "org/toolbox"),
            "toolbox: keepalive",
        )

    def test_desired_title_is_none_without_repository(self):
        self.assertIsNone(MODULE.desired_conversation_title("anything", ""))

    def _sync(self, current_title, args=None, fake=None):
        original = MODULE.requests
        try:
            fake = fake or _TitleRequests()
            MODULE.requests = fake
            changed = MODULE.sync_conversation_title(
                "https://app.all-hands.dev",
                {},
                "conversation-1",
                "org/toolbox",
                current_title,
                args or keepalive_args(),
            )
            return changed, fake.payloads
        finally:
            MODULE.requests = original

    def test_sync_skips_the_api_when_the_title_is_already_fine(self):
        changed, payloads = self._sync("toolbox: keepalive")
        self.assertFalse(changed)
        self.assertEqual(payloads, [])

    def test_sync_patches_only_the_title_field(self):
        changed, payloads = self._sync("Toolbox: jatka looppia")
        self.assertTrue(changed)
        self.assertEqual(payloads, [{"title": "toolbox: jatka looppia"}])

    def test_sync_writes_nothing_in_dry_run(self):
        changed, payloads = self._sync(
            "Toolbox: jatka looppia", args=keepalive_args(dry_run=True)
        )
        self.assertFalse(changed)
        self.assertEqual(payloads, [])

    def test_sync_can_be_disabled(self):
        changed, payloads = self._sync(
            "Toolbox: jatka loppia", args=keepalive_args(title_sync=False)
        )
        self.assertFalse(changed)
        self.assertEqual(payloads, [])

    def test_sync_reports_failure_without_raising(self):
        changed, payloads = self._sync(
            "Toolbox: jatka looppia", fake=_TitleRequests(status_code=403)
        )
        self.assertFalse(changed)
        self.assertEqual(payloads, [{"title": "toolbox: jatka looppia"}])

    def test_sync_survives_a_network_error(self):
        changed, _ = self._sync(
            "Toolbox: jatka looppia", fake=_TitleRequests(error=RuntimeError("down"))
        )
        self.assertFalse(changed)

    def test_check_conversation_syncs_the_title_it_manages(self):
        original_get = MODULE.get_conversation
        original_activity = MODULE.latest_activity_ts
        original_requests = MODULE.requests
        fake = _TitleRequests()
        try:
            MODULE.requests = fake
            MODULE.get_conversation = lambda *a: {
                "sandbox_status": "RUNNING",
                "execution_status": "running",
                "updated_at": "2026-10-07T10:00:00Z",
                "title": "Toolbox loop",
                "selected_repository": "org/toolbox",
                "sandbox_id": "sandbox-1",
            }
            MODULE.latest_activity_ts = lambda *a: MODULE.time.time()
            outcome = MODULE.check_conversation(
                "https://app.all-hands.dev",
                {},
                "conversation-1",
                keepalive_args(),
                {"nudges": {}, "last_resume": {}, "new_conversations": set()},
            )
        finally:
            MODULE.get_conversation = original_get
            MODULE.latest_activity_ts = original_activity
            MODULE.requests = original_requests
        self.assertEqual(outcome, "running")
        # repo-nimi siivotaan pois otsikon alusta, ettei se toistu, eikä
        # omistajaa koskaan kirjoiteta otsikkoon.
        self.assertEqual(fake.payloads, [{"title": "toolbox: loop"}])


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
            min_nudge_interval=1800,
            max_stalled_nudges=4,
            run_budget=420,
            fail_on_attention=False,
            verbose=False,
            title_sync=True,
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
        self.assertEqual(outcome, "resumed->nudged")
        self.assertEqual(len(calls), 1)

    def test_nudge_is_sent_only_after_the_sandbox_is_running(self):
        original_get = MODULE.get_conversation
        original_activity = MODULE.latest_activity_ts
        original_resume = MODULE.try_resume
        original_nudge = MODULE.send_nudge
        original_sleep = MODULE.time.sleep
        try:
            seen = []
            statuses = iter(["PAUSED", "STARTING", "RUNNING"])

            def fake_get(*a, **k):
                seen.append(next(statuses))
                return self._conversation(seen[-1])

            MODULE.get_conversation = fake_get
            MODULE.latest_activity_ts = lambda *a: MODULE.time.time() - 3600
            MODULE.try_resume = lambda *a: True
            nudged_when = []
            MODULE.send_nudge = lambda *a: nudged_when.append(list(seen)) or True
            MODULE.time.sleep = lambda *a: None
            outcome = MODULE.check_conversation(
                "https://app.all-hands.dev",
                {},
                "conversation-1",
                self._args(),
                {"nudges": {}, "last_resume": {}, "new_conversations": set()},
            )
        finally:
            MODULE.get_conversation = original_get
            MODULE.latest_activity_ts = original_activity
            MODULE.try_resume = original_resume
            MODULE.send_nudge = original_nudge
            MODULE.time.sleep = original_sleep
        self.assertEqual(outcome, "resumed->nudged")
        # Nudge lähti vasta kun sandbox oli RUNNING, ei vielä STARTING-tilassa.
        self.assertEqual(nudged_when, [["PAUSED", "STARTING", "RUNNING"]])

    def test_paused_sandbox_that_never_becomes_ready_reports_resuming(self):
        outcome, calls = self._run(
            self._args(resume_wait_seconds=0), ["PAUSED", "STARTING"]
        )
        self.assertEqual(outcome, "resuming")
        self.assertEqual(calls, [])

    def test_failed_resume_reports_failure_without_nudging(self):
        outcome, calls = self._run(self._args(), ["PAUSED"], resume=False)
        self.assertEqual(outcome, "resume-failed")
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
