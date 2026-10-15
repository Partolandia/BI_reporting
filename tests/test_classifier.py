"""
Behaviour tests for the deterministic workload classifier.

Each test states the situation a manager would describe, then asserts the
decision that must follow -- in particular that a ticket only consumes
assignment capacity when the assignee actually has a next step.
"""

import unittest

import workload
from tests import fixtures as fx


class ClientWaitTests(unittest.TestCase):
    """T01-T04: establishing (and refusing to establish) a client wait."""

    def test_t01_estimate_sent_awaiting_client_approval_has_no_active_units(self):
        fields = fx.classify(
            fx.issue(status="In Progress"),
            comments=[
                fx.comment(
                    "Please approve the attached 5.5-hour estimate so we can proceed.",
                    created=fx.days_ago(4),
                )
            ],
        )
        self.assertEqual(
            fields["workload_state"], workload.WAITING_CLIENT_ESTIMATE_APPROVAL
        )
        self.assertEqual(fields["active_units"], 0.0)
        self.assertEqual(fields["planning_units"], 0.0)
        self.assertFalse(fields["include_in_active_capacity"])
        self.assertEqual(fields["next_action_owner_role"], workload.OWNER_CLIENT)
        self.assertIsNotNone(fields["waiting_since"])

    def test_t02_delivered_work_awaiting_acceptance_has_no_active_units(self):
        fields = fx.classify(
            fx.issue(status="In Progress"),
            comments=[
                fx.comment(
                    "The report is deployed. Please validate and confirm whether "
                    "we may close.",
                    created=fx.days_ago(3),
                )
            ],
        )
        self.assertEqual(fields["workload_state"], workload.WAITING_CLIENT_ACCEPTANCE)
        self.assertEqual(fields["active_units"], 0.0)
        self.assertEqual(fields["work_phase"], workload.PHASE_DELIVERY_REVIEW)

    def test_t03_promise_to_send_estimate_is_still_active_work(self):
        fields = fx.classify(
            fx.issue(status="In Progress"),
            comments=[
                fx.comment(
                    "I will prepare and send the estimate tomorrow.",
                    created=fx.days_ago(1),
                )
            ],
        )
        self.assertIn(fields["workload_state"], workload.ACTIVE_STATES)
        self.assertEqual(fields["work_phase"], workload.PHASE_ESTIMATE)
        self.assertGreater(fields["active_units"], 0.0)

    def test_t04_internal_approval_is_never_a_client_wait(self):
        fields = fx.classify(
            fx.issue(status="In Progress"),
            comments=[
                fx.comment(
                    "Please approve my estimate internally before it is sent.",
                    created=fx.days_ago(2),
                )
            ],
        )
        self.assertNotIn(fields["workload_state"], workload.WAITING_CLIENT_STATES)
        self.assertEqual(fields["workload_state"], workload.WAITING_INTERNAL)
        self.assertEqual(fields["active_units"], 0.0)


