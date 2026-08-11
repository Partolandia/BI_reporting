"""
Run this to see every Jira project your account can access, with its real
project key. Useful for confirming the correct keys to put in
JIRA_PROJECT_KEYS in your .env file.

    python list_projects.py
"""

import os

import requests
from dotenv import load_dotenv

load_dotenv()

SITE_URL = os.environ["JIRA_SITE_URL"].rstrip("/")
EMAIL = os.environ["JIRA_EMAIL"]
API_TOKEN = os.environ["JIRA_API_TOKEN"]

URL = f"{SITE_URL}/rest/api/3/project/search"


def main():
    projects = []
    start_at = 0

    while True:
        response = requests.get(
            URL,
            auth=(EMAIL, API_TOKEN),
            params={"startAt": start_at, "maxResults": 50},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        projects.extend(data["values"])

        if data.get("isLast", True):
            break
        start_at += 50

    print(f"Found {len(projects)} accessible projects:\n")
    print(f"{'Key':<12} {'Name'}")
    print("-" * 50)
    for project in sorted(projects, key=lambda p: p["key"]):
        print(f"{project['key']:<12} {project['name']}")


if __name__ == "__main__":
    main()
