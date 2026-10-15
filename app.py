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
import workload
from staleness import (
    WORKLOAD_OVERRIDES_PATH,
    WORKLOAD_POLICY_PATH,
    backfill_issue_types,
    build_report,
)

load_dotenv()

REFRESH_MINUTES = int(os.environ.get("JIRA_REFRESH_MINUTES", "30"))
PORT = int(os.environ.get("JIRA_DASHBOARD_PORT", "5050"))
CACHE_FILE = Path(__file__).parent / "dashboard_cache.json"
REFRESH_LOCK_FILE = Path(__file__).parent / ".dashboard_refresh.lock"

# Bumped when the cached row shape changes. An older cache still renders:
# rows without workload source data show up as "Needs review" with an
# explanation instead of being silently dropped or counted as zero.
CACHE_VERSION = 2

# Workload drill-down tabs. Waiting views exist so follow-up work can happen
# without those tickets contaminating active assignment evaluation.
WORKLOAD_VIEWS = [
    ("active", "Active"),
    ("waiting_client", "Waiting on client"),
    ("waiting_other", "Waiting on others"),
    ("review", "Needs review"),
    ("all", "All owned"),
    ("excluded", "Excluded"),
]

_policy_store = workload.PolicyStore(WORKLOAD_POLICY_PATH, WORKLOAD_OVERRIDES_PATH)

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
_state = {
    "report": [],
    "last_refreshed": None,
    "refreshing": False,
    "error": None,
    "warning": None,
    "cache_version": None,
    "workload_identity_issues": [],
}


def _load_cache_from_disk():
    if not CACHE_FILE.exists():
        return
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return  # corrupt/old cache file -- ignore, the next refresh replaces it
    with _lock:
        _state["report"] = data["report"]
        _state["last_refreshed"] = data["last_refreshed"]
        _state["cache_version"] = data.get("cache_version")
        cache_updated = backfill_issue_types(_state["report"])
    _reclassify_workload()
    if cache_updated:
        _save_cache_to_disk()


def _save_cache_to_disk():
    """Atomic write: a crash mid-write must not leave a truncated cache."""
    with _lock:
        payload = {
            "cache_version": CACHE_VERSION,
            "policy_version": _policy_store.policy.get("policy_version"),
            "classifier_version": workload.CLASSIFIER_VERSION,
            "report": _state["report"],
            "last_refreshed": _state["last_refreshed"],
        }
    temp = CACHE_FILE.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, default=str), encoding="utf-8")
    temp.replace(CACHE_FILE)


def _reclassify_workload(now=None):
    """
    Recompute workload classification from the cached source records.

    Runs on cache load, on policy/override change, and after every refresh so
    time-dependent attention thresholds stay current even when no ticket
    content changed. Never calls Jira.
    """
    policy = _policy_store.policy
    overrides = _policy_store.overrides
    with _lock:
        report = _state["report"]
        if not report:
            return
        sources = [row.get("workload_source") for row in report]
        identity = workload.identity_from_sources(
            sources, jira_client.TEAM_MEMBERS, policy
        )
        workload.reclassify_report(
            report, policy, identity, overrides=overrides, now=now
        )
        _state["workload_identity_issues"] = list(identity.setup_issues)


def _pid_is_alive(pid):
    if pid <= 0:
        return False
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
    except Exception:
        pass
    return False


def _try_acquire_refresh_lock():
    """Only one dashboard process should pull Jira at a time."""
    if REFRESH_LOCK_FILE.exists():
        try:
            other_pid = int(REFRESH_LOCK_FILE.read_text().strip())
            if other_pid != os.getpid() and _pid_is_alive(other_pid):
                return False
        except (ValueError, OSError):
            pass
    try:
        REFRESH_LOCK_FILE.write_text(str(os.getpid()))
        return True
    except OSError:
        return False


def _release_refresh_lock():
    try:
        if REFRESH_LOCK_FILE.exists() and REFRESH_LOCK_FILE.read_text().strip() == str(os.getpid()):
            REFRESH_LOCK_FILE.unlink()
    except OSError:
        pass


