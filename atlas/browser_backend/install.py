"""Idempotent installer for the pinned Playwright-MCP server.

Behaviour (see ``tools/playwright-mcp/README.md``):

* require Node >= 18;
* prefer an already-installed launcher copy (``ATLAS_PLAYWRIGHT_MCP_CLI`` /
  ``ATLAS_PLAYWRIGHT_MCP_ROOT``) when it resolves to the pinned version;
* otherwise install ``@playwright/mcp@0.0.80`` into the product install root
  (default ``%LOCALAPPDATA%\\Atlas\\tools\\playwright-mcp\\0.0.80``) using
  exact-lock ``npm ci`` against the committed manifest;
* never install globally, never download a bundled browser (reuse Chrome/Edge);
* verify the resolved version and license after install;
* write no secrets.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from atlas.browser_backend import MCP_LICENSE, MCP_PACKAGE, MCP_VERSION, DEFAULT_INSTALL_SUBPATH


class InstallError(RuntimeError):
    pass


def default_install_root() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    return base.joinpath(*DEFAULT_INSTALL_SUBPATH)


def repo_manifest_dir() -> Path:
    # atlas/browser_backend/install.py -> repo root is parents[2]
    return Path(__file__).resolve().parents[2] / "tools" / "playwright-mcp"


def _node_version() -> Optional[tuple[int, int, int]]:
    exe = shutil.which("node")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    raw = (out.stdout or "").strip().lstrip("v")
    parts = raw.split(".")
    try:
        return (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0, int(parts[2]) if len(parts) > 2 else 0)
    except (ValueError, IndexError):
        return None


def node_ok() -> tuple[bool, str]:
    ver = _node_version()
    if ver is None:
        return False, "node not found or unparseable"
    if ver[0] < 18:
        return False, f"node {ver[0]}.{ver[1]}.{ver[2]} < 18"
    return True, f"node {ver[0]}.{ver[1]}.{ver[2]}"


def cli_version(cli_path: Path) -> Optional[str]:
    exe = shutil.which("node")
    if not exe or not Path(cli_path).exists():
        return None
    try:
        out = subprocess.run([exe, str(cli_path), "--version"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    text = ((out.stdout or "") + (out.stderr or "")).strip()
    # "Version 0.0.80" or "0.0.80"
    for tok in text.replace("Version", " ").split():
        if tok.count(".") >= 2:
            return tok.strip()
    return text or None


def _installed_license(root: Path) -> str:
    pj = root / "node_modules" / "@playwright" / "mcp" / "package.json"
    if pj.exists():
        try:
            return str(json.loads(pj.read_text(encoding="utf-8")).get("license", ""))
        except (ValueError, OSError):
            return ""
    return ""


def _resolve_env_cli() -> Optional[Path]:
    env_cli = os.environ.get("ATLAS_PLAYWRIGHT_MCP_CLI")
    if env_cli and Path(env_cli).exists():
        return Path(env_cli)
    root = os.environ.get("ATLAS_PLAYWRIGHT_MCP_ROOT")
    if root:
        p = Path(root) / "node_modules" / "@playwright" / "mcp" / "cli.js"
        if p.exists():
            return p
    return None


def install(*, install_root: Optional[Path] = None, force: bool = False, prefer_env: bool = True) -> dict:
    """Idempotently ensure the pinned MCP server is available. Returns a report
    dict; raises :class:`InstallError` only on an unrecoverable failure."""
    report: dict = {"package": MCP_PACKAGE, "version": MCP_VERSION, "license": MCP_LICENSE,
                    "action": "", "cli_path": "", "install_root": "", "node": "", "verified": False}
    ok, node_msg = node_ok()
    report["node"] = node_msg
    if not ok:
        raise InstallError(node_msg)

    if prefer_env and not force:
        env_cli = _resolve_env_cli()
        if env_cli is not None:
            ver = cli_version(env_cli)
            if ver == MCP_VERSION:
                report.update(action="reused-launcher-copy", cli_path=str(env_cli),
                              install_root=str(env_cli.parents[3]), verified=True)
                return report

    root = Path(install_root) if install_root else default_install_root()
    cli_path = root / "node_modules" / "@playwright" / "mcp" / "cli.js"

    if not force and cli_version(cli_path) == MCP_VERSION:
        report.update(action="already-installed", cli_path=str(cli_path), install_root=str(root),
                      verified=True, license=_installed_license(root) or MCP_LICENSE)
        return report

    npm = shutil.which("npm")
    if not npm:
        raise InstallError("npm not found on PATH")
    root.mkdir(parents=True, exist_ok=True)
    manifest = repo_manifest_dir()
    for name in ("package.json", "package-lock.json"):
        src = manifest / name
        if src.exists():
            shutil.copyfile(src, root / name)
    env = dict(os.environ)
    env["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] = "1"  # reuse installed Chrome/Edge
    env["npm_config_fund"] = "false"
    env["npm_config_audit"] = "false"
    have_lock = (root / "package-lock.json").exists()
    cmd = [npm, "ci", "--omit=dev"] if have_lock else [npm, "install", "--no-save", f"{MCP_PACKAGE}@{MCP_VERSION}"]
    try:
        proc = subprocess.run(cmd, cwd=str(root), env=env, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallError(f"npm install failed to start: {exc}") from exc
    if proc.returncode != 0:
        raise InstallError(f"npm install failed ({proc.returncode}): {(proc.stderr or '')[-500:]}")

    ver = cli_version(cli_path)
    lic = _installed_license(root) or MCP_LICENSE
    report.update(action="installed", cli_path=str(cli_path), install_root=str(root),
                  verified=(ver == MCP_VERSION), license=lic, resolved_version=ver)
    if ver != MCP_VERSION:
        raise InstallError(f"post-install version mismatch: {ver!r} != {MCP_VERSION!r}")
    if MCP_LICENSE.lower() not in lic.lower():
        report["license_warning"] = f"expected {MCP_LICENSE}, found {lic!r}"
    return report


__all__ = ["InstallError", "install", "node_ok", "cli_version", "default_install_root", "repo_manifest_dir"]
