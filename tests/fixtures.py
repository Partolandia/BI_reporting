"""
Small synthetic Jira fixtures for the workload tests.

Everything here is fabricated: no real tenant IDs, accountIds, client names or
ticket contents. Comment bodies are built as Atlassian Document Format,
matching what the v3 API actually returns, so the ADF extraction path is
exercised rather than bypassed.
"""

from datetime import datetime, timedelta, timezone

import workload

# Fabricated accountIds. Jira accountIds are opaque strings, which is the
# point: display names are presentation values only.
ASSIGNEE_ID = "acc-100-yaumara"
ASSIGNEE_NAME = "Yaumara Torres"
TEAMMATE_ID = "acc-200-viacheslav"
TEAMMATE_NAME = "Viacheslav Golovach"
MANAGER_ID = "acc-300-manager"
MANAGER_NAME = "Henry Glubb"  # internal account manager, NOT a client
CLIENT_ID = "acc-400-client"
CLIENT_NAME = "Dana Requester"
BOT_ID = "acc-500-automation"
BOT_NAME = "Automation for Jira"
STRANGER_ID = "acc-600-stranger"
STRANGER_NAME = "Unidentified Person"

TEAM_MEMBERS = [ASSIGNEE_NAME, TEAMMATE_NAME]

NOW = datetime(2026, 10, 15, 15, 0, tzinfo=timezone.utc)


def days_ago(days, base=NOW):
    return base - timedelta(days=days)


def jira_ts(value):
    """Format an aware datetime the way the Jira REST API does."""
    return value.strftime("%Y-%m-%dT%H:%M:%S.000%z")


def adf(text, quoted=None):
    """
    Build an ADF body. When `quoted` is given it is wrapped in a blockquote,
    which classification must treat as context rather than a new assertion.
    """
    content = []
    if quoted:
        content.append(
            {
                "type": "blockquote",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": quoted}],
                    }
                ],
            }
        )
    content.append(
        {"type": "paragraph", "content": [{"type": "text", "text": text}]}
    )
    return {"type": "doc", "version": 1, "content": content}


def adf_with_mention(text, mention_name):
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "mention", "attrs": {"text": f"@{mention_name}"}},
                    {"type": "text", "text": f" {text}"},
                ],
            }
        ],
    }


def comment(
    text,
    author_id=ASSIGNEE_ID,
    author_name=ASSIGNEE_NAME,
    created=None,
    updated=None,
    comment_id=None,
    quoted=None,
    body=None,
    visibility=None,
):
    created = created or days_ago(1)
    payload = {
        "id": comment_id or f"c{abs(hash((text, author_id, created))) % 100000}",
        "author": {"accountId": author_id, "displayName": author_name},
        "body": body if body is not None else adf(text, quoted=quoted),
        "created": jira_ts(created),
        "updated": jira_ts(updated or created),
        "updateAuthor": {"accountId": author_id, "displayName": author_name},
    }
    if visibility:
        payload["visibility"] = visibility
    return payload


def status_change(
    to_status, created, from_status="Open", author_id=ASSIGNEE_ID, author_name=ASSIGNEE_NAME
):
    return {
        "created": jira_ts(created),
        "author": {"accountId": author_id, "displayName": author_name},
        "items": [
            {"field": "status", "fromString": from_status, "toString": to_status}
        ],
    }


def assignment_change(
    to_account_id,
    to_display,
    created,
    author_id=MANAGER_ID,
    author_name=MANAGER_NAME,
):
    return {
        "created": jira_ts(created),
        "author": {"accountId": author_id, "displayName": author_name},
        "items": [
            {
                "field": "assignee",
                "to": to_account_id,
                "toString": to_display,
            }
        ],
    }


def field_change(field, created, author_id=ASSIGNEE_ID, author_name=ASSIGNEE_NAME):
    """A non-status, non-assignee edit (label, priority) -- never progress."""
    return {
        "created": jira_ts(created),
        "author": {"accountId": author_id, "displayName": author_name},
        "items": [{"field": field, "fromString": "a", "toString": "b"}],
    }


def issue(
    key="CSSD-1001",
    status="In Progress",
    status_id="3",
    issue_type="Service Request",
    issue_type_id="10100",
    project_key=None,
    project_id="20001",
    assignee_id=ASSIGNEE_ID,
    assignee_name=ASSIGNEE_NAME,
    summary="Standish DB - RP - rebuild the quarterly PCAP export",
    created=None,
    updated=None,
    remaining_seconds=None,
    original_seconds=None,
    priority="Medium",
    include_type_metadata=True,
    include_project_metadata=True,
):
    created = created or days_ago(30)
    updated = updated or days_ago(1)
    project_key = project_key or key.split("-")[0]

    fields = {
        "summary": summary,
        "status": {
            "id": status_id,
            "name": status,
            "statusCategory": {"name": "In Progress"},
        },
        "created": jira_ts(created),
        "updated": jira_ts(updated),
        "priority": {"id": "3", "name": priority},
        "timeoriginalestimate": original_seconds,
        "timeestimate": remaining_seconds,
        "reporter": {"accountId": CLIENT_ID, "displayName": CLIENT_NAME},
    }
    fields["assignee"] = (
        {"accountId": assignee_id, "displayName": assignee_name}
        if assignee_id or assignee_name
        else None
    )
    if include_project_metadata:
        fields["project"] = {"id": project_id, "key": project_key}
    if include_type_metadata:
        fields["issuetype"] = {"id": issue_type_id, "name": issue_type}
    else:
        fields["issuetype"] = {}

    return {"id": f"1{abs(hash(key)) % 10000}", "key": key, "fields": fields}


