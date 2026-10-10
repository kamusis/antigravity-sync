"""
Dynamic workspace and project ID discovery and remapping engine.

Aligns Antigravity project UUIDs and workspace URIs across platforms (macOS/Windows/Linux).
"""

import re
import sqlite3
import urllib.parse
from pathlib import Path
from typing import Dict, List, Tuple, Any, Optional


def extract_project_slug(workspace_uri_str: str) -> Optional[str]:
    """Extract a canonical project identifier/slug from workspace URI JSON string.

    Examples:
        '["file:///c%3A/Users/kamus/CascadeProjects/mopheus"]' -> 'CascadeProjects/mopheus'
        '["file:///Users/kamus/CascadeProjects/mopheus"]' -> 'CascadeProjects/mopheus'
        '["file:///c:/Users/kamus/CascadeProjects/axon-cli"]' -> 'CascadeProjects/axon-cli'

    Args:
        workspace_uri_str: Raw workspace_uris string from SQLite.

    Returns:
        Canonical slug string or None.
    """
    if not workspace_uri_str:
        return None

    # Unquote URL encoding (%3A -> :, etc.)
    decoded = urllib.parse.unquote(workspace_uri_str)

    # Search for common project root directory patterns
    patterns = [
        r"CascadeProjects/([^/\"\'\\,\]]+)",
        r"\.config/([^/\"\'\\,\]]+)",
        r"Desktop/([^/\"\'\\,\]]+)",
    ]

    for pat in patterns:
        m = re.search(pat, decoded)
        if m:
            category = pat.split("/")[0].replace("\\", "")
            return f"{category}/{m.group(1)}"

    # Fallback: extract last folder name before end of file URI
    m_folder = re.findall(r"file:///[^\"\'\]]+?/([^/\"\'\\,\]]+)", decoded)
    if m_folder:
        return m_folder[-1]

    return None


def get_workspaces_from_db(db_path: Path) -> Dict[str, Dict[str, str]]:
    """Query and extract unique workspaces and their project IDs from a database.

    Args:
        db_path: Path to conversation_summaries.db.

    Returns:
        Dict mapping slug -> {'project_id': str, 'workspace_uris': str, 'raw_uri': str}
    """
    if not db_path.exists():
        return {}

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    workspaces: Dict[str, Dict[str, str]] = {}

    try:
        rows = conn.execute(
            """
            SELECT DISTINCT project_id, workspace_uris 
            FROM conversation_summaries 
            WHERE workspace_uris IS NOT NULL AND workspace_uris != ''
            """
        ).fetchall()

        for r in rows:
            pid = r["project_id"]
            uris = r["workspace_uris"]
            slug = extract_project_slug(uris)
            if slug:
                # Prefer valid UUID over outside-of-project or empty
                if slug not in workspaces or (
                    pid not in ("outside-of-project", "")
                    and workspaces[slug]["project_id"] in ("outside-of-project", "")
                ):
                    workspaces[slug] = {
                        "project_id": pid,
                        "workspace_uris": uris,
                    }
    finally:
        conn.close()

    return workspaces


def build_alignment_mappings(
    source_workspaces: Dict[str, Dict[str, str]],
    target_workspaces: Dict[str, Dict[str, str]],
    source_user: str,
    target_user: str,
    source_platform: str,
    target_platform: str,
) -> Tuple[Dict[str, str], List[Tuple[str, str]]]:
    """Compute project_id and workspace_uri mappings between source and target machines.

    Args:
        source_workspaces: Workspaces from source machine.
        target_workspaces: Workspaces from target machine.
        source_user: Username on source machine.
        target_user: Username on target machine.
        source_platform: OS of source (e.g. 'Darwin', 'Windows').
        target_platform: OS of target (e.g. 'Darwin', 'Windows').

    Returns:
        Tuple of (project_id_map: {src_pid: dst_pid}, uri_replacements: [(src_str, dst_str)])
    """
    project_id_map: Dict[str, str] = {}
    uri_replacements: List[Tuple[str, str]] = []

    # 1. Align projects that match by slug
    for slug, s_info in source_workspaces.items():
        if slug in target_workspaces:
            t_info = target_workspaces[slug]
            s_pid = s_info["project_id"]
            t_pid = t_info["project_id"]

            if (
                s_pid
                and t_pid
                and s_pid != t_pid
                and s_pid != "outside-of-project"
                and t_pid != "outside-of-project"
            ):
                project_id_map[s_pid] = t_pid

            # Specific URI replacement for this project
            s_uri = s_info["workspace_uris"]
            t_uri = t_info["workspace_uris"]
            if s_uri and t_uri and s_uri != t_uri:
                # Strip outer quotes / brackets for substring replacement
                s_clean = s_uri.strip('[]"')
                t_clean = t_uri.strip('[]"')
                if s_clean and t_clean and s_clean != t_clean:
                    uri_replacements.append((s_clean, t_clean))

    # 2. General path prefix remapping based on platform and user
    if target_platform.lower() == "windows":
        # Remote is POSIX (macOS/Linux), target is Windows
        uri_replacements.extend(
            [
                (
                    f"file:///Users/{source_user}",
                    f"file:///c%3A/Users/{target_user}",
                ),
                (
                    f"file:///home/{source_user}",
                    f"file:///c%3A/Users/{target_user}",
                ),
                (f"/Users/{source_user}", f"c:/Users/{target_user}"),
                (f"/home/{source_user}", f"c:/Users/{target_user}"),
            ]
        )
    else:
        # Remote is Windows, target is POSIX (macOS/Linux)
        uri_replacements.extend(
            [
                (
                    f"file:///c%3A/Users/{source_user}",
                    f"file:///Users/{target_user}",
                ),
                (
                    f"file:///c:/Users/{source_user}",
                    f"file:///Users/{target_user}",
                ),
                (
                    f"file:///C%3A/Users/{source_user}",
                    f"file:///Users/{target_user}",
                ),
                (
                    f"file:///C:/Users/{source_user}",
                    f"file:///Users/{target_user}",
                ),
                (f"c:/Users/{source_user}", f"/Users/{target_user}"),
                (f"C:/Users/{source_user}", f"/Users/{target_user}"),
                (f"c:\\Users\\{source_user}", f"/Users/{target_user}"),
                (f"C:\\Users\\{source_user}", f"/Users/{target_user}"),
            ]
        )

    # Remove duplicate replacement rules
    unique_replacements: List[Tuple[str, str]] = []
    seen = set()
    for s, t in uri_replacements:
        if (s, t) not in seen and s != t:
            seen.add((s, t))
            unique_replacements.append((s, t))

    return project_id_map, unique_replacements
