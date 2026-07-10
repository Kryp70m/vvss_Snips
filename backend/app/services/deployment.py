from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path


PRESERVE_RELATIVE_PATHS = {
    ".env",
    "ADMIN_PASSWORD.txt",
    "ADMIN_LOGIN_DETAILS.txt",
    "backend/app/data/access.sqlite3",
}

# Directories that must never be deleted or overwritten during a ZIP update.
# Paths are relative to the deployment root.
PRESERVE_DIRS = {
    "backend/.venv",
}

# How long (seconds) to wait for the app to respond after restart.
STARTUP_TIMEOUT = 30


class DeploymentError(ValueError):
    pass


def _stamp() -> str:
    return datetime.utcnow().strftime("%Y%m%d-%H%M%S")


def _safe_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members: list[zipfile.ZipInfo] = []
    for member in archive.infolist():
        name = member.filename.replace("\\", "/")
        if not name or name.startswith("/") or ".." in Path(name).parts:
            raise DeploymentError(f"Unsafe ZIP path: {member.filename}")
        if member.is_dir():
            continue
        members.append(member)
    return members


def _detect_update_root(extract_dir: Path) -> Path:
    candidates = [extract_dir]
    children = [child for child in extract_dir.iterdir() if child.is_dir()]
    if len(children) == 1:
        candidates.append(children[0])
    for candidate in candidates:
        if (candidate / "spot-momentum-scanner.html").exists() and (candidate / "backend" / "app").exists():
            return candidate
    raise DeploymentError("ZIP must contain spot-momentum-scanner.html and backend/app")


def _copy_tree_contents(src: Path, dst: Path, backup_dir: Path) -> None:
    """
    Merge src/ into dst/, honouring PRESERVE_RELATIVE_PATHS and PRESERVE_DIRS.

    Directories listed in PRESERVE_DIRS are never touched – they are neither
    deleted nor overwritten even when the incoming ZIP contains a folder with
    the same name.
    """
    preserve_file_abs = {(dst / rel).resolve() for rel in PRESERVE_RELATIVE_PATHS}
    preserve_dir_abs  = {(dst / rel).resolve() for rel in PRESERVE_DIRS}

    for item in src.iterdir():
        target = dst / item.name

        # Never touch explicitly preserved files.
        if target.resolve() in preserve_file_abs:
            continue

        if item.is_dir():
            if item.name in {"__pycache__", ".pytest_cache"}:
                continue

            # Never delete or overwrite a preserved directory (e.g. .venv).
            if target.resolve() in preserve_dir_abs:
                continue
            # Also guard any subdirectory whose resolved path is *inside* a
            # preserved directory (handles nested copytree calls).
            if any(str(target.resolve()).startswith(str(p) + os.sep)
                   for p in preserve_dir_abs):
                continue

            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(
                item, target,
                ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "access.sqlite3"),
            )
        else:
            if item.name in {".server.pid"}:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)

    # Restore any preserved files that were wiped before this call.
    for rel in PRESERVE_RELATIVE_PATHS:
        backup_file = backup_dir / rel
        live_file   = dst / rel
        if backup_file.exists() and not live_file.exists():
            live_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup_file, live_file)


# ---------------------------------------------------------------------------
# Virtual-environment helpers
# ---------------------------------------------------------------------------

def _venv_python(root: Path) -> Path:
    return root / "backend" / ".venv" / "bin" / "python"


def _venv_pip(root: Path) -> Path:
    return root / "backend" / ".venv" / "bin" / "pip"


def _venv_uvicorn(root: Path) -> Path:
    return root / "backend" / ".venv" / "bin" / "uvicorn"


def _requirements_hash(root: Path) -> str | None:
    req = root / "backend" / "requirements.txt"
    if not req.exists():
        return None
    return hashlib.sha256(req.read_bytes()).hexdigest()


def _ensure_venv(root: Path) -> None:
    """Create the venv if it does not exist yet."""
    venv_dir = root / "backend" / ".venv"
    if not venv_dir.exists():
        subprocess.run(
            [sys.executable, "-m", "venv", str(venv_dir)],
            check=True,
            capture_output=True,
            text=True,
        )


