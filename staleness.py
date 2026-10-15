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

And a "needs_estimate" flag: someone in ESTIMATE_REQUESTERS (commonly
Henry Glubb) has left a comment that looks like a new estimate/scoping
request (see ESTIMATE_REQUEST_PHRASES), checked across ALL of their
comments, not just the latest, since the request can be several comments
back with quieter discussion after it. It clears once a TEAM MEMBER has
commented anything after that request -- we don't try to verify they
actually sent the estimate, just that someone responded, same
simplification used for needs_status_update.

This needs BOTH authorship and content, not authorship alone: requesters
like Henry also do ongoing hands-on work on a ticket once it's past the
estimate stage -- testing their own changes, configuring things, talking
directly to the client -- and none of that is a new request just because
it happens to be his most recent comment (see CSSD-6209, where the actual
estimate request was properly handled back in May, but a later, unrelated
comment from Henry -- "I updated X, can you test and confirm" -- kept
re-triggering the flag under an authorship-only rule).

Each row also carries the assignment facts this module already derives --
when the ticket was most recently assigned to its current assignee
(find_assignment_date) and whether that person has done anything on it
since (last_assignee_activity). _attach_workload_fields() hands those to
workload.py, which decides what is actionable; this module stays out of
that decision.
"""

import re
import time
from datetime import datetime, timezone
from pathlib import Path

import jira_client
import workload

WORKLOAD_POLICY_PATH = Path(__file__).parent / "workload_policy.json"
WORKLOAD_OVERRIDES_PATH = Path(__file__).parent / "workload_overrides.json"

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


# People whose comments count as an estimate request when they also match
# ESTIMATE_REQUEST_PHRASES below. Add more names here if others start
# requesting estimates the same way.
ESTIMATE_REQUESTERS = ["Henry Glubb"]

# Substring match (case-insensitive) against a requester's comment that
# means "this is a new estimate/scoping request" -- as opposed to routine
# back-and-forth on a ticket that's already past that stage (testing,
# clarifying questions, status updates). Covers estimate/estimation,
# "assign" (Henry's other common phrasing when kicking off a request), and
# "best way forward" (seen on a real request that used neither of those
# words). Built from real examples, not guessed -- add more phrasings here
# as you spot requests this misses, but keep new entries specific to
# "asking the team to scope/estimate something new," not just anything
# Henry might say.
ESTIMATE_REQUEST_PHRASES = ["estimat", "assign", "best way forward"]

# Issue types excluded from Team Workload only (staleness views still include them).
# IAP "change requests" use issuetype "[System] Change" in Jira; older caches
# inferred "Change Request" from the summary text.
WORKLOAD_EXCLUDED_ISSUE_TYPES_BY_PROJECT = {
    "IAP": {"Change Request", "[System] Change"},
}

# Cached reports saved before issue_type was added won't have the field until
# the next Jira refresh. IAP Change Requests usually include the phrase in the
# summary (e.g. "Business Intelligence - Change Request - ...").
_IAP_CHANGE_REQUEST_SUMMARY = re.compile(r"\bchange request\b", re.IGNORECASE)


def _infer_issue_type(row):
    """Best-effort issue type for legacy cache rows missing issuetype.name."""
    project = row["key"].split("-", 1)[0]
    if project == "IAP" and _IAP_CHANGE_REQUEST_SUMMARY.search(row.get("summary") or ""):
        return "Change Request"
    return None


def backfill_issue_types(report):
    """Fill issue_type on in-memory rows when we can infer it. Returns True if any row changed."""
    changed = False
    for row in report:
        if row.get("issue_type"):
            continue
        inferred = _infer_issue_type(row)
        if inferred:
            row["issue_type"] = inferred
            changed = True
    return changed


def _issue_type_for_workload(row):
    return row.get("issue_type") or _infer_issue_type(row)


def exclude_from_workload(row):
    """True if this ticket should not count toward /workload balances."""
    project = row["key"].split("-", 1)[0]
    excluded_types = WORKLOAD_EXCLUDED_ISSUE_TYPES_BY_PROJECT.get(project)
    if not excluded_types:
        return False
    issue_type = _issue_type_for_workload(row)
    if not issue_type:
        return False
    excluded_lower = {t.lower() for t in excluded_types}
    return issue_type.lower() in excluded_lower


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


def author_name(entry):
    """
    Safe accessor for a comment or changelog history entry's author display
    name. Jira sometimes records an entry (certain automation-driven
    changes in particular) with "author": null rather than just omitting
    the key, so entry.get("author", {}) isn't enough -- that default only
    kicks in when the key is missing, not when it's present but None.
    """
    return (entry.get("author") or {}).get("displayName", "")


def last_status_change(histories):
    """Latest timestamp where the 'status' field actually changed, or None."""
    latest = None
    for history in histories:
        if any(item["field"] == "status" for item in history["items"]):
            when = parse_jira_datetime(history["created"])
            if latest is None or when > latest:
                latest = when
    return latest


def find_assignment_date(histories, assignee_name):
    """
    Most recent changelog entry where the 'assignee' field was changed TO
    assignee_name. Returns (assigned_date, assigned_by) -- the history
    entry's author, whoever that was, not necessarily a specific person --
    or (None, None) if the ticket's changelog has no such entry (it was
    assigned to them at creation and never reassigned since, so there's no
    changelog record of it).
    """
    latest = None
    assigned_by = None
    for history in histories:
        for item in history["items"]:
            if item["field"] == "assignee" and item.get("toString") == assignee_name:
                when = parse_jira_datetime(history["created"])
                if latest is None or when > latest:
                    latest = when
                    assigned_by = author_name(history) or None
    return latest, assigned_by


def last_assignee_activity(histories, comments, assignee_name, after):
    """
    True if assignee_name has done anything -- commented, or changed any
    field -- on the ticket after the `after` timestamp. Used to tell
    whether someone has actually started working on a ticket since it was
    assigned to them.
    """
    for history in histories:
        if author_name(history) == assignee_name and parse_jira_datetime(history["created"]) > after:
            return True
    for comment in comments:
        if author_name(comment) == assignee_name and parse_jira_datetime(comment["created"]) > after:
            return True
    return False


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
        if author_name(comment).lower() in team_members_lower:
            return comment
    return None


def find_estimate_request(comments, requesters, team_members):
    """
    Finds the most recent comment from someone in `requesters` (e.g. Henry
    Glubb) that matches ESTIMATE_REQUEST_PHRASES and has had no reply from
    a TEAM MEMBER since. Checked across ALL comments, not just the latest,
    since the request can be several comments back. Returns None if no
    such request exists, or if a team member has replied to it already.

    Requires both authorship AND content match -- authorship alone is too
    broad, since requesters like Henry also do ongoing hands-on work on a
    ticket once it's past the estimate stage (testing, configuring,
    talking directly to the client), and that routine involvement isn't a
    new request just because it happens to be his most recent comment.
    """
    requesters_lower = {name.lower() for name in requesters}
    team_members_lower = {name.lower() for name in team_members}

    ordered = sorted(comments, key=lambda c: parse_jira_datetime(c["created"]))

    last_request = None
    for comment in ordered:
        author_lower = author_name(comment).lower()

        if author_lower in requesters_lower:
            text = adf_to_text(comment.get("body")).lower()
            if any(phrase in text for phrase in ESTIMATE_REQUEST_PHRASES):
                last_request = comment
        elif last_request is not None and author_lower in team_members_lower:
            last_request = None  # a team member replied after the request

    return last_request


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


_TICKET_FETCH_ATTEMPTS = 4


def _report_row_from_issue(issue, histories, comments):
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

    estimate_request = find_estimate_request(comments, ESTIMATE_REQUESTERS, jira_client.TEAM_MEMBERS)
    needs_estimate = estimate_request is not None
    estimate_request_text = adf_to_text(estimate_request.get("body")) if estimate_request else None
    estimate_request_by = estimate_request["author"]["displayName"] if estimate_request else None
    estimate_request_date = estimate_request["created"] if estimate_request else None

    assigned_date = None
    assigned_by = None
    touched_since_assignment = True
    if assignee_name:
        assigned_date, assigned_by = find_assignment_date(histories, assignee_name)
        if assigned_date is None:
            assigned_date = parse_jira_datetime(fields["created"])
        touched_since_assignment = last_assignee_activity(
            histories, comments, assignee_name, assigned_date
        )
    days_since_assignment = (
        round((datetime.now(timezone.utc) - assigned_date).total_seconds() / 86400, 1)
        if assigned_date
        else None
    )

    return {
        "key": issue["key"],
        "issue_type": fields["issuetype"]["name"],
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
        "assigned_date": assigned_date,
        "assigned_by": assigned_by,
        "touched_since_assignment": touched_since_assignment,
        "days_since_assignment": days_since_assignment,
        "fetch_degraded": False,
    }


def _degraded_report_row(issue):
    """
    Fallback when changelog/comments cannot be fetched (e.g. transient SSL).
    Uses Jira's generic updated timestamp so the refresh can still complete.
    """
    fields = issue["fields"]
    status_name = fields["status"]["name"]
    assignee = fields.get("assignee")
    assignee_name = assignee["displayName"] if assignee else None
    last_activity = parse_jira_datetime(fields["updated"])
    level, days = staleness_level(last_activity)
    party = responsible_party(status_name, assignee_name)

    return {
        "key": issue["key"],
        "issue_type": fields["issuetype"]["name"],
        "summary": fields["summary"],
        "status": status_name,
        "assignee": assignee_name or "Unassigned",
        "last_real_activity": last_activity,
        "days_inactive": round(days, 1),
        "level": level,
        "category": level,
        "latest_comment": "",
        "needs_status_update": False,
        "progress_comment": None,
        "progress_comment_author": None,
        "progress_comment_date": None,
        "responsible_party": party,
        "urgent": level == "red" and party != "Client",
        "needs_estimate": False,
        "estimate_request_text": None,
        "estimate_request_by": None,
        "estimate_request_date": None,
        "assigned_date": None,
        "assigned_by": None,
        "touched_since_assignment": True,
        "days_since_assignment": None,
        "fetch_degraded": True,
    }


def _attach_workload_fields(
    row,
    issue,
    comments,
    histories,
    policy,
    identity,
    overrides,
    history_complete,
    now=None,
):
    """
    Add the workload-specific fields alongside the shared staleness values.

    Nothing here rewrites an existing staleness field: touched_since_assignment
    and friends keep their current meaning for Team Overview, the person and
    project views, and the Slack digest. The normalized source record is kept
    on the row so a policy change can reclassify from cache without another
    Jira pull.
    """
    source = workload.build_workload_source(
        issue,
        comments=comments or [],
        histories=histories or [],
        comments_complete=history_complete,
        changelog_complete=history_complete,
    )
    fields = workload.classify_workload(
        source, policy, identity, overrides=overrides, now=now
    )
    row["workload_source"] = source
    row["priority_name"] = source.get("priority_name")
    collisions = set(fields) & set(row)
    if collisions:
        raise RuntimeError(
            "workload fields would overwrite staleness fields: "
            + ", ".join(sorted(collisions))
        )
    row.update(fields)
    return row


def build_report(
    project_keys=None,
    statuses=None,
    warnings=None,
    workload_policy=None,
    workload_overrides=None,
    workload_identity=None,
    now=None,
):
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

    # Workload classification runs off the same pull -- no extra Jira calls.
    if workload_policy is None:
        workload_policy = workload.load_policy(WORKLOAD_POLICY_PATH)
    if workload_overrides is None:
        workload_overrides = workload.load_overrides(WORKLOAD_OVERRIDES_PATH)
    identity = workload_identity or workload.build_identity(
        issues, jira_client.TEAM_MEMBERS, workload_policy
    )

    report = []
    for issue in issues:
        row = None
        last_error = None
        histories = comments = None
        for attempt in range(_TICKET_FETCH_ATTEMPTS):
            try:
                histories = jira_client.get_changelog(issue["key"])
                comments = jira_client.get_comments(issue["key"])
                row = _report_row_from_issue(issue, histories, comments)
                break
            except Exception as exc:
                last_error = exc
                histories = comments = None
                if attempt < _TICKET_FETCH_ATTEMPTS - 1:
                    time.sleep(2.0 * (attempt + 1))
        complete = row is not None
        if row is None:
            row = _degraded_report_row(issue)
            if warnings is not None:
                warnings.append(f"{issue['key']}: {last_error}")

        _attach_workload_fields(
            row,
            issue,
            comments,
            histories,
            workload_policy,
            identity,
            workload_overrides,
            history_complete=complete,
            now=now,
        )
        report.append(row)

    category_order = {"red": 0, "yellow": 1, "ready_to_close": 2, "green": 3}
    report.sort(key=lambda r: (category_order[r["category"]], -r["days_inactive"]))
    return report
