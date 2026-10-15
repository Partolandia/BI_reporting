"""
Actionable Team Workload: deterministic classification and aggregation.

The rule this module exists to enforce:

    A ticket counts toward assignment capacity only when the current
    assignee has an actionable next step. Waiting for client approval of an
    estimate, acceptance of delivered work, or required client information
    contributes ZERO active units.

Those tickets keep their owner and stay visible in a separate waiting list,
but they must not inflate the active chart, the active average/median, the
overdue-assignment flags, or the reassignment candidates.

This module is pure: no Jira I/O, no Flask, no global mutable state beyond
an explicitly managed policy store. Jira fetching stays in jira_client.py
and the shared staleness report stays in staleness.py. Nothing here changes
Team Overview's staleness semantics -- workload fields are added alongside
the existing ones, never on top of them.

Classification replays evidence chronologically rather than trusting the
latest developer comment, because an old "waiting for approval" message
must stop hiding a ticket once approval arrives or a new problem is
reported. Statuses are evidence, not infallible truth.

Uncertainty is visible by design: REVIEW_REQUIRED is counted provisionally
(1 unit) and surfaced, never silently hidden.
"""

import hashlib
import json
import re
import statistics
import unicodedata
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CLASSIFIER_VERSION = "deterministic-v1"
SOURCE_SCHEMA_VERSION = 1

# --------------------------------------------------------------------------
# Work states
# --------------------------------------------------------------------------

ACTIVE_READY = "ACTIVE_READY"
ACTIVE_IN_PROGRESS = "ACTIVE_IN_PROGRESS"
ACTIVE_REVIEW = "ACTIVE_REVIEW"
WAITING_CLIENT_ESTIMATE_APPROVAL = "WAITING_CLIENT_ESTIMATE_APPROVAL"
WAITING_CLIENT_ACCEPTANCE = "WAITING_CLIENT_ACCEPTANCE"
WAITING_CLIENT_INPUT = "WAITING_CLIENT_INPUT"
WAITING_INTERNAL = "WAITING_INTERNAL"
WAITING_EXTERNAL = "WAITING_EXTERNAL"
REVIEW_REQUIRED = "REVIEW_REQUIRED"
EXCLUDED = "EXCLUDED"

ACTIVE_STATES = frozenset({ACTIVE_READY, ACTIVE_IN_PROGRESS, ACTIVE_REVIEW})
WAITING_CLIENT_STATES = frozenset(
    {
        WAITING_CLIENT_ESTIMATE_APPROVAL,
        WAITING_CLIENT_ACCEPTANCE,
        WAITING_CLIENT_INPUT,
    }
)
WAITING_OTHER_STATES = frozenset({WAITING_INTERNAL, WAITING_EXTERNAL})
WAITING_STATES = WAITING_CLIENT_STATES | WAITING_OTHER_STATES
ALL_STATES = (
    ACTIVE_STATES | WAITING_STATES | frozenset({REVIEW_REQUIRED, EXCLUDED})
)

STATE_LABELS = {
    ACTIVE_READY: "Ready to work",
    ACTIVE_IN_PROGRESS: "In progress",
    ACTIVE_REVIEW: "Review owed by assignee",
    WAITING_CLIENT_ESTIMATE_APPROVAL: "Waiting: client estimate approval",
    WAITING_CLIENT_ACCEPTANCE: "Waiting: client acceptance",
    WAITING_CLIENT_INPUT: "Waiting: client input",
    WAITING_INTERNAL: "Waiting: internal",
    WAITING_EXTERNAL: "Waiting: external",
    REVIEW_REQUIRED: "Needs review",
    EXCLUDED: "Excluded from workload",
}

# Work phases. These describe which task is next, not the ticket's status.
PHASE_SCOPING = "scoping"
PHASE_ESTIMATE = "estimate"
PHASE_DISCOVERY = "discovery"
PHASE_IMPLEMENTATION = "implementation"
PHASE_TESTING = "testing"
PHASE_PEER_REVIEW = "peer_review"
PHASE_DELIVERY_REVIEW = "delivery_review"
PHASE_CLOSURE = "closure"
PHASE_UNKNOWN = "unknown"

# Next-action owner roles.
OWNER_TEAM_MEMBER = "team_member"
OWNER_CLIENT = "client"
OWNER_INTERNAL = "internal"
OWNER_EXTERNAL = "external"
OWNER_UNKNOWN = "unknown"

# Evidence-quality labels. These describe how good the evidence is, not a
# calibrated probability.
CONFIDENCE_EXPLICIT = "explicit"
CONFIDENCE_INFERRED = "inferred"
CONFIDENCE_REVIEW_REQUIRED = "review_required"

# Author roles resolved from configured identities.
ROLE_ASSIGNEE = "assignee"
ROLE_TEAM = "team"
ROLE_INTERNAL = "internal"
ROLE_CLIENT_APPROVER = "client_approver"
ROLE_AUTOMATION = "automation"
ROLE_UNKNOWN = "unknown"

# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------


class PolicyError(ValueError):
    """Raised when a workload policy document is structurally invalid."""


DEFAULT_POLICY = {
    "schema_version": 2,
    "policy_version": "actionable-workload-v1",
    "exclusion_rules": [],
    "status_defaults_by_name": {},
    "status_defaults_by_id": {},
    "client_approval_status_ids_by_project": {},
    "client_approval_status_names_by_project": {},
    "internal_account_ids": [],
    "client_approver_ids_by_client": {},
    "automation_account_ids": [],
    "external_dependency_names": [],
    "member_settings_by_account_id": {},
    "effort": {
        "hour_upper_bounds": [2, 8, 24, 40],
        "band_units": [0.5, 1.0, 2.0, 3.0, 4.0],
        "unknown_units": 1.0,
        "review_required_units": 1.0,
        "closure_units": 0.25,
    },
    "capacity": {
        "available_below_ratio": 0.7,
        "peer_min_people": 4,
        "peer_high_ratio": 1.5,
        "peer_min_excess_units": 2.0,
    },
    "attention": {
        "first_response_business_days": 2,
        "progress_business_days": 3,
        "warning_min_tickets": 3,
        "warning_min_ratio": 0.25,
        "client_followup_business_days": 5,
        "progress_status_names": ["In Progress"],
        "meaningful_comment_min_chars": 40,
    },
    "business_calendar": {
        "timezone": "America/New_York",
        "weekdays": [0, 1, 2, 3, 4],
        "start": "09:00",
        "end": "17:00",
        "holidays": [],
    },
    "include_unassigned": False,
    "unassigned_team_scope_jql": None,
    "llm_classifier_enabled": False,
}


def _deep_merge(base, override):
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _require(condition, message):
    if not condition:
        raise PolicyError(message)


def validate_policy(policy):
    """
    Validate a merged policy document atomically. Raises PolicyError with a
    specific message rather than silently coercing an unsafe value.
    """
    _require(isinstance(policy, dict), "policy must be a JSON object")
    _require(
        isinstance(policy.get("policy_version"), str) and policy["policy_version"],
        "policy_version must be a non-empty string",
    )

    rules = policy.get("exclusion_rules")
    _require(isinstance(rules, list), "exclusion_rules must be a list")
    seen_rule_ids = set()
    for rule in rules:
        _require(isinstance(rule, dict), "each exclusion rule must be an object")
        rule_id = rule.get("id")
        _require(
            isinstance(rule_id, str) and rule_id,
            "each exclusion rule needs a non-empty id",
        )
        _require(
            rule_id not in seen_rule_ids,
            f"duplicate exclusion rule id: {rule_id}",
        )
        seen_rule_ids.add(rule_id)
        for field in ("projects", "issue_type_ids", "issue_type_names"):
            value = rule.get(field, [])
            _require(
                isinstance(value, list)
                and all(isinstance(item, str) for item in value),
                f"exclusion rule {rule_id}: {field} must be a list of strings",
            )
        _require(
            rule.get("issue_type_ids") or rule.get("issue_type_names"),
            f"exclusion rule {rule_id} must match at least one issue type",
        )

    for field in ("status_defaults_by_name", "status_defaults_by_id"):
        mapping = policy.get(field, {})
        _require(isinstance(mapping, dict), f"{field} must be an object")
        for status, state in mapping.items():
            _require(
                state in ALL_STATES and state != EXCLUDED,
                f"{field}[{status}] is not a valid non-excluded work state: {state}",
            )

    effort = policy.get("effort", {})
    _require(isinstance(effort, dict), "effort must be an object")
    bounds = effort.get("hour_upper_bounds")
    units = effort.get("band_units")
    _require(
        isinstance(bounds, list) and all(isinstance(b, (int, float)) for b in bounds),
        "effort.hour_upper_bounds must be a list of numbers",
    )
    _require(
        isinstance(units, list) and all(isinstance(u, (int, float)) for u in units),
        "effort.band_units must be a list of numbers",
    )
    _require(
        len(units) == len(bounds) + 1,
        "effort.band_units must have exactly one more entry than hour_upper_bounds",
    )
    _require(
        all(b > 0 for b in bounds),
        "effort.hour_upper_bounds values must be positive",
    )
    _require(
        all(bounds[i] < bounds[i + 1] for i in range(len(bounds) - 1)),
        "effort.hour_upper_bounds must be strictly increasing",
    )
    for key in ("unknown_units", "review_required_units", "closure_units"):
        value = effort.get(key)
        _require(
            isinstance(value, (int, float)) and value >= 0,
            f"effort.{key} must be a non-negative number",
        )
    _require(
        all(u >= 0 for u in units), "effort.band_units values must be non-negative"
    )

    capacity = policy.get("capacity", {})
    _require(isinstance(capacity, dict), "capacity must be an object")
    ratio = capacity.get("available_below_ratio")
    _require(
        isinstance(ratio, (int, float)) and 0 < ratio <= 1,
        "capacity.available_below_ratio must be between 0 (exclusive) and 1",
    )
    _require(
        isinstance(capacity.get("peer_min_people"), int)
        and capacity["peer_min_people"] >= 1,
        "capacity.peer_min_people must be an integer >= 1",
    )
    _require(
        isinstance(capacity.get("peer_high_ratio"), (int, float))
        and capacity["peer_high_ratio"] > 1,
        "capacity.peer_high_ratio must be greater than 1",
    )
    _require(
        isinstance(capacity.get("peer_min_excess_units"), (int, float))
        and capacity["peer_min_excess_units"] >= 0,
        "capacity.peer_min_excess_units must be non-negative",
    )

    attention = policy.get("attention", {})
    _require(isinstance(attention, dict), "attention must be an object")
    for key in (
        "first_response_business_days",
        "progress_business_days",
        "client_followup_business_days",
    ):
        value = attention.get(key)
        _require(
            isinstance(value, (int, float)) and value > 0,
            f"attention.{key} must be a positive number",
        )
    _require(
        isinstance(attention.get("warning_min_tickets"), int)
        and attention["warning_min_tickets"] >= 1,
        "attention.warning_min_tickets must be an integer >= 1",
    )
    warn_ratio = attention.get("warning_min_ratio")
    _require(
        isinstance(warn_ratio, (int, float)) and 0 < warn_ratio <= 1,
        "attention.warning_min_ratio must be between 0 (exclusive) and 1",
    )
    _require(
        isinstance(attention.get("progress_status_names"), list),
        "attention.progress_status_names must be a list",
    )
    _require(
        isinstance(attention.get("meaningful_comment_min_chars"), int)
        and attention["meaningful_comment_min_chars"] >= 0,
        "attention.meaningful_comment_min_chars must be a non-negative integer",
    )

    calendar = policy.get("business_calendar", {})
    _require(isinstance(calendar, dict), "business_calendar must be an object")
    resolve_zone(calendar.get("timezone", ""))
    weekdays = calendar.get("weekdays")
    _require(
        isinstance(weekdays, list)
        and weekdays
        and all(isinstance(d, int) and 0 <= d <= 6 for d in weekdays),
        "business_calendar.weekdays must be a non-empty list of integers 0-6",
    )
    start = _parse_clock(calendar.get("start"))
    end = _parse_clock(calendar.get("end"))
    _require(
        start is not None and end is not None,
        "business_calendar.start and end must look like HH:MM",
    )
    _require(start < end, "business_calendar.start must be earlier than end")
    holidays = calendar.get("holidays", [])
    _require(isinstance(holidays, list), "business_calendar.holidays must be a list")
    for holiday in holidays:
        try:
            datetime.strptime(holiday, "%Y-%m-%d")
        except (TypeError, ValueError) as exc:
            raise PolicyError(
                f"business_calendar.holidays entry is not YYYY-MM-DD: {holiday!r}"
            ) from exc

    members = policy.get("member_settings_by_account_id", {})
    _require(
        isinstance(members, dict), "member_settings_by_account_id must be an object"
    )
    for account_id, settings in members.items():
        _require(
            isinstance(settings, dict),
            f"member_settings_by_account_id[{account_id}] must be an object",
        )
        capacity_units = settings.get("planning_capacity_units")
        if capacity_units is not None:
            _require(
                isinstance(capacity_units, (int, float)) and capacity_units >= 0,
                f"member_settings_by_account_id[{account_id}].planning_capacity_units "
                "must be a non-negative number",
            )
        max_started = settings.get("max_started_tickets")
        if max_started is not None:
            _require(
                isinstance(max_started, int) and max_started >= 0,
                f"member_settings_by_account_id[{account_id}].max_started_tickets "
                "must be a non-negative integer",
            )

    _require(
        isinstance(policy.get("include_unassigned"), bool),
        "include_unassigned must be true or false",
    )
    scope = policy.get("unassigned_team_scope_jql")
    _require(
        scope is None or (isinstance(scope, str) and scope.strip()),
        "unassigned_team_scope_jql must be null or a non-empty string",
    )
    if policy["include_unassigned"]:
        _require(
            scope,
            "include_unassigned is true but unassigned_team_scope_jql is not set",
        )
    return policy


