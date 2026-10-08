# Plan: Self-update for AzurePIM.exe

## TL;DR

Add a self-update feature to the PyInstaller `--onefile` tray app. On startup
(and from a new tray menu item), the app calls `GET <UPDATE_URL>/version`
returning JSON `{ "version": "x.y.z", "sha256": "...", "url": ".../AzurePIM.exe", "notes": "..." }`.
If the version is newer than the embedded `__version__`, show a tray
notification + Yes/No dialog; on confirm, download the new binary to a temp
file, verify SHA-256, then perform the standard **rename-and-relaunch** swap
that works around Windows' "cannot delete a running .exe" limitation. Stale
`.old` file is cleaned up on next startup.

## Decisions

- **Server contract**: two endpoints
  - `GET /version` → JSON `{ "version": "1.2.3", "sha256": "<hex>", "url": "https://.../AzurePIM.exe", "notes": "..." }`
  - `GET /AzurePIM.exe` (or whatever `url` points to) → binary stream
- **Trigger**: silent check on startup (background, non-blocking) + new tray menu item "⬆ Check for updates"
- **UX**: tray notification + a `tkinter.messagebox.askyesno` confirmation dialog before downloading/installing
- **Integrity**: HTTPS + SHA-256 hash from `/version` must match downloaded file before swap
- **Auth**: none (internal/public endpoint)
- **Config**: hard-coded constants `__version__` and `UPDATE_URL` in `pim_tray.py` — no `config.json` changes
- **Out of scope**: Authenticode signature verification, delta patching, rollback UI, fully-automatic install (no prompt), periodic background polling

## Windows self-update mechanism (the critical detail)

A running `.exe` on Windows **cannot be deleted or overwritten**, but it
**can be renamed** (`MoveFileEx`). The sequence that avoids needing an
external updater script:

1. Download new binary to `BASE_DIR / "AzurePIM.exe.new"`.
2. Verify SHA-256.
3. Rename current `AzurePIM.exe` → `AzurePIM.exe.old` (allowed even while running).
4. Rename `AzurePIM.exe.new` → `AzurePIM.exe`.
5. `subprocess.Popen([new_exe], creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)` to launch the new version detached.
6. Call `self.icon.stop()` and `sys.exit(0)` — the OS releases the file lock on the old (now-renamed) binary.
7. On next startup, `_cleanup_stale_update()` deletes `AzurePIM.exe.old` if present.

If any step before step 3 fails, no state has changed. If step 4 fails,
attempt to rename `.old` back. If launching the new process fails, the user
can still double-click the .exe manually.

Dev fallback (when not frozen / `sys.frozen` is False): the update flow logs
"update only available in built exe" and returns — no swap is performed.

## Steps

### Phase 1 — Versioning & config

1. Add module-level constants near the top of `pim_tray.py` (after the imports/logging block, ~line 50):
   - `__version__ = "1.0.0"` (bumped on each release going forward)
   - `UPDATE_URL = "https://your.server/azurepim"` (base URL — `/version` and binary are appended)
   - `UPDATE_TIMEOUT = 10` (seconds for the metadata GET)
   - `DOWNLOAD_TIMEOUT = 120` (seconds for the binary)
2. `build.bat` stays manual: edit `__version__` before each build.

### Phase 2 — Helper functions (inline in `pim_tray.py` to preserve the one-file build)

Add a section "Self-update" between the existing "Tray application" class and the "Entry point" `main()`:

3. `_fetch_latest_metadata() -> dict | None` — `requests.get(f"{UPDATE_URL}/version", timeout=UPDATE_TIMEOUT)`, returns parsed JSON or `None` on any error (logged). No exceptions bubble up — update failures must never crash the tray.
4. `_is_newer(remote: str, local: str) -> bool` — compare via `packaging.version.Version` (already a transitive dependency of `azure-identity`), so no new requirement.
5. `_download_to_temp(url: str, expected_sha256: str) -> Path` — stream download with `requests.get(url, stream=True)` to `BASE_DIR / "AzurePIM.exe.new"`, compute SHA-256 incrementally, raise on mismatch, return the path.
6. `_swap_and_relaunch(new_exe: Path) -> None` — implements the rename-rename-spawn-exit sequence above. Uses `subprocess.Popen` with `creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP` and `close_fds=True`.
7. `_cleanup_stale_update() -> None` — deletes `BASE_DIR / "AzurePIM.exe.old"` if it exists; called once at startup. Silently ignores `PermissionError` (means a previous still-running copy holds it; will be retried next run).

### Phase 3 — Integration into `PIMTrayApp`