def policy(**overrides):
    """
    Test policy mirroring workload_policy.json, plus the tenant identities
    that the shipped configuration intentionally leaves empty.
    """
    base = {
        "policy_version": "test-policy-v1",
        "exclusion_rules": [
            {
                "id": "iap_administrative_changes",
                "projects": ["IAP"],
                "issue_type_ids": [],
                "issue_type_names": ["Change Request", "[System] Change"],
                "reason": "Administrative change record; excluded from workload only",
            }
        ],
        "status_defaults_by_name": {
            "Open": workload.ACTIVE_READY,
            "Scoping": workload.ACTIVE_READY,
            "In Progress": workload.ACTIVE_IN_PROGRESS,
            "Pending Client": workload.WAITING_CLIENT_INPUT,
            "Client Review": workload.WAITING_CLIENT_ACCEPTANCE,
            "Sensitive Client Review": workload.WAITING_CLIENT_ACCEPTANCE,
            "Waiting for Approval": workload.REVIEW_REQUIRED,
            "Pending": workload.REVIEW_REQUIRED,
            "Peer Review": workload.REVIEW_REQUIRED,
        },
        "internal_account_ids": [MANAGER_ID],
        "client_approver_ids_by_client": {"Standish Management": [CLIENT_ID]},
        "automation_account_ids": [BOT_ID],
    }
    merged = workload._deep_merge(workload.DEFAULT_POLICY, base)
    merged = workload._deep_merge(merged, overrides)
    return workload.validate_policy(merged)


def identity(team_members=None, extra_accounts=None, current_policy=None):
    current_policy = current_policy or policy()
    mapping = {
        ASSIGNEE_NAME: {ASSIGNEE_ID},
        TEAMMATE_NAME: {TEAMMATE_ID},
    }
    mapping.update(extra_accounts or {})
    approvers = set()
    for ids in (current_policy.get("client_approver_ids_by_client") or {}).values():
        approvers.update(ids)
    return workload.Identity(
        team_display_names=team_members or TEAM_MEMBERS,
        display_to_account_ids=mapping,
        internal_account_ids=current_policy.get("internal_account_ids"),
        client_approver_ids=approvers,
        automation_account_ids=current_policy.get("automation_account_ids"),
    )


def classify(
    issue_payload,
    comments=(),
    histories=(),
    current_policy=None,
    current_identity=None,
    overrides=None,
    now=NOW,
    comments_complete=True,
    changelog_complete=True,
):
    """Classify one fixture issue and return just the workload fields."""
    current_policy = current_policy or policy()
    current_identity = current_identity or identity(current_policy=current_policy)
    source = workload.build_workload_source(
        issue_payload,
        comments=list(comments),
        histories=list(histories),
        comments_complete=comments_complete,
        changelog_complete=changelog_complete,
    )
    return workload.classify_workload(
        source, current_policy, current_identity, overrides=overrides, now=now
    )


def row(
    key="CSSD-1001",
    assignee=ASSIGNEE_NAME,
    state=workload.ACTIVE_IN_PROGRESS,
    active_units=1.0,
    provisional_units=0.0,
    started=True,
    first_response_overdue=False,
    progress_overdue=False,
    **extra,
):
    """A minimal classified row for aggregation tests."""
    data = {
        "key": key,
        "assignee": assignee,
        "summary": f"{key} summary",
        "workload_state": state,
        "workload_state_label": workload.STATE_LABELS.get(state, state),
        "include_in_active_capacity": state in workload.ACTIVE_STATES,
        "active_units": active_units if state in workload.ACTIVE_STATES else 0.0,
        "provisional_units": (
            provisional_units if state == workload.REVIEW_REQUIRED else 0.0
        ),
        "effort_source": "remaining_estimate",
        "started": started,
        "first_response_overdue": first_response_overdue,
        "progress_overdue": progress_overdue,
        "client_followup_due": False,
        "review_for_reassignment": False,
        "source_history_complete": True,
        "classification_stale": False,
        "priority_name": "Medium",
    }
    data["planning_units"] = data["active_units"] + data["provisional_units"]
    data.update(extra)
    return data
