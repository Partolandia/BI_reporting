"""
Run this to confirm the Jira connection works:

    python test_connection.py

It connects to Jira, pulls tickets from the CSSD, IIP, and ACPD projects,
and prints a summary table plus a per-project count so you can eyeball that
the numbers look right compared to what you see in Jira.
"""

import sys

from jira_client import PROJECT_KEYS, get_tickets, summarize


def main():
    print(f"Connecting to Jira and pulling tickets for: {', '.join(PROJECT_KEYS)}...\n")

    try:
        issues = get_tickets()
    except Exception as exc:
        print(f"Connection failed: {exc}")
        sys.exit(1)

    print(f"Pulled {len(issues)} tickets total.\n")

    counts = {}
    for issue in issues:
        project_key = issue["key"].split("-")[0]
        counts[project_key] = counts.get(project_key, 0) + 1

    print("Tickets per project:")
    for key in PROJECT_KEYS:
        print(f"  {key}: {counts.get(key, 0)}")
    print()

    print(f"{'Key':<10} {'Type':<12} {'Status':<18} {'Assignee':<22} {'Updated'}")
    print("-" * 90)
    for issue in issues[:20]:
        row = summarize(issue)
        print(
            f"{row['key']:<10} {row['type']:<12} {row['status']:<18} "
            f"{row['assignee']:<22} {row['updated']}"
        )

    if len(issues) > 20:
        print(f"\n...and {len(issues) - 20} more.")


if __name__ == "__main__":
    main()