8. In `PIMTrayApp.__init__`, add `self._update_in_progress = False` flag (protected by `self._lock`) to prevent double-clicking the menu item from running concurrent downloads.
9. In `start()` (around line 357), call `_cleanup_stale_update()` before constructing the icon.
10. In `_init_async()` (around line 368), after roles discovery succeeds, fire a background `threading.Thread(target=self._check_for_updates, args=(True,), daemon=True).start()` (silent if up-to-date).
11. Add method `PIMTrayApp._check_for_updates(self, silent_if_uptodate: bool)`:
    - Skip with log line if `not getattr(sys, "frozen", False)` (dev mode).
    - Guarded by `self._update_in_progress` flag.
    - Calls `_fetch_latest_metadata()`; on `None` and `silent_if_uptodate=False`, show "Could not reach update server" notification.
    - If not newer: notify "You're on the latest version (x.y.z)" only if `silent_if_uptodate=False`.
    - If newer: tray notification "Update available: x.y.z → y.y.y" + a `tkinter.messagebox.askyesno` (lazy-imported) confirmation. If yes, call `_download_to_temp`, then `_swap_and_relaunch`. On any exception, log + notify failure, keep current app running.
12. In `_build_menu()` (~line 415), add `pystray.MenuItem("⬆ Check for updates", _check_cb)` between the Refresh Roles and Exit items. `_check_cb` spawns a thread calling `self._check_for_updates(silent_if_uptodate=False)`.
13. Update the tray tooltip in `start()` from `"Azure PIM"` to `f"Azure PIM v{__version__}"` so the user can see the current version on hover.

### Phase 4 — Build / packaging

14. No changes to `AzurePIM.spec` required — all imports (`requests`, `hashlib`, `subprocess`, `tkinter`, `packaging`) are stdlib or already in the bundle.
15. Bump `__version__` in `pim_tray.py` before each `build.bat` run — this is the only release ritual.
16. Server-side (out of scope for this code change): the `/version` endpoint should serve fresh JSON with the SHA-256 of the binary it links to. The user must regenerate that JSON whenever they replace the .exe on the server.

## Relevant files

- `pim_tray.py` — **all code changes go here**. Add constants near line 50, helper functions before `main()` (~line 460), call sites in `PIMTrayApp.start()` / `_init_async()` / `_build_menu()`.
- `requirements.txt` — no change (everything needed is already pulled in transitively or stdlib).
- `AzurePIM.spec` — no change.
- `build.bat` — no change required; optionally add a reminder echo about bumping `__version__`.

## Verification

1. **Dev-mode skip**: `python pim_tray.py` → log shows `"Update check skipped: running unfrozen"` and no swap is attempted.
2. **Up-to-date path**: stand up a local server (`python -m http.server 8000` with a `version` JSON file in cwd) where `version.version == __version__`. Build the .exe, point `UPDATE_URL` at it, run; tray starts normally, log says `"Already on latest"`. Manual menu click → notification "You're on the latest version".
3. **Upgrade path (happy)**: bump JSON to a higher version, point `url` at a working `AzurePIM.exe` v2 build with correct sha256. Launch v1 .exe → notification + Yes/No dialog → click Yes → v1 exits, v2 starts (verify by hovering tray: tooltip shows v2). Check `BASE_DIR` afterward: `AzurePIM.exe.old` exists. Restart v2 → `.old` gets removed.
4. **Hash mismatch**: corrupt the sha256 in `/version`. Trigger update → log shows "SHA-256 mismatch", `.new` file is deleted, no swap occurs, current app keeps running, user notified.
5. **Server down**: shut down the http server. Click "Check for updates" → notification "Could not reach update server". Startup check fails silently (only log entry).
6. **Concurrent click**: spam-click menu item → only one download runs (guard flag).
7. **Locked `.old`**: simulate by holding a handle on `AzurePIM.exe.old` during next startup → `_cleanup_stale_update` swallows `PermissionError`, app starts normally.

## Further considerations

1. **Periodic checks** — currently only at startup + manual. If the app is left running for days, users won't see updates. Easy follow-up: add a `threading.Timer` re-arming every 6 h. Recommend: skip for v1, revisit if users complain.
2. **Sign the .exe with Authenticode** — strongest defense against a tampered server. Recommend: add later once you have a code-signing cert; the SHA-256 check is the practical minimum for now.
3. **`config.json` preservation** — already fine, since `config.json` lives in `BASE_DIR` (next to the .exe) and the swap only replaces the .exe itself. Worth a code comment in `_swap_and_relaunch` so it doesn't accidentally regress.
