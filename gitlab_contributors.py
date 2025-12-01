import requests
import csv
from collections import defaultdict

# -------------------------------------
# CONFIGURATION
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

    repo_names = {r.strip().lower() for r in GITLAB_REPO_NAMES.split(",")}

    print("Fetching group...")
    groups = api_get(f"{GITLAB_API_URL}/groups", params={"search": GITLAB_GROUP_NAME})
    group = next((g for g in groups
                  if g["name"].lower() == GITLAB_GROUP_NAME.lower()
                  or g["path"].lower() == GITLAB_GROUP_NAME.lower()), None)
    if not group:
        print("ERROR: Group not found.")
        return

    group_id = group["id"]

    print("Fetching all projects in the group...")
    projects = api_get(
        f"{GITLAB_API_URL}/groups/{group_id}/projects",
        params={"include_subgroups": "true", "with_shared": "true", "per_page": 100}
    )

    # Match based on project path (repo name)
    target_projects = [p for p in projects if p["path"].lower() in repo_names]

    if not target_projects:
        print("ERROR: No projects matched.")
        return

    contributor_data = defaultdict(lambda: {
        "name": None,
        "email": None,
        "commits": 0,
        "last_commit_at": None,
        "user_id": None,
        "state": "unknown"
    })

    print("\nProcessing projects...\n")

    for project in target_projects:
        project_id = project["id"]
        project_name = project["path"]

        print(f"→ Processing repo: {project_name}")

        # Contributors list
        contributors = api_get(
            f"{GITLAB_API_URL}/projects/{project_id}/repository/contributors"
        )

        for c in contributors:
            email = c.get("email")
            name = c.get("name")
            commit_count = c.get("commits", 0)

            if not email:
                continue

            key = email.lower()
            entry = contributor_data[key]
            entry["name"] = name
            entry["email"] = email
            entry["commits"] += commit_count

            # Fetch commits by this email to extract author_id + last commit
            commits = api_get(
                f"{GITLAB_API_URL}/projects/{project_id}/repository/commits",
                params={"author_email": email, "per_page": 100}
            )

            if commits:
                # last commit date
                last_date = max(commit["created_at"] for commit in commits)
                if not entry["last_commit_at"] or last_date > entry["last_commit_at"]:
                    entry["last_commit_at"] = last_date

                # GitLab maps author to user_id only when possible
                for commit in commits:
                    if commit.get("author_id"):
                        entry["user_id"] = commit["author_id"]

    print("\nFetching user status for each contributor (only those with user_id)...")

    # Lookup user status only for contributors with user_id
    for entry in contributor_data.values():
        user_id = entry["user_id"]
        if not user_id:
            entry["state"] = "not_found"
            continue

        try:
            user = api_get(f"{GITLAB_API_URL}/users/{user_id}")
            entry["state"] = user.get("state", "unknown")
        except:
            entry["state"] = "unknown"

    print("\nWriting CSV: contributors.csv\n")

    with open("contributors.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "user_id",
            "name",
            "email",
            "status",
            "last_commit_date",
            "commit_count"
        ])

        for entry in contributor_data.values():
            writer.writerow([
                entry["user_id"],
                entry["name"],
                entry["email"],
                entry["state"],
                entry["last_commit_at"],
                entry["commits"]
            ])

    print("Done ✓  Output saved to contributors.csv")

if __name__ == "__main__":
    main()
