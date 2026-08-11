"""
Run this to see the "At Risk" view: every open ticket for the team,
classified red/yellow/green by real activity, plus a separate "Ready to
Close" list for tickets where the latest comment sounds like a sign-off.

    python check_staleness.py

This takes longer than test_connection.py -- it fetches each open ticket's
full change history AND comments one at a time, so expect roughly 1-2
seconds per ticket.
"""

from staleness import build_report

CATEGORY_LABELS = {
    "red": "RED",
    "yellow": "YELLOW",
    "ready_to_close": "READY TO CLOSE",
    "green": "GREEN",
}
CATEGORY_ORDER = ["red", "yellow", "ready_to_close", "green"]


def main():
    print("Pulling open tickets, change history, and comments -- this may take a few minutes...\n")
    report = build_report()

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
