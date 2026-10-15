"""
Per-person aggregation, capacity and peer comparison (T20, T31, T32, T40)
plus the state and unit invariants and the attention-warning boundaries.
"""

import unittest

import workload
from tests import fixtures as fx


def build(rows, current_policy=None, team=None):
    current_policy = current_policy or fx.policy()
    return workload.build_workload_report(
        rows,
        team or fx.TEAM_MEMBERS,
        current_policy,
        fx.identity(team_members=team, current_policy=current_policy),
        now=fx.NOW,
    )


def person(snapshot, name=fx.ASSIGNEE_NAME):
    return next(p for p in snapshot["people"] if p["name"] == name)


class InvariantTests(unittest.TestCase):
    def test_owned_total_is_the_sum_of_the_four_non_excluded_buckets(self):
        rows = [
            fx.row("CSSD-1", state=workload.ACTIVE_IN_PROGRESS, active_units=2.0),
            fx.row("CSSD-2", state=workload.ACTIVE_READY, active_units=1.0),
            fx.row("CSSD-3", state=workload.WAITING_CLIENT_ACCEPTANCE),
            fx.row("CSSD-4", state=workload.WAITING_CLIENT_ESTIMATE_APPROVAL),
            fx.row("CSSD-5", state=workload.WAITING_INTERNAL),
            fx.row("CSSD-6", state=workload.REVIEW_REQUIRED, provisional_units=1.0),
            fx.row("IAP-7", state=workload.EXCLUDED),
        ]
        who = person(build(rows))
        self.assertEqual(who["confirmed_active_count"], 2)
        self.assertEqual(who["waiting_client_count"], 2)
        self.assertEqual(who["waiting_other_count"], 1)
        self.assertEqual(who["review_required_count"], 1)
        self.assertEqual(who["excluded_count"], 1)
        self.assertEqual(
            who["owned_total"],
            who["confirmed_active_count"]
            + who["waiting_client_count"]
            + who["waiting_other_count"]
            + who["review_required_count"],
        )
        self.assertEqual(who["owned_total"], 6)

    def test_planning_units_are_active_plus_provisional_only(self):
        rows = [
            fx.row("CSSD-1", state=workload.ACTIVE_IN_PROGRESS, active_units=2.0),
            fx.row("CSSD-2", state=workload.WAITING_CLIENT_INPUT),
            fx.row("CSSD-3", state=workload.REVIEW_REQUIRED, provisional_units=1.0),
            fx.row("IAP-4", state=workload.EXCLUDED),
        ]
        who = person(build(rows))
        self.assertEqual(who["active_units"], 2.0)
        self.assertEqual(who["provisional_units"], 1.0)
        self.assertEqual(who["planning_units"], 3.0)

    def test_waiting_client_counts_are_broken_down_by_reason(self):
        rows = [
            fx.row("CSSD-1", state=workload.WAITING_CLIENT_ESTIMATE_APPROVAL),
            fx.row("CSSD-2", state=workload.WAITING_CLIENT_ESTIMATE_APPROVAL),
            fx.row("CSSD-3", state=workload.WAITING_CLIENT_ACCEPTANCE),
        ]
        who = person(build(rows))
        self.assertEqual(
            who["waiting_client_by_reason"],
            {
                workload.WAITING_CLIENT_ESTIMATE_APPROVAL: 2,
                workload.WAITING_CLIENT_ACCEPTANCE: 1,
            },
        )

    def test_reconciliation_adds_up_to_the_fetched_scope(self):
        rows = [
            fx.row("CSSD-1", state=workload.ACTIVE_IN_PROGRESS, active_units=1.0),
            fx.row("CSSD-2", state=workload.WAITING_CLIENT_INPUT),
            fx.row("CSSD-3", state=workload.REVIEW_REQUIRED, provisional_units=1.0),
            fx.row("IAP-4", state=workload.EXCLUDED),
        ]
        recon = build(rows)["reconciliation"]
        self.assertEqual(recon["fetched"], 4)
        self.assertEqual(
            recon["confirmed_active"]
            + recon["waiting_client"]
            + recon["waiting_other"]
            + recon["review_required"]
            + recon["excluded"],
            recon["fetched"],
        )
        self.assertEqual(recon["planning_units"], 2.0)

    def test_assignees_outside_the_team_are_reported_not_counted(self):
        rows = [fx.row("CSSD-9", assignee="Someone External", active_units=5.0)]
        snapshot = build(rows)
        self.assertEqual(person(snapshot)["planning_units"], 0.0)
        self.assertEqual(snapshot["unknown_assignees"], {"Someone External": 1})


