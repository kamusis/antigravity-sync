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
from antigravity_sync.proto_utils import (
    read_agyhub_proto_file,
    merge_agyhub_proto_file,
    build_one_pass_replacer,
    transform_proto_bytes,
)

OUTSIDE_PROJECT_ID = "outside-of-project"


def build_blob_replacements(
    project_id_map: Optional[Dict[str, str]],
    remap_workspaces: Optional[List[Tuple[str, str]]],
) -> List[Tuple[bytes, bytes]]:
    """Build byte-level replacement pairs for patching protobuf blobs.

    Args:
        project_id_map: Mapping of source project UUID to target project UUID.
        remap_workspaces: List of (old_path, new_path) string pairs.

    Returns:
        List of (old_bytes, new_bytes) pairs.
    """
    replacements: List[Tuple[bytes, bytes]] = []
    if project_id_map:
        for s_pid, t_pid in project_id_map.items():
            if len(s_pid) == 36 and len(t_pid) == 36 and s_pid != t_pid:
                replacements.append((s_pid.encode("utf-8"), t_pid.encode("utf-8")))
    if remap_workspaces:
        replacements.extend(
            (old.encode("utf-8"), new.encode("utf-8")) if isinstance(old, str) else (old, new)
            for old, new in remap_workspaces
            if old and old != new
        )
    return replacements


