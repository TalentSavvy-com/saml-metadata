import requests
import csv
from collections import defaultdict

# -------------------------------------
# CONFIG
# -------------------------------------
GITLAB_API_URL = "https://gitlab.com/api/v4"
GITLAB_API_TOKEN = "YOUR_TOKEN_HERE"
GITLAB_GROUP_NAME = "your-group-name"
GITLAB_REPO_NAMES = "repo1,repo2,repo3,repo4,repo5,repo6"
# -------------------------------------

HEADERS = {"PRIVATE-TOKEN": GITLAB_API_TOKEN}

def api_get(url, params=None):
    r = requests.get(url, headers=HEADERS, params=params)
    r.raise_for_status()
    return r.json()

def api_get_all(url, params=None):
    """Follow pagination and return combined results."""
    results = []
    page = 1
    while True:
        _params = dict(params or {})
        _params.update({"per_page": 100, "page": page})
        r = requests.get(url, headers=HEADERS, params=_params)
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        results.extend(data)
        if len(data) < 100:
            break
        page += 1
    return results

def main():
    repo_list = [r.strip() for r in GITLAB_REPO_NAMES.split(",") if r.strip()]
    repo_set = {r.lower() for r in repo_list}

    print("Fetching group...")
    groups = api_get(f"{GITLAB_API_URL}/groups", params={"search": GITLAB_GROUP_NAME})
    group = next((g for g in groups if g["path"].lower() == GITLAB_GROUP_NAME.lower()
                  or g["name"].lower() == GITLAB_GROUP_NAME.lower()), None)
    if not group:
        print("ERROR: Group not found")
        return
    group_id = group["id"]

    print("Fetching group members...")
    members = api_get_all(f"{GITLAB_API_URL}/groups/{group_id}/members/all")
    member_by_email = {m.get("email", "").lower(): m for m in members if m.get("email")}
    member_by_name = {m.get("name", "").lower(): m for m in members if m.get("name")}

    print("Fetching all projects in group...")
    projects = api_get_