class CapacityTests(unittest.TestCase):
    def test_t40_without_configuration_no_overload_or_availability_claim(self):
        rows = [
            fx.row(f"CSSD-{i}", state=workload.ACTIVE_IN_PROGRESS, active_units=4.0)
            for i in range(10)
        ]
        who = person(build(rows))
        self.assertEqual(who["capacity_status"], "capacity_not_calibrated")
        self.assertEqual(who["capacity_label"], "Capacity not calibrated")
        self.assertIsNone(who["planning_capacity_units"])
        self.assertEqual(who["availability_label"], "availability not configured")

    def test_configured_capacity_drives_the_three_states(self):
        cases = {6.0: "above_capacity", 4.0: "at_or_near_capacity", 1.0: "possible_capacity_for_more"}
        for units, expected in cases.items():
            with self.subTest(units=units):
                policy = fx.policy(
                    member_settings_by_account_id={
                        fx.ASSIGNEE_ID: {
                            "planning_capacity_units": 5.0,
                            "role_group": "developer",
                            "assignment_eligible": True,
                            "availability": "available",
                        }
                    }
                )
                rows = [
                    fx.row(
                        "CSSD-1",
                        state=workload.ACTIVE_IN_PROGRESS,
                        active_units=units,
                    )
                ]
                who = person(build(rows, current_policy=policy))
                self.assertEqual(who["capacity_status"], expected)

    def test_classification_uncertainty_blocks_an_available_badge(self):
        policy = fx.policy(
            member_settings_by_account_id={
                fx.ASSIGNEE_ID: {
                    "planning_capacity_units": 10.0,
                    "role_group": "developer",
                    "assignment_eligible": True,
                    "availability": "available",
                }
            }
        )
        rows = [fx.row("CSSD-1", state=workload.REVIEW_REQUIRED, provisional_units=1.0)]
        who = person(build(rows, current_policy=policy))
        self.assertEqual(who["capacity_status"], "below_capacity_unconfirmed")

    def test_zero_capacity_means_unavailable_and_never_divides_by_zero(self):
        policy = fx.policy(
            member_settings_by_account_id={
                fx.ASSIGNEE_ID: {
                    "planning_capacity_units": 0,
                    "role_group": "manager",
                }
            }
        )
        who = person(build([], current_policy=policy))
        self.assertEqual(who["capacity_status"], "unavailable")

    def test_wip_limit_produces_its_own_badge(self):
        policy = fx.policy(
            member_settings_by_account_id={
                fx.ASSIGNEE_ID: {
                    "planning_capacity_units": 20.0,
                    "max_started_tickets": 2,
                    "role_group": "developer",
                    "assignment_eligible": True,
                    "availability": "available",
                }
            }
        )
        rows = [
            fx.row(f"CSSD-{i}", state=workload.ACTIVE_IN_PROGRESS, active_units=1.0, started=True)
            for i in range(4)
        ]
        who = person(build(rows, current_policy=policy))
        self.assertTrue(who["wip_limit_exceeded"])
        self.assertEqual(who["started_count"], 4)

    def test_t20_empty_team_snapshot_is_safe_and_flag_free(self):
        snapshot = build([])
        self.assertEqual(len(snapshot["people"]), len(fx.TEAM_MEMBERS))
        for who in snapshot["people"]:
            self.assertEqual(who["owned_total"], 0)
            self.assertEqual(who["planning_units"], 0.0)
            self.assertFalse(who["higher_than_peers"])
            self.assertFalse(who["attention_warning"])

    def test_peer_signal_requires_enough_people_and_a_positive_median(self):
        team = ["A", "B", "C", "D"]
        policy = fx.policy(
            member_settings_by_account_id={
                f"acc-{name}": {
                    "planning_capacity_units": None,
                    "role_group": "developer",
                    "assignment_eligible": True,
                    "availability": "available",
                }
                for name in team
            }
        )
        identity = workload.Identity(
            team_display_names=team,
            display_to_account_ids={name: {f"acc-{name}"} for name in team},
        )
        rows = (
            [
                fx.row(f"CSSD-A{i}", assignee="A", state=workload.ACTIVE_IN_PROGRESS, active_units=2.0)
                for i in range(5)
            ]
            + [fx.row("CSSD-B1", assignee="B", state=workload.ACTIVE_IN_PROGRESS, active_units=2.0)]
            + [fx.row("CSSD-C1", assignee="C", state=workload.ACTIVE_IN_PROGRESS, active_units=2.0)]
            + [fx.row("CSSD-D1", assignee="D", state=workload.ACTIVE_IN_PROGRESS, active_units=2.0)]
        )
        snapshot = workload.build_workload_report(rows, team, policy, identity, now=fx.NOW)
        loaded = next(p for p in snapshot["people"] if p["name"] == "A")
        self.assertEqual(loaded["peer_group_size"], 4)
        self.assertTrue(loaded["higher_than_peers"])

        quiet = next(p for p in snapshot["people"] if p["name"] == "B")
        self.assertFalse(quiet["higher_than_peers"])

    def test_peer_signal_is_suppressed_when_the_median_is_zero(self):
        team = ["A", "B", "C", "D"]
        identity = workload.Identity(
            team_display_names=team,
            display_to_account_ids={name: {f"acc-{name}"} for name in team},
        )
        rows = [
            fx.row("CSSD-A1", assignee="A", state=workload.ACTIVE_IN_PROGRESS, active_units=3.0)
        ]
        snapshot = workload.build_workload_report(
            rows, team, fx.policy(), identity, now=fx.NOW
        )
        loaded = next(p for p in snapshot["people"] if p["name"] == "A")
        self.assertEqual(loaded["peer_median_units"], 0.0)
        self.assertFalse(loaded["higher_than_peers"])