def _pip_install(root: Path) -> None:
    """Run pip install -r requirements.txt inside the existing venv."""
    req = root / "backend" / "requirements.txt"
    if not req.exists():
        return
    pip = _venv_pip(root)
    result = subprocess.run(
        [str(pip), "install", "-r", str(req)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise DeploymentError(
            f"pip install failed (exit {result.returncode}):\n{result.stderr[-2000:]}"
        )


def _sync_dependencies_if_changed(root: Path, old_hash: str | None) -> None:
    """Install/upgrade packages only when requirements.txt changed."""
    new_hash = _requirements_hash(root)
    if new_hash is None:
        return  # No requirements file – nothing to do.
    if new_hash == old_hash:
        return  # File unchanged – skip costly pip run.
    _ensure_venv(root)
    _pip_install(root)


def _verify_uvicorn(root: Path) -> None:
    """Raise DeploymentError if uvicorn is not executable inside the venv."""
    uvicorn = _venv_uvicorn(root)
    if not uvicorn.exists():
        raise DeploymentError(
            f"uvicorn not found at {uvicorn}. "
            "The virtual-environment may be missing or pip install may have failed."
        )
    result = subprocess.run(
        [str(uvicorn), "--version"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise DeploymentError(
            f"uvicorn binary exists but --version failed: {result.stderr}"
        )


# ---------------------------------------------------------------------------
# Startup validation (systemd / direct)
# ---------------------------------------------------------------------------

def _verify_app_starts(root: Path) -> None:
    """
    Spawn the app with --workers 1 --timeout-keep-alive 1 for a brief smoke
    test.  We send it SIGTERM after STARTUP_TIMEOUT seconds and check the
    exit code; a clean shutdown (0 / 143) means startup succeeded.

    This function is intentionally *not* async so it can block the deploy
    call and give a synchronous yes/no answer before we write the success
    marker.
    """
    uvicorn = _venv_uvicorn(root)
    app_dir  = root / "backend"

    proc = subprocess.Popen(
        [
            str(uvicorn),
            "app.main:app",
            "--host", "127.0.0.1",
            "--port", "8765",   # ephemeral port, not the production one
            "--workers", "1",
        ],
        cwd=str(app_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        proc.wait(timeout=STARTUP_TIMEOUT)
        # If uvicorn exits on its own within the timeout it crashed.
        stdout = proc.stdout.read() if proc.stdout else ""
        stderr = proc.stderr.read() if proc.stderr else ""
        raise DeploymentError(
            f"App exited unexpectedly during startup validation "
            f"(exit {proc.returncode}).\nstdout: {stdout[-1000:]}\nstderr: {stderr[-1000:]}"
        )
    except subprocess.TimeoutExpired:
        # Still running after STARTUP_TIMEOUT → startup succeeded.
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        # returncode 0 (clean) or -15 (SIGTERM) are both fine.
        return


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def _backup_current(root: Path) -> Path:
    backup_dir = root / "backups" / f"pre-update-{_stamp()}"
    backup_dir.parent.mkdir(parents=True, exist_ok=True)
    ignore = shutil.ignore_patterns("backups", "__pycache__", ".pytest_cache", ".server.pid")
    shutil.copytree(root, backup_dir, ignore=ignore)
    return backup_dir


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def deploy_zip(root: Path, zip_bytes: bytes) -> dict:
    if len(zip_bytes) < 100:
        raise DeploymentError("Uploaded ZIP is empty or too small")

    updates_dir = root / "backups" / "uploaded-updates"
    extract_dir = updates_dir / f"extract-{_stamp()}"
    zip_path    = updates_dir / f"update-{_stamp()}.zip"
    updates_dir.mkdir(parents=True, exist_ok=True)
    zip_path.write_bytes(zip_bytes)

    # Snapshot requirements hash *before* any files are touched.
    old_req_hash = _requirements_hash(root)

    backup_dir = _backup_current(root)
    try:
        with zipfile.ZipFile(zip_path) as archive:
            members = _safe_members(archive)
            if not members:
                raise DeploymentError("ZIP has no files")
            archive.extractall(extract_dir)

        update_root = _detect_update_root(extract_dir)
        _copy_tree_contents(update_root, root, backup_dir)

        # Sync dependencies if requirements.txt changed; venv is always kept.
        _sync_dependencies_if_changed(root, old_req_hash)

        # Hard gate: uvicorn must be present before we even attempt a restart.
        _verify_uvicorn(root)

        # Smoke-test: confirm the app actually starts with the new code.
        _verify_app_starts(root)

    except Exception:
        # Any failure → restore the previous working state automatically.
        rollback_from_backup(root, backup_dir)
        raise
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)

    marker = root / "backups" / "last-successful-backup.txt"
    marker.write_text(str(backup_dir), encoding="utf-8")
    return {
        "ok": True,
        "backup": str(backup_dir),
        "zip": str(zip_path),
        "restart_required": True,
        "message": "Update deployed, startup validated. App restart scheduled.",
    }


def rollback_from_backup(root: Path, backup_dir: Path) -> None:
    if not backup_dir.exists() or not (backup_dir / "backend" / "app").exists():
        raise DeploymentError("Backup is missing or invalid")
    _copy_tree_contents(backup_dir, root, backup_dir)


def rollback_last(root: Path) -> dict:
    marker = root / "backups" / "last-successful-backup.txt"
    if not marker.exists():
        raise DeploymentError("No rollback backup is available")
    backup_dir = Path(marker.read_text(encoding="utf-8").strip())
    rollback_from_backup(root, backup_dir)
    return {
        "ok": True,
        "backup": str(backup_dir),
        "restart_required": True,
        "message": "Rollback restored. App restart scheduled.",
    }


async def restart_process_later(delay: float = 1.5) -> None:
    await asyncio.sleep(delay)
    os._exit(0)
