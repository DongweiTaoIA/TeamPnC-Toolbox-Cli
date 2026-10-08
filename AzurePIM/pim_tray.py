"""
Azure PIM System Tray Application
----------------------------------
Discovers your eligible PIM role assignments dynamically and lets you
activate them from a right-click tray menu.

Requirements: pip install -r requirements.txt
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from azure.identity import InteractiveBrowserCredential, TokenCachePersistenceOptions
from azure.mgmt.resource import SubscriptionClient
import pystray
from PIL import Image, ImageDraw

# ---------------------------------------------------------------------------
# Paths & logging
# ---------------------------------------------------------------------------

# When bundled with PyInstaller (`--onefile`), ``__file__`` points to a
# temporary extraction folder that is wiped on exit. Use the directory of the
# running executable instead so ``config.json`` and ``pim_tray.log`` persist
# next to the .exe the user double-clicks.
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
LOG_PATH = BASE_DIR / "pim_tray.log"

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ARM_SCOPE = "https://management.azure.com/.default"
ARM_BASE = "https://management.azure.com"
API_VERSION = "2020-10-01"
ACTIVATION_DURATION = "PT8H"  # ISO 8601 – 8 hours
DEFAULT_JUSTIFICATION = "Routine operational access"

# Claims challenge used when the PIM policy requires an MFA-bearing access
# token (error: "MfaRule - Multi-factor authentication is required …").
# Requesting this claim forces azure-identity to perform an interactive
# sign-in that satisfies MFA and embeds the "mfa" amr value in the token.
MFA_CLAIMS = '{"access_token":{"amr":{"essential":true,"values":["mfa"]}}}'

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    """Decode the payload section of a JWT without verifying signature."""
    payload_b64 = token.split(".")[1]
    # Add padding
    payload_b64 += "=" * (4 - len(payload_b64) % 4)
    return json.loads(base64.urlsafe_b64decode(payload_b64))


def _show_notification(title: str, message: str) -> None:
    """Show a Windows toast notification (best-effort)."""
    try:
        from winotify import Notification, audio  # type: ignore

        toast = Notification(
            app_id="Azure PIM Tray",
            title=title,
            msg=message,
            duration="short",
        )
        toast.set_audio(audio.Default, loop=False)
        toast.show()
    except Exception:
        log.warning("Toast notification failed (winotify not available?)")


# ---------------------------------------------------------------------------
# Credential & token management
# ---------------------------------------------------------------------------


def _build_credential() -> InteractiveBrowserCredential:
    cache_opts = TokenCachePersistenceOptions(name="AzurePIMTray", enable_persistent_cache=True)
    return InteractiveBrowserCredential(token_cache_persistence_options=cache_opts)


def _get_arm_token(
    credential: InteractiveBrowserCredential,
    claims: str | None = None,
) -> str:
    if claims:
        token = credential.get_token(ARM_SCOPE, claims=claims)
    else:
        token = credential.get_token(ARM_SCOPE)
    return token.token


def _get_principal_id(arm_token: str) -> str:
    claims = _decode_jwt_payload(arm_token)
    return claims["oid"]


# ---------------------------------------------------------------------------
# Role discovery
# ---------------------------------------------------------------------------


def discover_eligible_roles(
    credential: InteractiveBrowserCredential,
) -> dict[str, dict[str, str]]:
    """
    Enumerate all subscriptions and return a map of:
        display_name → {scope, roleDefinitionId, principalId, roleName, scopeName}
    """
    arm_token = _get_arm_token(credential)
    principal_id = _get_principal_id(arm_token)
    headers = {"Authorization": f"Bearer {arm_token}"}

    sub_client = SubscriptionClient(credential)
    subscriptions = list(sub_client.subscriptions.list())
    log.info("Found %d subscription(s)", len(subscriptions))

    roles: dict[str, dict[str, str]] = {}

    for sub in subscriptions:
        sub_id = sub.subscription_id
        sub_name = sub.display_name or sub_id
        scope = f"/subscriptions/{sub_id}"

        url = (
            f"{ARM_BASE}{scope}/providers/Microsoft.Authorization"
            f"/roleEligibilityScheduleInstances"
            f"?api-version={API_VERSION}"
            f"&$filter=assignedTo('{principal_id}')"
        )

        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            items = resp.json().get("value", [])
            log.info("Sub %s: %d eligible role(s)", sub_name, len(items))
        except Exception as exc:
            log.warning("Failed to query %s: %s", sub_name, exc)
            continue

        for item in items:
            props = item.get("properties", {})
            role_def_id = props.get("roleDefinitionId", "")
            role_name = props.get("expandedProperties", {}).get("roleDefinition", {}).get("displayName", role_def_id.split("/")[-1])
            resource_scope = props.get("scope", scope)

            # Build a friendly display name
            scope_label = resource_scope.split("/")[-1]  # last segment (e.g. resource group or subscription id)
            if scope_label == sub_id:
                scope_label = sub_name
            display_name = f"{role_name} – {scope_label}"

            # Deduplicate
            key = display_name
            suffix = 2
            while key in roles:
                key = f"{display_name} ({suffix})"
                suffix += 1

            roles[key] = {
                "scope": resource_scope,
                "roleDefinitionId": role_def_id,
                "principalId": principal_id,
                "roleName": role_name,
                "scopeName": scope_label,
            }

    return roles


# ---------------------------------------------------------------------------
# Role activation
# ---------------------------------------------------------------------------


def _is_mfa_required_error(error_msg: str) -> bool:
    lowered = error_msg.lower()
    return (
        "mfarule" in lowered
        or "multi-factor authentication is required" in lowered
        or "multifactor authentication" in lowered
    )


def _put_activation(arm_token: str, scope: str, body: dict) -> requests.Response:
    headers = {
        "Authorization": f"Bearer {arm_token}",
        "Content-Type": "application/json",
    }
    schedule_request_name = str(uuid.uuid4())
    url = (
        f"{ARM_BASE}{scope}/providers/Microsoft.Authorization"
        f"/roleAssignmentScheduleRequests/{schedule_request_name}"
        f"?api-version={API_VERSION}"
    )
    return requests.put(url, headers=headers, json=body, timeout=30)


def activate_role(
    credential: InteractiveBrowserCredential,
    role_info: dict[str, str],
) -> None:
    scope = role_info["scope"]

    body = {
        "properties": {
            "principalId": role_info["principalId"],
            "roleDefinitionId": role_info["roleDefinitionId"],
            "requestType": "SelfActivate",
            "scheduleInfo": {
                "startDateTime": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "expiration": {
                    "type": "AfterDuration",
                    "duration": ACTIVATION_DURATION,
                },
            },
            "justification": DEFAULT_JUSTIFICATION,
        }
    }

    log.info("Activating role: %s on %s", role_info.get("roleName"), scope)
    arm_token = _get_arm_token(credential)
    resp = _put_activation(arm_token, scope, body)

    # If PIM rejects because the token lacks an MFA claim, re-prompt the user
    # with a claims challenge so azure-identity performs an interactive MFA
    # sign-in, then retry the activation once.
    if resp.status_code not in (200, 201):
        try:
            error_msg = resp.json().get("error", {}).get("message", resp.text)
        except ValueError:
            error_msg = resp.text

        if _is_mfa_required_error(error_msg):
            log.info("PIM requires MFA – requesting fresh token with MFA claim …")
            _show_notification(
                "🔐 MFA Required",
                f"{role_info.get('roleName')} needs MFA – complete the sign-in prompt.",
            )
            try:
                arm_token = _get_arm_token(credential, claims=MFA_CLAIMS)
            except Exception as exc:
                log.exception("MFA token acquisition failed: %s", exc)
                _show_notification(
                    "❌ PIM Activation Failed",
                    f"MFA sign-in failed: {str(exc)[:120]}",
                )
                raise
            resp = _put_activation(arm_token, scope, body)

    if resp.status_code in (200, 201):
        log.info("Activation succeeded for %s", role_info.get("roleName"))
        _show_notification(
            "✅ PIM Role Activated",
            f"{role_info.get('roleName')} on {role_info.get('scopeName')} (8 hours)",
        )
        return

    try:
        error_msg = resp.json().get("error", {}).get("message", resp.text)
    except ValueError:
        error_msg = resp.text
    log.error("Activation failed: %s", error_msg)
    if "already exists" in error_msg.lower():
        _show_notification(
            "ℹ️ Already Active",
            f"{role_info.get('roleName')} on {role_info.get('scopeName')} is already activated.",
        )
    else:
        _show_notification(
            "❌ PIM Activation Failed",
            f"{role_info.get('roleName')}: {error_msg[:120]}",
        )
        raise RuntimeError(error_msg)


# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------


def load_config() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"roles": {}}


def save_config(roles: dict[str, dict[str, str]]) -> None:
    data = load_config()
    data["roles"] = roles
    CONFIG_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Config saved to %s", CONFIG_PATH)


# ---------------------------------------------------------------------------
# Tray icon image (generated programmatically – no external .ico needed)
# ---------------------------------------------------------------------------


def _make_icon_image() -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Blue circle background
    draw.ellipse([2, 2, size - 2, size - 2], fill=(0, 120, 212, 255))
    # White "P" letter
    draw.text((20, 14), "P", fill="white")
    return img


# ---------------------------------------------------------------------------
# Tray application
# ---------------------------------------------------------------------------


class PIMTrayApp:
    def __init__(self) -> None:
        self.credential: InteractiveBrowserCredential | None = None
        self.roles: dict[str, dict[str, str]] = {}
        self.icon: pystray.Icon | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def start(self) -> None:
        image = _make_icon_image()
        self.icon = pystray.Icon(
            "AzurePIM",
            image,
            "Azure PIM",
            menu=self._build_menu(),
        )
        # Authenticate & discover in background so the icon appears immediately
        threading.Thread(target=self._init_async, daemon=True).start()
        self.icon.run()

    def _init_async(self) -> None:
        try:
            log.info("Building credential …")
            self.credential = _build_credential()
            # Eagerly obtain token – this triggers browser login if needed
            _get_arm_token(self.credential)
            log.info("Credential ready, discovering roles …")
            self._refresh_roles()
        except Exception as exc:
            log.exception("Init failed: %s", exc)
            _show_notification("Azure PIM", f"Startup error: {exc}")

    # ------------------------------------------------------------------
    # Role refresh
    # ------------------------------------------------------------------

    def _refresh_roles(self) -> None:
        if self.credential is None:
            return
        try:
            _show_notification("Azure PIM", "Discovering eligible roles …")
            roles = discover_eligible_roles(self.credential)
            with self._lock:
                self.roles = roles
            save_config(roles)
            self._rebuild_menu()
            _show_notification("Azure PIM", f"Found {len(roles)} eligible role(s)")
        except Exception as exc:
            log.exception("Role refresh failed: %s", exc)
            _show_notification("Azure PIM – Error", str(exc)[:200])

    def _rebuild_menu(self) -> None:
        if self.icon:
            self.icon.menu = self._build_menu()
            self.icon.update_menu()

    # ------------------------------------------------------------------
    # Menu
    # ------------------------------------------------------------------

    @staticmethod
    def _make_activate_callback(app, name: str, info: dict):
        """Factory to create a 2-arg callback that pystray accepts."""
        def callback(icon, item):
            app._on_activate(name, info)
        return callback

    def _build_menu(self) -> pystray.Menu:
        items: list[pystray.MenuItem] = []

        with self._lock:
            roles = dict(self.roles)

        if not roles:
            items.append(pystray.MenuItem("(No roles discovered yet)", None, enabled=False))
        else:
            for display_name, role_info in sorted(roles.items()):
                _name = display_name
                _info = dict(role_info)
                items.append(
                    pystray.MenuItem(
                        _name,
                        self._make_activate_callback(self, _name, _info),
                    )
                )

        items.append(pystray.Menu.SEPARATOR)

        def _refresh_cb(icon, item):
            threading.Thread(target=self._refresh_roles, daemon=True).start()

        items.append(pystray.MenuItem("🔄 Refresh Roles", _refresh_cb))
        items.append(pystray.MenuItem("🚪 Exit", self._on_exit))

        return pystray.Menu(*items)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _on_activate(self, display_name: str, role_info: dict[str, str]) -> None:
        def _run() -> None:
            try:
                activate_role(self.credential, role_info)
            except Exception as exc:
                log.exception("Activation error for %s: %s", display_name, exc)

        threading.Thread(target=_run, daemon=True).start()

    def _on_exit(self, icon: pystray.Icon, item: pystray.MenuItem = None) -> None:
        icon.stop()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    log.info("=== Azure PIM Tray starting ===")
    app = PIMTrayApp()
    app.start()


if __name__ == "__main__":
    main()
