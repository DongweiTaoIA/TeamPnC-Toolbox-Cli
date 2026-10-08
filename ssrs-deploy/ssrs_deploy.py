"""SSRS Report Deploy CLI - pull latest and upload selected .rdl reports to FNCT/ACCP/PROD."""

import argparse
import getpass
import logging
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urljoin

import requests
import urllib3
from defusedxml import ElementTree as ET

# Suppress InsecureRequestWarning for internal SSRS servers with self-signed certs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ---------------------------------------------------------------------------
# Env / config
# ---------------------------------------------------------------------------

def load_env() -> None:
    """Load variables from .env file in the script's directory."""
    env_path = Path(__file__).resolve().parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())


load_env()

REPO_ROOT_DEV = Path(os.environ.get("REPO_ROOT_DEV", r"C:\repos\tfs\DP_Unifi_Resources\Dev"))
REPO_ROOT_MAIN = Path(os.environ.get("REPO_ROOT_MAIN", r"C:\repos\tfs\DP_Unifi_Resources\Main"))

# env -> DPReports src folder
ENV_PATHS: dict[str, Path] = {
    "FNCT": REPO_ROOT_DEV / "_FNCT" / "SSRS" / "DPReports" / "Src",
    "ACCP": REPO_ROOT_DEV / "_ACCP" / "SSRS" / "DPReports" / "Src",
    "PROD": REPO_ROOT_MAIN / "SSRS" / "DPReports" / "Src",
}

RPTPROJ_NAME = "DPReports.rptproj"
MSBUILD_NS = "http://schemas.microsoft.com/developer/msbuild/2003"
LOG = logging.getLogger("ssrs-deploy")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(log_file: str | None) -> None:
    LOG.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    LOG.addHandler(console)

    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        LOG.addHandler(fh)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ssrs-deploy",
        description="Deploy selected SSRS reports (.rdl) to FNCT / ACCP / PROD.",
    )
    env_group = parser.add_mutually_exclusive_group(required=True)
    env_group.add_argument("-FNCT", dest="env", action="store_const", const="FNCT",
                           help="Deploy to FNCT (current Windows user).")
    env_group.add_argument("-ACCP", dest="env", action="store_const", const="ACCP",
                           help="Deploy to ACCP (current Windows user).")
    env_group.add_argument("-PROD", dest="env", action="store_const", const="PROD",
                           help="Deploy to PROD (requires -u / -p).")

    parser.add_argument("-r", "--reports", required=True,
                        help="Comma-separated report names (case-insensitive, .rdl optional). "
                             "Example: -r DPR928E,DPR928F")
    parser.add_argument("-u", "--user", default=None,
                        help="PROD user (e.g. 'iap.iafg.net\\lidot' or 'lidot').")
    parser.add_argument("-p", "--password", default=None,
                        help="PROD password. If omitted with -PROD, you will be prompted.")

    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen without pulling or uploading.")
    parser.add_argument("--no-pull", action="store_true",
                        help="Skip the tf.exe get step.")
    parser.add_argument("--log-file", default=None,
                        help="Also write log to this file.")

    return parser.parse_args()


def normalize_reports(raw: str) -> list[str]:
    """Split '-r' value into clean report stems (no .rdl, no whitespace, order preserved, deduped)."""
    seen: set[str] = set()
    out: list[str] = []
    for token in raw.split(","):
        name = token.strip()
        if not name:
            continue
        if name.lower().endswith(".rdl"):
            name = name[:-4]
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    if not out:
        sys.exit("Error: -r/--reports produced no report names.")
    return out


def normalize_prod_user(user: str) -> str:
    """Accept 'domain/user' or 'domain\\user' or bare 'user' -> canonical 'domain\\user' (or 'user')."""
    u = user.strip().replace("/", "\\")
    return u


# ---------------------------------------------------------------------------
# TFVC pull
# ---------------------------------------------------------------------------

# Fixed candidate paths for tf.exe / vswhere.exe. No env-var input flows into
# subprocess; the user should add tf.exe to PATH if it lives somewhere else.
_TF_CANDIDATES: tuple[str, ...] = (
    r"C:\Program Files\Microsoft Visual Studio\2022\Enterprise\Common7\IDE\CommonExtensions\Microsoft\TeamFoundation\Team Explorer\tf.exe",
    r"C:\Program Files\Microsoft Visual Studio\2022\Professional\Common7\IDE\CommonExtensions\Microsoft\TeamFoundation\Team Explorer\tf.exe",
    r"C:\Program Files\Microsoft Visual Studio\2022\Community\Common7\IDE\CommonExtensions\Microsoft\TeamFoundation\Team Explorer\tf.exe",
    r"C:\Program Files (x86)\Microsoft Visual Studio\2019\Enterprise\Common7\IDE\CommonExtensions\Microsoft\TeamFoundation\Team Explorer\tf.exe",
    r"C:\Program Files (x86)\Microsoft Visual Studio\2019\Professional\Common7\IDE\CommonExtensions\Microsoft\TeamFoundation\Team Explorer\tf.exe",
    r"C:\Program Files (x86)\Microsoft Visual Studio\2019\Community\Common7\IDE\CommonExtensions\Microsoft\TeamFoundation\Team Explorer\tf.exe",
)


