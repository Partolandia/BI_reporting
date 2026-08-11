"""
Business logic for deciding whether a ticket is stale.

The generic "status" column can lag behind reality: someone might leave a
ticket sitting on "Open" even after sending an estimate, getting client
approval, or starting development. So staleness isn't "days since the
status column changed" -- it's days since ANYTHING real happened: a status
change, a comment, or (for a brand new ticket) its creation.

Thresholds:
  green  = less than 2 days since real last activity
  yellow = 2 to 3 days
  red    = 3+ days (this is the "stale" flag)
"""

from datetime import datetime, timezone

import jira_client

YELLOW_AFTER_DAYS = 2
RED_AFTER_DAYS = 3


def parse_jira_datetime(value):
    """Jira timestamps look like '2026-08-11T13:52:10.254-0400'."""
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")


def last_status_change(histories):
    """Latest timestamp where the 'status' field actually changed, or None."""
    latest = None
    for history in histories:
        if any(item["field"] == "status" for item in history["items"]):
            when = parse_jira_datetime(history["created"])
            if latest is None or when > latest:
                latest = when
    return latest


def last_comment(fields):
    """Latest comment timestamp on the ticket, or None if there are none."""
    comments = fields.get("comment", {}).get("comments", [])
    if not comments:
        return None
    return max(parse_jira_datetime(c["created"]) for c in comments)


def real_last_activity(issue, histories):
    """
    The most recent point of real activity on a ticket: whichever is most
    recent out of its creation date, its last actual status change, and its
    last comment.
    """
    fields = issue["fields"]
    candidates = [parse_jira_datetime(fields["created"])]

    status_change = last_status_change(histories)
    if status_change:
        candidates.append(status_change)

    comment = last_comment(fields)
    if comment:
        candidates.append(comment)

    return max(candidates)


def staleness_level(last_activity, now=None):
    """Returns ('green' | 'yellow' | 'red', days_since_activity)."""
    now = now or datetime.now(timezone.utc)
    days = (now - last_activity).total_seconds() / 86400

    if days >= RED_AFTER_DAYS:
        return "red", days
    if days >= YELLOW_AFTER_DAYS:
        return "yellow", days
    return "green", days


def build_report():
    """
    Pulls every open (not-Done) ticket for the tracked team/projects, works
    out each one's real last-activity date, and classifies it green/yellow/red.
    Returns a list of dicts, worst (most stale) first.
    """
    issues = jira_client.get_tickets(extra_jql="statusCategory != Done")

    report = []
    for issue in issues:
        histories = jira_client.get_changelog(issue["key"])
        last_activity = real_last_activity(issue, histories)
        level, days = staleness_level(last_activity)

        fields = issue["fields"]
        assignee = fields.get("assignee")
        report.append(
            {
                "key": issue["key"],
                "summary": fields["summary"],
                "status": fields["status"]["name"],
                "assignee": assignee["displayName"] if assignee else "Unassigned",
                "last_real_activity": last_activity,
                "days_inactive": round(days, 1),
                "level": level,
            }
        )

    level_order = {"red": 0, "yellow": 1, "green": 2}
    report.sort(key=lambda r: (level_order[r["level"]], -r["days_inactive"]))
    return report