class ReactivationTests(unittest.TestCase):
    """T05-T11: later evidence must be able to reopen a ticket."""

    def test_t05_client_approval_reactivates_even_when_status_says_pending_client(self):
        fields = fx.classify(
            fx.issue(status="Pending Client"),
            comments=[
                fx.comment(
                    "Please approve the attached estimate.", created=fx.days_ago(12)
                ),
                fx.comment(
                    "Approved, please proceed with this estimate.",
                    author_id=fx.CLIENT_ID,
                    author_name=fx.CLIENT_NAME,
                    created=fx.days_ago(1),
                ),
            ],
        )
        self.assertEqual(fields["workload_state"], workload.ACTIVE_READY)
        self.assertTrue(fields["implementation_authorized"])
        self.assertGreater(fields["active_units"], 0.0)
        self.assertIsNotNone(fields["actionable_since"])

    def test_t06_client_defect_after_delivery_reactivates_investigation(self):
        fields = fx.classify(
            fx.issue(status="Client Review"),
            comments=[
                fx.comment(
                    "The report is deployed. Please validate.", created=fx.days_ago(8)
                ),
                fx.comment(
                    "The totals are still incorrect for Q3.",
                    author_id=fx.CLIENT_ID,
                    author_name=fx.CLIENT_NAME,
                    created=fx.days_ago(1),
                ),
            ],
        )
        self.assertEqual(fields["workload_state"], workload.ACTIVE_READY)
        self.assertEqual(fields["work_phase"], workload.PHASE_DISCOVERY)
        self.assertFalse(fields["implementation_authorized"])

    def test_t07_thanks_and_will_review_do_not_invent_acceptance(self):
        base_comments = [
            fx.comment("The report is deployed. Please validate.", created=fx.days_ago(6))
        ]
        for text in ("Thanks.", "I will review next week."):
            with self.subTest(text=text):
                fields = fx.classify(
                    fx.issue(status="Client Review"),
                    comments=base_comments
                    + [
                        fx.comment(
                            text,
                            author_id=fx.CLIENT_ID,
                            author_name=fx.CLIENT_NAME,
                            created=fx.days_ago(1),
                        )
                    ],
                )
                self.assertEqual(
                    fields["workload_state"], workload.WAITING_CLIENT_ACCEPTANCE
                )
                self.assertEqual(fields["active_units"], 0.0)

    def test_t08_discovery_approval_does_not_authorize_implementation(self):
        fields = fx.classify(
            fx.issue(status="Pending Client"),
            comments=[
                fx.comment(
                    "Please approve the attached estimate.", created=fx.days_ago(9)
                ),
                fx.comment(
                    "Approved for two hours of discovery.",
                    author_id=fx.CLIENT_ID,
                    author_name=fx.CLIENT_NAME,
                    created=fx.days_ago(1),
                ),
            ],
        )
        self.assertEqual(fields["workload_state"], workload.ACTIVE_READY)
        self.assertEqual(fields["work_phase"], workload.PHASE_DISCOVERY)
        self.assertFalse(fields["implementation_authorized"])

    def test_t09_phase_one_delivered_while_phase_two_continues_is_active(self):
        fields = fx.classify(
            fx.issue(status="In Progress"),
            comments=[
                fx.comment(
                    "Phase 1 was delivered to the client; I am still completing "
                    "approved phase 2.",
                    created=fx.days_ago(1),
                )
            ],
        )
        self.assertEqual(fields["workload_state"], workload.ACTIVE_IN_PROGRESS)
        self.assertGreater(fields["active_units"], 0.0)

    def test_t10_negation_quote_and_bot_text_never_create_a_client_wait(self):
        cases = {
            "negated": fx.comment(
                "We have not sent the estimate yet.", created=fx.days_ago(2)
            ),
            "quoted": fx.comment(
                "Still digging into the calculation.",
                quoted="Estimate sent, please approve.",
                created=fx.days_ago(2),
            ),
            "bot": fx.comment(
                "Awaiting client response.",
                author_id=fx.BOT_ID,
                author_name=fx.BOT_NAME,
                created=fx.days_ago(2),
            ),
        }
        for label, payload in cases.items():
            with self.subTest(case=label):
                fields = fx.classify(fx.issue(status="In Progress"), comments=[payload])
                self.assertNotIn(
                    fields["workload_state"], workload.WAITING_CLIENT_STATES
                )

    def test_t11_client_question_during_estimate_wait_creates_a_response_task(self):
        fields = fx.classify(
            fx.issue(status="In Progress"),
            comments=[
                fx.comment(
                    "Please approve the attached estimate.", created=fx.days_ago(5)
                ),
                fx.comment(
                    "Which funds should the totals include?",
                    author_id=fx.CLIENT_ID,
                    author_name=fx.CLIENT_NAME,
                    created=fx.days_ago(1),
                ),
            ],
        )
        self.assertEqual(fields["workload_state"], workload.ACTIVE_READY)
        self.assertFalse(
            fields["implementation_authorized"],
            "answering a question must not authorize implementation",
        )


