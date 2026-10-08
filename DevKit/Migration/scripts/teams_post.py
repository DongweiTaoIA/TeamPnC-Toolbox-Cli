"""Post a message to a Teams channel or chat via Microsoft Graph.

Posts as the signed-in user (delegated auth), so the message shows your name and
supports a real ``subject`` line -- the bold title above the body in Teams.

Prints the rendered message and exits unless ``--yes`` is given.

Setup
-----
Needs an Azure AD app registration in your tenant:

1. portal.azure.com -> Microsoft Entra ID -> App registrations -> New registration
2. Name it anything; Supported account types: this organizational directory only
3. Authentication -> Add a platform -> Mobile and desktop applications ->
   tick ``https://login.microsoftonline.com/common/oauth2/nativeclient``
4. API permissions -> Microsoft Graph -> Delegated -> ``ChannelMessage.Send``
   and ``Chat.ReadWrite``
5. Copy the Application (client) ID and Directory (tenant) ID

Then set ``GRAPH_CLIENT_ID`` and ``GRAPH_TENANT_ID`` in the environment.

Target ids come from Teams: right-click the channel -> Get link to channel. The
URL holds ``groupId`` (team id) and a ``19:...@thread.tacv2`` segment (channel id).

Usage::

    # dry run against your own self-chat
    python scripts/teams_post.py --self-chat --body "This is just a test, please ignore"

    # actually send
    python scripts/teams_post.py --self-chat --body "..." --yes
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx
import msal

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["ChannelMessage.Send", "Chat.ReadWrite"]

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOKEN_CACHE = PROJECT_ROOT / ".auth" / "graph-token-cache.json"


def build_app() -> msal.PublicClientApplication:
    client_id = os.environ.get("GRAPH_CLIENT_ID")
    tenant_id = os.environ.get("GRAPH_TENANT_ID")
    if not client_id or not tenant_id:
        raise SystemExit(
            "Set GRAPH_CLIENT_ID and GRAPH_TENANT_ID first (see the module docstring)."
        )

    cache = msal.SerializableTokenCache()
    if TOKEN_CACHE.exists():
        cache.deserialize(TOKEN_CACHE.read_text(encoding="utf-8"))

    app = msal.PublicClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        token_cache=cache,
    )
    return app


def save_cache(app: msal.PublicClientApplication) -> None:
    cache = app.token_cache
    if cache.has_state_changed:
        TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_CACHE.write_text(cache.serialize(), encoding="utf-8")
        # Tokens are credentials; keep them out of any shared location.
        os.chmod(TOKEN_CACHE, 0o600)


def acquire_token(app: msal.PublicClientApplication) -> str:
    accounts = app.get_accounts()
    result = app.acquire_token_silent(SCOPES, account=accounts[0]) if accounts else None

    if not result:
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise SystemExit(f"Could not start sign-in: {flow.get('error_description')}")
        print(f"\n{flow['message']}\n")
        result = app.acquire_token_by_device_flow(flow)

    if "access_token" not in result:
        raise SystemExit(f"Sign-in failed: {result.get('error_description', result)}")

    save_cache(app)
    return result["access_token"]


def post_message(token: str, url: str, payload: dict[str, object]) -> dict[str, object]:
    with httpx.Client(headers={"Authorization": f"Bearer {token}"}, timeout=30.0) as client:
        resp = client.post(url, json=payload)
        if resp.status_code >= 400:
            raise SystemExit(f"Graph returned {resp.status_code}: {resp.text}")
        return resp.json()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subject", default=None, help="bold title above the body")
    parser.add_argument("--body", required=True, help="message body (HTML allowed)")
    parser.add_argument("--team-id", default=os.environ.get("TEAMS_TEAM_ID"))
    parser.add_argument("--channel-id", default=os.environ.get("TEAMS_CHANNEL_ID"))
    parser.add_argument("--chat-id", help="post to a chat instead of a channel")
    parser.add_argument("--self-chat", action="store_true", help="post to your own chat")
    parser.add_argument("--yes", action="store_true", help="actually send")
    args = parser.parse_args()

    if args.self_chat:
        url = f"{GRAPH}/chats/48:notes/messages"
        target = "your self-chat"
    elif args.chat_id:
        url = f"{GRAPH}/chats/{args.chat_id}/messages"
        target = f"chat {args.chat_id}"
    elif args.team_id and args.channel_id:
        url = f"{GRAPH}/teams/{args.team_id}/channels/{args.channel_id}/messages"
        target = f"channel {args.channel_id}"
    else:
        raise SystemExit("Give --self-chat, --chat-id, or both --team-id and --channel-id.")

    payload: dict[str, object] = {"body": {"contentType": "html", "content": args.body}}
    if args.subject:
        payload["subject"] = args.subject

    print(f"Target : {target}")
    print(f"URL    : {url}")
    print(f"Payload:\n{json.dumps(payload, indent=2)}")

    if not args.yes:
        print("\nDry run. Nothing was sent. Re-run with --yes to post.")
        return 0

    token = acquire_token(build_app())
    result = post_message(token, url, payload)
    print(f"\nPosted. Message id: {result.get('id')}")
    if link := result.get("webUrl"):
        print(link)
    return 0


if __name__ == "__main__":
    sys.exit(main())
