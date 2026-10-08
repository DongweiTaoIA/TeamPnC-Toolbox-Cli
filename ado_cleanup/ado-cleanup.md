# ADO Work Item Cleanup CLI

## Purpose

Python command-line tool to clean up an Azure DevOps Server (on-prem TFS) work item by deleting:

- All **comments** from a specified user
- All **attachments** on the work item

## Prerequisites

- Python 3.10+
- `requests` library
- A Personal Access Token (PAT) with **Work Items (Read & Write)** scope

## Installation

```bash
pip install -r requirements.txt
```

## Usage

```bash
python ado_cleanup.py --url <work-item-url> --user <display-name> [options]
```

### Examples

```bash
# Dry run — preview what would be deleted
python ado_cleanup.py \
  --url "https://cv.websct.ia.iafg.net/tfs/SOPCV_SNW/DP_Unifi_NEW/_workitems/edit/196572" \
  --user "Doe, John" \
  --dry-run

# Delete with confirmation prompt
python ado_cleanup.py \
  --url "https://cv.websct.ia.iafg.net/tfs/SOPCV_SNW/DP_Unifi_NEW/_workitems/edit/196572" \
  --user "Doe, John"

# Delete without confirmation
python ado_cleanup.py \
  --url "https://cv.websct.ia.iafg.net/tfs/SOPCV_SNW/DP_Unifi_NEW/_workitems/edit/196572" \
  --user "Doe, John" \
  --yes
```

### Arguments

| Flag | Description | Required | Default |
|------|-------------|----------|---------|
| `--url` | Full URL to the work item | Yes | — |
| `--user` | Display name (or email) of the comment author to match | Yes | — |
| `--pat` | Personal Access Token (overrides `ADO_PAT` env var) | No | `$ADO_PAT` |
| `--dry-run` | List what would be deleted without making changes | No | `false` |
| `--yes` | Skip confirmation prompt | No | `false` |

## URL Parsing

Input URL format:

```
https://<server>/tfs/<collection>/<project>/_workitems/edit/<id>
```

Extracted components:

| Component | Example |
|-----------|---------|
| Server URL | `https://cv.websct.ia.iafg.net/tfs` |
| Collection | `SOPCV_SNW` |
| Project | `DP_Unifi_NEW` |
| Work Item ID | `196572` |

## API Endpoints Used

### List Comments

```
GET {server}/{collection}/{project}/_apis/wit/workitems/{id}/comments?api-version=7.1-preview.3
```

Response contains `comments[]` with `id`, `text`, `createdBy.displayName`.

### Delete Comment

```
DELETE {server}/{collection}/{project}/_apis/wit/workitems/{id}/comments/{commentId}?api-version=7.1-preview.3
```

### Get Work Item (with relations)

```
GET {server}/{collection}/{project}/_apis/wit/workitems/{id}?$expand=relations&api-version=7.1
```

Response contains `relations[]` with `rel`, `url`, `attributes.name`.

### Remove Attachment (JSON Patch)

```
PATCH {server}/{collection}/{project}/_apis/wit/workitems/{id}?api-version=7.1
Content-Type: application/json-patch+json

[
  { "op": "remove", "path": "/relations/{index}" }
]
```

> Attachments are removed from highest index to lowest to prevent index shifting.

## Flow

```
1. Parse URL → extract server_url, collection, project, work_item_id
2. Resolve PAT (--pat arg or ADO_PAT env var)
3. Fetch all comments (paginated via continuationToken)
4. Filter comments where createdBy.displayName matches --user
5. Fetch work item with $expand=relations
6. Filter relations where rel == "AttachedFile"
7. Display summary (X comments, Y attachments to delete)
8. If --dry-run → exit
9. If not --yes → prompt user for confirmation
10. Delete comments one by one
11. Remove attachments via PATCH (reverse index order)
12. Print final summary
```

## Safety

- **Dry-run mode**: `--dry-run` lists items without deleting anything
- **Confirmation prompt**: by default, asks "Delete X comments and Y attachments? [y/N]"
- **`--yes` flag**: skips confirmation for automation/scripting
- **Reverse-index deletion**: attachments removed from last to first to avoid index corruption