def _tz_database_available():
    """
    Windows ships no IANA database, so zoneinfo needs the `tzdata` package
    (see requirements.txt). Distinguish "no database installed" from "that
    zone name is wrong": the first is an environment problem that must not
    take the dashboard down, the second is a real policy error.
    """
    try:
        ZoneInfo("UTC")
    except ZoneInfoNotFoundError:
        return False
    return True


def resolve_zone(name):
    """
    Returns (tzinfo, warning). Falls back to the system local zone only when
    the IANA database is missing entirely, and says so.
    """
    try:
        return ZoneInfo(name), None
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        pass
    if _tz_database_available():
        raise PolicyError(f"business_calendar.timezone is not a known zone: {name!r}")
    local = datetime.now(timezone.utc).astimezone().tzinfo
    return (
        local,
        f"IANA time zone database not installed, so {name!r} fell back to the "
        "system local zone. Run: pip install tzdata",
    )


def _parse_clock(value):
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


def load_policy(path):
    """
    Read, merge over defaults, and validate a policy file. Returns the
    validated policy. Raises PolicyError for unusable documents so callers
    can retain the previous valid policy.
    """
    path = Path(path)
    if not path.exists():
        return validate_policy(_deep_merge(DEFAULT_POLICY, {}))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyError(f"could not read policy file {path.name}: {exc}") from exc
    if not isinstance(raw, dict):
        raise PolicyError(f"{path.name} must contain a JSON object")
    return validate_policy(_deep_merge(DEFAULT_POLICY, raw))


class PolicyStore:
    """
    Holds the last valid policy and overrides. A bad reload keeps the
    previous valid policy and records a warning instead of breaking the page.
    """

    def __init__(self, policy_path, overrides_path=None):
        self.policy_path = Path(policy_path)
        self.overrides_path = Path(overrides_path) if overrides_path else None
        self.policy = validate_policy(_deep_merge(DEFAULT_POLICY, {}))
        self.overrides = {}
        self.warning = None
        self._policy_stamp = None
        self._overrides_stamp = None

    def _stamp(self, path):
        try:
            stat = path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def reload_if_changed(self):
        """Returns True when the effective policy or overrides changed."""
        changed = False
        warnings = []

        policy_stamp = self._stamp(self.policy_path)
        if policy_stamp != self._policy_stamp:
            try:
                self.policy = load_policy(self.policy_path)
                changed = True
            except PolicyError as exc:
                warnings.append(f"Workload policy not reloaded: {exc}")
            self._policy_stamp = policy_stamp

        if self.overrides_path is not None:
            overrides_stamp = self._stamp(self.overrides_path)
            if overrides_stamp != self._overrides_stamp:
                try:
                    self.overrides = load_overrides(self.overrides_path)
                    changed = True
                except PolicyError as exc:
                    warnings.append(f"Workload overrides not reloaded: {exc}")
                self._overrides_stamp = overrides_stamp

        self.warning = " ".join(warnings) if warnings else None
        return changed


_OVERRIDE_STATES = (ALL_STATES - {EXCLUDED}) | {REVIEW_REQUIRED}


def load_overrides(path):
    """
    Load per-ticket overrides keyed by issue id and key. Unsafe shapes are
    rejected outright rather than partially applied.
    """
    path = Path(path)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyError(f"could not read {path.name}: {exc}") from exc

    entries = raw.get("overrides") if isinstance(raw, dict) else raw
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise PolicyError(f"{path.name} must contain a list of overrides")

    by_ref = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise PolicyError("each override must be an object")
        state = entry.get("state")
        if state not in _OVERRIDE_STATES:
            raise PolicyError(f"override state is not allowed: {state!r}")
        issue_id = entry.get("issue_id")
        issue_key = entry.get("issue_key")
        if not issue_id and not issue_key:
            raise PolicyError("override needs issue_id or issue_key")
        normalized = {
            "state": state,
            "phase": entry.get("phase") or PHASE_UNKNOWN,
            "reason": entry.get("reason") or "manual override",
            "recorded_by": entry.get("recorded_by"),
            "recorded_at": _parse_iso(entry.get("recorded_at")),
            "expires_at": _parse_iso(entry.get("expires_at")),
            "size_units": entry.get("size_units"),
        }
        if normalized["size_units"] is not None and (
            not isinstance(normalized["size_units"], (int, float))
            or normalized["size_units"] < 0
        ):
            raise PolicyError("override size_units must be a non-negative number")
        for ref in (issue_id, issue_key):
            if ref:
                by_ref[str(ref)] = normalized
    return by_ref


# --------------------------------------------------------------------------
# Time helpers and business calendar
# --------------------------------------------------------------------------

_JIRA_DATETIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S%z",
)


