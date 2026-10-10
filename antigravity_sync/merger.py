"""
Database and file merger engine for importing Antigravity conversation bundles.
"""

import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple, Callable

from antigravity_sync.storage import backup_file, get_antigravity_dir
from antigravity_sync.bundle import extract_bundle, read_bundle_manifest


def get_table_columns(conn: sqlite3.Connection, table_name: str) -> List[str]:
    """Get list of column names for a table in the database.

    Args:
        conn: Open SQLite connection.
        table_name: Name of the table.

    Returns:
        List of column name strings.
    """
    cursor = conn.cursor()
    try:
        cursor.execute(f"PRAGMA table_info({table_name});")
        return [row[1] for row in cursor.fetchall()]
    except sqlite3.OperationalError:
        return []


def merge_summaries_db(
    target_db_path: Path,
    source_db_path: Path,
    remap_workspaces: Optional[List[Tuple[str, str]]] = None,
) -> Dict[str, int]:
    """Merge a source summaries database into the target summaries database using ATTACH and UPSERT.

    Args:
        target_db_path: Path to target conversation_summaries.db.
        source_db_path: Path to source conversation_summaries.db from bundle.
        remap_workspaces: Optional list of (old_prefix, new_prefix) path pairs to remap.

    Returns:
        Dictionary with count statistics {'inserted': int, 'updated': int}.
    """
    if not source_db_path.exists():
        return {"inserted": 0, "updated": 0}

    # If target DB does not exist, simply copy source DB and perform path remapping
    if not target_db_path.exists():
        target_db_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_db_path, target_db_path)
        if remap_workspaces:
            _apply_path_remapping(target_db_path, remap_workspaces)
        return {"inserted": -1, "updated": 0}

    target_conn = sqlite3.connect(str(target_db_path))
    target_conn.row_factory = sqlite3.Row
    cursor = target_conn.cursor()

    try:
        # Determine common columns between target and source tables
        target_cols = set(get_table_columns(target_conn, "conversation_summaries"))
        
        # Attach source database
        cursor.execute(f"ATTACH DATABASE ? AS src;", (str(source_db_path),))
        
        src_cols_query = cursor.execute("PRAGMA src.table_info(conversation_summaries);").fetchall()
        src_cols = set(row[1] for row in src_cols_query)

        common_cols = [c for c in target_cols if c in src_cols]
        if "conversation_id" not in common_cols:
            raise ValueError("Incompatible database: missing conversation_id primary key.")

        cols_str = ", ".join(common_cols)
        update_set_str = ", ".join(f"{c} = excluded.{c}" for c in common_cols if c != "conversation_id")

        # 1. UPSERT into conversation_summaries using INSERT OR REPLACE
        upsert_sql = f"""
            INSERT OR REPLACE INTO conversation_summaries ({cols_str})
            SELECT {cols_str} FROM src.conversation_summaries;
        """
        cursor.execute(upsert_sql)

        # 2. Sync conversations_fts (Full Text Search) if present
        target_has_fts = bool(
            cursor.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversations_fts'"
            ).fetchone()
        )
        src_has_fts = bool(
            cursor.execute(
                "SELECT 1 FROM src.sqlite_master WHERE type='table' AND name='conversations_fts'"
            ).fetchone()
        )

        if target_has_fts and src_has_fts:
            fts_cols = [
                "conversation_id", "chunk_index", "chunk_first_step", "chunk_last_step",
                "last_modified_time", "step_count", "last_step_created_at", "archived",
                "user_step_byte_offsets", "agent_step_byte_offsets",
                "title", "workspace", "project", "user_body", "agent_body"
            ]
            cols_clause = ", ".join(fts_cols)

            # Avoid duplicates in virtual table by clearing existing chunks for incoming conversations
            cursor.execute("""
                DELETE FROM conversations_fts 
                WHERE conversation_id IN (SELECT conversation_id FROM src.conversation_summaries);
            """)

            cursor.execute(f"""
                INSERT INTO conversations_fts ({cols_clause})
                SELECT {cols_clause} FROM src.conversations_fts;
            """)

        # 3. Apply path remapping if requested
        if remap_workspaces:
            for old_path, new_path in remap_workspaces:
                cursor.execute(
                    "UPDATE conversation_summaries SET workspace_uris = REPLACE(workspace_uris, ?, ?) WHERE workspace_uris LIKE ?;",
                    (old_path, new_path, f"%{old_path}%")
                )
                if target_has_fts:
                    cursor.execute(
                        "UPDATE conversations_fts SET workspace = REPLACE(workspace, ?, ?) WHERE workspace LIKE ?;",
                        (old_path, new_path, f"%{old_path}%")
                    )

        target_conn.commit()
    finally:
        try:
            cursor.execute("DETACH DATABASE src;")
        except Exception:
            pass
        target_conn.close()

    return {"inserted": 1, "updated": 0}


