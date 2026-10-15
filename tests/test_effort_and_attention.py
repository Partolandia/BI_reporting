"""
Effort scoring (T21, T22), attention clocks (T24-T27) and the business
calendar (T28).

Units here are planning proxies. The tests check the gate-then-estimate
order: responsibility decides whether anything counts at all, and only then
does effort decide how much.
"""

import unittest
from datetime import datetime, timezone

import workload
from tests import fixtures as fx


class EffortTests(unittest.TestCase):
    def test_t22_remaining_estimate_maps_to_configured_bands(self):
        cases = {2: 0.5, 6: 1.0, 20: 2.0, 36: 3.0, 60: 4.0}
        for hours, expected_units in cases.items():
            with self.subTest(hours=hours):
                fields = fx.classify(
                    fx.issue(status="In Progress", remaining_seconds=hours * 3600)
                )
                self.assertEqual(fields["active_units"], expected_units)
                self.assertEqual(fields["effort_source"], "remaining_estimate")
                self.assertEqual(fields["effort_quality"], "reported")

    def test_t22_band_is_capped_so_one_bad_estimate_cannot_dominate(self):
        fields = fx.classify(
            fx.issue(status="In Progress", remaining_seconds=4000 * 3600)
        )
        self.assertEqual(fields["active_units"], 4.0)

    def test_t21_zero_remaining_estimate_is_a_conflict_not_zero_workload(self):
        fields = fx.classify(fx.issue(status="In Progress", remaining_seconds=0))
        self.assertEqual(fields["active_units"], 1.0)
        self.assertEqual(fields["effort_source"], "unknown")
        self.assertEqual(
            fields["effort_quality"], "conflict_zero_remaining_estimate"
        )

    def test_t21_unknown_estimate_falls_back_to_one_unit(self):
        fields = fx.classify(fx.issue(status="In Progress"))
        self.assertEqual(fields["active_units"], 1.0)
        self.assertEqual(fields["effort_source"], "unknown")

    def test_original_estimate_is_only_a_proxy_before_work_starts(self):
        not_started = fx.classify(
            fx.issue(
                status="Open",
                original_seconds=30 * 3600,
                created=fx.days_ago(1),
            )
        )
        self.assertEqual(not_started["active_units"], 3.0)
        self.assertEqual(not_started["effort_source"], "original_estimate_proxy")

        started = fx.classify(
            fx.issue(status="In Progress", original_seconds=30 * 3600),
            comments=[
                fx.comment(
                    "I am working on the calculation changes and the data model "
                    "updates for this request.",
                    created=fx.days_ago(1),
                )
            ],
        )
        self.assertEqual(started["effort_source"], "unknown")

    def test_waiting_and_excluded_states_are_exactly_zero(self):
        waiting = fx.classify(
            fx.issue(status="In Progress", remaining_seconds=40 * 3600),
            comments=[
                fx.comment(
                    "Please approve the attached estimate.", created=fx.days_ago(2)
                )
            ],
        )
        self.assertEqual(waiting["active_units"], 0.0)
        self.assertEqual(waiting["provisional_units"], 0.0)
        self.assertEqual(waiting["planning_units"], 0.0)

        excluded = fx.classify(
            fx.issue(
                key="IAP-1", issue_type="[System] Change", remaining_seconds=40 * 3600
            )
        )
        self.assertEqual(excluded["planning_units"], 0.0)

    def test_configured_size_override_wins_over_estimate(self):
        overrides = {
            "CSSD-1001": {
                "state": workload.ACTIVE_IN_PROGRESS,
                "phase": workload.PHASE_IMPLEMENTATION,
                "reason": "sized by hand",
                "recorded_by": "isaac",
                "recorded_at": fx.days_ago(1),
                "expires_at": None,
                "size_units": 2.5,
            }
        }
        fields = fx.classify(
            fx.issue(status="In Progress", remaining_seconds=2 * 3600),
            overrides=overrides,
        )
        self.assertEqual(fields["active_units"], 2.5)
        self.assertEqual(fields["effort_source"], "configured_override")