class AmbiguityTests(unittest.TestCase):
    """T12, T13, T18, T38: uncertainty stays visible instead of being guessed."""

    def test_t12_pending_without_owner_evidence_needs_review(self):
        fields = fx.classify(fx.issue(status="Pending"))
        self.assertEqual(fields["workload_state"], workload.REVIEW_REQUIRED)
        self.assertEqual(fields["provisional_units"], 1.0)
        self.assertEqual(fields["active_units"], 0.0)
        self.assertEqual(fields["planning_units"], 1.0)
        self.assertFalse(fields["include_in_active_capacity"])

    def test_t13_peer_review_ownership_decides_active_versus_internal_wait(self):
        owned = fx.classify(
            fx.issue(status="Peer Review"),
            comments=[
                fx.comment("I am reviewing the code now.", created=fx.days_ago(1))
            ],
        )
        self.assertEqual(owned["workload_state"], workload.ACTIVE_REVIEW)
        self.assertGreater(owned["active_units"], 0.0)

        handed_off = fx.classify(
            fx.issue(status="Peer Review"),
            comments=[
                fx.comment(
                    "please review this when you get a chance.",
                    body=fx.adf_with_mention(
                        "please review this when you get a chance.", fx.TEAMMATE_NAME
                    ),
                    created=fx.days_ago(1),
                )
            ],
        )
        self.assertEqual(handed_off["workload_state"], workload.WAITING_INTERNAL)
        self.assertEqual(handed_off["active_units"], 0.0)

        unclear = fx.classify(fx.issue(status="Peer Review"))
        self.assertEqual(unclear["workload_state"], workload.REVIEW_REQUIRED)

    def test_t18_internal_manager_is_not_treated_as_a_client(self):
        fields = fx.classify(
            fx.issue(status="In Progress"),
            comments=[
                fx.comment(
                    "Approved, please proceed.",
                    author_id=fx.MANAGER_ID,
                    author_name=fx.MANAGER_NAME,
                    created=fx.days_ago(1),
                )
            ],
        )
        self.assertNotEqual(fields["rule_id"], "comment.client_approved_next_phase")
        self.assertNotIn(fields["workload_state"], workload.WAITING_CLIENT_STATES)

    def test_t38_approval_from_unidentified_author_requires_review(self):
        fields = fx.classify(
            fx.issue(status="In Progress"),
            comments=[
                fx.comment(
                    "Approved.",
                    author_id=fx.STRANGER_ID,
                    author_name=fx.STRANGER_NAME,
                    created=fx.days_ago(1),
                )
            ],
        )
        self.assertEqual(fields["workload_state"], workload.REVIEW_REQUIRED)
        self.assertEqual(
            fields["classification_confidence"], workload.CONFIDENCE_REVIEW_REQUIRED
        )
        self.assertFalse(fields["implementation_authorized"])


