"""
Storage and file-system abstraction for Antigravity runtime data.
"""

import os
import sys
import shutil
import sqlite3
import datetime
import subprocess
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple


def get_antigravity_dir(custom_path: Optional[str] = None) -> Path:
    """Resolve the base Antigravity application data directory.

    Args:
        custom_path: Optional override path specified by user.

    Returns:
        Path object pointing to the Antigravity data directory.
    """
    if custom_path:
        return Path(custom_path).expanduser().resolve()

    env_dir = os.environ.get("ANTIGRAVITY_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()

    # Standard default across platforms
    return (Path.home() / ".gemini" / "antigravity").resolve()


def is_antigravity_running() -> bool:
    """Check if Antigravity or its LanguageServer processes are currently running.

    Returns:
        True if running process detected, False otherwise.
    """
    try:
        if sys.platform == "win32":
            output = subprocess.check_output(
                ["tasklist", "/FO", "CSV", "/NH"],
                text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            process_names = ["antigravity.exe", "language_server.exe", "language_server_windows_x64.exe"]
            lower_output = output.lower()
            return any(p in lower_output for p in process_names)
        else:
            # macOS / Linux: match process executable name boundary
            result = subprocess.run(
                ["pgrep", "-f", r"(^|/)antigravity(\s|$)|(^|/)language_server(\s|$)"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return result.returncode == 0
    except Exception:
        return False


def create_safe_db_snapshot(src_db_path: Path, dst_db_path: Path) -> None:
    """Create an atomic and consistent snapshot of a SQLite database using the SQLite Backup API.

    This ensures that even if WAL mode is active, all uncommitted pages and active transactions
    are cleanly checkpointed and saved to the snapshot file without locking issues.

    Args:
        src_db_path: Path to the source SQLite database.
        dst_db_path: Path to write the clean snapshot database.
    """
    if dst_db_path.exists():
        dst_db_path.unlink()

    dst_db_path.parent.mkdir(parents=True, exist_ok=True)

    src_conn = sqlite3.connect(str(src_db_path))
    try:
        # Attempt a passive checkpoint if WAL is active
        try:
            src_conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
        except sqlite3.OperationalError:
            pass

        dst_conn = sqlite3.connect(str(dst_db_path))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def backup_file(file_path: Path) -> Path:
    """Create a timestamped backup copy of a file before modification.

    Args:
        file_path: File to back up.

    Returns:
        Path of the newly created backup file.
    """
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = file_path.with_name(f"{file_path.name}.bak_{timestamp}")
    shutil.copy2(file_path, backup_path)
    return backup_path


def list_conversations_from_db(
    db_path: Path,
    since: Optional[str] = None,
    workspace: Optional[str] = None,
    conv_ids: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Query conversations metadata from conversation_summaries.db.

    Args:
        db_path: Path to conversation_summaries.db.
        since: Optional minimum last_modified_time filter (YYYY-MM-DD or ISO format).
        workspace: Optional workspace substring to match against workspace_uris.
        conv_ids: Optional specific list of conversation UUIDs.

    Returns:
        List of conversation metadata dictionaries.
    """
    if not db_path.exists():
        return []

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    query = """
        SELECT conversation_id, title, preview, step_count, 
               last_modified_time, workspace_uris, status, source
        FROM conversation_summaries
        WHERE 1=1
    """
    params: List[Any] = []

    if since:
        query += " AND (last_modified_time >= ? OR last_user_input_time >= ?)"
        params.extend([since, since])

    if workspace:
        query += " AND workspace_uris LIKE ?"
        params.append(f"%{workspace}%")

    if conv_ids:
        placeholders = ",".join("?" for _ in conv_ids)
        query += f" AND conversation_id IN ({placeholders})"
        params.extend(conv_ids)

    query += " ORDER BY last_modified_time DESC"

    try:
        cursor.execute(query, params)
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
