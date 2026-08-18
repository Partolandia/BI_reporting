"""
Local web dashboard for the Jira ticket monitoring project.

Run this, then open http://localhost:5000 in your browser:

    python app.py

It uses the exact same staleness engine as check_staleness.py, but instead
of recomputing it on demand (which takes minutes), it refreshes in the
background on a schedule (JIRA_REFRESH_MINUTES in .env, default 30) and
serves the cached result instantly. There's also a "Refresh now" button for
an on-demand pull -- it runs in the background too, so the page doesn't
hang waiting for it; reload a bit later to see the update.

The last successful pull is also saved to dashboard_cache.json, so
restarting the app shows the last-known data immediately instead of an
empty page while the first background refresh runs.

This is a LOCAL, single-user tool: run it with `python app.py` on your own
computer. It isn't set up to be hosted publicly -- putting this on a server
other people can reach would need real thought about where the Jira token
lives and who can access it, which is a separate conversation.
"""

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, abort, redirect, render_template, request, url_for

import jira_client
from staleness import build_report

load_dotenv()

REFRESH_MINUTES = int(os.environ.get("JIRA_REFRESH_MINUTES", "30"))
PORT = int(os.environ.get("JIRA_DASHBOARD_PORT", "5050"))
CACHE_FILE = Path(__file__).parent / "dashboard_cache.json"

CATEGORY_ORDER = ["red", "yellow", "ready_to_close", "green"]
CATEGORY_LABELS = {
    "red": "Red",
    "yellow": "Yellow",
    "ready_to_close": "Ready to Close",
    "green": "Green",
}

# Statuses with a dedicated quick-filter link in the nav (e.g. for
# following up with clients on tickets waiting on their approval). Add
# more status names here for other one-off filters -- the /status/<name>
# route behind it works for any status, this list just controls what shows
# up as a clickable shortcut.
QUICK_FILTER_STATUSES = ["Waiting for Approval"]

# Client name -> keywords to match (case-insensitive, substring) against a
# ticket's summary. Jira doesn't have a dedicated "client" field in this
# workflow -- the client's name just shows up as text in the ticket title
# (e.g. "Standish DB - RP - ...", "StandishDB - IP - ..."), so we match on
# that instead. Add more clients here the same way; list multiple keywords
# per client if their name appears in more than one form.
CLIENT_FILTERS = {
    "Standish Management": ["standish"],
}

app = Flask(__name__)

_lock = threading.Lock()
_state = {"report": [], "last_refreshed": None, "refreshing": False, "error": None}


def _load_cache_from_disk():
    if not CACHE_FILE.exists():
        return
    try:
        data = json.loads(CACHE_FILE.read_text())
    except Exception:
        return  # corrupt/old cache file -- ignore, the next refresh replaces it
    with _lock:
        _state["report"] = data["report"]
        _state["last_refreshed"] = data["last_refreshed"]


def _save_cache_to_disk():
    with _lock:
        payload = {"report": _state["report"], "last_refreshed": _state["last_refreshed"]}
    CACHE_FILE.write_text(json.dumps(payload, default=str))


def refresh_now():
    with _lock:
        if _state["refreshing"]:
            return
        _state["refreshing"] = True

    try:
        report = build_report()
        with _lock:
            _state["report"] = report
            _state["last_refreshed"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            _state["error"] = None
        _save_cache_to_disk()
    except Exception as exc:
        with _lock:
            _state["error"] = str(exc)
    finally:
        with _lock:
            _state["refreshing"] = False


def _background_loop():
    while True:
        refresh_now()
        time.sleep(REFRESH_MINUTES * 60)


def build_context(report, title):
    counts = {c: 0 for c in CATEGORY_ORDER}
    needs_status_update_rows = []
    needs_estimate_rows = []
    urgent_rows = []
    for row in report:
        counts[row["category"]] += 1
        if row["needs_status_update"]:
            needs_status_update_rows.append(row)
        if row["needs_estimate"]:
            needs_estimate_rows.append(row)
        if row["urgent"]:
            urgent_rows.append(row)

    groups = {c: [r for r in report if r["category"] == c] for c in CATEGORY_ORDER}

    with _lock:
        last_refreshed = _state["last_refreshed"]
        refreshing = _state["refreshing"]
        error = _state["error"]

    return {
        "title": title,
        "total": len(report),
        "counts": counts,
        "needs_status_update_rows": needs_status_update_rows,
        "needs_estimate_rows": needs_estimate_rows,
        "urgent_rows": urgent_rows,
        "groups": groups,
        "category_order": CATEGORY_ORDER,
        "category_labels": CATEGORY_LABELS,
        "last_refreshed": last_refreshed,
        "refreshing": refreshing,
        "error": error,
        "team_members": jira_client.TEAM_MEMBERS,
        "project_keys": jira_client.PROJECT_KEYS,
        "quick_filter_statuses": QUICK_FILTER_STATUSES,
        "client_names": list(CLIENT_FILTERS.keys()),
        "refresh_minutes": REFRESH_MINUTES,
        "jira_site_url": jira_client.SITE_URL,
    }


@app.route("/")
def dashboard():
    with _lock:
        report = list(_state["report"])
    return render_template("dashboard.html", **build_context(report, "Team Overview"))


@app.route("/person/<path:name>")
def person(name):
    if name not in jira_client.TEAM_MEMBERS:
        abort(404)
    with _lock:
        report = [r for r in _state["report"] if r["assignee"] == name]
    return render_template("dashboard.html", **build_context(report, name))


@app.route("/project/<key>")
def project(key):
    key = key.upper()
    if key not in jira_client.PROJECT_KEYS:
        abort(404)
    with _lock:
        report = [r for r in _state["report"] if r["key"].split("-")[0] == key]
    return render_template("dashboard.html", **build_context(report, key))


@app.route("/status/<path:status_name>")
def status_filter(status_name):
    with _lock:
        report = [r for r in _state["report"] if r["status"] == status_name]
    return render_template("dashboard.html", **build_context(report, status_name))


@app.route("/client/<path:name>")
def client_filter(name):
    keywords = CLIENT_FILTERS.get(name)
    if keywords is None:
        abort(404)
    with _lock:
        report = [r for r in _state["report"] if any(k in r["summary"].lower() for k in keywords)]
    return render_template("dashboard.html", **build_context(report, name))


@app.route("/search")
def search():
    query = request.args.get("q", "").strip()
    if not query:
        return redirect(url_for("dashboard"))
    with _lock:
        report = [r for r in _state["report"] if query.lower() in r["key"].lower()]
    return render_template("dashboard.html", **build_context(report, f'Search: "{query}"'), search_query=query)


@app.route("/refresh", methods=["POST"])
def refresh():
    threading.Thread(target=refresh_now, daemon=True).start()
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    _load_cache_from_disk()
    threading.Thread(target=_background_loop, daemon=True).start()
    print(f"Dashboard starting -- open http://localhost:{PORT} in your browser")
    print(f"Refreshing in the background every {REFRESH_MINUTES} minutes.")
    # If this port is already taken, Flask's dev server prints its own clear
    # "Address already in use" message and exits -- nothing extra needed here.
    app.run(host="127.0.0.1", port=PORT, debug=False)
