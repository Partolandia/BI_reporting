"""
Policy and override validation, reload safety (T33, T34) and the shipped
workload_policy.json.
"""

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

import workload
from tests import fixtures as fx

REPO_ROOT = Path(__file__).resolve().parent.parent


class ShippedPolicyTests(unittest.TestCase):
    def test_repository_policy_file_is_valid(self):
        policy = workload.load_policy(REPO_ROOT / "workload_policy.json")
        self.assertEqual(policy["policy_version"], "actionable-workload-v1")
        self.assertEqual(
            policy["status_defaults_by_name"]["Client Review"],
            workload.WAITING_CLIENT_ACCEPTANCE,
        )

    def test_waiting_for_approval_defaults_to_review_not_a_client_wait(self):
        policy = workload.load_policy(REPO_ROOT / "workload_policy.json")
        self.assertEqual(
            policy["status_defaults_by_name"]["Waiting for Approval"],
            workload.REVIEW_REQUIRED,
            "an internal approval must not be hidden as client work by default",
        )

    def test_shipped_policy_ships_no_invented_capacities_or_identities(self):
        policy = workload.load_policy(REPO_ROOT / "workload_policy.json")
        self.assertEqual(policy["member_settings_by_account_id"], {})
        self.assertEqual(policy["client_approver_ids_by_client"], {})
        self.assertEqual(policy["internal_account_ids"], [])

    def test_missing_policy_file_falls_back_to_defaults(self):
        policy = workload.load_policy(REPO_ROOT / "does-not-exist.json")
        self.assertEqual(policy["policy_version"], "actionable-workload-v1")


class ValidationTests(unittest.TestCase):
    def _invalid(self, **overrides):
        merged = workload._deep_merge(workload.DEFAULT_POLICY, overrides)
        with self.assertRaises(workload.PolicyError):
            workload.validate_policy(merged)

    def test_unknown_state_is_rejected(self):
        self._invalid(status_defaults_by_name={"Open": "SOMETHING_ELSE"})

    def test_excluded_cannot_be_a_status_default(self):
        self._invalid(status_defaults_by_name={"Open": workload.EXCLUDED})

    def test_negative_and_malformed_effort_values_are_rejected(self):
        self._invalid(effort={"unknown_units": -1})
        self._invalid(effort={"hour_upper_bounds": [8, 2]})
        self._invalid(effort={"band_units": [1.0]})

    def test_impossible_thresholds_are_rejected(self):
        self._invalid(attention={"warning_min_ratio": 1.5})
        self._invalid(attention={"first_response_business_days": 0})
        self._invalid(capacity={"peer_high_ratio": 0.5})

    def test_impossible_calendar_settings_are_rejected(self):
        self._invalid(business_calendar={"start": "18:00", "end": "09:00"})
        self._invalid(business_calendar={"weekdays": [9]})
        self._invalid(business_calendar={"holidays": ["not-a-date"]})

    def test_duplicate_exclusion_rule_ids_are_rejected(self):
        self._invalid(
            exclusion_rules=[
                {"id": "dup", "projects": ["IAP"], "issue_type_names": ["A"]},
                {"id": "dup", "projects": ["IAP"], "issue_type_names": ["B"]},
            ]
        )

    def test_exclusion_rule_must_match_an_issue_type(self):
        self._invalid(exclusion_rules=[{"id": "x", "projects": ["IAP"]}])

    def test_negative_member_capacity_is_rejected(self):
        self._invalid(
            member_settings_by_account_id={"acc-1": {"planning_capacity_units": -2}}
        )

    def test_t33_unassigned_backlog_requires_an_explicit_ownership_scope(self):
        self._invalid(include_unassigned=True, unassigned_team_scope_jql=None)
        valid = workload.validate_policy(
            workload._deep_merge(
                workload.DEFAULT_POLICY,
                {
                    "include_unassigned": True,
                    "unassigned_team_scope_jql": 'component in ("BI Reporting")',
                },
            )
        )
        self.assertTrue(valid["include_unassigned"])

    def test_unknown_timezone_is_rejected_when_the_database_is_available(self):
        if not workload._tz_database_available():
            self.skipTest("IANA time zone database not installed")
        self._invalid(business_calendar={"timezone": "Mars/Olympus_Mons"})


class OverrideLoadingTests(unittest.TestCase):
    def _write(self, payload):
        temp = Path(tempfile.mkdtemp()) / "workload_overrides.json"
        temp.write_text(json.dumps(payload), encoding="utf-8")
        return temp

    def test_overrides_are_keyed_by_both_id_and_key(self):
        path = self._write(
            {
                "overrides": [
                    {
                        "issue_id": "10001",
                        "issue_key": "CSSD-1001",
                        "state": workload.WAITING_EXTERNAL,
                        "reason": "vendor ticket",
                        "recorded_by": "isaac",
                        "recorded_at": "2026-10-10T12:00:00.000+0000",
                        "expires_at": "2026-10-17T12:00:00.000+0000",
                    }
                ]
            }
        )
        overrides = workload.load_overrides(path)
        self.assertIn("10001", overrides)
        self.assertIn("CSSD-1001", overrides)
        self.assertEqual(
            overrides["CSSD-1001"]["state"], workload.WAITING_EXTERNAL
        )

    def test_an_excluded_override_state_is_refused(self):
        path = self._write(
            [{"issue_key": "CSSD-1", "state": workload.EXCLUDED}]
        )
        with self.assertRaises(workload.PolicyError):
            workload.load_overrides(path)

    def test_an_override_without_a_reference_is_refused(self):
        path = self._write([{"state": workload.ACTIVE_READY}])
        with self.assertRaises(workload.PolicyError):
            workload.load_overrides(path)

    def test_a_negative_size_override_is_refused(self):
        path = self._write(
            [
                {
                    "issue_key": "CSSD-1",
                    "state": workload.ACTIVE_READY,
                    "size_units": -3,
                }
            ]
        )
        with self.assertRaises(workload.PolicyError):
            workload.load_overrides(path)

    def test_a_missing_override_file_is_simply_empty(self):
        self.assertEqual(workload.load_overrides(REPO_ROOT / "nope.json"), {})