def _parse_iso(value):
    """Parse a Jira or ISO timestamp into an aware datetime, or None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    for fmt in _JIRA_DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


class BusinessCalendar:
    """
    Business-time arithmetic for attention thresholds. Deliberately separate
    from staleness.py's calendar-day math, which must stay unchanged.
    """

    def __init__(self, config):
        config = _deep_merge(DEFAULT_POLICY["business_calendar"], config or {})
        self.tz, self.timezone_warning = resolve_zone(config["timezone"])
        self.weekdays = frozenset(config["weekdays"])
        self.start = _parse_clock(config["start"]) or time(9, 0)
        self.end = _parse_clock(config["end"]) or time(17, 0)
        self.holidays = frozenset(config.get("holidays") or [])
        self.hours_per_day = (
            datetime.combine(datetime(2000, 1, 1), self.end)
            - datetime.combine(datetime(2000, 1, 1), self.start)
        ).total_seconds() / 3600.0

    def _is_working_day(self, day):
        return (
            day.weekday() in self.weekdays
            and day.strftime("%Y-%m-%d") not in self.holidays
        )

    def _window(self, day):
        """Working window for a local date, as aware datetimes."""
        start = datetime.combine(day, self.start, tzinfo=self.tz)
        end = datetime.combine(day, self.end, tzinfo=self.tz)
        return start, end

    def business_hours_between(self, start, end):
        """Working hours between two aware datetimes (0.0 if end <= start)."""
        if start is None or end is None:
            return 0.0
        start = start.astimezone(self.tz)
        end = end.astimezone(self.tz)
        if end <= start:
            return 0.0

        total = 0.0
        day = start.date()
        last_day = end.date()
        while day <= last_day:
            if self._is_working_day(day):
                window_start, window_end = self._window(day)
                overlap_start = max(start, window_start)
                overlap_end = min(end, window_end)
                if overlap_end > overlap_start:
                    total += (overlap_end - overlap_start).total_seconds() / 3600.0
            day += timedelta(days=1)
        return total

    def business_days_between(self, start, end):
        if not self.hours_per_day:
            return 0.0
        return self.business_hours_between(start, end) / self.hours_per_day


# --------------------------------------------------------------------------
# Text extraction and normalization
# --------------------------------------------------------------------------

_MAX_COMMENT_CHARS = 2000
_BLOCK_NODES = {
    "paragraph",
    "heading",
    "listItem",
    "bulletList",
    "orderedList",
    "codeBlock",
    "panel",
    "tableRow",
}


def adf_to_blocks(node, quoted=False):
    """
    Walk an Atlassian Document Format body into text blocks, preserving
    paragraph/list boundaries, mentions, and whether the text sits inside a
    quoted block. Quoted earlier messages are context, not a new assertion
    by the current author, so they are tracked separately.
    """
    blocks = []

    def walk(current, in_quote):
        if isinstance(current, list):
            for item in current:
                walk(item, in_quote)
            return
        if not isinstance(current, dict):
            return

        node_type = current.get("type")
        if node_type == "blockquote":
            in_quote = True

        if node_type == "text":
            text = current.get("text", "")
            if text:
                blocks.append({"text": text, "quoted": in_quote, "inline": True})
            return
        if node_type == "mention":
            name = (current.get("attrs") or {}).get("text") or ""
            if name:
                blocks.append(
                    {
                        "text": name if name.startswith("@") else f"@{name}",
                        "quoted": in_quote,
                        "inline": True,
                    }
                )
            return
        if node_type == "hardBreak":
            blocks.append({"text": "\n", "quoted": in_quote, "inline": True})
            return

        content = current.get("content")
        if content:
            if node_type in _BLOCK_NODES:
                blocks.append({"text": "\n", "quoted": in_quote, "inline": True})
            walk(content, in_quote)
            if node_type in _BLOCK_NODES:
                blocks.append({"text": "\n", "quoted": in_quote, "inline": True})
        elif isinstance(current.get("text"), str) and current["text"]:
            blocks.append(
                {"text": current["text"], "quoted": in_quote, "inline": True}
            )

    walk(node, quoted)
    return blocks


def adf_to_text_parts(body):
    """
    Returns (unquoted_text, quoted_text, has_quote) for an ADF body or a
    plain string. Never regexes the raw JSON.
    """
    if body is None:
        return "", "", False
    if isinstance(body, str):
        return body, "", False

    blocks = adf_to_blocks(body)
    unquoted = "".join(b["text"] for b in blocks if not b["quoted"])
    quoted = "".join(b["text"] for b in blocks if b["quoted"])
    return unquoted, quoted, bool(quoted.strip())


def normalize_text(text):
    """
    Unicode/case/whitespace normalization that keeps punctuation, because
    negation, questions, and sentence boundaries carry meaning here.
    """
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.replace("\u2019", "'").replace("\u2018", "'")
    normalized = normalized.replace("\u201c", '"').replace("\u201d", '"')
    normalized = re.sub(r"[ \t\r\f\v]+", " ", normalized)
    normalized = re.sub(r"\n{2,}", "\n", normalized)
    return normalized.strip().lower()


def truncate_comment_text(text, limit=_MAX_COMMENT_CHARS):
    """Bound cached comment text so the cache stays a reasonable size."""
    if text is None:
        return "", False
    if len(text) <= limit:
        return text, False
    return text[:limit], True


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


class Identity:
    """
    Resolved identities for classification. accountId is the matching key;
    display names are presentation values only.

    Team membership comes from the existing JIRA_TEAM_MEMBERS configuration,
    resolved against accountIds actually seen in the fetched data. Missing or
    ambiguous matches are surfaced rather than silently resolved to the first
    candidate.
    """

    def __init__(
        self,
        team_display_names,
        display_to_account_ids=None,
        internal_account_ids=(),
        client_approver_ids=(),
        automation_account_ids=(),
    ):
        self.team_display_names = list(team_display_names or [])
        self.display_to_account_ids = {
            name: set(ids) for name, ids in (display_to_account_ids or {}).items()
        }
        self.internal_account_ids = set(internal_account_ids or ())
        self.client_approver_ids = set(client_approver_ids or ())
        self.automation_account_ids = set(automation_account_ids or ())

        self.team_account_ids = set()
        self.setup_issues = []
        lowered = {name.lower(): name for name in self.team_display_names}
        for name, account_ids in self.display_to_account_ids.items():
            if name.lower() not in lowered:
                continue
            if len(account_ids) > 1:
                self.setup_issues.append(
                    f"{name} resolves to {len(account_ids)} Jira accountIds; "
                    "set member_settings_by_account_id to disambiguate"
                )
            self.team_account_ids.update(account_ids)
        for name in self.team_display_names:
            if not self.display_to_account_ids.get(name):
                self.setup_issues.append(
                    f"{name} has no matching Jira accountId in the current data"
                )

    def role_for(self, account_id, display_name, assignee_account_id):
        """
        Resolve an actor's role. Being outside the seven-person team does not
        make somebody a client: an account manager or another internal
        colleague stays internal/unknown unless configured as a client
        approver.
        """
        if account_id and account_id in self.automation_account_ids:
            return ROLE_AUTOMATION
        if account_id and assignee_account_id and account_id == assignee_account_id:
            return ROLE_ASSIGNEE
        if account_id and account_id in self.team_account_ids:
            return ROLE_TEAM
        if account_id and account_id in self.internal_account_ids:
            return ROLE_INTERNAL
        if account_id and account_id in self.client_approver_ids:
            return ROLE_CLIENT_APPROVER
        if not account_id and display_name:
            # Legacy rows without accountIds: fall back to display-name
            # matching for team membership only, never to infer client status.
            lowered = {name.lower() for name in self.team_display_names}
            if display_name.lower() in lowered:
                return ROLE_TEAM
        return ROLE_UNKNOWN


def build_identity(issues, team_display_names, policy):
    """
    Build an Identity from raw Jira issues plus configured identities, with
    no extra API calls: assignee accountIds and comment authors already
    arrive with the existing pull.
    """
    display_to_account_ids = {}

    def record(account_id, display_name):
        if not display_name:
            return
        display_to_account_ids.setdefault(display_name, set())
        if account_id:
            display_to_account_ids[display_name].add(account_id)

    for issue in issues or []:
        fields = issue.get("fields") or {}
        assignee = fields.get("assignee") or {}
        record(assignee.get("accountId"), assignee.get("displayName"))
        reporter = fields.get("reporter") or {}
        record(reporter.get("accountId"), reporter.get("displayName"))

    approver_ids = set()
    for ids in (policy.get("client_approver_ids_by_client") or {}).values():
        approver_ids.update(ids or [])

    return Identity(
        team_display_names=team_display_names,
        display_to_account_ids=display_to_account_ids,
        internal_account_ids=policy.get("internal_account_ids") or (),
        client_approver_ids=approver_ids,
        automation_account_ids=policy.get("automation_account_ids") or (),
    )


def identity_from_sources(sources, team_display_names, policy):
    """
    Build an Identity from cached workload sources, so reclassifying from the
    cache resolves roles exactly as the original pull did -- no Jira calls.
    """
    display_to_account_ids = {}
    for source in sources or []:
        name = (source or {}).get("assignee_display")
        if not name:
            continue
        display_to_account_ids.setdefault(name, set())
        if source.get("assignee_account_id"):
            display_to_account_ids[name].add(source["assignee_account_id"])

    approver_ids = set()
    for ids in (policy.get("client_approver_ids_by_client") or {}).values():
        approver_ids.update(ids or [])

    return Identity(
        team_display_names=team_display_names,
        display_to_account_ids=display_to_account_ids,
        internal_account_ids=policy.get("internal_account_ids") or (),
        client_approver_ids=approver_ids,
        automation_account_ids=policy.get("automation_account_ids") or (),
    )


# --------------------------------------------------------------------------
# Source normalization
# --------------------------------------------------------------------------


def _seconds_from_fields(fields, key, tracking_key):
    value = fields.get(key)
    if isinstance(value, (int, float)):
        return int(value)
    tracking = fields.get("timetracking") or {}
    value = tracking.get(tracking_key)
    if isinstance(value, (int, float)):
        return int(value)
    return None


def build_workload_source(
    issue,
    comments=None,
    histories=None,
    comments_complete=True,
    changelog_complete=True,
    fetch_errors=None,
):
    """
    Normalize one Jira issue plus its comments and changelog into the compact
    record classification needs. Stored in the cache so a policy change can
    reclassify without a new Jira pull.
    """
    fields = issue.get("fields") or {}
    project = fields.get("project") or {}
    issue_type = fields.get("issuetype") or {}
    status = fields.get("status") or {}
    status_category = (status.get("statusCategory") or {}).get("name")
    assignee = fields.get("assignee") or {}
    priority = fields.get("priority") or {}

    normalized_comments = []
    for comment in comments or []:
        author = comment.get("author") or {}
        update_author = comment.get("updateAuthor") or {}
        unquoted, quoted, has_quote = adf_to_text_parts(comment.get("body"))
        text, truncated = truncate_comment_text(unquoted)
        quoted_text, quoted_truncated = truncate_comment_text(quoted, 600)
        visibility = "restricted" if comment.get("visibility") else "unknown"
        normalized_comments.append(
            {
                "id": comment.get("id"),
                "author_account_id": author.get("accountId"),
                "author_display": author.get("displayName"),
                "created": comment.get("created"),
                "updated": comment.get("updated"),
                "update_author_account_id": update_author.get("accountId"),
                "text": text,
                "quoted_text": quoted_text,
                "has_quote": has_quote,
                "truncated": truncated or quoted_truncated,
                # Generic Jira comment visibility is NOT the same thing as a
                # Jira Service Management public/internal flag, so the real
                # audience stays unknown unless reliable metadata exists.
                "visibility": visibility,
            }
        )

    transitions = []
    for history in histories or []:
        author = history.get("author") or {}
        for item in history.get("items") or []:
            if item.get("field") != "status":
                continue
            transitions.append(
                {
                    "at": history.get("created"),
                    "from": item.get("fromString"),
                    "to": item.get("toString"),
                    "author_account_id": author.get("accountId"),
                    "author_display": author.get("displayName"),
                }
            )

    assignments = []
    for history in histories or []:
        author = history.get("author") or {}
        for item in history.get("items") or []:
            if item.get("field") != "assignee":
                continue
            assignments.append(
                {
                    "at": history.get("created"),
                    "to_account_id": item.get("to"),
                    "to_display": item.get("toString"),
                    "author_account_id": author.get("accountId"),
                    "author_display": author.get("displayName"),
                }
            )

    return {
        "schema": SOURCE_SCHEMA_VERSION,
        "issue_id": issue.get("id"),
        "key": issue.get("key"),
        "project_key": project.get("key"),
        "project_id": project.get("id"),
        "issue_type_id": issue_type.get("id"),
        "issue_type_name": issue_type.get("name"),
        "status_id": status.get("id"),
        "status_name": status.get("name"),
        "status_category": status_category,
        "priority_id": priority.get("id"),
        "priority_name": priority.get("name"),
        "summary": fields.get("summary"),
        "created": fields.get("created"),
        "updated": fields.get("updated"),
        "assignee_account_id": assignee.get("accountId"),
        "assignee_display": assignee.get("displayName"),
        "original_estimate_seconds": _seconds_from_fields(
            fields, "timeoriginalestimate", "originalEstimateSeconds"
        ),
        "remaining_estimate_seconds": _seconds_from_fields(
            fields, "timeestimate", "remainingEstimateSeconds"
        ),
        "comments": normalized_comments,
        "transitions": transitions,
        "assignments": assignments,
        "comments_complete": bool(comments_complete),
        "changelog_complete": bool(changelog_complete),
        "fetch_errors": list(fetch_errors or []),
    }


def source_fingerprint(source, policy, overrides_signature=""):
    """
    Fingerprint the relevant source data plus policy/override/classifier
    versions, so time-independent reclassification can be skipped safely.
    """
    parts = [
        str(source.get("status_id")),
        str(source.get("status_name")),
        str(source.get("assignee_account_id")),
        str(source.get("issue_type_id")),
        str(source.get("issue_type_name")),
        str(source.get("project_key")),
        str(source.get("remaining_estimate_seconds")),
        str(source.get("original_estimate_seconds")),
        str(len(source.get("transitions") or [])),
        str(source.get("comments_complete")),
        str(source.get("changelog_complete")),
    ]
    for comment in source.get("comments") or []:
        parts.append(f"{comment.get('id')}:{comment.get('updated')}")
    parts.append(policy.get("policy_version", ""))
    parts.append(CLASSIFIER_VERSION)
    parts.append(overrides_signature)
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


# --------------------------------------------------------------------------
# Assertion detection
# --------------------------------------------------------------------------

# Assertion kinds produced by reading one comment.
A_ESTIMATE_SENT = "estimate_sent"
A_DELIVERED = "delivered"
A_CLIENT_INPUT_NEEDED = "client_input_needed"
A_INTERNAL_APPROVAL = "internal_approval"
A_INTERNAL_HANDOFF = "internal_handoff"
A_EXTERNAL_BLOCK = "external_block"
A_ASSIGNEE_PROGRESS = "assignee_progress"
A_ASSIGNEE_FUTURE_INTENT = "assignee_future_intent"
A_REMAINING_WORK = "remaining_work"
A_CLIENT_APPROVAL = "client_approval"
A_CLIENT_PROBLEM = "client_problem"
A_CLIENT_QUESTION = "client_question"
A_CLIENT_NEUTRAL = "client_neutral"
A_CLIENT_CLOSE_REQUEST = "client_close_request"
A_AMBIGUOUS_APPROVAL = "ambiguous_approval"
A_BOT_BOILERPLATE = "bot_boilerplate"
A_PEER_REVIEW_OWNED = "peer_review_owned"


def _compile(patterns):
    return [re.compile(pattern) for pattern in patterns]


# Future tense / intent: "I will send the estimate" is active work, not a
# completed handoff.
_FUTURE_CUES = _compile(
    [
        r"\b(?:i|we)(?:'| wi)?ll\b",
        r"\b(?:i|we) will\b",
        r"\bgoing to\b",
        r"\bplan(?:ning)? to\b",
        r"\babout to\b",
        r"\bintend to\b",
        r"\bshould (?:send|have|be able)\b",
        r"\b(?:need|needs|have) to (?:send|prepare|finish|complete|provide)\b",
        r"\bonce (?:i|we)\b",
        r"\bbefore (?:i|we)\b",
        r"\bafter (?:i|we)\b",
        r"\bwill be (?:sent|sending|ready|provided)\b",
        r"\btomorrow\b",
        r"\blater today\b",
    ]
)

_NEGATION_CUES = _compile(
    [
        r"\bnot\b",
        r"\bno longer\b",
        r"\bnever\b",
        r"\bcannot\b",
        r"\bcan't\b",
        r"\bhaven't\b",
        r"\bhas not\b",
        r"\bhasn't\b",
        r"\bdidn't\b",
        r"\bdid not\b",
        r"\bisn't\b",
        r"\bwithout\b",
        r"\byet to\b",
        r"\bstill (?:need|needs|have|has)\b",
    ]
)

_ESTIMATE_SENT_CUES = _compile(
    [
        r"\bestimate\b[^.\n]{0,40}\b(?:is |has been |was |been )?"
        r"(?:sent|attached|submitted|provided|shared|delivered)\b",
        r"\b(?:sent|submitted|provided|shared|attached)\b[^.\n]{0,40}\bestimate\b",
        r"\bplease approve\b[^.\n]{0,60}\bestimate\b",
        r"\bestimate\b[^.\n]{0,40}\bfor (?:your|client) approval\b",
        r"\bapprove the attached\b",
        r"\bawaiting (?:your|client|their) approval\b[^.\n]{0,40}\bestimate\b",
        r"\bestimate\b[^.\n]{0,40}\b(?:pending|awaiting)\b[^.\n]{0,20}\bapproval\b",
    ]
)

# Asking for approval or validation is itself addressed at the approver, even
# when the sentence never says "you" ("Please approve the attached estimate").
_APPROVAL_REQUEST_CUES = _compile(
    [
        r"\bplease approve\b",
        r"\bfor approval\b",
        r"\bawaiting\b[^.\n]{0,30}\bapproval\b",
        r"\bapprove the attached\b",
        r"\bpending approval\b",
        r"\bsign[- ]?off\b",
    ]
)

_VALIDATION_REQUEST_CUES = _compile(
    [
        r"\bplease validate\b",
        r"\bplease (?:review and )?confirm\b",
        r"\bplease review\b",
        r"\bwhether we (?:may|can) close\b",
        r"\bready for (?:your|client) review\b",
        r"\blet (?:us|me) know\b",
    ]
)

_INTERNAL_APPROVAL_CUES = _compile(
    [
        r"\bapprove\b[^.\n]{0,30}\binternally\b",
        r"\binternal(?:ly)?\b[^.\n]{0,20}\b(?:approval|review|sign[- ]?off)\b",
        r"\bbefore (?:it is |it's |we )?(?:sent|send|sharing|shared)\b",
        r"\bbefore sending\b[^.\n]{0,30}\bclient\b",
    ]
)

_DELIVERED_CUES = _compile(
    [
        r"\b(?:is |has been |was )?deployed\b",
        r"\bdeployed to (?:production|prod)\b",
        r"\b(?:has been |is |was )?delivered\b",
        r"\breleased\b",
        r"\breport is (?:ready|live|available)\b",
        r"\bplease validate\b",
        r"\bplease (?:review and )?confirm\b",
        r"\bready for (?:your|client) review\b",
        r"\bcompleted and (?:is )?available\b",
        r"\bwhether we (?:may|can) close\b",
    ]
)

_CLIENT_INPUT_CUES = _compile(
    [
        r"\b(?:cannot|can't|unable to) proceed (?:until|without)\b[^.\n]{0,40}\byou\b",
        r"\bwaiting (?:on|for) (?:you|the client|your)\b",
        r"\bplease (?:provide|send|share|upload)\b[^.\n]{0,60}"
        r"\b(?:file|sample|screenshot|access|example|data|credential|spreadsheet|detail)",
        r"\bneed\b[^.\n]{0,30}\bfrom (?:you|the client)\b",
        r"\bblocked (?:until|by)\b[^.\n]{0,40}\b(?:you|client)\b",
    ]
)

# Work the assignee still owes, which prevents the whole ticket from being
# treated as waiting.
_REMAINING_WORK_CUES = _compile(
    [
        r"\b(?:i|we) still (?:need|have) to\b",
        r"\bstill (?:need|needs|have|has) to\b",
        r"\b(?:i|we)(?:'m| am| are) still\b",
        r"\bstill (?:completing|working|finishing|testing|deploying|developing)\b",
        r"\bmeanwhile\b",
        r"\bin the meantime\b",
        r"\bwhile (?:i|we)\b",
        r"\bphase 2\b",
        r"\balready[- ]approved\b",
        r"\bcontinue the already\b",
    ]
)

_RESUMPTION_CUES = _compile(
    [
        r"\b(?:i|we) (?:have )?(?:started|resumed|began|begun)\b",
        r"\b(?:i|we)(?:'m| am| are) (?:working|implementing|developing|testing"
        r"|investigating|analysing|analyzing|building)\b",
        r"\b(?:working|investigating|looking) (?:on|into) (?:this|it|the)\b",
        r"\b(?:i|we) (?:implemented|fixed|updated|built|deployed|tested|completed)\b",
        r"\bpicking (?:this|it) up\b",
        r"\bcontinuing\b",
    ]
)

_CLIENT_APPROVAL_CUES = _compile(
    [
        r"\bapproved\b",
        r"\bapprove\b",
        r"\bplease proceed\b",
        r"\bgo ahead\b",
        r"\byou (?:may|can) proceed\b",
        r"\bsign(?:ed)?[- ]?off\b",
        r"\bauthoriz(?:e|ed)\b",
    ]
)

_PARTIAL_APPROVAL_CUES = _compile(
    [
        r"\bapproved for\b[^.\n]{0,40}\b(?:discovery|analysis|investigation|scoping)\b",
        r"\b(?:discovery|analysis|investigation|scoping) only\b",
        r"\bapproved\b[^.\n]{0,20}\b\d+(?:\.\d+)? ?(?:hour|hr)s?\b[^.\n]{0,30}"
        r"\b(?:discovery|analysis|investigation|scoping)\b",
    ]
)

_CLIENT_PROBLEM_CUES = _compile(
    [
        r"\bstill (?:incorrect|wrong|not right|failing|broken)\b",
        r"\b(?:totals|numbers|figures|values|amounts)\b[^.\n]{0,30}"
        r"\b(?:incorrect|wrong|off|mismatch)\b",
        r"\b(?:not|isn't|doesn't|does not) (?:work|working|match|correct)\b",
        r"\bissue (?:persists|remains|is back)\b",
        r"\b(?:getting|seeing|receiving) an? (?:error|exception)\b",
        r"\b(?:bug|defect)\b",
        r"\bis broken\b",
        r"\bincorrect\b",
    ]
)

_CLIENT_NEUTRAL_CUES = _compile(
    [
        r"^\s*thanks?\b",
        r"^\s*thank you\b",
        r"\bnoted\b",
        r"\back(?:nowledged)?\b",
        r"\bwill review\b",
        r"\breview(?:ing)? (?:it )?(?:next week|later|soon|shortly)\b",
        r"\bgot it\b",
    ]
)

_CLIENT_CLOSE_CUES = _compile(
    [
        r"\b(?:please )?close (?:the |this )?(?:ticket|request|issue|case)\b",
        r"\bwe (?:may|can) close\b",
        r"\byou (?:can|may) close\b",
        r"\bok(?:ay)? to close\b",
        r"\bfeel free to close\b",
    ]
)

_CLIENT_ACCEPTANCE_CUES = _compile(
    [
        r"\b(?:looks|look|working) (?:good|great|correct)\b",
        r"\b(?:confirmed|verified) (?:working|correct)\b",
        r"\bthis (?:is|looks) (?:correct|right|what we needed)\b",
        r"\baccepted\b",
    ]
)

_CHASE_CUES = _compile(
    [
        r"\bstill awaiting\b",
        r"\bany update\b",
        r"\bfollowing up\b",
        r"\bfollow(?:ing)? up\b",
        r"\bgentle reminder\b",
        r"\bfriendly reminder\b",
        r"\bjust checking in\b",
        r"\bbump\b",
    ]
)

_BOT_CUES = _compile(
    [
        r"\bautomated (?:reminder|message|notification)\b",
        r"\bthis is an automated\b",
        r"\bawaiting client response\b",
        r"\bsla (?:breach|warning)\b",
    ]
)

_INTERNAL_HANDOFF_CUES = _compile(
    [
        r"\bplease review\b",
        r"\bhanded (?:over|off) to\b",
        r"\bassigned to\b",
        r"\bover to you\b",
        r"\bcan you (?:review|take a look|pick this up)\b",
    ]
)

_PEER_REVIEW_OWNED_CUES = _compile(
    [
        r"\b(?:i|we)(?:'m| am| are) review(?:ing)?\b",
        r"\b(?:i|we) will review\b",
        r"\bmy review\b",
        r"\breviewing (?:this|it|the code)\b",
        r"\b(?:i|we) (?:have )?reviewed\b",
    ]
)

_PHASE_CUES = (
    (PHASE_TESTING, _compile([r"\btest(?:ing|ed)?\b", r"\bqa\b", r"\buat\b"])),
    (
        PHASE_DISCOVERY,
        _compile([r"\bdiscovery\b", r"\binvestigat", r"\banalys", r"\banalyz"]),
    ),
    (PHASE_ESTIMATE, _compile([r"\bestimat"])),
    (PHASE_SCOPING, _compile([r"\bscop(?:e|ing)\b", r"\brequirement"])),
    (
        PHASE_IMPLEMENTATION,
        _compile([r"\bimplement", r"\bdevelop", r"\bbuild(?:ing)?\b", r"\bcod(?:e|ing)\b"]),
    ),
    (PHASE_PEER_REVIEW, _compile([r"\bpeer review\b", r"\bcode review\b"])),
    (PHASE_DELIVERY_REVIEW, _compile([r"\bdeploy", r"\bdeliver", r"\breleas"])),
    (PHASE_CLOSURE, _compile([r"\bclose\b", r"\bclosure\b"])),
)


def _any(patterns, text):
    return any(pattern.search(text) for pattern in patterns)


def _matched(patterns, text):
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return match
    return None


def _negated_near(text, match, window=45):
    """
    True when a negation sits just before the matched cue. Deliberately
    local: a negation elsewhere in a long comment should not cancel an
    unrelated statement.
    """
    if match is None:
        return False
    start = max(0, match.start() - window)
    prefix = text[start : match.start()]
    return _any(_NEGATION_CUES, prefix)


def _addressed_to_client(text):
    return bool(
        re.search(r"\byou\b|\byour\b|\bclient\b|\bcustomer\b", text)
    )


def _detect_phase(text, default=PHASE_UNKNOWN):
    for phase, patterns in _PHASE_CUES:
        if _any(patterns, text):
            return phase
    return default


def detect_assertion(comment, role, text=None):
    """
    Read one comment into at most one assertion, using combinations of
    completed action, owner/recipient, phase, negation, future tense, and
    remaining-task evidence.

    Deliberately not a bare keyword match on "approve", "delivered", or
    "waiting": those words appear in promises, negations, quotes, and bot
    reminders. When the combination cannot be resolved, the caller gets
    REVIEW_REQUIRED instead of a confident answer.
    """
    if text is None:
        text = normalize_text(comment.get("text"))
    if not text:
        return None

    if role == ROLE_AUTOMATION or _any(_BOT_CUES, text):
        return {
            "kind": A_BOT_BOILERPLATE,
            "rule_id": "comment.automation_boilerplate",
            "decisive": False,
            "phase": PHASE_UNKNOWN,
        }

    is_internal_author = role in (ROLE_ASSIGNEE, ROLE_TEAM, ROLE_INTERNAL)
    is_client_author = role == ROLE_CLIENT_APPROVER
    has_remaining_work = _any(_REMAINING_WORK_CUES, text)
    is_future = _any(_FUTURE_CUES, text)
    is_chase = _any(_CHASE_CUES, text)

    # ---- statements by the assignee or another internal participant ----
    if is_internal_author:
        internal_approval = _matched(_INTERNAL_APPROVAL_CUES, text)
        if internal_approval and not _negated_near(text, internal_approval):
            return {
                "kind": A_INTERNAL_APPROVAL,
                "rule_id": "comment.internal_approval_requested",
                "decisive": True,
                "state": WAITING_INTERNAL,
                "phase": PHASE_ESTIMATE if _any(_compile([r"\bestimat"]), text) else PHASE_UNKNOWN,
                "confidence": CONFIDENCE_EXPLICIT,
                "owner": OWNER_INTERNAL,
            }

        estimate_sent = _matched(_ESTIMATE_SENT_CUES, text)
        if estimate_sent and not _negated_near(text, estimate_sent):
            if is_future and not is_chase:
                return {
                    "kind": A_ASSIGNEE_FUTURE_INTENT,
                    "rule_id": "comment.estimate_promised_not_sent",
                    "decisive": True,
                    "state": ACTIVE_READY,
                    "phase": PHASE_ESTIMATE,
                    "confidence": CONFIDENCE_EXPLICIT,
                    "owner": OWNER_TEAM_MEMBER,
                }
            if has_remaining_work:
                return {
                    "kind": A_REMAINING_WORK,
                    "rule_id": "comment.estimate_sent_with_remaining_work",
                    "decisive": True,
                    "state": ACTIVE_IN_PROGRESS,
                    "phase": _detect_phase(text, PHASE_IMPLEMENTATION),
                    "confidence": CONFIDENCE_INFERRED,
                    "owner": OWNER_TEAM_MEMBER,
                }
            if _addressed_to_client(text) or _any(_APPROVAL_REQUEST_CUES, text):
                return {
                    "kind": A_ESTIMATE_SENT,
                    "rule_id": "comment.estimate_sent_awaiting_client_approval",
                    "decisive": True,
                    "state": WAITING_CLIENT_ESTIMATE_APPROVAL,
                    "phase": PHASE_ESTIMATE,
                    "confidence": CONFIDENCE_EXPLICIT,
                    "owner": OWNER_CLIENT,
                    "preserves_waiting_since": is_chase,
                }

        if is_future and _any(_compile([r"\bestimat"]), text):
            return {
                "kind": A_ASSIGNEE_FUTURE_INTENT,
                "rule_id": "comment.estimate_promised_not_sent",
                "decisive": True,
                "state": ACTIVE_READY,
                "phase": PHASE_ESTIMATE,
                "confidence": CONFIDENCE_EXPLICIT,
                "owner": OWNER_TEAM_MEMBER,
            }

        delivered = _matched(_DELIVERED_CUES, text)
        if delivered and not _negated_near(text, delivered):
            if has_remaining_work:
                return {
                    "kind": A_REMAINING_WORK,
                    "rule_id": "comment.delivered_phase_with_remaining_work",
                    "decisive": True,
                    "state": ACTIVE_IN_PROGRESS,
                    "phase": _detect_phase(text, PHASE_IMPLEMENTATION),
                    "confidence": CONFIDENCE_EXPLICIT,
                    "owner": OWNER_TEAM_MEMBER,
                }
            if is_future:
                return {
                    "kind": A_ASSIGNEE_FUTURE_INTENT,
                    "rule_id": "comment.delivery_promised_not_done",
                    "decisive": True,
                    "state": ACTIVE_IN_PROGRESS,
                    "phase": _detect_phase(text, PHASE_IMPLEMENTATION),
                    "confidence": CONFIDENCE_EXPLICIT,
                    "owner": OWNER_TEAM_MEMBER,
                }
            if _addressed_to_client(text) or _any(_VALIDATION_REQUEST_CUES, text):
                return {
                    "kind": A_DELIVERED,
                    "rule_id": "comment.delivered_awaiting_client_acceptance",
                    "decisive": True,
                    "state": WAITING_CLIENT_ACCEPTANCE,
                    "phase": PHASE_DELIVERY_REVIEW,
                    "confidence": CONFIDENCE_EXPLICIT,
                    "owner": OWNER_CLIENT,
                    "preserves_waiting_since": is_chase,
                }

        client_input = _matched(_CLIENT_INPUT_CUES, text)
        if client_input and not _negated_near(text, client_input):
            if has_remaining_work:
                return {
                    "kind": A_REMAINING_WORK,
                    "rule_id": "comment.client_input_with_parallel_work",
                    "decisive": True,
                    "state": ACTIVE_IN_PROGRESS,
                    "phase": _detect_phase(text, PHASE_DISCOVERY),
                    "confidence": CONFIDENCE_EXPLICIT,
                    "owner": OWNER_TEAM_MEMBER,
                }
            return {
                "kind": A_CLIENT_INPUT_NEEDED,
                "rule_id": "comment.client_input_blocking",
                "decisive": True,
                "state": WAITING_CLIENT_INPUT,
                "phase": _detect_phase(text),
                "confidence": CONFIDENCE_EXPLICIT,
                "owner": OWNER_CLIENT,
                "preserves_waiting_since": is_chase,
            }

        peer_owned = _matched(_PEER_REVIEW_OWNED_CUES, text)
        if peer_owned and role == ROLE_ASSIGNEE and not _negated_near(text, peer_owned):
            return {
                "kind": A_PEER_REVIEW_OWNED,
                "rule_id": "comment.assignee_owns_review",
                "decisive": True,
                "state": ACTIVE_REVIEW,
                "phase": PHASE_PEER_REVIEW,
                "confidence": CONFIDENCE_EXPLICIT,
                "owner": OWNER_TEAM_MEMBER,
            }

        handoff = _matched(_INTERNAL_HANDOFF_CUES, text)
        if handoff and re.search(r"@\w", text) and not _negated_near(text, handoff):
            return {
                "kind": A_INTERNAL_HANDOFF,
                "rule_id": "comment.internal_reviewer_named",
                "decisive": True,
                "state": WAITING_INTERNAL,
                "phase": PHASE_PEER_REVIEW,
                "confidence": CONFIDENCE_INFERRED,
                "owner": OWNER_INTERNAL,
            }

        if has_remaining_work or _any(_RESUMPTION_CUES, text):
            return {
                "kind": A_ASSIGNEE_PROGRESS,
                "rule_id": "comment.assignee_work_in_progress",
                "decisive": True,
                "state": ACTIVE_IN_PROGRESS,
                "phase": _detect_phase(text, PHASE_IMPLEMENTATION),
                "confidence": CONFIDENCE_INFERRED,
                "owner": OWNER_TEAM_MEMBER,
            }

        # A routine chase is not new development activity, and a neutral
        # internal note cannot cancel unresolved technical work.
        return {
            "kind": A_ASSIGNEE_PROGRESS if not is_chase else A_CLIENT_NEUTRAL,
            "rule_id": "comment.internal_note_non_decisive",
            "decisive": False,
            "phase": PHASE_UNKNOWN,
            "preserves_waiting_since": True,
        }

    # ---- statements by a client or an unidentified author ----
    close_request = _matched(_CLIENT_CLOSE_CUES, text)
    acceptance = _matched(_CLIENT_ACCEPTANCE_CUES, text)
    approval = _matched(_CLIENT_APPROVAL_CUES, text)
    problem = _matched(_CLIENT_PROBLEM_CUES, text)
    neutral_only = _any(_CLIENT_NEUTRAL_CUES, text) and not (
        close_request or approval or problem
    )

    if problem and not _negated_near(text, problem):
        # Reactivation is the conservative direction: it never hides work, so
        # an unidentified reporter is accepted with lower confidence.
        return {
            "kind": A_CLIENT_PROBLEM,
            "rule_id": "comment.client_reported_problem",
            "decisive": True,
            "state": ACTIVE_READY,
            "phase": PHASE_DISCOVERY,
            "confidence": (
                CONFIDENCE_EXPLICIT if is_client_author else CONFIDENCE_INFERRED
            ),
            "owner": OWNER_TEAM_MEMBER,
            "implementation_authorized": False,
        }

    if close_request and not _negated_near(text, close_request):
        if is_client_author:
            return {
                "kind": A_CLIENT_CLOSE_REQUEST,
                "rule_id": "comment.client_accepted_and_requested_closure",
                "decisive": True,
                "state": ACTIVE_READY,
                "phase": PHASE_CLOSURE,
                "confidence": CONFIDENCE_EXPLICIT,
                "owner": OWNER_TEAM_MEMBER,
                "closure_only": True,
            }
        return {
            "kind": A_AMBIGUOUS_APPROVAL,
            "rule_id": "comment.closure_requested_by_unknown_author",
            "decisive": True,
            "state": REVIEW_REQUIRED,
            "phase": PHASE_CLOSURE,
            "confidence": CONFIDENCE_REVIEW_REQUIRED,
            "owner": OWNER_UNKNOWN,
        }

    if neutral_only:
        return {
            "kind": A_CLIENT_NEUTRAL,
            "rule_id": "comment.client_neutral_acknowledgement",
            "decisive": False,
            "phase": PHASE_UNKNOWN,
            "preserves_waiting_since": True,
        }

    if approval and not _negated_near(text, approval):
        if not is_client_author:
            return {
                "kind": A_AMBIGUOUS_APPROVAL,
                "rule_id": "comment.approval_from_unidentified_author",
                "decisive": True,
                "state": REVIEW_REQUIRED,
                "phase": PHASE_UNKNOWN,
                "confidence": CONFIDENCE_REVIEW_REQUIRED,
                "owner": OWNER_UNKNOWN,
            }
        partial = _matched(_PARTIAL_APPROVAL_CUES, text)
        if partial:
            return {
                "kind": A_CLIENT_APPROVAL,
                "rule_id": "comment.client_approved_discovery_only",
                "decisive": True,
                "state": ACTIVE_READY,
                "phase": PHASE_DISCOVERY,
                "confidence": CONFIDENCE_EXPLICIT,
                "owner": OWNER_TEAM_MEMBER,
                "implementation_authorized": False,
            }
        return {
            "kind": A_CLIENT_APPROVAL,
            "rule_id": "comment.client_approved_next_phase",
            "decisive": True,
            "state": ACTIVE_READY,
            "phase": PHASE_IMPLEMENTATION,
            "confidence": CONFIDENCE_EXPLICIT,
            "owner": OWNER_TEAM_MEMBER,
            "implementation_authorized": True,
        }

    if acceptance and not _negated_near(text, acceptance) and is_client_author:
        return {
            "kind": A_CLIENT_CLOSE_REQUEST,
            "rule_id": "comment.client_accepted_delivery",
            "decisive": True,
            "state": ACTIVE_READY,
            "phase": PHASE_CLOSURE,
            "confidence": CONFIDENCE_EXPLICIT,
            "owner": OWNER_TEAM_MEMBER,
            "closure_only": True,
        }

    if "?" in (comment.get("text") or ""):
        return {
            "kind": A_CLIENT_QUESTION,
            "rule_id": "comment.client_question_awaiting_answer",
            "decisive": True,
            "state": ACTIVE_READY,
            "phase": PHASE_UNKNOWN,
            "confidence": (
                CONFIDENCE_EXPLICIT if is_client_author else CONFIDENCE_INFERRED
            ),
            "owner": OWNER_TEAM_MEMBER,
            "implementation_authorized": False,
        }

    return {
        "kind": A_CLIENT_NEUTRAL,
        "rule_id": "comment.no_actionable_signal",
        "decisive": False,
        "phase": PHASE_UNKNOWN,
        "preserves_waiting_since": True,
    }


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

_DEFAULT_STATUS_PHASES = {
    "open": PHASE_SCOPING,
    "scoping": PHASE_SCOPING,
    "in progress": PHASE_IMPLEMENTATION,
    "waiting for approval": PHASE_ESTIMATE,
    "pending client": PHASE_UNKNOWN,
    "client review": PHASE_DELIVERY_REVIEW,
    "sensitive client review": PHASE_DELIVERY_REVIEW,
    "pending": PHASE_UNKNOWN,
    "peer review": PHASE_PEER_REVIEW,
}


def _matches_exclusion(source, policy):
    """
    Hard scope rules. Project and issue type come from Jira metadata; a key
    prefix is only a legacy fallback. Legacy summary text never overrides a
    known issue type.
    """
    project_key = source.get("project_key")
    project_from_metadata = bool(project_key)
    if not project_key:
        key = source.get("key") or ""
        project_key = key.split("-", 1)[0] if "-" in key else None

    type_id = source.get("issue_type_id")
    type_name = source.get("issue_type_name")

    for rule in policy.get("exclusion_rules") or []:
        projects = rule.get("projects") or []
        if projects and project_key not in projects:
            continue
        if type_id and type_id in (rule.get("issue_type_ids") or []):
            return rule, project_from_metadata, False
        if type_name:
            names = {
                normalize_text(name) for name in (rule.get("issue_type_names") or [])
            }
            if normalize_text(type_name) in names:
                return rule, project_from_metadata, False
        if not type_id and not type_name:
            # Missing type metadata: a legacy exclusion candidate for review,
            # never a confident silent exclusion.
            summary = normalize_text(source.get("summary"))
            names = [
                normalize_text(name) for name in (rule.get("issue_type_names") or [])
            ]
            if any(name and name in summary for name in names):
                return rule, project_from_metadata, True
    return None, project_from_metadata, False


def _status_default(source, policy):
    status_id = source.get("status_id")
    status_name = source.get("status_name") or ""
    by_id = policy.get("status_defaults_by_id") or {}
    if status_id and str(status_id) in by_id:
        return by_id[str(status_id)], f"status_id:{status_id}"

    by_name = {
        normalize_text(name): state
        for name, state in (policy.get("status_defaults_by_name") or {}).items()
    }
    normalized = normalize_text(status_name)
    if normalized in by_name:
        return by_name[normalized], f"status_name:{status_name}"
    return None, f"status_unmapped:{status_name or 'unknown'}"


def _client_approval_status(source, policy):
    """
    Whether this project/status is confirmed to mean a client approval wait.
    Kept configurable per project/status so internal approvals are never
    hidden as client work.
    """
    project_key = source.get("project_key") or (source.get("key") or "").split("-")[0]
    by_id = policy.get("client_approval_status_ids_by_project") or {}
    by_name = policy.get("client_approval_status_names_by_project") or {}
    status_id = str(source.get("status_id") or "")
    status_name = normalize_text(source.get("status_name"))

    if status_id and status_id in {str(s) for s in by_id.get(project_key, [])}:
        return True
    names = {normalize_text(n) for n in by_name.get(project_key, [])}
    return bool(status_name and status_name in names)


def _build_timeline(source, identity, policy):
    """Chronological comments and real status transitions."""
    assignee_account_id = source.get("assignee_account_id")
    events = []

    for comment in source.get("comments") or []:
        created = _parse_iso(comment.get("created"))
        if created is None:
            continue
        role = identity.role_for(
            comment.get("author_account_id"),
            comment.get("author_display"),
            assignee_account_id,
        )
        text = normalize_text(comment.get("text"))
        assertion = detect_assertion(comment, role, text=text)
        updated = _parse_iso(comment.get("updated"))
        events.append(
            {
                "type": "comment",
                # Conversation order uses comment.created: editing a historical
                # comment must not make it the newest approval or wait.
                "at": created,
                "comment_id": comment.get("id"),
                "author_role": role,
                "author_display": comment.get("author_display"),
                "author_account_id": comment.get("author_account_id"),
                "edited": bool(updated and created and updated > created),
                "update_author_account_id": comment.get("update_author_account_id"),
                "excerpt": (comment.get("text") or "")[:240],
                "truncated": comment.get("truncated"),
                "assertion": assertion,
            }
        )

    status_defaults_by_name = {
        normalize_text(name): state
        for name, state in (policy.get("status_defaults_by_name") or {}).items()
    }
    for transition in source.get("transitions") or []:
        at = _parse_iso(transition.get("at"))
        if at is None:
            continue
        to_status = transition.get("to")
        mapped = status_defaults_by_name.get(normalize_text(to_status))
        events.append(
            {
                "type": "transition",
                "at": at,
                "to_status": to_status,
                "from_status": transition.get("from"),
                "author_account_id": transition.get("author_account_id"),
                "author_display": transition.get("author_display"),
                "mapped_state": mapped,
            }
        )

    events.sort(key=lambda event: (event["at"], 0 if event["type"] == "comment" else 1))
    return events


def _resolve_state(source, events, policy, identity):
    """
    Resolve the current work state by replaying evidence. Statuses provide the
    baseline; later unambiguous comments or real transitions supersede it.
    """
    status_state, status_reason = _status_default(source, policy)
    status_name = source.get("status_name") or "unknown"
    normalized_status = normalize_text(status_name)

    if status_state == WAITING_CLIENT_ESTIMATE_APPROVAL and not _client_approval_status(
        source, policy
    ):
        # "Waiting for Approval" only means a client wait where that workflow's
        # approver is confirmed to be the client.
        status_state = REVIEW_REQUIRED
        status_reason = (
            f"status '{status_name}' has no confirmed client approver configured"
        )

    state = status_state
    phase = _DEFAULT_STATUS_PHASES.get(normalized_status, PHASE_UNKNOWN)
    owner = OWNER_CLIENT if state in WAITING_CLIENT_STATES else OWNER_TEAM_MEMBER
    confidence = (
        CONFIDENCE_REVIEW_REQUIRED
        if state in (None, REVIEW_REQUIRED)
        else CONFIDENCE_INFERRED
    )
    rule_id = "status.default"
    reason = f"Status baseline: {status_reason}"
    classification_source = "status_default"
    evidence = []
    waiting_since = None
    actionable_since = None
    # Authorization is never assumed: only confirmed active work carries it.
    implementation_authorized = state in ACTIVE_STATES
    closure_only = False
    conflicts = []

    if state is None:
        state = REVIEW_REQUIRED
        confidence = CONFIDENCE_REVIEW_REQUIRED
        reason = f"No workload policy mapping for status '{status_name}'"
        rule_id = "status.unmapped"

    previous_state = state
    for event in events:
        if event["type"] == "transition":
            mapped = event.get("mapped_state")
            if not mapped:
                continue
            # A real transition performed by automation still changes workflow
            # responsibility, so these are not filtered by author role.
            if mapped == WAITING_CLIENT_ESTIMATE_APPROVAL and not _client_approval_status(
                source, policy
            ):
                mapped = REVIEW_REQUIRED
            if mapped in WAITING_STATES and previous_state in ACTIVE_STATES:
                waiting_since = event["at"]
            if mapped in ACTIVE_STATES and previous_state in WAITING_STATES:
                actionable_since = event["at"]
            state = mapped
            previous_state = mapped
            phase = _DEFAULT_STATUS_PHASES.get(
                normalize_text(event.get("to_status")), phase
            )
            owner = OWNER_CLIENT if state in WAITING_CLIENT_STATES else OWNER_TEAM_MEMBER
            confidence = (
                CONFIDENCE_REVIEW_REQUIRED
                if state == REVIEW_REQUIRED
                else CONFIDENCE_INFERRED
            )
            rule_id = "transition.status_change"
            reason = (
                f"Status changed to '{event.get('to_status')}' on "
                f"{event['at'].date().isoformat()}"
            )
            classification_source = "status_transition"
            continue

        assertion = event.get("assertion") or {}
        if not assertion.get("decisive"):
            continue

        new_state = assertion.get("state")
        if new_state is None:
            continue

        if new_state in WAITING_STATES and previous_state in ACTIVE_STATES:
            waiting_since = event["at"]
        if new_state in WAITING_STATES and waiting_since is None:
            waiting_since = event["at"]
        if new_state in ACTIVE_STATES and previous_state in WAITING_STATES:
            actionable_since = event["at"]

        if assertion.get("preserves_waiting_since") and new_state in WAITING_STATES:
            # A routine "still awaiting your approval" keeps the original date.
            waiting_since = waiting_since or event["at"]

        if event.get("edited"):
            conflicts.append(
                f"comment {event.get('comment_id')} was edited after posting"
            )

        state = new_state
        previous_state = new_state
        phase = assertion.get("phase") or phase
        owner = assertion.get("owner") or owner
        confidence = assertion.get("confidence") or CONFIDENCE_INFERRED
        rule_id = assertion.get("rule_id")
        reason = _assertion_reason(assertion, event)
        classification_source = "comment_evidence"
        implementation_authorized = assertion.get(
            "implementation_authorized", new_state in ACTIVE_STATES
        )
        closure_only = bool(assertion.get("closure_only"))
        evidence.append(
            {
                "comment_id": event.get("comment_id"),
                "author": event.get("author_display"),
                "author_role": event.get("author_role"),
                "at": _iso(event["at"]),
                "excerpt": event.get("excerpt"),
                "rule_id": assertion.get("rule_id"),
                "kind": assertion.get("kind"),
            }
        )

    if state in WAITING_STATES and waiting_since is None:
        waiting_since = _parse_iso(source.get("updated"))

    return {
        "state": state,
        "phase": phase,
        "owner": owner,
        "confidence": confidence,
        "rule_id": rule_id,
        "reason": reason,
        "classification_source": classification_source,
        "evidence": evidence[-3:],
        "waiting_since": waiting_since,
        "actionable_since": actionable_since,
        "implementation_authorized": implementation_authorized,
        "closure_only": closure_only,
        "conflicts": conflicts,
    }


def _assertion_reason(assertion, event):
    author = event.get("author_display") or "an unidentified author"
    when = event["at"].date().isoformat()
    kind = assertion.get("kind")
    texts = {
        A_ESTIMATE_SENT: f"{author} recorded the estimate as sent on {when}; only client approval remains",
        A_DELIVERED: f"{author} recorded delivery on {when}; only client validation remains",
        A_CLIENT_INPUT_NEEDED: f"{author} is blocked on client-provided information since {when}",
        A_INTERNAL_APPROVAL: f"{author} requested internal approval on {when}",
        A_INTERNAL_HANDOFF: f"{author} handed the next step to another internal reviewer on {when}",
        A_CLIENT_APPROVAL: f"{author} approved the next phase on {when}",
        A_CLIENT_PROBLEM: f"{author} reported a problem on {when}, so investigation is owed",
        A_CLIENT_QUESTION: f"{author} asked a question on {when} that needs a developer answer",
        A_CLIENT_CLOSE_REQUEST: f"{author} accepted the work on {when}; only closure remains",
        A_AMBIGUOUS_APPROVAL: f"{author} posted an approval on {when} but is not a configured client approver",
        A_ASSIGNEE_PROGRESS: f"{author} recorded work in progress on {when}",
        A_ASSIGNEE_FUTURE_INTENT: f"{author} stated the work is still to be done as of {when}",
        A_REMAINING_WORK: f"{author} still owes work as of {when}",
        A_PEER_REVIEW_OWNED: f"{author} owns the review as of {when}",
    }
    return texts.get(kind, f"{author} posted relevant evidence on {when}")


def _resolve_peer_review(source, resolved, identity):
    """
    Peer Review is only active work when the assignee owns the review. A
    named other reviewer means internal waiting; anything else needs review.
    """
    if normalize_text(source.get("status_name")) != "peer review":
        return resolved
    if resolved["classification_source"] == "comment_evidence":
        return resolved
    resolved = dict(resolved)
    resolved["state"] = REVIEW_REQUIRED
    resolved["confidence"] = CONFIDENCE_REVIEW_REQUIRED
    resolved["rule_id"] = "status.peer_review_owner_unknown"
    resolved["reason"] = (
        "Status is Peer Review but no evidence identifies who owes the review"
    )
    resolved["phase"] = PHASE_PEER_REVIEW
    resolved["owner"] = OWNER_UNKNOWN
    return resolved


def _apply_override(source, resolved, overrides, events, now):
    """
    Apply a valid per-ticket override after evidence evaluation. A newer
    substantive event invalidates a conflicting waiting override: a
    time-limited override is not permission to conceal new obligations.
    """
    override = None
    for ref in (source.get("issue_id"), source.get("key")):
        if ref and str(ref) in (overrides or {}):
            override = overrides[str(ref)]
            break
    if override is None:
        return resolved, None

    if override.get("expires_at") and override["expires_at"] <= now:
        return resolved, "override_expired"

    recorded_at = override.get("recorded_at")
    if override["state"] in WAITING_STATES and recorded_at:
        for event in events:
            if event["at"] <= recorded_at:
                continue
            if event["type"] == "transition" and event.get("mapped_state") in ACTIVE_STATES:
                return resolved, "override_invalidated_by_status_change"
            assertion = event.get("assertion") or {}
            if assertion.get("decisive") and assertion.get("state") in ACTIVE_STATES:
                return resolved, "override_invalidated_by_newer_event"

    resolved = dict(resolved)
    resolved["state"] = override["state"]
    resolved["phase"] = override.get("phase") or resolved["phase"]
    resolved["rule_id"] = "override.manual"
    resolved["reason"] = f"Manual override: {override.get('reason')}"
    resolved["classification_source"] = "override"
    resolved["confidence"] = (
        CONFIDENCE_REVIEW_REQUIRED
        if override["state"] == REVIEW_REQUIRED
        else CONFIDENCE_EXPLICIT
    )
    resolved["owner"] = (
        OWNER_CLIENT if override["state"] in WAITING_CLIENT_STATES else OWNER_TEAM_MEMBER
    )
    resolved["override_size_units"] = override.get("size_units")
    return resolved, None


# --------------------------------------------------------------------------
# Attention metrics
# --------------------------------------------------------------------------


def _assignment_episode(source, identity, calendar):
    """
    Start of the current assignment episode, matched on accountId. Falls back
    to creation only when the available history shows no later assignment,
    and marks that it is an assumption.
    """
    assignee_account_id = source.get("assignee_account_id")
    assignee_display = source.get("assignee_display")
    latest = None
    assigned_by = None

    for assignment in source.get("assignments") or []:
        at = _parse_iso(assignment.get("at"))
        if at is None:
            continue
        matches = False
        if assignee_account_id and assignment.get("to_account_id"):
            matches = assignment["to_account_id"] == assignee_account_id
        elif assignee_display and assignment.get("to_display"):
            matches = normalize_text(assignment["to_display"]) == normalize_text(
                assignee_display
            )
        if matches and (latest is None or at > latest):
            latest = at
            assigned_by = assignment.get("author_display")

    if latest is not None:
        return latest, assigned_by, False

    assumed = not source.get("changelog_complete", True)
    return _parse_iso(source.get("created")), None, True or assumed


def _meaningful_assignee_activity(source, events, identity, policy):
    """
    Substantive assignee engagement. Self-assignment, label/priority edits,
    generic acknowledgements, bot reminders, and routine chasing do not count.
    """
    min_chars = (policy.get("attention") or {}).get(
        "meaningful_comment_min_chars", 40
    )
    progress_statuses = {
        normalize_text(name)
        for name in (policy.get("attention") or {}).get("progress_status_names") or []
    }
    assignee_account_id = source.get("assignee_account_id")
    timestamps = []

    for event in events:
        if event["type"] == "transition":
            if (
                assignee_account_id
                and event.get("author_account_id") == assignee_account_id
                and normalize_text(event.get("to_status")) in progress_statuses
            ):
                timestamps.append(event["at"])
            continue

        if event.get("author_role") != ROLE_ASSIGNEE:
            continue
        assertion = event.get("assertion") or {}
        kind = assertion.get("kind")
        if kind in (A_BOT_BOILERPLATE, A_CLIENT_NEUTRAL):
            continue
        if assertion.get("rule_id") == "comment.internal_note_non_decisive":
            # Generic internal note: only counts when it is substantive.
            if len(event.get("excerpt") or "") < min_chars:
                continue
        timestamps.append(event["at"])

    timestamps.sort()
    return timestamps


def _attention_metrics(source, events, resolved, identity, policy, calendar, now):
    assignment_start, assigned_by, assignment_assumed = _assignment_episode(
        source, identity, calendar
    )
    actionable_since = resolved.get("actionable_since")
    if actionable_since is None and resolved["state"] in ACTIVE_STATES:
        actionable_since = assignment_start

    # The clock starts at the later of the assignment start and the current
    # actionable window: approval after ten waiting days does not make the
    # assignee ten days late immediately.
    clock_candidates = [c for c in (assignment_start, actionable_since) if c]
    clock_start = max(clock_candidates) if clock_candidates else None

    activity = _meaningful_assignee_activity(source, events, identity, policy)
    in_window = [stamp for stamp in activity if clock_start and stamp >= clock_start]
    first_engagement = in_window[0] if in_window else None
    last_activity = activity[-1] if activity else None

    attention = policy.get("attention") or {}
    first_response_days = attention.get("first_response_business_days", 2)
    progress_days = attention.get("progress_business_days", 3)

    waiting_first_response = (
        calendar.business_days_between(clock_start, now) if clock_start else 0.0
    )
    reference = first_engagement or clock_start
    business_since_progress = (
        calendar.business_days_between(
            max(filter(None, [last_activity, clock_start])) if clock_start else last_activity,
            now,
        )
        if (clock_start or last_activity)
        else 0.0
    )

    is_active = resolved["state"] in ACTIVE_STATES
    first_response_overdue = bool(
        is_active
        and first_engagement is None
        and clock_start is not None
        and waiting_first_response >= first_response_days
    )
    progress_overdue = bool(
        is_active
        and first_engagement is not None
        and business_since_progress >= progress_days
    )

    client_followup_days = attention.get("client_followup_business_days", 5)
    waiting_since = resolved.get("waiting_since")
    client_followup_due = bool(
        resolved["state"] in WAITING_CLIENT_STATES
        and waiting_since is not None
        and calendar.business_days_between(waiting_since, now) >= client_followup_days
    )

    return {
        "assignment_episode_start": _iso(assignment_start),
        "assignment_start_assumed": assignment_assumed,
        "assigned_by": assigned_by,
        "actionable_window_start": _iso(actionable_since),
        "attention_clock_start": _iso(clock_start),
        "first_meaningful_engagement_at": _iso(first_engagement),
        "last_meaningful_assignee_activity_at": _iso(last_activity),
        "business_days_awaiting_first_engagement": round(waiting_first_response, 2),
        "business_days_since_progress": round(business_since_progress, 2),
        "first_response_overdue": first_response_overdue,
        "progress_overdue": progress_overdue,
        "client_followup_due": client_followup_due,
        "started": first_engagement is not None,
        "_reference": reference,
    }


# --------------------------------------------------------------------------
# Effort scoring
# --------------------------------------------------------------------------


def _band_units(hours, effort):
    bounds = effort.get("hour_upper_bounds") or []
    units = effort.get("band_units") or []
    for index, bound in enumerate(bounds):
        if hours <= bound:
            return units[index]
    return units[-1] if units else 1.0


def score_effort(source, resolved, attention, policy, override_units=None):
    """
    Gate responsibility first, then estimate effort. Waiting and excluded
    states are exactly zero; REVIEW_REQUIRED is provisional.

    These are configurable planning proxies, not billable hours, utilization,
    or a claim about productivity.
    """
    effort = policy.get("effort") or {}
    state = resolved["state"]

    if state == EXCLUDED or state in WAITING_STATES:
        return {
            "active_units": 0.0,
            "provisional_units": 0.0,
            "planning_units": 0.0,
            "effort_source": "not_applicable",
            "effort_quality": "not_applicable",
        }

    if state == REVIEW_REQUIRED:
        units = float(effort.get("review_required_units", 1.0))
        return {
            "active_units": 0.0,
            "provisional_units": units,
            "planning_units": units,
            "effort_source": "review_required",
            "effort_quality": "provisional",
        }

    if override_units is not None:
        units = float(override_units)
        return {
            "active_units": units,
            "provisional_units": 0.0,
            "planning_units": units,
            "effort_source": "configured_override",
            "effort_quality": "configured",
        }

    if resolved.get("closure_only"):
        units = float(effort.get("closure_units", 0.25))
        return {
            "active_units": units,
            "provisional_units": 0.0,
            "planning_units": units,
            "effort_source": "closure_proxy",
            "effort_quality": "administrative_proxy",
        }

    remaining = source.get("remaining_estimate_seconds")
    original = source.get("original_estimate_seconds")

    if isinstance(remaining, (int, float)) and remaining > 0:
        units = _band_units(remaining / 3600.0, effort)
        return {
            "active_units": float(units),
            "provisional_units": 0.0,
            "planning_units": float(units),
            "effort_source": "remaining_estimate",
            "effort_quality": "reported",
        }

    if isinstance(remaining, (int, float)) and remaining == 0:
        # A zero remaining estimate on actionable work is a data-quality
        # conflict, not zero workload.
        units = float(effort.get("unknown_units", 1.0))
        return {
            "active_units": units,
            "provisional_units": 0.0,
            "planning_units": units,
            "effort_source": "unknown",
            "effort_quality": "conflict_zero_remaining_estimate",
        }

    if (
        isinstance(original, (int, float))
        and original > 0
        and not attention.get("started")
    ):
        units = _band_units(original / 3600.0, effort)
        return {
            "active_units": float(units),
            "provisional_units": 0.0,
            "planning_units": float(units),
            "effort_source": "original_estimate_proxy",
            "effort_quality": "proxy_phase_not_started",
        }

    units = float(effort.get("unknown_units", 1.0))
    return {
        "active_units": units,
        "provisional_units": 0.0,
        "planning_units": units,
        "effort_source": "unknown",
        "effort_quality": "unknown",
    }


# --------------------------------------------------------------------------
# Public classification entry points
# --------------------------------------------------------------------------


def classify_workload(source, policy, identity, overrides=None, now=None):
    """
    Classify one normalized workload source. Pure: same inputs, same output.

    Returns the workload-specific fields only. Shared staleness values are
    never replaced.
    """
    now = now or datetime.now(timezone.utc)
    calendar = BusinessCalendar(policy.get("business_calendar"))
    data_quality = []

    if not source or source.get("schema") != SOURCE_SCHEMA_VERSION:
        return _legacy_review_required(source, policy, now, reason="missing_workload_source")

    rule, project_from_metadata, needs_review = _matches_exclusion(source, policy)
    if not project_from_metadata:
        data_quality.append("project_key_inferred_from_issue_key")

    if rule is not None and not needs_review:
        return _finalize(
            source=source,
            policy=policy,
            resolved={
                "state": EXCLUDED,
                "phase": PHASE_UNKNOWN,
                "owner": OWNER_UNKNOWN,
                "confidence": CONFIDENCE_EXPLICIT,
                "rule_id": f"exclusion.{rule['id']}",
                "reason": rule.get("reason")
                or "Excluded from workload by configuration",
                "classification_source": "exclusion_rule",
                "evidence": [],
                "waiting_since": None,
                "actionable_since": None,
                "implementation_authorized": False,
                "closure_only": False,
                "conflicts": [],
            },
            attention={},
            scoring={
                "active_units": 0.0,
                "provisional_units": 0.0,
                "planning_units": 0.0,
                "effort_source": "not_applicable",
                "effort_quality": "not_applicable",
            },
            data_quality=data_quality,
            now=now,
        )

    if rule is not None and needs_review:
        data_quality.append("issue_type_metadata_missing")

    events = _build_timeline(source, identity, policy)
    resolved = _resolve_state(source, events, policy, identity)
    resolved = _resolve_peer_review(source, resolved, identity)

    resolved, override_note = _apply_override(
        source, resolved, overrides, events, now
    )
    if override_note:
        data_quality.append(override_note)

    history_complete = bool(
        source.get("comments_complete", True) and source.get("changelog_complete", True)
    )
    if not history_complete:
        data_quality.append("history_incomplete")
        if resolved["state"] in WAITING_STATES or resolved["state"] == EXCLUDED:
            # Truncated or failed retrieval cannot establish a new confident
            # exclusion from active work.
            resolved = dict(resolved)
            resolved["state"] = REVIEW_REQUIRED
            resolved["confidence"] = CONFIDENCE_REVIEW_REQUIRED
            resolved["rule_id"] = "evidence.incomplete_history"
            resolved["reason"] = (
                "Comment or changelog history was incomplete, so a waiting "
                "classification cannot be confirmed"
            )

    if resolved.get("conflicts"):
        data_quality.extend(resolved["conflicts"])
        if resolved["state"] in WAITING_STATES:
            resolved = dict(resolved)
            resolved["state"] = REVIEW_REQUIRED
            resolved["confidence"] = CONFIDENCE_REVIEW_REQUIRED
            resolved["rule_id"] = "evidence.conflicting_edit"
            resolved["reason"] = (
                "Evidence was edited after posting and conflicts with newer "
                "activity; chronology could not be resolved"
            )

    if needs_review and resolved["state"] != EXCLUDED:
        resolved = dict(resolved)
        resolved["state"] = REVIEW_REQUIRED
        resolved["confidence"] = CONFIDENCE_REVIEW_REQUIRED
        resolved["rule_id"] = "exclusion.legacy_candidate_needs_metadata"
        resolved["reason"] = (
            "Issue type metadata is missing; a possible workload exclusion "
            "needs confirmation after the next refresh"
        )

    attention = _attention_metrics(
        source, events, resolved, identity, policy, calendar, now
    )
    scoring = score_effort(
        source,
        resolved,
        attention,
        policy,
        override_units=resolved.get("override_size_units"),
    )

    if attention.get("assignment_start_assumed"):
        data_quality.append("assignment_date_assumed_from_creation")

    return _finalize(
        source=source,
        policy=policy,
        resolved=resolved,
        attention=attention,
        scoring=scoring,
        data_quality=data_quality,
        now=now,
    )


def _finalize(source, policy, resolved, attention, scoring, data_quality, now):
    state = resolved["state"]
    include = state in ACTIVE_STATES

    review_for_reassignment = bool(
        state == ACTIVE_READY
        and not attention.get("started")
        and attention.get("first_response_overdue")
    )

    fields = {
        "workload_state": state,
        "workload_state_label": STATE_LABELS.get(state, state),
        "work_phase": resolved.get("phase") or PHASE_UNKNOWN,
        "next_action_owner_role": resolved.get("owner") or OWNER_UNKNOWN,
        "next_action_owner_account_id": (
            source.get("assignee_account_id")
            if resolved.get("owner") == OWNER_TEAM_MEMBER
            else None
        ),
        "include_in_active_capacity": include,
        "active_units": scoring["active_units"],
        "provisional_units": scoring["provisional_units"],
        "planning_units": scoring["planning_units"],
        "effort_source": scoring["effort_source"],
        "effort_quality": scoring["effort_quality"],
        "classification_source": resolved.get("classification_source"),
        "classification_confidence": resolved.get("confidence"),
        "rule_id": resolved.get("rule_id"),
        "workload_reason": resolved.get("reason"),
        "evidence": resolved.get("evidence") or [],
        "waiting_since": _iso(resolved.get("waiting_since")),
        "actionable_since": attention.get("actionable_window_start"),
        "implementation_authorized": resolved.get("implementation_authorized", True),
        "source_history_complete": bool(
            source.get("comments_complete", True)
            and source.get("changelog_complete", True)
        ),
        "data_quality_reasons": sorted(set(data_quality)),
        "policy_version": policy.get("policy_version"),
        "classifier_version": CLASSIFIER_VERSION,
        "review_for_reassignment": review_for_reassignment,
        "review_for_reassignment_reason": (
            "Ready to work, not started in the current window, and overdue for "
            "first engagement"
            if review_for_reassignment
            else None
        ),
        "classification_stale": False,
    }

    for key in (
        "assignment_episode_start",
        "assignment_start_assumed",
        "actionable_window_start",
        "attention_clock_start",
        "first_meaningful_engagement_at",
        "last_meaningful_assignee_activity_at",
        "business_days_awaiting_first_engagement",
        "business_days_since_progress",
        "first_response_overdue",
        "progress_overdue",
        "client_followup_due",
        "started",
    ):
        fields[key] = attention.get(key)

    fields.setdefault("first_response_overdue", False)
    fields.setdefault("progress_overdue", False)
    fields.setdefault("client_followup_due", False)
    fields.setdefault("started", False)
    return fields


def _legacy_review_required(source, policy, now, reason):
    """
    Old cache rows without workload source data are shown as REVIEW_REQUIRED
    with provisional units and an explanation, never as False/zero or a
    silent exclusion.
    """
    units = float((policy.get("effort") or {}).get("review_required_units", 1.0))
    return {
        "workload_state": REVIEW_REQUIRED,
        "workload_state_label": STATE_LABELS[REVIEW_REQUIRED],
        "work_phase": PHASE_UNKNOWN,
        "next_action_owner_role": OWNER_UNKNOWN,
        "next_action_owner_account_id": None,
        "include_in_active_capacity": False,
        "active_units": 0.0,
        "provisional_units": units,
        "planning_units": units,
        "effort_source": "review_required",
        "effort_quality": "provisional",
        "classification_source": "legacy_cache",
        "classification_confidence": CONFIDENCE_REVIEW_REQUIRED,
        "rule_id": "cache.missing_workload_source",
        "workload_reason": (
            "This ticket was cached before workload classification existed. "
            "It will classify after the next background refresh."
        ),
        "evidence": [],
        "waiting_since": None,
        "actionable_since": None,
        "implementation_authorized": False,
        "source_history_complete": False,
        "data_quality_reasons": [reason],
        "policy_version": policy.get("policy_version"),
        "classifier_version": CLASSIFIER_VERSION,
        "review_for_reassignment": False,
        "review_for_reassignment_reason": None,
        "classification_stale": True,
        "assignment_episode_start": None,
        "assignment_start_assumed": True,
        "actionable_window_start": None,
        "attention_clock_start": None,
        "first_meaningful_engagement_at": None,
        "last_meaningful_assignee_activity_at": None,
        "business_days_awaiting_first_engagement": 0.0,
        "business_days_since_progress": 0.0,
        "first_response_overdue": False,
        "progress_overdue": False,
        "client_followup_due": False,
        "started": False,
    }


def classify_issue(
    issue,
    comments,
    histories,
    policy,
    identity,
    overrides=None,
    now=None,
    comments_complete=True,
    changelog_complete=True,
):
    """Convenience wrapper: normalize a raw issue, then classify it."""
    source = build_workload_source(
        issue,
        comments=comments,
        histories=histories,
        comments_complete=comments_complete,
        changelog_complete=changelog_complete,
    )
    fields = classify_workload(source, policy, identity, overrides=overrides, now=now)
    return source, fields


def reclassify_report(report, policy, identity, overrides=None, now=None):
    """
    Recompute workload fields for cached rows without touching Jira. Lets a
    policy or override change take effect on the next page render, and
    re-evaluates time-dependent attention thresholds on refresh.
    """
    now = now or datetime.now(timezone.utc)
    for row in report or []:
        source = row.get("workload_source")
        fields = classify_workload(
            source, policy, identity, overrides=overrides, now=now
        )
        row.update(fields)
    return report


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


def _member_settings(policy, account_id):
    settings = (policy.get("member_settings_by_account_id") or {}).get(
        account_id or "", {}
    )
    return {
        "planning_capacity_units": settings.get("planning_capacity_units"),
        "max_started_tickets": settings.get("max_started_tickets"),
        "role_group": settings.get("role_group") or "unspecified",
        "assignment_eligible": settings.get("assignment_eligible"),
        "availability": settings.get("availability"),
    }


def _state_bucket(state):
    if state in ACTIVE_STATES:
        return "active"
    if state in WAITING_CLIENT_STATES:
        return "waiting_client"
    if state in WAITING_OTHER_STATES:
        return "waiting_other"
    if state == REVIEW_REQUIRED:
        return "review_required"
    return "excluded"


def build_workload_report(report, team_members, policy, identity, now=None):
    """
    Aggregate classified rows into the canonical per-person snapshot.

    Capacity and peer comparisons are always computed from this full snapshot,
    before any client/project display filter, so a filtered slice can never
    redefine somebody's overall capacity.
    """
    now = now or datetime.now(timezone.utc)
    attention_policy = policy.get("attention") or {}
    capacity_policy = policy.get("capacity") or {}

    display_to_account = {}
    for name, ids in (identity.display_to_account_ids or {}).items():
        if len(ids) == 1:
            display_to_account[name] = next(iter(ids))

    people = {}
    for name in team_members:
        people[name] = {
            "name": name,
            "account_id": display_to_account.get(name),
            "rows": [],
            "buckets": {
                "active": [],
                "waiting_client": [],
                "waiting_other": [],
                "review_required": [],
                "excluded": [],
            },
        }

    unknown_assignees = {}
    for row in report or []:
        assignee = row.get("assignee")
        bucket = _state_bucket(row.get("workload_state"))
        target = people.get(assignee)
        if target is None:
            if assignee and assignee != "Unassigned":
                unknown_assignees.setdefault(assignee, 0)
                unknown_assignees[assignee] += 1
            continue
        target["rows"].append(row)
        target["buckets"][bucket].append(row)

    summaries = []
    for name in team_members:
        person = people[name]
        buckets = person["buckets"]
        active = buckets["active"]
        review = buckets["review_required"]

        active_units = round(sum(r.get("active_units") or 0.0 for r in active), 2)
        provisional_units = round(
            sum(r.get("provisional_units") or 0.0 for r in review), 2
        )
        planning_units = round(active_units + provisional_units, 2)

        waiting_client_by_reason = {}
        for row in buckets["waiting_client"]:
            state = row.get("workload_state")
            waiting_client_by_reason.setdefault(state, 0)
            waiting_client_by_reason[state] += 1

        started = [r for r in active if r.get("started")]
        overdue_rows = [
            r
            for r in active
            if r.get("first_response_overdue") or r.get("progress_overdue")
        ]
        overdue_count = len(overdue_rows)
        active_count = len(active)
        ratio = (overdue_count / active_count) if active_count else 0.0
        attention_warning = bool(
            overdue_count >= attention_policy.get("warning_min_tickets", 3)
            and ratio >= attention_policy.get("warning_min_ratio", 0.25)
        )

        settings = _member_settings(policy, person["account_id"])
        owned_total = (
            active_count
            + len(buckets["waiting_client"])
            + len(buckets["waiting_other"])
            + len(review)
        )

        summaries.append(
            {
                "name": name,
                "account_id": person["account_id"],
                "owned_total": owned_total,
                "confirmed_active_count": active_count,
                "started_count": len(started),
                "waiting_client_count": len(buckets["waiting_client"]),
                "waiting_client_by_reason": waiting_client_by_reason,
                "waiting_other_count": len(buckets["waiting_other"]),
                "review_required_count": len(review),
                "excluded_count": len(buckets["excluded"]),
                "active_units": active_units,
                "provisional_units": provisional_units,
                "planning_units": planning_units,
                "unknown_effort_count": sum(
                    1 for r in active if r.get("effort_source") == "unknown"
                ),
                "proxy_effort_count": sum(
                    1
                    for r in active
                    if r.get("effort_source") == "original_estimate_proxy"
                ),
                "overdue_active_count": overdue_count,
                "overdue_active_ratio": round(ratio, 3),
                "attention_warning": attention_warning,
                "client_followup_due_count": sum(
                    1 for r in buckets["waiting_client"] if r.get("client_followup_due")
                ),
                "review_for_reassignment": [
                    r for r in active if r.get("review_for_reassignment")
                ],
                "urgent_active": [
                    r for r in active if (r.get("priority_name") or "") in ("Highest", "High")
                ],
                "settings": settings,
                "rows_by_bucket": buckets,
                "has_classification_uncertainty": bool(review)
                or any(r.get("classification_stale") for r in person["rows"]),
            }
        )

    _apply_capacity(summaries, policy, capacity_policy)
    summaries.sort(key=lambda person: (-person["planning_units"], person["name"]))

    return {
        "people": summaries,
        "policy_version": policy.get("policy_version"),
        "classifier_version": CLASSIFIER_VERSION,
        "setup_issues": list(identity.setup_issues),
        "unknown_assignees": unknown_assignees,
        "reconciliation": build_reconciliation(report, summaries),
        "generated_at": _iso(now),
    }


def _apply_capacity(summaries, policy, capacity_policy):
    """
    Absolute configured capacity first; peer comparison only as secondary
    context. Never invent a capacity value, and never divide by zero.
    """
    available_ratio = capacity_policy.get("available_below_ratio", 0.7)
    peer_min_people = capacity_policy.get("peer_min_people", 4)
    peer_high_ratio = capacity_policy.get("peer_high_ratio", 1.5)
    peer_min_excess = capacity_policy.get("peer_min_excess_units", 2.0)

    by_role = {}
    for person in summaries:
        settings = person["settings"]
        eligible = settings.get("assignment_eligible")
        availability = settings.get("availability")
        comparable = eligible is not False and availability not in ("unavailable",)
        if comparable:
            by_role.setdefault(settings.get("role_group"), []).append(person)

    for person in summaries:
        settings = person["settings"]
        capacity = settings.get("planning_capacity_units")
        availability = settings.get("availability")
        eligible = settings.get("assignment_eligible")
        load = person["planning_units"]

        status = "capacity_not_calibrated"
        label = "Capacity not calibrated"
        if availability in ("unavailable",) or capacity == 0:
            status = "unavailable"
            label = "Unavailable for assignment"
        elif isinstance(capacity, (int, float)) and capacity > 0:
            utilization = load / capacity
            if load > capacity:
                status = "above_capacity"
                label = "Above configured capacity"
            elif utilization >= available_ratio:
                status = "at_or_near_capacity"
                label = "At or near configured capacity"
            elif (
                eligible is not False
                and availability not in (None, "unknown", "unavailable")
                and not person["has_classification_uncertainty"]
                and not person["attention_warning"]
            ):
                status = "possible_capacity_for_more"
                label = "Possible capacity for more work"
            else:
                status = "below_capacity_unconfirmed"
                label = "Below capacity, availability or classification unconfirmed"
            person["capacity_utilization"] = round(utilization, 3)
        person["capacity_status"] = status
        person["capacity_label"] = label
        person["planning_capacity_units"] = capacity
        person["availability_label"] = (
            availability if availability else "availability not configured"
        )

        max_started = settings.get("max_started_tickets")
        person["wip_limit_exceeded"] = bool(
            isinstance(max_started, int)
            and max_started >= 0
            and person["started_count"] > max_started
        )

        peers = [
            p
            for p in by_role.get(settings.get("role_group"), [])
            if p["name"] != person["name"]
        ]
        comparable_group = peers + (
            [person]
            if eligible is not False and availability not in ("unavailable",)
            else []
        )
        person["peer_group_size"] = len(comparable_group)
        person["peer_median_units"] = None
        person["higher_than_peers"] = False
        if len(comparable_group) >= peer_min_people:
            median = statistics.median(
                [p["planning_units"] for p in comparable_group]
            )
            person["peer_median_units"] = round(median, 2)
            if median > 0:
                person["higher_than_peers"] = bool(
                    load >= peer_high_ratio * median
                    and (load - median) >= peer_min_excess
                )


def build_reconciliation(report, summaries):
    """
    Explainable reconciliation from fetched scope down to final units, so the
    numbers can be audited rather than trusted.
    """
    counts = {
        "fetched": len(report or []),
        "excluded": 0,
        "waiting_client": 0,
        "waiting_other": 0,
        "confirmed_active": 0,
        "review_required": 0,
        "unknown_effort": 0,
        "incomplete_history": 0,
    }
    for row in report or []:
        counts[_state_bucket(row.get("workload_state"))] = (
            counts.get(_state_bucket(row.get("workload_state")), 0) + 1
        )
        if row.get("effort_source") == "unknown":
            counts["unknown_effort"] += 1
        if not row.get("source_history_complete", True):
            counts["incomplete_history"] += 1

    counts["confirmed_active"] = counts.pop("active", 0)
    counts["active_units"] = round(
        sum(p["active_units"] for p in summaries), 2
    )
    counts["provisional_units"] = round(
        sum(p["provisional_units"] for p in summaries), 2
    )
    counts["planning_units"] = round(
        sum(p["planning_units"] for p in summaries), 2
    )
    counts["owned_total"] = sum(p["owned_total"] for p in summaries)
    return counts


def filter_rows_for_view(rows, view):
    """Display filter for the workload tabs. Never used for capacity math."""
    if view == "active":
        return [r for r in rows if r.get("workload_state") in ACTIVE_STATES]
    if view == "waiting_client":
        return [r for r in rows if r.get("workload_state") in WAITING_CLIENT_STATES]
    if view == "waiting_other":
        return [r for r in rows if r.get("workload_state") in WAITING_OTHER_STATES]
    if view == "review":
        return [r for r in rows if r.get("workload_state") == REVIEW_REQUIRED]
    if view == "excluded":
        return [r for r in rows if r.get("workload_state") == EXCLUDED]
    return list(rows)
