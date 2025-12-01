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
    projects = api_get_all(
        f"{GITLAB_API_URL}/groups/{group_id}/projects",
        params={"include_subgroups": "true"}
    )
    target_projects = [p for p in projects if p["path"].lower() in repo_set]
    if not target_projects:
        print("No matching repos found.")
        return

    # Key: contributor name (lowercase) -> contributor info
    contributor_data = defaultdict(lambda: {
        "name": None,
        "email": None,
        "username": "unknown",
        "status": "unknown",
        "last_commit_at": None,
        "repos": {}  # repo_name -> commit_count
    })

    print("\nProcessing repositories...\n")
    for project in target_projects:
        repo_name = project["path"]
        project_id = project["id"]

        print(f"→ {repo_name}")
        contributors = api_get_all(f"{GITLAB_API_URL}/projects/{project_id}/repository/contributors")

        for c in contributors:
            name = c.get("name")
            email = c.get("email")
            if not name:
                continue
            key = name.lower()
            entry = contributor_data[key]

            # Populate basic info if not already set
            if not entry["name"]:
                entry["name"] = name
            if not entry["email"] and email:
                entry["email"] = email
            # Store repo commit count (one row per repo)
            entry["repos"][repo_name] = c.get("commits", 0)

            # Optional: fetch last commit date
            try:
                commits = api_get(
                    f"{GITLAB_API_URL}/projects/{project_id}/repository/commits",
                    params={"author": name, "per_page": 50}
                )
                if commits:
                    last_date = max(cmt["created_at"] for cmt in commits if cmt.get("created_at"))
                    if not entry["last_commit_at"] or last_date > entry["last_commit_at"]:
                        entry["last_commit_at"] = last_date
            except requests.HTTPError:
                pass

    print("\nMatching contributors to group members...\n")
    for key, entry in contributor_data.items():
        email = entry.get("email")
        # Match by email first
        m = member_by_email.get(email.lower()) if email else None
        # Fallback: match by name
        if not m:
            m = member_by_name.get(entry["name"].lower())
        if m:
            entry["username"] = m.get("username", "unknown")
            entry["status"] = m.get("state", "unknown")
        else:
            entry["status"] = "unknown"

    print("Writing contributors.csv ...")
    with open("contributors.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        header = ["username", "name", "email", "status", "repo_name", "commit_count", "last_commit_date"]
        writer.writerow(header)

        for entry in contributor_data.values():
            for repo_name, commit_count in entry["repos"].items():
                row = [
                    entry["username"],
                    entry["name"],
                    entry["email"],
                    entry["status"],
                    repo_name,
                    commit_count,
                    entry["last_commit_at"]
                ]
                writer.writerow(row)

    print("Done ✓ contributors.csv generated.")

if __name__ == "__main__":
    main()
