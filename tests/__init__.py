"""
Test suite for the Jira ticket dashboard.

Run everything from the repository root:

    python -m unittest discover -s tests -v

Stdlib unittest on purpose: the project ships only requests, python-dotenv,
Flask and tzdata, and these tests must not add a dependency. No test performs
a live Jira call or any Jira write.
"""

import os

# jira_client reads credentials at import time. Provide harmless defaults so
# the suite runs on a machine without a populated .env, without ever talking
# to a real Jira site.
os.environ.setdefault("JIRA_SITE_URL", "https://example.invalid")
os.environ.setdefault("JIRA_EMAIL", "tests@example.invalid")
os.environ.setdefault("JIRA_API_TOKEN", "test-token-not-real")
os.environ.setdefault("JIRA_PROJECT_KEYS", "CSSD,PIE,ACP,IAP")
os.environ.setdefault(
    "JIRA_TEAM_MEMBERS", "Yaumara Torres,Viacheslav Golovach,Isaac Bernal"
)