def _apply_path_remapping(db_path: Path, remap_workspaces: List[Tuple[str, str]]) -> None:
    """Apply workspace path replacement on a standalone database."""
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    try:
        has_fts = bool(
            cursor.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversations_fts'"
            ).fetchone()
        )
        for old_path, new_path in remap_workspaces:
            cursor.execute(
                "UPDATE conversation_summaries SET workspace_uris = REPLACE(workspace_uris, ?, ?) WHERE workspace_uris LIKE ?;",
                (old_path, new_path, f"%{old_path}%")
            )
            if has_fts:
                cursor.execute(
                    "UPDATE conversations_fts SET workspace = REPLACE(workspace, ?, ?) WHERE workspace LIKE ?;",
                    (old_path, new_path, f"%{old_path}%")
                )
        conn.commit()
    finally:
        conn.close()


def import_bundle(
    bundle_path: Path,
    target_dir: Optional[Path] = None,
    mode: str = "merge",
    remap_workspaces: Optional[List[Tuple[str, str]]] = None,
    dry_run: bool = False,
    create_backup: bool = True,
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
) -> Dict[str, Any]:
    """Execute bundle import into target Antigravity data directory.

    Args:
        bundle_path: Path to the .agypkg file.
        target_dir: Root target Antigravity directory (defaults to user standard directory).
        mode: 'merge' (incremental merge) or 'overwrite' (replace all).
        remap_workspaces: Optional list of (old_path, new_path) pairs for workspace URI replacement.
        dry_run: If True, only inspect and report plan without modifying files.
        create_backup: Whether to create a backup of target summaries.db before importing.
        progress_callback: Optional callback func(item_name, current_index, total_count).

    Returns:
        Dictionary containing execution summary and statistics.
    """
    target = target_dir or get_antigravity_dir()
    manifest = read_bundle_manifest(bundle_path)

    incoming_convs = manifest.get("conversations", [])
    total_convs = len(incoming_convs)
    incoming_ids = [c["conversation_id"] for c in incoming_convs]

    target_conv_dir = target / "conversations"
    target_brain_dir = target / "brain"
    target_db_path = target / "conversation_summaries.db"

    # Detect existing conflicts
    existing_ids = set()
    if target_conv_dir.exists():
        for f in os.listdir(target_conv_dir):
            if f.endswith(".db") or f.endswith(".pb"):
                cid = f.rsplit(".", 1)[0]
                existing_ids.add(cid)

    conflicting_ids = [cid for cid in incoming_ids if cid in existing_ids]

    report = {
        "bundle_version": manifest.get("version"),
        "created_at": manifest.get("created_at"),
        "source_platform": manifest.get("source_platform"),
        "total_incoming": total_convs,
        "conflicts": len(conflicting_ids),
        "new_conversations": total_convs - len(conflicting_ids),
        "mode": mode,
        "dry_run": dry_run,
        "backup_path": None,
    }

    if dry_run:
        return report

    # 1. Create safety backup if database exists
    if create_backup and target_db_path.exists():
        report["backup_path"] = str(backup_file(target_db_path))

    # 2. Extract bundle to temporary staging area
    with tempfile.TemporaryDirectory(prefix="agy_import_") as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        extract_bundle(bundle_path, temp_dir)

        # 3. Deploy conversation database/pb files
        target_conv_dir.mkdir(parents=True, exist_ok=True)
        src_conv_dir = temp_dir / "conversations"
        if src_conv_dir.exists():
            for f in os.listdir(src_conv_dir):
                shutil.copy2(src_conv_dir / f, target_conv_dir / f)

        # 4. Deploy brain directories
        target_brain_dir.mkdir(parents=True, exist_ok=True)
        src_brain_dir = temp_dir / "brain"
        if src_brain_dir.exists():
            for d in os.listdir(src_brain_dir):
                src_sub = src_brain_dir / d
                dst_sub = target_brain_dir / d
                if src_sub.is_dir():
                    shutil.copytree(src_sub, dst_sub, dirs_exist_ok=True)

        # 5. Merge SQLite indexes
        src_db_path = temp_dir / "conversation_summaries.db"
        if src_db_path.exists():
            merge_summaries_db(target_db_path, src_db_path, remap_workspaces=remap_workspaces)

    return report