class ScopeTests(unittest.TestCase):
    """T14-T16, T37: exclusions are workload-only and never silent."""

    def test_t14_iap_change_types_are_excluded_in_mixed_case(self):
        for type_name in ("[System] Change", "[SYSTEM]  change", " Change Request "):
            with self.subTest(issue_type=type_name):
                fields = fx.classify(
                    fx.issue(
                        key="IAP-119606",
                        issue_type=type_name,
                        status="In Progress",
                        summary="Business Intelligence - Change Request - Top Tier",
                    )
                )
                self.assertEqual(fields["workload_state"], workload.EXCLUDED)
                self.assertEqual(fields["active_units"], 0.0)
                self.assertEqual(fields["planning_units"], 0.0)
                self.assertTrue(fields["rule_id"].startswith("exclusion."))

    def test_t15_summary_text_does_not_exclude_a_known_service_request(self):
        fields = fx.classify(
            fx.issue(
                key="IAP-119000",
                issue_type="Service Request",
                status="In Progress",
                summary="Business Intelligence - Change Request - Standish Management",
            )
        )
        self.assertNotEqual(fields["workload_state"], workload.EXCLUDED)

    def test_t16_missing_issue_type_metadata_is_a_review_candidate(self):
        fields = fx.classify(
            fx.issue(
                key="IAP-118500",
                status="In Progress",
                include_type_metadata=False,
                summary="Business Intelligence - Change Request - Legacy row",
            )
        )
        self.assertEqual(fields["workload_state"], workload.REVIEW_REQUIRED)
        self.assertIn("issue_type_metadata_missing", fields["data_quality_reasons"])
        self.assertEqual(fields["provisional_units"], 1.0)

    def test_t37_exclusion_wins_over_a_waiting_status_exactly_once(self):
        fields = fx.classify(
            fx.issue(
                key="IAP-119630", issue_type="[System] Change", status="Client Review"
            ),
            comments=[
                fx.comment("The report is deployed. Please validate.", created=fx.days_ago(3))
            ],
        )
        self.assertEqual(fields["workload_state"], workload.EXCLUDED)
        self.assertEqual(fields["planning_units"], 0.0)

    def test_project_key_falls_back_to_issue_key_only_when_metadata_missing(self):
        fields = fx.classify(
            fx.issue(
                key="IAP-119631",
                issue_type="[System] Change",
                include_project_metadata=False,
            )
        )
        self.assertEqual(fields["workload_state"], workload.EXCLUDED)
        self.assertIn(
            "project_key_inferred_from_issue_key", fields["data_quality_reasons"]
        )


class EvidenceIntegrityTests(unittest.TestCase):
    """T17, T19, T23, T29: identity, completeness and chronology guards."""

    def test_t17_ambiguous_or_missing_identity_is_surfaced_for_setup(self):
        ambiguous = fx.identity(
            extra_accounts={fx.ASSIGNEE_NAME: {fx.ASSIGNEE_ID, "acc-999-duplicate"}}
        )
        self.assertTrue(
            any("accountIds" in issue for issue in ambiguous.setup_issues),
            ambiguous.setup_issues,
        )

        renamed = workload.Identity(
            team_display_names=fx.TEAM_MEMBERS,
            display_to_account_ids={"Someone Else": {"acc-777"}},
        )
        self.assertTrue(
            any("no matching Jira accountId" in issue for issue in renamed.setup_issues)
        )

    def test_t19_incomplete_history_blocks_a_confident_exclusion(self):
        fields = fx.classify(
            fx.issue(status="In Progress"),
            comments=[
                fx.comment(
                    "Please approve the attached estimate.", created=fx.days_ago(3)
                )
            ],
            comments_complete=False,
        )
        self.assertEqual(fields["workload_state"], workload.REVIEW_REQUIRED)
        self.assertIn("history_incomplete", fields["data_quality_reasons"])
        self.assertFalse(fields["source_history_complete"])

    def test_t23_repeated_chasing_preserves_the_original_waiting_date(self):
        fields = fx.classify(
            fx.issue(status="In Progress"),
            comments=[
                fx.comment(
                    "Please approve the attached estimate.", created=fx.days_ago(40)
                ),
                fx.comment("Still awaiting your approval.", created=fx.days_ago(2)),
            ],
        )
        self.assertEqual(
            fields["workload_state"], workload.WAITING_CLIENT_ESTIMATE_APPROVAL
        )
        self.assertEqual(fields["active_units"], 0.0)
        self.assertTrue(fields["waiting_since"].startswith("2026-09"))

    def test_t29_edited_old_comment_does_not_become_the_newest_wait(self):
        fields = fx.classify(
            fx.issue(status="In Progress"),
            comments=[
                fx.comment(
                    "Please approve the attached estimate.",
                    created=fx.days_ago(14),
                    updated=fx.days_ago(1),
                    comment_id="c-edited",
                ),
                fx.comment(
                    "Approved, please proceed.",
                    author_id=fx.CLIENT_ID,
                    author_name=fx.CLIENT_NAME,
                    created=fx.days_ago(7),
                ),
            ],
        )
        self.assertEqual(fields["workload_state"], workload.ACTIVE_READY)
        self.assertTrue(
            any("edited" in reason for reason in fields["data_quality_reasons"])
        )