class AttentionWarningBoundaryTests(unittest.TestCase):
    """The warning needs both a count and a share, checked with >=."""

    def _rows(self, overdue, total):
        rows = []
        for index in range(total):
            rows.append(
                fx.row(
                    f"CSSD-{index}",
                    state=workload.ACTIVE_IN_PROGRESS,
                    active_units=1.0,
                    first_response_overdue=index < overdue,
                )
            )
        return rows

    def test_exactly_at_both_thresholds_warns(self):
        who = person(build(self._rows(overdue=3, total=12)))
        self.assertEqual(who["overdue_active_count"], 3)
        self.assertEqual(who["overdue_active_ratio"], 0.25)
        self.assertTrue(who["attention_warning"])

    def test_below_the_count_threshold_does_not_warn(self):
        who = person(build(self._rows(overdue=2, total=4)))
        self.assertFalse(who["attention_warning"])

    def test_below_the_ratio_threshold_does_not_warn(self):
        who = person(build(self._rows(overdue=3, total=13)))
        self.assertLess(who["overdue_active_ratio"], 0.25)
        self.assertFalse(who["attention_warning"])

    def test_a_ticket_overdue_for_both_reasons_is_counted_once(self):
        rows = [
            fx.row(
                "CSSD-1",
                state=workload.ACTIVE_IN_PROGRESS,
                active_units=1.0,
                first_response_overdue=True,
                progress_overdue=True,
            )
        ]
        who = person(build(rows))
        self.assertEqual(who["overdue_active_count"], 1)


class ReassignmentAndFilterTests(unittest.TestCase):
    def test_t32_only_eligible_ready_work_is_a_reassignment_candidate(self):
        rows = [
            fx.row(
                "CSSD-1",
                state=workload.WAITING_CLIENT_ESTIMATE_APPROVAL,
                review_for_reassignment=False,
            ),
            fx.row(
                "CSSD-2",
                state=workload.ACTIVE_READY,
                active_units=1.0,
                started=False,
                first_response_overdue=True,
                review_for_reassignment=True,
            ),
            fx.row(
                "CSSD-3",
                state=workload.REVIEW_REQUIRED,
                provisional_units=1.0,
                review_for_reassignment=False,
            ),
        ]
        who = person(build(rows))
        candidates = [row["key"] for row in who["review_for_reassignment"]]
        self.assertEqual(candidates, ["CSSD-2"])

    def test_t31_display_filters_do_not_change_capacity_or_units(self):
        rows = [
            fx.row(
                "CSSD-1",
                state=workload.ACTIVE_IN_PROGRESS,
                active_units=2.0,
                summary="Standish DB - RP - quarterly export",
            ),
            fx.row(
                "IAP-2",
                state=workload.ACTIVE_IN_PROGRESS,
                active_units=1.0,
                summary="Other client - portal tweak",
            ),
        ]
        snapshot = build(rows)
        who = person(snapshot)
        before = (who["planning_units"], who["capacity_status"], who["peer_median_units"])

        filtered = workload.filter_rows_for_view(
            [row for row in rows if "standish" in row["summary"].lower()], "active"
        )
        self.assertEqual(len(filtered), 1)

        who_again = person(build(rows))
        after = (
            who_again["planning_units"],
            who_again["capacity_status"],
            who_again["peer_median_units"],
        )
        self.assertEqual(before, after)

    def test_view_filter_selects_the_expected_states(self):
        rows = [
            fx.row("CSSD-1", state=workload.ACTIVE_READY, active_units=1.0),
            fx.row("CSSD-2", state=workload.WAITING_CLIENT_INPUT),
            fx.row("CSSD-3", state=workload.WAITING_INTERNAL),
            fx.row("CSSD-4", state=workload.REVIEW_REQUIRED, provisional_units=1.0),
            fx.row("IAP-5", state=workload.EXCLUDED),
        ]
        self.assertEqual(
            [r["key"] for r in workload.filter_rows_for_view(rows, "active")], ["CSSD-1"]
        )
        self.assertEqual(
            [r["key"] for r in workload.filter_rows_for_view(rows, "waiting_client")],
            ["CSSD-2"],
        )
        self.assertEqual(
            [r["key"] for r in workload.filter_rows_for_view(rows, "waiting_other")],
            ["CSSD-3"],
        )
        self.assertEqual(
            [r["key"] for r in workload.filter_rows_for_view(rows, "review")], ["CSSD-4"]
        )
        self.assertEqual(
            [r["key"] for r in workload.filter_rows_for_view(rows, "excluded")], ["IAP-5"]
        )
        self.assertEqual(len(workload.filter_rows_for_view(rows, "all")), 5)


if __name__ == "__main__":
    unittest.main()
