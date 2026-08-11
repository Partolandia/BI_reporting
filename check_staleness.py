"""
Run this to see the "At Risk" view: every open ticket for the team,
classified green/yellow/red based on real activity (not just the status
label).

    python check_staleness.py

This takes longer than test_connection.py -- it fetches each open ticket's
full change history one at a time, so expect roughly 1 second per ticket.
"""

from staleness import build_report

LEVEL_LABELS = {"red": "RED", "yellow": "YELLOW", "green": "GREEN"}


def main():
    print("Pulling open tickets and their change history -- this may take a minute...\n")
    report = build_report()

    counts = {"red": 0, "yellow": 0, "green": 0}
    for row in report:
        counts[row["level"]] += 1

    print(f"{len(report)} open tickets checked.")
    print(f"  RED (3+ days, no real activity):    {counts['red']}")
    print(f"  YELLOW (2-3 days, no real activity): {counts['yellow']}")
    print(f"  GREEN (active):                      {counts['green']}\n")

    print(f"{'Flag':<7} {'Key':<10} {'Days':<6} {'Status':<20} {'Assignee':<22} Summary")
    print("-" * 110)
    for row in report:
        print(
            f"{LEVEL_LABELS[row['level']]:<7} {row['key']:<10} {row['days_inactive']:<6} "
            f"{row['status']:<20} {row['assignee']:<22} {row['summary'][:40]}"
        )


if __name__ == "__main__":
    main()