def refresh_now():
    with _lock:
        if _state["refreshing"]:
            return
        _state["refreshing"] = True

    if not _try_acquire_refresh_lock():
        with _lock:
            _state["refreshing"] = False
        return

    try:
        _policy_store.reload_if_changed()
        warnings = []
        report = build_report(
            warnings=warnings,
            workload_policy=_policy_store.policy,
            workload_overrides=_policy_store.overrides,
        )
        degraded = sum(1 for row in report if row.get("fetch_degraded"))
        with _lock:
            _state["report"] = report
            _state["cache_version"] = CACHE_VERSION
            _state["last_refreshed"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            _state["error"] = None
            if degraded:
                _state["warning"] = (
                    f"Refresh completed, but {degraded} ticket(s) used limited data "
                    f"because Jira connection errors persisted after retries."
                )
            else:
                _state["warning"] = None
        _save_cache_to_disk()
    except Exception as exc:
        with _lock:
            _state["error"] = str(exc)
    finally:
        _release_refresh_lock()
        with _lock:
            _state["refreshing"] = False


def _background_loop():
    # Avoid hammering Jira the instant the server starts (and racing a manual refresh).
    time.sleep(20)
    while True:
        refresh_now()
        time.sleep(REFRESH_MINUTES * 60)


def nav_context():
    """Header/nav data shared by every page (both the staleness views and /workload)."""
    with _lock:
        last_refreshed = _state["last_refreshed"]
        refreshing = _state["refreshing"]
        error = _state["error"]
        warning = _state["warning"]
        cache_version = _state.get("cache_version")

    return {
        "last_refreshed": last_refreshed,
        "refreshing": refreshing,
        "error": error,
        "warning": warning,
        "team_members": jira_client.TEAM_MEMBERS,
        "project_keys": jira_client.PROJECT_KEYS,
        "quick_filter_statuses": QUICK_FILTER_STATUSES,
        "client_names": list(CLIENT_FILTERS.keys()),
        "refresh_minutes": REFRESH_MINUTES,
        "jira_site_url": jira_client.SITE_URL,
        "cache_stale": cache_version not in (None, CACHE_VERSION),
    }


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
        **nav_context(),
    }


# SVG geometry for the workload bar chart -- a horizontal bar per team
# member (chosen over a pie: 7 long names compare far more clearly as
# horizontal bars, and bar color encodes risk status rather than spending
# 7 hues on identity that the name label already carries).
WORKLOAD_LABEL_WIDTH = 190
WORKLOAD_TRACK_WIDTH = 340
WORKLOAD_ROW_HEIGHT = 34
WORKLOAD_BAR_HEIGHT = 20
WORKLOAD_PADDING_TOP = 14

# Bar colour encodes configured-capacity status, not identity. Uncalibrated
# capacity is deliberately neutral rather than green: "we don't know" must not
# read as "available for more work".
_CAPACITY_COLORS = {
    "above_capacity": "var(--critical)",
    "at_or_near_capacity": "var(--warning)",
    "possible_capacity_for_more": "var(--good)",
    "below_capacity_unconfirmed": "var(--info)",
    "capacity_not_calibrated": "var(--info)",
    "unavailable": "var(--muted)",
}