class AttentionTests(unittest.TestCase):
    def test_t24_generic_comment_and_label_edit_do_not_clear_the_warning(self):
        histories = [
            fx.assignment_change(fx.ASSIGNEE_ID, fx.ASSIGNEE_NAME, fx.days_ago(20)),
            fx.field_change("labels", fx.days_ago(2)),
        ]
        fields = fx.classify(
            fx.issue(status="Open"),
            comments=[fx.comment("ok", created=fx.days_ago(2))],
            histories=histories,
        )
        self.assertTrue(fields["first_response_overdue"])
        self.assertFalse(fields["started"])

    def test_substantive_assignee_comment_counts_as_first_engagement(self):
        histories = [
            fx.assignment_change(fx.ASSIGNEE_ID, fx.ASSIGNEE_NAME, fx.days_ago(20))
        ]
        fields = fx.classify(
            fx.issue(status="In Progress"),
            comments=[
                fx.comment(
                    "I reviewed the source data and started rebuilding the "
                    "quarterly aggregation logic.",
                    created=fx.days_ago(1),
                )
            ],
            histories=histories,
        )
        self.assertTrue(fields["started"])
        self.assertFalse(fields["first_response_overdue"])

    def test_t25_approval_today_after_a_long_wait_is_not_immediately_overdue(self):
        histories = [
            fx.assignment_change(fx.ASSIGNEE_ID, fx.ASSIGNEE_NAME, fx.days_ago(60))
        ]
        fields = fx.classify(
            fx.issue(status="Pending Client"),
            comments=[
                fx.comment(
                    "Please approve the attached estimate.", created=fx.days_ago(40)
                ),
                fx.comment(
                    "Approved, please proceed.",
                    author_id=fx.CLIENT_ID,
                    author_name=fx.CLIENT_NAME,
                    created=fx.NOW,
                ),
            ],
            histories=histories,
        )
        self.assertEqual(fields["workload_state"], workload.ACTIVE_READY)
        self.assertFalse(fields["first_response_overdue"])
        self.assertFalse(fields["progress_overdue"])

    def test_t26_client_followup_does_not_reset_the_progress_clock(self):
        histories = [
            fx.assignment_change(fx.ASSIGNEE_ID, fx.ASSIGNEE_NAME, fx.days_ago(40))
        ]
        comments = [
            fx.comment(
                "I started the analysis and confirmed the source tables are "
                "populated correctly.",
                created=fx.days_ago(30),
            )
        ]
        before = fx.classify(
            fx.issue(status="In Progress"), comments=comments, histories=histories
        )
        self.assertTrue(before["progress_overdue"])

        after = fx.classify(
            fx.issue(status="In Progress"),
            comments=comments
            + [
                fx.comment(
                    "Any update on this one?",
                    author_id=fx.CLIENT_ID,
                    author_name=fx.CLIENT_NAME,
                    created=fx.days_ago(1),
                )
            ],
            histories=histories,
        )
        self.assertTrue(
            after["progress_overdue"],
            "a client chase is not assignee progress",
        )
        self.assertEqual(
            before["last_meaningful_assignee_activity_at"],
            after["last_meaningful_assignee_activity_at"],
        )

    def test_t27_current_assignment_episode_wins_over_older_assignments(self):
        histories = [
            fx.assignment_change(fx.TEAMMATE_ID, fx.TEAMMATE_NAME, fx.days_ago(50)),
            fx.assignment_change(fx.ASSIGNEE_ID, fx.ASSIGNEE_NAME, fx.days_ago(3)),
        ]
        fields = fx.classify(fx.issue(status="Open"), histories=histories)
        self.assertTrue(fields["assignment_episode_start"].startswith("2026-10-12"))
        self.assertFalse(fields["assignment_start_assumed"])
        self.assertNotIn(
            "assignment_date_assumed_from_creation", fields["data_quality_reasons"]
        )

    def test_t27_assignment_at_creation_is_labelled_as_an_assumption(self):
        fields = fx.classify(fx.issue(status="Open", created=fx.days_ago(10)))
        self.assertTrue(fields["assignment_start_assumed"])
        self.assertIn(
            "assignment_date_assumed_from_creation", fields["data_quality_reasons"]
        )

    def test_client_followup_due_after_the_configured_business_days(self):
        fields = fx.classify(
            fx.issue(status="In Progress"),
            comments=[
                fx.comment(
                    "Please approve the attached estimate.", created=fx.days_ago(20)
                )
            ],
        )
        self.assertTrue(fields["client_followup_due"])
        self.assertEqual(fields["active_units"], 0.0)

    def test_review_for_reassignment_only_targets_unstarted_ready_work(self):
        histories = [
            fx.assignment_change(fx.ASSIGNEE_ID, fx.ASSIGNEE_NAME, fx.days_ago(15))
        ]
        ready = fx.classify(fx.issue(status="Open"), histories=histories)
        self.assertTrue(ready["review_for_reassignment"])

        waiting = fx.classify(
            fx.issue(status="Pending Client"), histories=histories
        )
        self.assertFalse(waiting["review_for_reassignment"])


