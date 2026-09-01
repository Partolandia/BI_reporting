"""
Debug helper: pulls one ticket's comments and shows exactly what the
Needs Estimate logic sees, so we can figure out why a ticket isn't
getting flagged the way you expect.

    python inspect_ticket.py CSSD-6596
"""

import sys

import jira_client
import staleness as s


def main():
    if len(sys.argv) < 2:
        print("Usage: python inspect_ticket.py TICKET-KEY")
        sys.exit(1)

    key = sys.argv[1].upper()
    comments = jira_client.get_comments(key)

    print(f"{key}: {len(comments)} comment(s)\n")
    for comment in sorted(comments, key=lambda c: c["created"]):
        author = comment.get("author", {}).get("displayName", "?")
        text = s.adf_to_text(comment.get("body"))
        print(f"[{comment['created']}] {author}:")
        print(f"    {text}\n")

    print("=" * 70)
    print(f"ESTIMATE_REQUESTERS = {s.ESTIMATE_REQUESTERS}")
    print(f"ESTIMATE_REQUEST_PHRASES = {s.ESTIMATE_REQUEST_PHRASES}")
    print(f"TEAM_MEMBERS = {jira_client.TEAM_MEMBERS}")
    print()

    result = s.find_estimate_request(comments, s.ESTIMATE_REQUESTERS, jira_client.TEAM_MEMBERS)
    if result:
        author = result["author"]["displayName"]
        print(f"NEEDS ESTIMATE: YES -- triggered by {author} at {result['created']}")
    else:
        print("NEEDS ESTIMATE: no")
        print(
            "Reasons this could be 'no': no comment author exactly matches "
            "ESTIMATE_REQUESTERS, no comment text matches ESTIMATE_REQUEST_PHRASES, "
            "or a TEAM_MEMBERS comment came after the request."
        )


if __name__ == "__main__":
    main()