def build_workload_chart(people):
    """
    Bars are planning units, split into confirmed active and provisional
    (needs-review) segments. Waiting tickets contribute zero, so they never
    lengthen a bar.
    """
    max_units = max((p["planning_units"] for p in people), default=0)
    for person in people:
        capacity = person.get("planning_capacity_units")
        if isinstance(capacity, (int, float)):
            max_units = max(max_units, capacity)
    scale = (WORKLOAD_TRACK_WIDTH / max_units) if max_units else 0

    bars = []
    for index, person in enumerate(people):
        y = WORKLOAD_PADDING_TOP + index * WORKLOAD_ROW_HEIGHT
        active_width = round(person["active_units"] * scale, 1)
        provisional_width = round(person["provisional_units"] * scale, 1)
        capacity = person.get("planning_capacity_units")
        bars.append(
            {
                "name": person["name"],
                "planning_units": person["planning_units"],
                "active_units": person["active_units"],
                "provisional_units": person["provisional_units"],
                "active_count": person["confirmed_active_count"],
                "y": y,
                "active_width": active_width,
                "provisional_width": provisional_width,
                "provisional_x": round(WORKLOAD_LABEL_WIDTH + active_width, 1),
                "label_x": round(
                    WORKLOAD_LABEL_WIDTH + active_width + provisional_width + 8, 1
                ),
                "color": _CAPACITY_COLORS.get(
                    person.get("capacity_status"), "var(--info)"
                ),
                "capacity_x": (
                    round(WORKLOAD_LABEL_WIDTH + capacity * scale, 1)
                    if isinstance(capacity, (int, float)) and capacity > 0
                    else None
                ),
                "text_y": round(y + WORKLOAD_BAR_HEIGHT / 2 + 4, 1),
            }
        )

    return {
        "bars": bars,
        "label_width": WORKLOAD_LABEL_WIDTH,
        "track_width": WORKLOAD_TRACK_WIDTH,
        "bar_height": WORKLOAD_BAR_HEIGHT,
        "svg_width": WORKLOAD_LABEL_WIDTH + WORKLOAD_TRACK_WIDTH + 70,
        "svg_height": WORKLOAD_PADDING_TOP + len(people) * WORKLOAD_ROW_HEIGHT + 10,
    }


@app.route("/")
def dashboard():
    with _lock:
        report = list(_state["report"])
    return render_template("dashboard.html", **build_context(report, "Team Overview"))


def _matches_client(row, keywords):
    summary = (row.get("summary") or "").lower()
    return any(keyword in summary for keyword in keywords)


@app.route("/workload", endpoint="workload")
def workload_view():
    """
    Actionable workload. Renders from cache only -- no Jira calls here.

    Capacity and peer figures always come from the canonical full-team
    snapshot, so changing a client or project filter cannot redefine
    anyone's overall capacity or shift the thresholds behind their badge.
    """
    if _policy_store.reload_if_changed():
        _reclassify_workload()

    with _lock:
        report = list(_state["report"])

    policy = _policy_store.policy
    identity = workload.identity_from_sources(
        [row.get("workload_source") for row in report],
        jira_client.TEAM_MEMBERS,
        policy,
    )
    snapshot = workload.build_workload_report(
        report, jira_client.TEAM_MEMBERS, policy, identity
    )

    view = request.args.get("view", "active")
    if view not in {key for key, _ in WORKLOAD_VIEWS}:
        view = "active"

    client_name = request.args.get("client") or None
    project_key = (request.args.get("project") or "").upper() or None
    client_keywords = CLIENT_FILTERS.get(client_name) if client_name else None

    for person in snapshot["people"]:
        buckets = person["rows_by_bucket"]
        owned = (
            buckets["active"]
            + buckets["waiting_client"]
            + buckets["waiting_other"]
            + buckets["review_required"]
        )
        pool = owned + buckets["excluded"] if view == "excluded" else owned
        rows = workload.filter_rows_for_view(pool, view)
        if client_keywords:
            rows = [r for r in rows if _matches_client(r, client_keywords)]
        if project_key:
            rows = [r for r in rows if r["key"].split("-")[0] == project_key]
        person["visible_rows"] = sorted(
            rows,
            key=lambda row: (
                not row.get("first_response_overdue"),
                not row.get("progress_overdue"),
                -(row.get("planning_units") or 0),
                row["key"],
            ),
        )

    filtered = bool(client_keywords or project_key)
    return render_template(
        "workload.html",
        title="Team Workload",
        snapshot=snapshot,
        chart=build_workload_chart(snapshot["people"]),
        view=view,
        views=WORKLOAD_VIEWS,
        selected_client=client_name,
        selected_project=project_key,
        display_filtered=filtered,
        policy=policy,
        policy_warning=_policy_store.warning,
        unassigned_configured=bool(
            policy.get("include_unassigned") and policy.get("unassigned_team_scope_jql")
        ),
        **nav_context(),
    )


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
