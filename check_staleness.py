"""
Run this to see the "At Risk" view: every open ticket for the team,
classified red/yellow/green by real activity, plus a separate "Ready to
Close" list for tickets where the latest comment sounds like a sign-off.

    python check_staleness.py
    python check_staleness.py CSSD
    python check_staleness.py CSSD,IAP

With no argument, checks every project in JIRA_PROJECT_KEYS. Pass one or
more project keys (comma-separated, no spaces) to check just those.

This takes longer than test_connection.py -- it fetches each open ticket's
full change history AND comments one at a time, so expect roughly 1-2
seconds per ticket.
"""

import sys

import jira_client
from staleness import build_report

CATEGORY_LABELS = {
    "red": "RED",
    "yellow": "YELLOW",
    "ready_to_close": "READY TO CLOSE",
    "green": "GREEN",
}
CATEGORY_ORDER = ["red", "yellow", "ready_to_close", "green"]


def parse_project_keys(argv):
    if len(argv) < 2:
        return None

    requested = [key.strip().upper() for key in argv[1].split(",") if key.strip()]
    unknown = [key for key in requested if key not in jira_client.PROJECT_KEYS]
    if unknown:
        print(
            f"Warning: {', '.join(unknown)} not in JIRA_PROJECT_KEYS "
            f"({', '.join(jira_client.PROJECT_KEYS)}) -- checking anyway, "
            "but you may get zero results if the key is wrong.\n"
        )
    return requested


def main():
    project_keys = parse_project_keys(sys.argv)
    scope = ", ".join(project_keys) if project_keys else ", ".join(jira_client.PROJECT_KEYS)
    print(f"Checking projects: {scope}")
    print("Pulling open tickets, change history, and comments -- this may take a few minutes...\n")
    report = build_report(project_keys=project_keys)

    counts = {c: 0 for c in CATEGORY_ORDER}
    for row in report:
        counts[row["category"]] += 1

    print(f"{len(report)} open tickets checked.")
    print(f"  RED (3+ days, no real activity):    {counts['red']}")
    print(f"  YELLOW (2-3 days, no real activity): {counts['yellow']}")
    print(f"  READY TO CLOSE (sign-off, unclosed): {counts['ready_to_close']}")
    print(f"  GREEN (active):                      {counts['green']}\n")

    for category in CATEGORY_ORDER:
        rows = [r for r in report if r["category"] == category]
        if not rows:
            continue

        print(f"\n=== {CATEGORY_LABELS[category]} ({len(rows)}) ===")
        print(f"{'Key':<10} {'Days':<6} {'Status':<20} {'Assignee':<22} Summary")
        print("-" * 100)
        for row in rows:
            print(
                f"{row['key']:<10} {row['days_inactive']:<6} {row['status']:<20} "
                f"{row['assignee']:<22} {row['summary'][:40]}"
            )
            if category == "ready_to_close" and row["latest_comment"]:
                print(f"           -> last comment: {row['latest_comment'][:90]}")


if __name__ == "__main__":
    main()
