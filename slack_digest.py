"""
Posts a daily digest of stale/urgent tickets to Slack.

    python slack_digest.py

Uses the same staleness engine as check_staleness.py and app.py -- pulls
live from Jira (takes a few minutes, same as check_staleness.py), then
posts a formatted summary to the Slack channel behind SLACK_WEBHOOK_URL
in .env.

To run this automatically every day, schedule it with Windows Task
Scheduler (see the README) rather than leaving a terminal window open.
"""

import os
import sys
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

import jira_client
from staleness import build_report

load_dotenv()

WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")

# Cap how many rows we list per section so one bad day doesn't produce a
# message Slack rejects for being too long. Full detail is always in the
# dashboard.
MAX_ROWS_PER_SECTION = 15


def issue_link(row):
    return f"<{jira_client.SITE_URL}/browse/{row['key']}|{row['key']}>"


def format_ticket_line(row):
    return f"• {issue_link(row)} ({row['days_inactive']}d, {row['assignee']}) — {row['summary']}"


def section_block(title, rows, empty_text=None):
    if not rows:
        if empty_text:
            return {"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*\n{empty_text}"}}
        return None

    lines = [format_ticket_line(row) for row in rows[:MAX_ROWS_PER_SECTION]]
    if len(rows) > MAX_ROWS_PER_SECTION:
        lines.append(f"_...and {len(rows) - MAX_ROWS_PER_SECTION} more — see the dashboard for the full list._")

    text = f"*{title} ({len(rows)})*\n" + "\n".join(lines)
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def build_message(report):
    counts = {"red": 0, "yellow": 0, "ready_to_close": 0, "green": 0}
    urgent_rows, needs_estimate_rows, needs_status_update_rows = [], [], []
    for row in report:
        counts[row["category"]] += 1
        if row["urgent"]:
            urgent_rows.append(row)
        if row["needs_estimate"]:
            needs_estimate_rows.append(row)
        if row["needs_status_update"]:
            needs_status_update_rows.append(row)

    today = datetime.now(timezone.utc).strftime("%A, %B %d")

    summary = (
        f"*{len(report)} open tickets* — "
        f"🔴 {len(urgent_rows)} Urgent · "
        f"{counts['red']} Red · {counts['yellow']} Yellow · {counts['green']} Green · "
        f"{counts['ready_to_close']} Ready to Close · "
        f"{len(needs_status_update_rows)} Status Needs Updating · "
        f"{len(needs_estimate_rows)} Needs Estimate"
    )

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📊 Daily Ticket Digest — {today}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
        {"type": "divider"},
    ]

    for title, rows in [
        ("🔴 Urgent — needs attention now", urgent_rows),
        ("💬 Needs Estimate", needs_estimate_rows),
        ("⚠️ Status Needs Updating (stuck on Open)", needs_status_update_rows),
    ]:
        block = section_block(title, rows, empty_text="Nothing here today. 🎉" if title.startswith("🔴") else None)
        if block:
            blocks.append(block)

    if not urgent_rows and not needs_estimate_rows and not needs_status_update_rows:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "Nothing urgent today. 🎉"}})

    fallback_text = summary.replace("*", "")
    return {"text": fallback_text, "blocks": blocks}


def send(payload):
    if not WEBHOOK_URL:
        print("SLACK_WEBHOOK_URL is not set in .env -- nothing to send to.")
        sys.exit(1)

    response = requests.post(WEBHOOK_URL, json=payload, timeout=30)
    response.raise_for_status()


def main():
    print("Pulling tickets and building the digest -- this takes a few minutes, same as check_staleness.py...")
    report = build_report()
    payload = build_message(report)
    send(payload)
    print("Digest posted to Slack.")


if __name__ == "__main__":
    main()
