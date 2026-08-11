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
  red    = 3+ days (this is the "stale" / needs-attention flag)

There's also a separate "ready_to_close" category: tickets where the most
recent comment sounds like the client/requester signing off (e.g. "looks
good", "thanks, that works"). Those get pulled OUT of red/yellow -- work
sitting untouched after a sign-off isn't the team dropping the ball, it's
just an unclosed ticket, so it shouldn't read as urgent.

And a separate "needs_status_update" flag: tickets still sitting on the
generic "Open" status where a team member has actually commented. The rule
is deliberately simple: a ticket is only legitimately "Open" if none of us
has touched it yet. The moment anyone on the team comments -- whatever they
say -- that's real work happening, so the status field is now wrong and
someone should move it to whatever it should actually say (Scoping,
Pending Client, In Development...). This isn't about urgency, it's a
data-hygiene nudge.

Every ticket also gets a "responsible_party": Client, Team member, or
Internal -- whoever needs to take the next action, based on its status
(see STATUS_RESPONSIBLE_PARTY). A ticket is "urgent" when it's red AND the
next action is on us (Team member or Internal) rather than the client --
being red because we're waiting on the client isn't the team dropping the
ball.

And a "needs_estimate" flag: the ticket's most recent comment is from one
of ESTIMATE_REQUESTERS (commonly Henry Glubb) and mentions an estimate --
i.e. someone asked for one and nobody's replied yet. Checking only the
latest comment means this clears itself automatically once a team member
responds, same pattern as ready_to_close.
"""

from datetime import datetime, timezone

import jira_client

YELLOW_AFTER_DAYS = 2
RED_AFTER_DAYS = 3

# Statuses excluded from every view by default -- these mean the work is
# already done on our end and is just sitting in a separate implementation/
# scheduling process, so they're noise for staleness purposes. Still
# reachable by explicitly passing them via check_staleness.py's --status.
DEFAULT_EXCLUDED_STATUSES = ["Awaiting Implementation", "Scheduling Acceptance"]

# Simple keyword heuristic for "the client/requester sounds satisfied and
# this ticket is probably just waiting to be formally closed." This is NOT
# smart -- it's a plain substring match against the latest comment, so it
# will miss phrasings we haven't listed and can occasionally misfire on a
# comment that happens to contain one of these phrases in a different
# context. Treat "Ready to Close" as a worth-a-look list, not gospel, and
# add/remove phrases here as you see false positives or misses.
CLOSURE_SIGNAL_PHRASES = [
    "looks good",
    "look good",
    "all good",
    "sounds good",
    "that works",
    "this works",
    "works great",
    "perfect, thank",
    "thanks, this works",
    "resolved",
    "all set",
    "no further",
    "please close",
    "closing this",
    "confirmed working",
    "everything is good",
    "everything looks good",
    "that's all we needed",
    "this is exactly what we needed",
]

# Maps a Jira status name (lowercased) to who needs to act next: "Client"
# (the client/requester owes a response), "Team member" (the assignee
# needs to act), or "Internal" (needs attention from someone other than
# the assignee, e.g. an unowned ticket needing triage). Anything not
# listed here defaults to "Team member" in responsible_party() below.
# These are guesses based on the status names seen so far -- correct this
# mapping as you spot statuses classified wrong.
STATUS_RESPONSIBLE_PARTY = {
    "pending client": "Client",
    "client review": "Client",
    "waiting for approval": "Client",
    "pending": "Client",
    "scheduling acceptance": "Internal",
}


def responsible_party(status_name, assignee_name):
    """Who needs to take the next action: 'Client', 'Team member', or 'Internal'."""
    if assignee_name is None:
        return "Internal"
    return STATUS_RESPONSIBLE_PARTY.get(status_name.lower(), "Team member")


# People whose comments count as an estimate request. Add more names here
# if others start requesting estimates the same way.
ESTIMATE_REQUESTERS = ["Henry Glubb"]


def parse_jira_datetime(value):
    """Jira timestamps look like '2026-08-11T13:52:10.254-0400'."""
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")


def adf_to_text(node):
    """
    Comment bodies come back as Atlassian Document Format (a nested JSON
    structure), not plain text. This walks it and pulls out just the text.
    """
    if isinstance(node, dict):
        parts = [node.get("text", "")]
        parts.extend(adf_to_text(child) for child in node.get("content", []))
        return " ".join(p for p in parts if p)
    if isinstance(node, list):
        return " ".join(adf_to_text(item) for item in node)
    return ""


def last_status_change(histories):
    """Latest timestamp where the 'status' field actually changed, or None."""
    latest = None
    for history in histories:
        if any(item["field"] == "status" for item in history["items"]):
            when = parse_jira_datetime(history["created"])
            if latest is None or when > latest:
                latest = when
    return latest


def last_comment_time(comments):
    """Latest comment timestamp on the ticket, or None if there are none."""
    if not comments:
        return None
    return max(parse_jira_datetime(c["created"]) for c in comments)


def latest_comment_text(comments):
    """Plain text of the single most recent comment, or "" if there are none."""
    if not comments:
        return ""
    latest = max(comments, key=lambda c: parse_jira_datetime(c["created"]))
    return adf_to_text(latest.get("body"))


def looks_like_closure_signal(comment_text):
    lowered = comment_text.lower()
    return any(phrase in lowered for phrase in CLOSURE_SIGNAL_PHRASES)


def find_team_comment(comments, team_members):
    """
    Returns the first comment (in Jira's returned order, oldest first)
    authored by one of team_members (matched case-insensitively against
    Jira display names), or None if no team member has commented.
    """
    team_members_lower = {name.lower() for name in team_members}
    for comment in comments:
        author = comment.get("author", {}).get("displayName", "")
        if author.lower() in team_members_lower:
            return comment
    return None


def find_estimate_request(comments, requesters):
    """
    If the ticket's most recent comment is from one of `requesters` and
    mentions an estimate, returns that comment. Otherwise returns None --
    including when someone else has replied more recently, since that
    means the request has already been addressed.
    """
    if not comments:
        return None
    latest = max(comments, key=lambda c: parse_jira_datetime(c["created"]))
    author = latest.get("author", {}).get("displayName", "")
    if author not in requesters:
        return None
    text = adf_to_text(latest.get("body"))
    if "estimat" not in text.lower():  # covers estimate/estimation/estimating
        return None
    return latest


def real_last_activity(issue, histories, comments):
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

    comment_time = last_comment_time(comments)
    if comment_time:
        candidates.append(comment_time)

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


def build_report(project_keys=None, statuses=None):
    """
    Pulls every open (not-Done) ticket for the tracked team/projects, works
    out each one's real last-activity date, and classifies it. Returns a
    list of dicts, worst (most stale) first.

    "category" is what should drive the dashboard/digest display:
    red / yellow / green for normal staleness, or ready_to_close when the
    latest comment sounds like a sign-off (see CLOSURE_SIGNAL_PHRASES).

    "needs_status_update" is a separate True/False flag: the ticket's
    status is literally "Open" but a team member (per JIRA_TEAM_MEMBERS)
    has commented on it. A ticket can be both, e.g. red AND needing a
    status update.

    Pass project_keys (e.g. ["CSSD"] or ["CSSD", "IAP"]) to check only
    specific projects instead of every project in JIRA_PROJECT_KEYS.

    Pass statuses (e.g. ["Open"] or ["Open", "Scoping"]) to check only
    tickets currently sitting in those exact Jira statuses, instead of
    every non-Done status. When statuses isn't given, DEFAULT_EXCLUDED_STATUSES
    are left out automatically; passing statuses explicitly (including one
    of those excluded ones) overrides that default.
    """
    extra_jql = "statusCategory != Done"
    if statuses:
        quoted_statuses = ", ".join(f'"{status}"' for status in statuses)
        extra_jql += f" AND status in ({quoted_statuses})"
    elif DEFAULT_EXCLUDED_STATUSES:
        quoted_excluded = ", ".join(f'"{status}"' for status in DEFAULT_EXCLUDED_STATUSES)
        extra_jql += f" AND status not in ({quoted_excluded})"

    issues = jira_client.get_tickets(project_keys=project_keys, extra_jql=extra_jql)

    report = []
    for issue in issues:
        histories = jira_client.get_changelog(issue["key"])
        comments = jira_client.get_comments(issue["key"])

        last_activity = real_last_activity(issue, histories, comments)
        level, days = staleness_level(last_activity)

        comment_text = latest_comment_text(comments)
        ready_to_close = looks_like_closure_signal(comment_text)
        category = "ready_to_close" if ready_to_close else level

        fields = issue["fields"]
        status_name = fields["status"]["name"]

        team_comment = None
        if status_name == "Open":
            team_comment = find_team_comment(comments, jira_client.TEAM_MEMBERS)
        needs_status_update = team_comment is not None
        progress_comment = adf_to_text(team_comment.get("body")) if team_comment else None
        progress_comment_author = team_comment["author"]["displayName"] if team_comment else None
        progress_comment_date = team_comment["created"] if team_comment else None

        assignee = fields.get("assignee")
        assignee_name = assignee["displayName"] if assignee else None
        party = responsible_party(status_name, assignee_name)
        urgent = category == "red" and party != "Client"

        estimate_request = find_estimate_request(comments, ESTIMATE_REQUESTERS)
        needs_estimate = estimate_request is not None
        estimate_request_text = adf_to_text(estimate_request.get("body")) if estimate_request else None
        estimate_request_by = estimate_request["author"]["displayName"] if estimate_request else None
        estimate_request_date = estimate_request["created"] if estimate_request else None

        report.append(
            {
                "key": issue["key"],
                "summary": fields["summary"],
                "status": status_name,
                "assignee": assignee_name or "Unassigned",
                "last_real_activity": last_activity,
                "days_inactive": round(days, 1),
                "level": level,
                "category": category,
                "latest_comment": comment_text,
                "needs_status_update": needs_status_update,
                "progress_comment": progress_comment,
                "progress_comment_author": progress_comment_author,
                "progress_comment_date": progress_comment_date,
                "responsible_party": party,
                "urgent": urgent,
                "needs_estimate": needs_estimate,
                "estimate_request_text": estimate_request_text,
                "estimate_request_by": estimate_request_by,
                "estimate_request_date": estimate_request_date,
            }
        )

    category_order = {"red": 0, "yellow": 1, "ready_to_close": 2, "green": 3}
    report.sort(key=lambda r: (category_order[r["category"]], -r["days_inactive"]))
    return report
