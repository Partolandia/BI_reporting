"""
T36: the /workload page renders from cache without touching Jira, and the
existing Team Overview routes keep working against the same cache.
"""

import unittest
from unittest import mock

import app as flask_app
import jira_client
import staleness
import workload
from tests import fixtures as fx


def cached_row(issue, comments=(), histories=(), policy=None, identity=None):
    """
    Build a cache row the way staleness.build_report does: the real staleness
    row first, then the workload fields attached on top of it.
    """
    policy = policy or fx.policy()
    identity = identity or fx.identity(current_policy=policy)
    row = staleness._report_row_from_issue(issue, list(histories), list(comments))
    return staleness._attach_workload_fields(
        row,
        issue,
        list(comments),
        list(histories),
        policy,
        identity,
        overrides={},
        history_complete=True,
        now=fx.NOW,
    )


class WorkloadRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        flask_app.app.config["TESTING"] = True

    def setUp(self):
        self.client = flask_app.app.test_client()
        self.policy = fx.policy()
        self.identity = fx.identity(current_policy=self.policy)
        self.rows = [
            cached_row(
                fx.issue(key="CSSD-1001", status="In Progress", remaining_seconds=4 * 3600),
                policy=self.policy,
                identity=self.identity,
            ),
            cached_row(
                fx.issue(
                    key="CSSD-1002",
                    status="Client Review",
                    summary="Standish DB - RP - awaiting sign-off",
                ),
                policy=self.policy,
                identity=self.identity,
            ),
            cached_row(
                fx.issue(
                    key="CSSD-1003",
                    status="Peer Review",
                    assignee_id=fx.TEAMMATE_ID,
                    assignee_name=fx.TEAMMATE_NAME,
                ),
                policy=self.policy,
                identity=self.identity,
            ),
            cached_row(
                fx.issue(
                    key="IAP-1004",
                    issue_type="[System] Change",
                    summary="Change request record",
                ),
                policy=self.policy,
                identity=self.identity,
            ),
        ]
        self._previous = dict(flask_app._state)
        flask_app._state["report"] = self.rows
        flask_app._state["last_refreshed"] = fx.NOW
        flask_app._state["cache_version"] = flask_app.CACHE_VERSION
        flask_app._state["error"] = None
        flask_app._state["warning"] = None

        # Any Jira access from a request handler is a bug, not a slow page.
        self._patches = [
            mock.patch.object(
                jira_client,
                "_request",
                side_effect=AssertionError("the request path must not call Jira"),
            ),
            mock.patch.object(
                flask_app._policy_store, "policy", self.policy, create=False
            ),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self._patches):
            patch.stop()
        flask_app._state.update(self._previous)

    def test_default_view_renders_active_work_only(self):
        response = self.client.get("/workload")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("CSSD-1001", body)
        self.assertNotIn("CSSD-1002", body)
        self.assertNotIn("IAP-1004", body)

    def test_waiting_on_client_drill_down_renders_the_reason(self):
        body = self.client.get("/workload?view=waiting_client").get_data(as_text=True)
        self.assertIn("CSSD-1002", body)
        self.assertNotIn("CSSD-1001", body)
        self.assertIn("Waiting on client", body)

    def test_needs_review_drill_down_renders(self):
        body = self.client.get("/workload?view=review").get_data(as_text=True)
        self.assertIn("CSSD-1003", body)

    def test_excluded_view_shows_the_excluded_iap_change(self):
        body = self.client.get("/workload?view=excluded").get_data(as_text=True)
        self.assertIn("IAP-1004", body)

    def test_all_owned_view_excludes_the_excluded_ticket(self):
        body = self.client.get("/workload?view=all").get_data(as_text=True)
        self.assertIn("CSSD-1001", body)
        self.assertIn("CSSD-1002", body)
        self.assertNotIn("IAP-1004", body)

    def test_an_unknown_view_falls_back_to_active(self):
        response = self.client.get("/workload?view=nonsense")
        self.assertEqual(response.status_code, 200)
        self.assertIn("CSSD-1001", response.get_data(as_text=True))

    def test_a_display_filter_announces_itself_as_a_slice(self):
        body = self.client.get("/workload?view=all&project=CSSD").get_data(as_text=True)
        self.assertNotIn("IAP-1004", body)
        self.assertIn("filtered", body.lower())

    def test_an_empty_cache_still_renders(self):
        flask_app._state["report"] = []
        response = self.client.get("/workload")
        self.assertEqual(response.status_code, 200)

    def test_a_legacy_cache_row_renders_as_needs_review(self):
        flask_app._state["report"] = [
            {
                "key": "CSSD-9999",
                "summary": "row written by an older cache version",
                "status": "In Progress",
                "assignee": fx.ASSIGNEE_NAME,
                "issue_type": "Service Request",
                "days_inactive": 10.0,
                "level": "yellow",
                "category": "yellow",
            }
        ]
        flask_app._state["cache_version"] = 1
        flask_app._reclassify_workload(now=fx.NOW)
        body = self.client.get("/workload?view=review").get_data(as_text=True)
        self.assertIn("CSSD-9999", body)


class TeamOverviewPreservationTests(unittest.TestCase):
    """T35: the staleness views must be unaffected by the workload work."""

    def setUp(self):
        flask_app.app.config["TESTING"] = True
        self.client = flask_app.app.test_client()
        self._previous = dict(flask_app._state)
        self.rows = [
            cached_row(fx.issue(key="CSSD-1001")),
            cached_row(fx.issue(key="IAP-1004", issue_type="[System] Change")),
        ]
        flask_app._state["report"] = self.rows
        flask_app._state["last_refreshed"] = fx.NOW
        flask_app._state["error"] = None
        flask_app._state["warning"] = None

    def tearDown(self):
        flask_app._state.update(self._previous)

    def test_dashboard_still_shows_every_ticket_including_excluded_ones(self):
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn("CSSD-1001", body)
        self.assertIn(
            "IAP-1004",
            body,
            "workload exclusion must not remove tickets from Team Overview",
        )

    def test_staleness_fields_match_the_unclassified_result_exactly(self):
        for issue in (fx.issue(key="CSSD-1001"), fx.issue(key="IAP-1004")):
            baseline = staleness._report_row_from_issue(issue, [], [])
            classified = cached_row(issue)
            for field, value in baseline.items():
                self.assertEqual(
                    classified[field],
                    value,
                    f"{field} changed for {issue['key']} once workload ran",
                )

    def test_person_and_project_views_still_render(self):
        with mock.patch.object(jira_client, "TEAM_MEMBERS", fx.TEAM_MEMBERS):
            response = self.client.get(f"/person/{fx.ASSIGNEE_NAME}")
        self.assertEqual(response.status_code, 200)
        with mock.patch.object(jira_client, "PROJECT_KEYS", ["CSSD", "IAP"]):
            response = self.client.get("/project/CSSD")
        self.assertEqual(response.status_code, 200)

    def test_reclassifying_from_cache_does_not_mutate_staleness_categories(self):
        staleness_fields = ("category", "level", "days_inactive", "urgent",
                            "responsible_party", "touched_since_assignment")
        before = [tuple(r[f] for f in staleness_fields) for r in self.rows]
        workload.reclassify_report(
            self.rows, fx.policy(), fx.identity(), now=fx.NOW
        )
        after = [tuple(r[f] for f in staleness_fields) for r in self.rows]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
