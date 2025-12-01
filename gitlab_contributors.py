import requests
import csv
from collections import defaultdict

# -------------------------------
# Configuration
# -------------------------------
GITLAB_API_URL = "https://gitlab.com/api/v4"
GITLAB_API_TOKEN = "YOUR_TOKEN_HERE"
GITLAB_GROUP_NAME = "your-group-name"
GITLAB_REPO_NAMES = "repo1,repo2,repo3"   # comma-separated
# -------------------------------

HEADERS = {"PRIVATE-TOKEN": GITLAB_API_TOKEN}

def api_get(url, params=None):
    """Simple GET request wrapper."""
    r = requests.get(url, headers=HEADERS, params=params)
    r.raise_for_status()
    return r.json()

def find_user_by_email(email):
    """Search for a user by email."""
    if not email:
        return None
    users = api_get(f"{GITLAB_API_URL}/users", params={"search": email})
    # GitLab search is fuzzy; ensure exact email match when possible
    for u in users:
        if "email" in u and u["email"] == email:
            return u
    return users[0] if users else None


def main():
    repo_names = [r.strip() for r in GITLAB_REPO_NAMES.split(",")]

    print("Fetching group info...")
    groups = api_get(f"{GITLAB_API_URL}/groups", params={"search": GITLAB_GROUP_NAME})
    group = None
    for g in groups:
        if g["name"].lower() == GITLAB_GROUP_NAME.lower() or g["path"].lower() == GITLAB_GROUP_NAME.lower():
            group = g
            break
    if not group:
        print("ERROR: Group not found")
        return

    group_id = group["id"]

    print("Fetching projects...")
    all_projects = api_get(f"{GITLAB_API_URL}/groups/{group_id}/projects", params={"per_page": 100})

    # Filter projects by names provided
    projects = []
    for p in all_projects:
        if p["name"] in repo_names or p["path"] in repo_names:
            projects.append(p)

    if not projects:
        print("ERROR: No matching projects found in the group.")
        return

    # Aggregate contributors across all repos
    contributor_data = defaultdict(lambda: {
        "name": None,
        "email": None,
        "commits": 0,
        "last_commit_at": None,
        "user_id": None,
        "state": "unknown"
    })

    for project in projects:
        print(f"\nProcessing project: {project['name']} (ID {project['id']})")

        # Contributors from GitLab
        contributors = api_get(f"{GITLAB_API_URL}/projects/{project['id']}/repository/contributors")

        for c in contributors:
            email = c.get("email")
            name = c.get("name")
            commits = c.get("commits", 0)

            # Combine contributions
            key = email.lower() if email else name
            entry = contributor_data[key]

            entry["name"] = name
            entry["email"] = email
            entry["commits"] += commits

            # Fetch last commit date for this email
            if email:
                commits_api = api_get(
                    f"{GITLAB_API_URL}/projects/{project['id']}/repository/commits",
                    params={"author_email": email, "per_page": 100}
                )
                if commits_api:
                    commit_dates = [cmt["created_at"] for cmt in commits_api]
                    last_date = max(commit_dates)
                    if not entry["last_commit_at"] or last_date > entry["last_commit_at"]:
                        entry["last_commit_at"] = last_date

    print("\nFetching user status for each contributor...")
    for key, entry in contributor_data.items():
        email = entry["email"]
        user = find_user_by_email(email)
        if user:
            entry["user_id"] = user.get("id")
            entry["state"] = user.get("state", "unknown")
        else:
            entry["state"] = "not_found"

    print("\nWriting CSV file: contributors.csv")
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

    print("Done! Output saved to contributors.csv")

if __name__ == "__main__":
    main()
