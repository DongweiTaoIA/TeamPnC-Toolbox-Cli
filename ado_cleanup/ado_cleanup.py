"""ADO Work Item Cleanup CLI — Delete comments and attachments from a work item."""

import argparse
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests
import urllib3

# Suppress InsecureRequestWarning for self-signed certs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def load_env():
    """Load variables from .env file in the script's directory."""
    env_path = Path(__file__).resolve().parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())


load_env()


def parse_work_item_url(url: str) -> dict:
    """Extract server_url, collection, project, and work_item_id from a work item URL."""
    # Expected format: https://<host>/tfs/<collection>/<project>/_workitems/edit/<id>
    pattern = r"^(https?://.+?/tfs)/([^/]+)/([^/]+)/_workitems/edit/(\d+)"
    match = re.match(pattern, url)
    if not match:
        sys.exit(f"Error: Unable to parse work item URL: {url}")
    return {
        "server_url": match.group(1),
        "collection": match.group(2),
        "project": match.group(3),
        "work_item_id": int(match.group(4)),
    }


def make_session(pat: str) -> requests.Session:
    """Create an authenticated requests session."""
    session = requests.Session()
    session.auth = ("", pat)
    session.verify = False
    session.headers.update({"Content-Type": "application/json"})
    return session


def get_comments(session: requests.Session, base_url: str, work_item_id: int) -> list:
    """Fetch all comments for a work item (handles pagination)."""
    comments = []
    url = f"{base_url}/_apis/wit/workitems/{work_item_id}/comments?api-version=7.1-preview.3"

    while url:
        resp = session.get(url)
        resp.raise_for_status()
        data = resp.json()
        comments.extend(data.get("comments", []))
        continuation_token = data.get("continuationToken")
        if continuation_token:
            url = f"{base_url}/_apis/wit/workitems/{work_item_id}/comments?continuationToken={continuation_token}&api-version=7.1-preview.3"
        else:
            url = None

    return comments


def delete_comment(session: requests.Session, base_url: str, work_item_id: int, comment_id: int) -> bool:
    """Delete a single comment. Returns True on success."""
    url = f"{base_url}/_apis/wit/workitems/{work_item_id}/comments/{comment_id}?api-version=7.1-preview.3"
    resp = session.delete(url)
    return resp.status_code in (200, 204)


def get_work_item_relations(session: requests.Session, base_url: str, work_item_id: int) -> list:
    """Fetch work item relations (includes attachments)."""
    url = f"{base_url}/_apis/wit/workitems/{work_item_id}?$expand=relations&api-version=7.1"
    resp = session.get(url)
    resp.raise_for_status()
    data = resp.json()
    return data.get("relations", []) or []


def remove_attachments(session: requests.Session, base_url: str, work_item_id: int, indices: list[int]) -> int:
    """Remove attachments by relation indices (must be sorted descending). Returns count of successful removals."""
    removed = 0
    for idx in sorted(indices, reverse=True):
        patch_url = f"{base_url}/_apis/wit/workitems/{work_item_id}?api-version=7.1"
        patch_body = [{"op": "remove", "path": f"/relations/{idx}"}]
        resp = session.patch(
            patch_url,
            json=patch_body,
            headers={"Content-Type": "application/json-patch+json"},
        )
        if resp.status_code == 200:
            removed += 1
        else:
            print(f"  Warning: Failed to remove attachment at index {idx} (HTTP {resp.status_code})")
    return removed


def main():
    parser = argparse.ArgumentParser(description="Clean up an Azure DevOps work item (delete comments and attachments).")
    parser.add_argument("ticket", nargs="?", help="Work item ID (e.g. 196572)")
    parser.add_argument("--url", default=None, help="Full URL to the work item (alternative to ticket number)")
    parser.add_argument("--user", default=None, help="Display name of comment author (defaults to ADO_USER env var)")
    parser.add_argument("--pat", default=None, help="Personal Access Token (defaults to ADO_PAT env var)")
    parser.add_argument("--dry-run", action="store_true", help="Preview what would be deleted without making changes")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    # Resolve ticket → URL
    if args.ticket:
        base_url = os.environ.get("ADO_BASE_URL", "")
        if not base_url:
            sys.exit("Error: ADO_BASE_URL not set. Use --url or set ADO_BASE_URL in .env")
        work_item_id = int(args.ticket)
    elif args.url:
        parts = parse_work_item_url(args.url)
        base_url = f"{parts['server_url']}/{parts['collection']}/{parts['project']}"
        work_item_id = parts["work_item_id"]
    else:
        sys.exit("Error: Provide a ticket number or --url")

    # Resolve user
    user = args.user or os.environ.get("ADO_USER")
    if not user:
        sys.exit("Error: --user not provided and ADO_USER not set in .env")

    # Resolve PAT
    pat = args.pat or os.environ.get("ADO_PAT")
    if not pat:
        import getpass
        pat = getpass.getpass("Enter PAT: ")
        if not pat:
            sys.exit("Error: PAT not provided. Use --pat, set ADO_PAT env var, or enter when prompted.")

    print(f"Work Item: {work_item_id}")
    print(f"Base URL:  {base_url}")
    print(f"User:      {user}")
    print()

    session = make_session(pat)

    # --- Comments ---
    print("Fetching comments...")
    all_comments = get_comments(session, base_url, work_item_id)
    my_comments = [
        c for c in all_comments
        if user.lower() in c.get("createdBy", {}).get("displayName", "").lower()
    ]
    print(f"  Found {len(all_comments)} total comments, {len(my_comments)} from '{user}'")

    if my_comments:
        for c in my_comments:
            preview = c.get("text", "")[:80].replace("\n", " ")
            print(f"    - [{c['id']}] {preview}...")

    # --- Attachments ---
    print("\nFetching attachments...")
    relations = get_work_item_relations(session, base_url, work_item_id)
    attachment_indices = []
    for i, rel in enumerate(relations):
        if rel.get("rel") == "AttachedFile":
            attachment_indices.append(i)

    print(f"  Found {len(attachment_indices)} attachment(s)")
    for idx in attachment_indices:
        name = relations[idx].get("attributes", {}).get("name", "unknown")
        print(f"    - [{idx}] {name}")

    # --- Summary ---
    total = len(my_comments) + len(attachment_indices)
    if total == 0:
        print("\nNothing to delete.")
        return

    print(f"\nWill delete: {len(my_comments)} comment(s) + {len(attachment_indices)} attachment(s)")

    if args.dry_run:
        print("\n[DRY RUN] No changes made.")
        return

    # Confirmation
    if not args.yes:
        answer = input("\nProceed? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            return

    # --- Delete comments ---
    if my_comments:
        print("\nDeleting comments...")
        deleted = 0
        for c in my_comments:
            if delete_comment(session, base_url, work_item_id, c["id"]):
                deleted += 1
                print(f"  Deleted comment {c['id']}")
            else:
                print(f"  Failed to delete comment {c['id']}")
        print(f"  {deleted}/{len(my_comments)} comments deleted")

    # --- Remove attachments ---
    if attachment_indices:
        print("\nRemoving attachments...")
        removed = remove_attachments(session, base_url, work_item_id, attachment_indices)
        print(f"  {removed}/{len(attachment_indices)} attachments removed")

    print("\nDone.")


if __name__ == "__main__":
    main()
