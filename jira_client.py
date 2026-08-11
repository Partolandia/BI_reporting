"""
Connects to Jira Cloud and pulls tickets for the projects we track
(CSSD, PIE, ACP, IAP).

Auth: Jira Cloud uses "basic auth" over HTTPS, where the "password" is an API
token instead of your real password. Docs: id.atlassian.com/manage-profile/security/api-tokens

Search endpoint: Jira retired the old /rest/api/3/search endpoint in May 2025.
The current endpoint is POST /rest/api/3/search/jql, which pages results using
a nextPageToken instead of an offset.
"""

import os

import requests
from dotenv import load_dotenv

load_dotenv()

SITE_URL = os.environ["JIRA_SITE_URL"].rstrip("/")
EMAIL = os.environ["JIRA_EMAIL"]
API_TOKEN = os.environ["JIRA_API_TOKEN"]
PROJECT_KEYS = [key.strip() for key in os.environ["JIRA_PROJECT_KEYS"].split(",")]

# Only track these team members' tickets. Set JIRA_TEAM_MEMBERS in .env to
# their Jira display names exactly as they appear in Jira (comma-separated).
# Leave it blank/unset in .env to pull tickets for every assignee instead.
_team_members_raw = os.environ.get("JIRA_TEAM_MEMBERS", "")
TEAM_MEMBERS = [name.strip() for name in _team_members_raw.split(",") if name.strip()]

SEARCH_URL = f"{SITE_URL}/rest/api/3/search/jql"

# Fields we need for the dashboard: identity, ownership, and everything the
# staleness logic needs to judge real activity vs. a stale "Open" status
# label. Comments are NOT fetched here -- the search endpoint's embedded
# "comment" field doesn't reliably return full comment data, so we pull
# comments separately per ticket via get_comments() instead.
FIELDS = [
    "summary",
    "status",
    "assignee",
    "reporter",
    "issuetype",
    "priority",
    "created",
    "updated",
]


def _auth():
    return (EMAIL, API_TOKEN)


def get_tickets(project_keys=None, extra_jql=None, page_size=100):
    """
    Pull all tickets for the given project keys (defaults to PROJECT_KEYS).
    Returns a list of raw Jira issue dicts (as returned by the REST API).
    """
    project_keys = project_keys or PROJECT_KEYS
    project_list = ", ".join(project_keys)
    jql = f"project in ({project_list})"

    if TEAM_MEMBERS:
        quoted_names = ", ".join(f'"{name}"' for name in TEAM_MEMBERS)
        jql += f" AND assignee in ({quoted_names})"

    if extra_jql:
        jql += f" AND {extra_jql}"
    jql += " ORDER BY updated DESC"

    issues = []
    next_page_token = None

    while True:
        body = {
            "jql": jql,
            "maxResults": page_size,
            "fields": FIELDS,
        }
        if next_page_token:
            body["nextPageToken"] = next_page_token

        response = requests.post(SEARCH_URL, auth=_auth(), json=body, timeout=30)
        response.raise_for_status()
        data = response.json()

        issues.extend(data["issues"])

        if data.get("isLast", True) or not data.get("nextPageToken"):
            break
        next_page_token = data["nextPageToken"]

    return issues


def get_changelog(issue_key):
    """
    Pull the full change history for one ticket via
    GET /rest/api/3/issue/{key}/changelog: every field change ever made,
    with a timestamp. We use this to find the last time the "status" field
    actually changed, as opposed to the ticket's generic "updated" date
    (which can bump for reasons that aren't real progress).
    """
    url = f"{SITE_URL}/rest/api/3/issue/{issue_key}/changelog"
    histories = []
    start_at = 0

    while True:
        response = requests.get(
            url,
            auth=_auth(),
            params={"startAt": start_at, "maxResults": 100},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        histories.extend(data["values"])

        if data.get("isLast", True):
            break
        start_at += len(data["values"])

    return histories


def get_comments(issue_key):
    """
    Pull every comment on one ticket via GET /rest/api/3/issue/{key}/comment
    -- the dedicated, stable comments endpoint (as opposed to the "comment"
    field on search results, which doesn't reliably return full comment
    data on the current search API).
    """
    url = f"{SITE_URL}/rest/api/3/issue/{issue_key}/comment"
    comments = []
    start_at = 0

    while True:
        response = requests.get(
            url,
            auth=_auth(),
            params={"startAt": start_at, "maxResults": 100},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        comments.extend(data["comments"])

        start_at += len(data["comments"])
        if start_at >= data["total"]:
            break

    return comments


def summarize(issue):
    """Pull out the handful of fields we actually care about for display."""
    fields = issue["fields"]
    assignee = fields.get("assignee")
    return {
        "key": issue["key"],
        "type": fields["issuetype"]["name"],
        "summary": fields["summary"],
        "status": fields["status"]["name"],
        "assignee": assignee["displayName"] if assignee else "Unassigned",
        "updated": fields["updated"],
        "created": fields["created"],
    }