class ClosureTests(unittest.TestCase):
    """T39: acceptance plus a closure request is small administrative work."""

    def test_t39_client_acceptance_leaves_only_a_closure_task(self):
        fields = fx.classify(
            fx.issue(status="Client Review", remaining_seconds=20 * 3600),
            comments=[
                fx.comment("The report is deployed. Please validate.", created=fx.days_ago(9)),
                fx.comment(
                    "This is exactly what we needed, please close the ticket.",
                    author_id=fx.CLIENT_ID,
                    author_name=fx.CLIENT_NAME,
                    created=fx.days_ago(1),
                ),
            ],
        )
        self.assertEqual(fields["workload_state"], workload.ACTIVE_READY)
        self.assertEqual(fields["work_phase"], workload.PHASE_CLOSURE)
        self.assertEqual(fields["active_units"], 0.25)
        self.assertEqual(fields["effort_source"], "closure_proxy")


class OverrideTests(unittest.TestCase):
    """T30: a time-limited override cannot conceal a newer obligation."""

    def test_t30_expired_override_is_ignored(self):
        overrides = {
            "CSSD-1001": {
                "state": workload.WAITING_CLIENT_INPUT,
                "phase": workload.PHASE_UNKNOWN,
                "reason": "manual",
                "recorded_by": "isaac",
                "recorded_at": fx.days_ago(20),
                "expires_at": fx.days_ago(5),
                "size_units": None,
            }
        }
        fields = fx.classify(fx.issue(status="In Progress"), overrides=overrides)
        self.assertNotEqual(fields["classification_source"], "override")
        self.assertIn("override_expired", fields["data_quality_reasons"])

    def test_t30_newer_client_request_invalidates_a_waiting_override(self):
        overrides = {
            "CSSD-1001": {
                "state": workload.WAITING_CLIENT_INPUT,
                "phase": workload.PHASE_UNKNOWN,
                "reason": "waiting on sample file",
                "recorded_by": "isaac",
                "recorded_at": fx.days_ago(10),
                "expires_at": None,
                "size_units": None,
            }
        }
        fields = fx.classify(
            fx.issue(status="In Progress"),
            comments=[
                fx.comment(
                    "The totals are still incorrect.",
                    author_id=fx.CLIENT_ID,
                    author_name=fx.CLIENT_NAME,
                    created=fx.days_ago(1),
                )
            ],
            overrides=overrides,
        )
        self.assertIn(fields["workload_state"], workload.ACTIVE_STATES)
        self.assertIn(
            "override_invalidated_by_newer_event", fields["data_quality_reasons"]
        )

    def test_valid_override_applies_and_is_labelled(self):
        overrides = {
            "CSSD-1001": {
                "state": workload.WAITING_EXTERNAL,
                "phase": workload.PHASE_IMPLEMENTATION,
                "reason": "blocked on vendor ticket",
                "recorded_by": "isaac",
                "recorded_at": fx.days_ago(1),
                "expires_at": None,
                "size_units": None,
            }
        }
        fields = fx.classify(fx.issue(status="In Progress"), overrides=overrides)
        self.assertEqual(fields["workload_state"], workload.WAITING_EXTERNAL)
        self.assertEqual(fields["classification_source"], "override")
        self.assertEqual(fields["active_units"], 0.0)


class LegacyCacheTests(unittest.TestCase):
    """Old cache rows must be visible, not silently zero."""

    def test_missing_workload_source_renders_as_needs_review(self):
        fields = workload.classify_workload(
            None, fx.policy(), fx.identity(), now=fx.NOW
        )
        self.assertEqual(fields["workload_state"], workload.REVIEW_REQUIRED)
        self.assertEqual(fields["provisional_units"], 1.0)
        self.assertTrue(fields["classification_stale"])
        self.assertIn("missing_workload_source", fields["data_quality_reasons"])


if __name__ == "__main__":
    unittest.main()