class BusinessCalendarTests(unittest.TestCase):
    """T28: weekends, business hours and daylight saving boundaries."""

    def setUp(self):
        self.calendar = workload.BusinessCalendar(
            workload.DEFAULT_POLICY["business_calendar"]
        )

    def _et(self, year, month, day, hour, minute=0):
        return datetime(
            year, month, day, hour, minute, tzinfo=self.calendar.tz
        )

    def test_weekend_is_skipped(self):
        # Friday 15:00 -> Monday 11:00 is 2h Friday + 2h Monday.
        hours = self.calendar.business_hours_between(
            self._et(2026, 10, 9, 15), self._et(2026, 10, 12, 11)
        )
        self.assertAlmostEqual(hours, 4.0, places=6)
        self.assertAlmostEqual(
            self.calendar.business_days_between(
                self._et(2026, 10, 9, 15), self._et(2026, 10, 12, 11)
            ),
            0.5,
            places=6,
        )

    def test_hours_outside_the_working_window_do_not_count(self):
        hours = self.calendar.business_hours_between(
            self._et(2026, 10, 13, 5), self._et(2026, 10, 13, 23)
        )
        self.assertAlmostEqual(hours, 8.0, places=6)

    def test_daylight_saving_transition_keeps_whole_business_days(self):
        # US DST starts Sunday 8 March 2026; the Mon-Fri 09:00-17:00 window is
        # still eight hours either side of it.
        hours = self.calendar.business_hours_between(
            self._et(2026, 3, 6, 9), self._et(2026, 3, 9, 17)
        )
        self.assertAlmostEqual(hours, 16.0, places=6)

    def test_offsets_are_normalized_before_comparison(self):
        utc_start = datetime(2026, 10, 13, 13, tzinfo=timezone.utc)  # 09:00 ET
        utc_end = datetime(2026, 10, 13, 21, tzinfo=timezone.utc)  # 17:00 ET
        self.assertAlmostEqual(
            self.calendar.business_hours_between(utc_start, utc_end), 8.0, places=6
        )

    def test_holidays_are_excluded(self):
        calendar = workload.BusinessCalendar(
            {**workload.DEFAULT_POLICY["business_calendar"], "holidays": ["2026-10-13"]}
        )
        hours = calendar.business_hours_between(
            datetime(2026, 10, 13, 9, tzinfo=calendar.tz),
            datetime(2026, 10, 13, 17, tzinfo=calendar.tz),
        )
        self.assertEqual(hours, 0.0)

    def test_reversed_and_missing_bounds_are_zero_not_negative(self):
        self.assertEqual(
            self.calendar.business_hours_between(
                self._et(2026, 10, 13, 17), self._et(2026, 10, 13, 9)
            ),
            0.0,
        )
        self.assertEqual(self.calendar.business_hours_between(None, None), 0.0)


if __name__ == "__main__":
    unittest.main()
