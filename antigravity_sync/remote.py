"""
Remote SSH synchronization module for bidirectional Antigravity conversation sync.
"""

import os
import sys
import json
import time
import shutil
import tempfile
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional


def run_ssh_command(ssh_host: str, command: str) -> Tuple[int, str, str]:
    """Execute a command on remote host via OpenSSH.

    Args:
        ssh_host: SSH host or alias (e.g. 'macbook').
        command: Shell command to run on remote.

    Returns:
        Tuple of (exit_code, stdout_str, stderr_str).
    """
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", ssh_host, command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def scp_upload(ssh_host: str, local_path: Path, remote_path: str) -> None:
    """Upload a file to remote host using scp."""
    proc = subprocess.run(
        ["scp", "-o", "BatchMode=yes", str(local_path), f"{ssh_host}:{remote_path}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"SCP upload failed: {proc.stderr}")


def scp_download(ssh_host: str, remote_path: str, local_path: Path) -> None:
    """Download a file from remote host using scp."""
    proc = subprocess.run(
        ["scp", "-o", "BatchMode=yes", f"{ssh_host}:{remote_path}", str(local_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"SCP download failed: {proc.stderr}")


def run_ssh_python(ssh_host: str, script: str) -> Tuple[int, str, str]:
    """Execute python script on remote host via OpenSSH stdin."""
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", ssh_host, "python3 -"],
        input=script,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def get_remote_conversations(ssh_host: str) -> Dict[str, Dict[str, Any]]:
    """Query conversation metadata from remote Antigravity installation.

    Returns:
        Dictionary mapping conversation_id to {last_modified_time, title, preview, step_count, workspace_uris, project_id}.
    """
    script = (
        'import sqlite3, os, json\n'
        'p = os.path.expanduser("~/.gemini/antigravity/conversation_summaries.db")\n'
        'if not os.path.exists(p):\n'
        '    print(json.dumps({}))\n'
        '    exit(0)\n'
        'conn = sqlite3.connect(p)\n'
        'conn.row_factory = sqlite3.Row\n'
        'rows = conn.execute("SELECT conversation_id, title, preview, step_count, last_modified_time, workspace_uris, project_id FROM conversation_summaries").fetchall()\n'
        'data = {r["conversation_id"]: dict(r) for r in rows}\n'
        'conn.close()\n'
        'print(json.dumps(data))\n'
    )

    rc, stdout, stderr = run_ssh_python(ssh_host, script)

    if rc != 0:
        raise RuntimeError(f"Failed to query remote conversations: {stderr}")

    try:
        return json.loads(stdout.strip())
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse remote conversation list: {e}. Output was: {stdout}")


def get_remote_workspaces(ssh_host: str) -> Dict[str, Dict[str, str]]:
    """Query distinct workspaces and their project IDs from remote Antigravity installation.

    Returns:
        Dict mapping project slug to {'project_id': str, 'workspace_uris': str}.
    """
    script = (
        'import json, sys\n'
        'from pathlib import Path\n'
        'sys.path.insert(0, str(Path.home() / "CascadeProjects/antigravity-sync"))\n'
        'from antigravity_sync.workspace_mapper import get_workspaces_from_db\n'
        'from antigravity_sync.storage import get_antigravity_dir\n'
        'db = get_antigravity_dir() / "conversation_summaries.db"\n'
        'print(json.dumps(get_workspaces_from_db(db)))\n'
    )

    rc, stdout, stderr = run_ssh_python(ssh_host, script)

    if rc != 0:
        raise RuntimeError(f"Failed to query remote workspaces: {stderr}")

    try:
        return json.loads(stdout.strip())
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse remote workspace list: {e}. Output was: {stdout}")


def get_remote_platform(ssh_host: str) -> str:
    """Detect remote operating system platform (e.g. Darwin, Linux, Windows)."""
    rc, stdout, _ = run_ssh_command(ssh_host, "uname -s")
    if rc == 0 and stdout.strip():
        return stdout.strip()
    return "Unknown"


def auto_detect_remapping(local_user: str, remote_user: str, remote_platform: str) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
    """Compute bidirectional workspace path remapping pairs.

    Returns:
        (push_remap_rules, pull_remap_rules)
    """
    push_rules = []
    pull_rules = []

    if sys.platform == "win32" and remote_platform == "Darwin":
        # Windows -> macOS
        push_rules = [
            (f"c:/Users/{local_user}", f"/Users/{remote_user}"),
            (f"C:/Users/{local_user}", f"/Users/{remote_user}"),
            (f"c:\\Users\\{local_user}", f"/Users/{remote_user}"),
            (f"C:\\Users\\{local_user}", f"/Users/{remote_user}"),
        ]
        # macOS -> Windows
        pull_rules = [
            (f"/Users/{remote_user}", f"c:/Users/{local_user}"),
        ]
    elif sys.platform == "darwin" and remote_platform == "Windows":
        # macOS -> Windows
        push_rules = [
            (f"/Users/{local_user}", f"c:/Users/{remote_user}"),
        ]
        # Windows -> macOS
        pull_rules = [
            (f"c:/Users/{remote_user}", f"/Users/{local_user}"),
            (f"C:/Users/{remote_user}", f"/Users/{local_user}"),
        ]

    return push_rules, pull_rules