def find_tf_exe() -> str:
    """Locate tf.exe: PATH first, then a fixed allowlist of standard VS install paths."""
    from shutil import which
    on_path = which("tf.exe") or which("tf")
    if on_path and Path(on_path).is_file() and Path(on_path).name.lower() == "tf.exe":
        return on_path

    for candidate in _TF_CANDIDATES:
        if Path(candidate).is_file():
            return candidate

    sys.exit("Error: tf.exe not found. Add Visual Studio's Team Explorer 'tf.exe' to PATH.")


def tf_get(src_dir: Path) -> None:
    """Run `tf vc get <src_dir> /recursive /noprompt` and stream output."""
    tf_exe = find_tf_exe()
    src_abs = str(src_dir.resolve())
    LOG.info("[TF-GET] Using %s", tf_exe)
    LOG.info("[TF-GET] Getting latest for %s", src_abs)

    proc = subprocess.Popen(
        [tf_exe, "vc", "get", src_abs, "/recursive", "/noprompt"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, shell=False,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            LOG.info("[TF-GET] %s", line)
    proc.wait()
    if proc.returncode != 0:
        sys.exit(f"Error: tf get failed with exit code {proc.returncode}.")
    LOG.info("[TF-GET] Complete.")


# ---------------------------------------------------------------------------
# .rptproj parsing
# ---------------------------------------------------------------------------

def parse_rptproj(rptproj_path: Path) -> tuple[str, str, set[str]]:
    """Return (target_server_url, target_report_folder, registered_report_names) from the Debug config."""
    if not rptproj_path.is_file():
        sys.exit(f"Error: .rptproj not found: {rptproj_path}")

    tree = ET.parse(rptproj_path)
    root = tree.getroot()
    ns = {"m": MSBUILD_NS}

    debug_group = None
    for pg in root.findall("m:PropertyGroup", ns):
        cond = pg.attrib.get("Condition", "")
        if "'$(Configuration)'" in cond and "'Debug'" in cond:
            debug_group = pg
            break
    if debug_group is None:
        sys.exit(f"Error: Debug PropertyGroup not found in {rptproj_path}.")

    def _text(tag: str) -> str:
        el = debug_group.find(f"m:{tag}", ns)
        if el is None or not (el.text or "").strip():
            sys.exit(f"Error: <{tag}> missing in Debug config of {rptproj_path}.")
        return el.text.strip()

    server_url = _text("TargetServerURL")
    report_folder = _text("TargetReportFolder")

    registered: set[str] = set()
    for item_group in root.findall("m:ItemGroup", ns):
        for rep in item_group.findall("m:Report", ns):
            include = rep.attrib.get("Include", "").strip()
            if include.lower().endswith(".rdl"):
                registered.add(include[:-4].lower())

    return server_url, report_folder, registered


# ---------------------------------------------------------------------------
# Report resolution
# ---------------------------------------------------------------------------

def resolve_report_files(
    report_names: list[str], src_dir: Path, registered: set[str]
) -> list[Path]:
    """Case-insensitive lookup of <name>.rdl under src_dir. Abort if any missing."""
    files_by_stem = {p.stem.lower(): p for p in src_dir.glob("*.rdl")}
    resolved: list[Path] = []
    missing: list[str] = []
    for name in report_names:
        key = name.lower()
        match = files_by_stem.get(key)
        if match is None:
            missing.append(name)
            continue
        resolved.append(match)
        if key not in registered:
            LOG.warning("[RESOLVE] '%s.rdl' is not registered in .rptproj (will still upload).", match.stem)
    if missing:
        LOG.error("[RESOLVE] Not found under %s: %s", src_dir, ", ".join(missing))
        sys.exit(1)
    return resolved


# ---------------------------------------------------------------------------
# SOAP client
# ---------------------------------------------------------------------------

def build_soap_client(server_url: str, env: str, prod_user: str | None, prod_password: str | None):
    """Build a zeep client for ReportService2010 with env-appropriate auth."""
    from zeep import Client
    from zeep.transports import Transport

    session = requests.Session()
    session.verify = False

    if env == "PROD":
        from requests_ntlm import HttpNtlmAuth
        assert prod_user and prod_password, "PROD requires user and password."
        session.auth = HttpNtlmAuth(prod_user, prod_password)
    else:
        from requests_negotiate_sspi import HttpNegotiateAuth
        session.auth = HttpNegotiateAuth()

    endpoint = urljoin(server_url if server_url.endswith("/") else server_url + "/",
                       "ReportService2010.asmx")
    wsdl = endpoint + "?wsdl"

    LOG.info("[SOAP] WSDL: %s", wsdl)
    try:
        client = Client(wsdl=wsdl, transport=Transport(session=session, timeout=60))
    except Exception as ex:
        sys.exit(f"Error: failed to load SSRS WSDL from {wsdl}: {ex}")
    return client


def deploy_report(client, target_folder: str, rdl_path: Path) -> tuple[bool, str]:
    """Upload one .rdl via CreateCatalogItem(Overwrite=True). Return (success, message)."""
    name = rdl_path.stem
    parent = "/" + target_folder.strip("/").replace("\\", "/")
    definition = rdl_path.read_bytes()
    try:
        result = client.service.CreateCatalogItem(
            ItemType="Report",
            Name=name,
            Parent=parent,
            Overwrite=True,
            Definition=definition,
            Properties=None,
        )
    except Exception as ex:
        return False, f"{ex}"

    warnings = []
    ret = getattr(result, "Warnings", None) if result is not None else None
    if ret:
        for w in ret:
            msg = getattr(w, "Message", str(w))
            warnings.append(msg)
    return True, "; ".join(warnings) if warnings else "ok"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()
    setup_logging(args.log_file)

    env = args.env
    src_dir = ENV_PATHS[env]
    rptproj_path = src_dir / RPTPROJ_NAME

    if not src_dir.is_dir():
        sys.exit(f"Error: source folder for {env} does not exist: {src_dir}")

    report_names = normalize_reports(args.reports)

    prod_user: str | None = None
    prod_password: str | None = None
    if env == "PROD":
        if not args.user:
            sys.exit("Error: -PROD requires -u (user).")
        prod_user = normalize_prod_user(args.user)
        prod_password = args.password
        if not prod_password:
            prod_password = getpass.getpass(f"Password for {prod_user}: ")
        if not prod_password:
            sys.exit("Error: PROD password not provided.")

    LOG.info("Environment: %s", env)
    LOG.info("Source:      %s", src_dir)
    LOG.info("Reports:     %s", ", ".join(report_names))

    if not args.no_pull and not args.dry_run:
        tf_get(src_dir)
    elif args.no_pull:
        LOG.info("[TF-GET] Skipped (--no-pull).")
    elif args.dry_run:
        LOG.info("[TF-GET] Skipped (--dry-run).")

    LOG.info("[PARSE] Reading %s", rptproj_path)
    server_url, report_folder, registered = parse_rptproj(rptproj_path)
    LOG.info("[PARSE] TargetServerURL:    %s", server_url)
    LOG.info("[PARSE] TargetReportFolder: %s", report_folder)

    LOG.info("[RESOLVE] Matching %d report(s) under %s", len(report_names), src_dir)
    files = resolve_report_files(report_names, src_dir, registered)
    for f in files:
        LOG.info("[RESOLVE] %s -> %s", f.stem, f)

    print()
    print("=" * 60)
    print(f"  Environment:  {env}")
    print(f"  Server URL:   {server_url}")
    print(f"  Report path:  /{report_folder.strip('/')}")
    print(f"  Reports:      {', '.join(f.stem for f in files)}")
    if env == "PROD":
        print(f"  User:         {prod_user}")
    print("=" * 60)

    if args.dry_run:
        LOG.info("[DRY RUN] No upload performed.")
        return

    if env == "PROD":
        answer = input("Proceed with PROD deploy? [y/N] ").strip().lower()
        if answer != "y":
            LOG.info("Aborted by user.")
            return

    client = build_soap_client(server_url, env, prod_user, prod_password)

    results: list[tuple[str, bool, str]] = []
    for rdl in files:
        LOG.info("[UPLOAD] %s -> %s", rdl.name, server_url)
        ok, msg = deploy_report(client, report_folder, rdl)
        if ok:
            LOG.info("[UPLOAD] %s: %s", rdl.stem, msg)
        else:
            LOG.error("[UPLOAD] %s FAILED: %s", rdl.stem, msg)
        results.append((rdl.stem, ok, msg))

    print()
    print("Summary:")
    for name, ok, msg in results:
        status = "OK  " if ok else "FAIL"
        print(f"  [{status}] {name}  {msg if not ok else ''}")

    if any(not ok for _, ok, _ in results):
        sys.exit(2)


if __name__ == "__main__":
    main()