def patch_conversation_blob(
    db_path: Path,
    project_id_map: Optional[Dict[str, str]] = None,
    remap_workspaces: Optional[List[Tuple[str, str]]] = None,
) -> bool:
    """Patch trajectory_metadata_blob inside a conversation database in place.

    Rewrites the embedded project UUID(s) and workspace paths so the local
    LanguageServer recognizes the conversation as belonging to a local project.
    Length prefixes are recomputed, keeping the protobuf wire format valid.

    Args:
        db_path: Path to the conversation <id>.db file.
        project_id_map: Mapping of source project UUID to target project UUID.
        remap_workspaces: List of (old_path, new_path) string pairs.

    Returns:
        True if the blob was modified, False otherwise.
    """
    replacements = build_blob_replacements(project_id_map, remap_workspaces)
    if not replacements or not db_path.exists():
        return False

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT id, data FROM trajectory_metadata_blob;")
        except sqlite3.DatabaseError:
            # Not a usable SQLite database (e.g. placeholder file): skip.
            return False
        rows = cursor.fetchall()
        changed = False
        for row_id, data in rows:
            if not data:
                continue
            blob = data if isinstance(data, bytes) else data.encode("utf-8", "surrogateescape")
            new_blob = transform_proto_bytes(blob, replacements)
            if new_blob != blob:
                cursor.execute(
                    "UPDATE trajectory_metadata_blob SET data = ? WHERE id = ?;",
                    (new_blob, row_id),
                )
                changed = True
        if changed:
            conn.commit()
        return changed
    finally:
        conn.close()


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
    project_id_map: Optional[Dict[str, str]] = None,
) -> Dict[str, int]:
    """Merge a source summaries database into the target summaries database using ATTACH and UPSERT.

    Args:
        target_db_path: Path to target conversation_summaries.db.
        source_db_path: Path to source conversation_summaries.db from bundle.
        remap_workspaces: Optional list of (old_prefix, new_prefix) path pairs to remap.
        project_id_map: Optional dict mapping source project_id to target project_id.

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

        # 3. Synchronize titles so sessions never appear untitled.
        # Prefer the source FTS title (what the source UI displayed), then the
        # summaries title, and only fall back to preview (raw prompt) last.
        if target_has_fts and src_has_fts:
            cursor.execute("""
                UPDATE conversation_summaries
                SET title = (
                    SELECT f.title FROM conversations_fts f
                    WHERE f.conversation_id = conversation_summaries.conversation_id
                      AND f.chunk_index = -1
                )
                WHERE (title IS NULL OR title = '')
                  AND conversation_id IN (SELECT conversation_id FROM src.conversation_summaries)
                  AND EXISTS (
                      SELECT 1 FROM conversations_fts f
                      WHERE f.conversation_id = conversation_summaries.conversation_id
                        AND f.chunk_index = -1 AND f.title IS NOT NULL AND f.title != ''
                  );
            """)
        cursor.execute("""
            UPDATE conversation_summaries
            SET title = preview
            WHERE (title IS NULL OR title = '') AND preview IS NOT NULL AND preview != '';
        """)
        cursor.execute("""
            UPDATE conversation_summaries
            SET preview = title
            WHERE (preview IS NULL OR preview = '') AND title IS NOT NULL AND title != '';
        """)
        if target_has_fts:
            cursor.execute("""
                UPDATE conversations_fts
                SET title = (
                    SELECT s.title FROM conversation_summaries s
                    WHERE s.conversation_id = conversations_fts.conversation_id
                )
                WHERE chunk_index = -1 AND (title IS NULL OR title = '')
                  AND conversation_id IN (SELECT conversation_id FROM src.conversation_summaries);
            """)

        # 4. Remap project IDs across machines (summaries only; the FTS table
        # stores directory slugs, not UUIDs, and is corrected in step 6).
        if project_id_map:
            for s_pid, t_pid in project_id_map.items():
                cursor.execute(
                    "UPDATE conversation_summaries SET project_id = ? WHERE project_id = ?;",
                    (t_pid, s_pid),
                )

        # 5. Apply path remapping in a single pass (sequential REPLACE would
        # re-match the output of earlier rules and corrupt paths).
        if remap_workspaces:
            replacer = build_one_pass_replacer([
                (old.encode("utf-8"), new.encode("utf-8")) if isinstance(old, str) else (old, new)
                for old, new in remap_workspaces
            ])
            rows = cursor.execute(
                "SELECT conversation_id, workspace_uris FROM conversation_summaries WHERE workspace_uris IS NOT NULL AND workspace_uris != '';"
            ).fetchall()
            for cid, uris in rows:
                new_uris = replacer(uris.encode("utf-8")).decode("utf-8")
                if new_uris != uris:
                    cursor.execute(
                        "UPDATE conversation_summaries SET workspace_uris = ? WHERE conversation_id = ?;",
                        (new_uris, cid),
                    )
            if target_has_fts:
                rows = cursor.execute(
                    "SELECT rowid, workspace FROM conversations_fts WHERE workspace IS NOT NULL AND workspace != '';"
                ).fetchall()
                for rowid, ws in rows:
                    new_ws = replacer(ws.encode("utf-8")).decode("utf-8")
                    if new_ws != ws:
                        cursor.execute(
                            "UPDATE conversations_fts SET workspace = ? WHERE rowid = ?;",
                            (new_ws, rowid),
                        )

        # 6. Align FTS project/workspace slugs with the target machine's own
        # grouping: derive project_id -> slug from native target rows, then set
        # each imported conversation's slug accordingly. Conversations mapped
        # to 'outside-of-project' are ungrouped (''), matching native behavior.
        if target_has_fts and src_has_fts:
            pid_slugs: Dict[str, Tuple[str, str]] = {}
            for row in cursor.execute("""
                SELECT s.project_id AS pid, f.project AS proj, f.workspace AS ws
                FROM conversation_summaries s
                JOIN conversations_fts f
                  ON f.conversation_id = s.conversation_id AND f.chunk_index = -1
                WHERE f.project IS NOT NULL AND f.project != ''
                  AND s.conversation_id NOT IN (SELECT conversation_id FROM src.conversation_summaries);
            """):
                pid = row["pid"]
                if pid and pid != OUTSIDE_PROJECT_ID and pid not in pid_slugs:
                    pid_slugs[pid] = (row["proj"], row["ws"])

            imported = cursor.execute(
                "SELECT conversation_id, project_id FROM src.conversation_summaries;"
            ).fetchall()
            for cid, src_pid in imported:
                mapped_pid = project_id_map.get(src_pid, src_pid) if project_id_map else src_pid
                if mapped_pid in pid_slugs:
                    proj, ws = pid_slugs[mapped_pid]
                elif mapped_pid in (OUTSIDE_PROJECT_ID, ""):
                    proj, ws = "", ""
                else:
                    # Unknown/foreign project: keep the copied source slug;
                    # the local LanguageServer will re-evaluate on rescan.
                    continue
                cursor.execute(
                    "UPDATE conversations_fts SET project = ?, workspace = ? WHERE conversation_id = ? AND chunk_index = -1;",
                    (proj, ws, cid),
                )

        target_conn.commit()

        # Checkpoint WAL cleanly
        try:
            target_conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
        except sqlite3.OperationalError:
            pass
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
    project_id_map: Optional[Dict[str, str]] = None,
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
        project_id_map: Optional dict mapping source project_id to target project_id.
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
            # 3b. Patch trajectory_metadata_blob inside deployed conversation
            # databases so the local LanguageServer sees local project UUIDs
            # and paths instead of the source machine's ones.
            if project_id_map or remap_workspaces:
                for f in os.listdir(src_conv_dir):
                    if f.endswith(".db"):
                        patch_conversation_blob(
                            target_conv_dir / f,
                            project_id_map=project_id_map,
                            remap_workspaces=remap_workspaces,
                        )

        # 3c. Deploy per-conversation annotations (title/archived state).
        src_ann_dir = temp_dir / "annotations"
        if src_ann_dir.exists():
            target_ann_dir = target / "annotations"
            target_ann_dir.mkdir(parents=True, exist_ok=True)
            for f in os.listdir(src_ann_dir):
                if f.endswith(".pbtxt"):
                    shutil.copy2(src_ann_dir / f, target_ann_dir / f)

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
            merge_summaries_db(
                target_db_path,
                src_db_path,
                remap_workspaces=remap_workspaces,
                project_id_map=project_id_map,
            )

        # 6. Merge protobuf cache (agyhub_summaries_proto.pb)
        src_pb_path = temp_dir / "agyhub_summaries.pb"
        target_pb_path = target / "agyhub_summaries_proto.pb"
        if src_pb_path.exists():
            incoming_pb = read_agyhub_proto_file(src_pb_path)
            if incoming_pb:
                merge_agyhub_proto_file(target_pb_path, incoming_pb)

    return report