class PolicyStoreTests(unittest.TestCase):
    """T34: a bad reload keeps the last valid policy and warns."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.policy_path = self.dir / "workload_policy.json"
        self.overrides_path = self.dir / "workload_overrides.json"

    def _write_policy(self, payload):
        self.policy_path.write_text(json.dumps(payload), encoding="utf-8")

    def test_bad_reload_retains_the_last_valid_policy_with_a_warning(self):
        self._write_policy({"policy_version": "good-v1"})
        store = workload.PolicyStore(self.policy_path, self.overrides_path)
        store.reload_if_changed()
        self.assertEqual(store.policy["policy_version"], "good-v1")
        self.assertIsNone(store.warning)

        self.policy_path.write_text("{ not json", encoding="utf-8")
        store.reload_if_changed()
        self.assertEqual(
            store.policy["policy_version"],
            "good-v1",
            "the previous valid policy must survive a bad reload",
        )
        self.assertIn("not reloaded", store.warning)

    def test_invalid_values_also_retain_the_previous_policy(self):
        self._write_policy({"policy_version": "good-v1"})
        store = workload.PolicyStore(self.policy_path, self.overrides_path)
        store.reload_if_changed()

        self._write_policy(
            {"policy_version": "bad-v2", "attention": {"warning_min_ratio": 9}}
        )
        store.reload_if_changed()
        self.assertEqual(store.policy["policy_version"], "good-v1")
        self.assertIsNotNone(store.warning)

    def test_a_changed_policy_is_detected_once(self):
        self._write_policy({"policy_version": "v1"})
        store = workload.PolicyStore(self.policy_path, self.overrides_path)
        self.assertTrue(store.reload_if_changed())
        self.assertFalse(store.reload_if_changed())
        self._write_policy({"policy_version": "v2"})
        self.assertTrue(store.reload_if_changed())
        self.assertEqual(store.policy["policy_version"], "v2")


class ReclassificationTests(unittest.TestCase):
    """A policy change must take effect without a new Jira pull."""

    def test_policy_change_reclassifies_cached_rows_in_place(self):
        issue = fx.issue(status="Waiting for Approval")
        source = workload.build_workload_source(issue, comments=[], histories=[])
        rows = [{"key": issue["key"], "assignee": fx.ASSIGNEE_NAME, "workload_source": source}]

        conservative = fx.policy()
        workload.reclassify_report(
            rows, conservative, fx.identity(current_policy=conservative), now=fx.NOW
        )
        self.assertEqual(rows[0]["workload_state"], workload.REVIEW_REQUIRED)

        confirmed = fx.policy(
            status_defaults_by_name={
                **conservative["status_defaults_by_name"],
                "Waiting for Approval": workload.WAITING_CLIENT_ESTIMATE_APPROVAL,
            },
            client_approval_status_names_by_project={"CSSD": ["Waiting for Approval"]},
        )
        workload.reclassify_report(
            rows, confirmed, fx.identity(current_policy=confirmed), now=fx.NOW
        )
        self.assertEqual(
            rows[0]["workload_state"], workload.WAITING_CLIENT_ESTIMATE_APPROVAL
        )
        self.assertEqual(rows[0]["active_units"], 0.0)

    def test_fingerprint_changes_with_policy_and_comment_edits(self):
        issue = fx.issue()
        source = workload.build_workload_source(
            issue,
            comments=[
                fx.comment("hello", comment_id="c1", created=fx.days_ago(2))
            ],
            histories=[],
        )
        policy_a = fx.policy()
        policy_b = fx.policy(policy_version="test-policy-v2")
        first = workload.source_fingerprint(source, policy_a)
        self.assertEqual(first, workload.source_fingerprint(source, policy_a))
        self.assertNotEqual(first, workload.source_fingerprint(source, policy_b))

        edited = workload.build_workload_source(
            issue,
            comments=[
                fx.comment(
                    "hello",
                    comment_id="c1",
                    created=fx.days_ago(2),
                    updated=fx.days_ago(1),
                )
            ],
            histories=[],
        )
        self.assertNotEqual(first, workload.source_fingerprint(edited, policy_a))

    def test_time_dependent_attention_is_re_evaluated_on_reclassification(self):
        issue = fx.issue(status="Open")
        histories = [
            fx.assignment_change(fx.ASSIGNEE_ID, fx.ASSIGNEE_NAME, fx.days_ago(1))
        ]
        source = workload.build_workload_source(issue, comments=[], histories=histories)
        rows = [{"key": issue["key"], "assignee": fx.ASSIGNEE_NAME, "workload_source": source}]
        policy = fx.policy()
        identity = fx.identity(current_policy=policy)

        workload.reclassify_report(rows, policy, identity, now=fx.NOW)
        self.assertFalse(rows[0]["first_response_overdue"])

        workload.reclassify_report(
            rows, policy, identity, now=fx.NOW + timedelta(days=14)
        )
        self.assertTrue(rows[0]["first_response_overdue"])


if __name__ == "__main__":
    unittest.main()
