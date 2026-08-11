"""
Run this to see the "At Risk" view: an URGENT section up top, then every
ticket classified red/yellow/green by real activity, plus separate "Ready
to Close" and "Status Needs Updating" lists. Every ticket also shows who's
responsible for the next action: Client, Team member, or Internal.

    python check_staleness.py
    python check_staleness.py CSSD
    python check_staleness.py CSSD,IAP
    python check_staleness.py --status Open
    python check_staleness.py CSSD --status Open,Scoping

With no arguments, checks every non-Done status across every project in
JIRA_PROJECT_KEYS. The first argument (optional) filters by project key,
comma-separated. --status filters by exact Jira status name(s),
comma-separated.

This takes longer than test_connection.py -- it fetches each ticket's full
change history AND comments one at a time, so expect roughly 1-2 seconds
per ticket.
"""

import argparse

import jira_client
from staleness import build_report

CATEGORY_LABELS = {
    "red": "RED",
    "yellow": "YELLOW",
    "ready_to_close": "READY TO CLOSE",
    "green": "GREEN",
}
CATEGORY_ORDER = ["red", "yellow", "ready_to_close", "green"]


def parse_args():
    parser = argparse.ArgumentParser(description="Check ticket staleness.")
    parser.add_argument(
        "projects",
        nargs="?",
        default=None,
        help="Comma-separated project keys, e.g. CSSD or CSSD,IAP (default: all)",
    )
    parser.add_argument(
        "--status",
        default=None,
        help="Comma-separated Jira status names, e.g. Open or Open,Scoping (default: all non-Done)",
    )
    return parser.parse_args()


def resolve_project_keys(raw):
    if not raw:
        return None
    requested = [key.strip().upper() for key in raw.split(",") if key.strip()]
    unknown = [key for key in requested if key not in jira_client.PROJECT_KEYS]
    if unknown:
        print(
            f"Warning: {', '.join(unknown)} not in JIRA_PROJECT_KEYS "
            f"({', '.join(jira_client.PROJECT_KEYS)}) -- checking anyway, "
            "but you may get zero results if the key is wrong.\n"
        )
    return requested


def resolve_statuses(raw):
    if not raw:
        return None
    return [status.strip() for status in raw.split(",") if status.strip()]


def print_row(row, category):
    flag = " [STATUS NEEDS UPDATING]" if row["needs_status_update"] else ""
    print(
        f"{row['key']:<10} {row['days_inactive']:<6} {row['status']:<20} "
        f"{row['responsible_party']:<12} {row['assignee']:<22} {row['summary'][:35]}{flag}"
    )
    if category == "ready_to_close" and row["latest_comment"]:
        print(f"           -> last comment: {row['latest_comment'][:90]}")
    if row["needs_status_update"]:
        print(f"           -> {row['progress_comment_author']} commented "
              f"({row['progress_comment_date']}): {row['progress_comment'][:90]}")


def main():
    args = parse_args()
    project_keys = resolve_project_keys(args.projects)
    statuses = resolve_statuses(args.status)

    project_scope = ", ".join(project_keys) if project_keys else ", ".join(jira_client.PROJECT_KEYS)
    status_scope = ", ".join(statuses) if statuses else "all non-Done statuses"
    print(f"Checking projects: {project_scope}")
    print(f"Checking statuses: {status_scope}")
    print("Pulling tickets, change history, and comments -- this may take a few minutes...\n")

    report = build_report(project_keys=project_keys, statuses=statuses)

    counts = {c: 0 for c in CATEGORY_ORDER}
    needs_status_update_count = 0
    urgent_rows = []
    for row in report:
        counts[row["category"]] += 1
        if row["needs_status_update"]:
            needs_status_update_count += 1
        if row["urgent"]:
            urgent_rows.append(row)

    print(f"{len(report)} tickets checked.")
    print(f"  URGENT (red, action is on us):       {len(urgent_rows)}")
    print(f"  RED (3+ days, no real activity):     {counts['red']}")
    print(f"  YELLOW (2-3 days, no real activity):  {counts['yellow']}")
    print(f"  READY TO CLOSE (sign-off, unclosed):  {counts['ready_to_close']}")
    print(f"  GREEN (active):                       {counts['green']}")
    print(f"  STATUS NEEDS UPDATING (stuck Open):   {needs_status_update_count}\n")

    if urgent_rows:
        print(f"\n### URGENT -- needs attention now ({len(urgent_rows)}) ###")
        print(f"{'Key':<10} {'Days':<6} {'Status':<20} {'Resp.':<12} {'Assignee':<22} Summary")
        print("=" * 110)
        for row in urgent_rows:
            print_row(row, "urgent")

    for category in CATEGORY_ORDER:
        rows = [r for r in report if r["category"] == category]
        if not rows:
            continue

        print(f"\n=== {CATEGORY_LABELS[category]} ({len(rows)}) ===")
        print(f"{'Key':<10} {'Days':<6} {'Status':<20} {'Resp.':<12} {'Assignee':<22} Summary")
        print("-" * 110)
        for row in rows:
            print_row(row, category)


if __name__ == "__main__":
    main()
