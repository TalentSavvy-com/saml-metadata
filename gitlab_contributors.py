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

def main():

    repo_list = [r.strip() for r in GITLAB_REPO_NAMES.split(",")]
    repo_set = {r.lower() for r in repo_list}

    print("Fetching group...")
    groups = api_get(f"{GITLAB_API_URL}/groups", params={"search": GITLAB_GROUP_NAME})
    group = next((g for g in groups if g["path"].lower() == GITLAB_GROUP_NAME.lower()
                  or g["name"].lower() == GITLAB_GROUP_NAME.lower()), None)
    if not group:
        print("ERROR: Group not found")
        return

    group_id = group["id"]

    print("Fetching group members (for user_id + status mapping)...")
    members = api_get(
        f"{GITLAB_API_URL}/groups/{group_id}/members/all",
        params={"per_page": 100}
    )

    member_lookup = {}
    for m in members:
        member_lookup[m["name"].lower()] = m  # name-based matching

    print("Fetching all projects in group...")
    projects = api_get(
        f"{GITLAB_API_URL}/groups/{group_id}/projects",
        params={"include_subgroups": "true", "per_page": 200}
    )

    # Match repos by project "path" (repo name)
    target_projects = [p for p in projects if p["path"].lower() in repo_set]

    if not target_projects:
        print("No matching repos found.")
        return

    # Data structure for contributors
    contributor_data = defaultdict(lambda: {
        "name": None,
        "email": None,
        "user_id": None,
        "state": "unknown",
        "last_commit_at": None,
        "total_commits": 0,
        "repo_commits": {r: 0 for r in repo_list}
    })

    print("\nProcessing repositories...\n")

    for project in target_projects:
        repo_name = project["path"]
        project_id = project["id"]

        print(f"→ {repo_name}")

        contributors = api_get(
            f"{GITLAB_API_URL}/projects/{project_id}/repository/contributors"
        )

        for c in contributors:
            email = c.get("email")
            name = c.get("name")

            if not name:
                continue

            key = name.lower()  # use display name as stable key
            entry = contributor_data[key]

            entry["name"] = name
            entry["email"] = email

            # Repo-specific commit count
            entry["repo_commits"][repo_name] += c.get("commits", 0)
            entry["total_commits"] += c.get("commits", 0)

            # Find last commit date
            commits = api_get(
                f"{GITLAB_API_URL}/projects/{project_id}/repository/commits",
                params={"author": name, "per_page": 50}
            )

            if commits:
                last_date = max(cmt["created_at"] for cmt in commits)
                if not entry["last_commit_at"] or last_date > entry["last_commit_at"]:
                    entry["last_commit_at"] = last_date

    print("\nMatching contributors to GitLab members...\n")

    for key, entry in contributor_data.items():
        name = entry["name"].lower()

        if name in member_lookup:
            m = member_lookup[name]
            entry["user_id"] = m["id"]
            entry["state"] = m["state"]
        else:
            entry["state"] = "not_found"

    print("Writing contributors.csv ...")

    with open("contributors.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        header = ["user_id", "name", "email", "status", "last_commit_date"]
        header += [f"commit_count_{r}" for r in repo_list]
        header.append("total_commits")
        writer.writerow(header)

        for entry in contributor_data.values():
            row = [
                entry["user_id"],
                entry["name"],
                entry["email"],
                entry["state"],
                entry["last_commit_at"]
            ]
            row += [entry["repo_commits"][r] for r in repo_list]
            row.append(entry["total_commits"])
            writer.writerow(row)

    print("Done ✓ contributors.csv generated.")

if __name__ == "__main__":
    main()
