"""
One-time metadata repair for cross-machine Antigravity conversation sync.

Repairs the LOCAL machine's Antigravity data using:
  - the local pristine backup (conversation_summaries.db.bak_*) as ground truth
    for locally-native conversations, and
  - a peer snapshot JSON (generated on the other machine via the `snapshot`
    subcommand) as ground truth for imported conversations.

Repairs performed for every imported (peer-native) conversation:
  1. conversation_summaries: project_id, workspace_uris, title, preview,
     raw_summary (patched to local project UUIDs / paths).
  2. conversations_fts (chunk_index=-1): project/workspace slugs, title,
     archived flag.
  3. conversations/<id>.db: trajectory_metadata_blob protobuf rewritten with
     local project UUIDs and local paths (length prefixes recomputed).
  4. agyhub_summaries_proto.pb: entries overwritten with patched raw summaries.

Usage (on Windows):
  python scripts/repair_metadata.py snapshot --remote macbook -o mac_snapshot.json
  python scripts/repair_metadata.py repair --side win --peer-snapshot mac_snapshot.json --apply

Usage (on macOS, after scp'ing this repo and win_snapshot.json):
  python3 scripts/repair_metadata.py repair --side mac --peer-snapshot win_snapshot.json --apply

Omit --apply for a dry-run report.
"""

import base64
import json
import os
import re
import sqlite3
import subprocess
import sys
import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from antigravity_sync.proto_utils import (  # noqa: E402
    build_one_pass_replacer,
    merge_agyhub_proto_file,
    set_leading_string_field,
    transform_proto_bytes,
)
from antigravity_sync.storage import get_antigravity_dir, backup_file  # noqa: E402
from antigravity_sync.merger import patch_conversation_blob  # noqa: E402

# Pristine backups created before any sync was attempted (see handoff doc).
DEFAULT_LOCAL_BAK = {
    "win": "conversation_summaries.db.bak_20261010_231228",
    "mac": "conversation_summaries.db.bak_20261010_231113",
}

# Project UUID pairs established from both machines' pristine backups.
PROJECT_ID_MAP_WIN = {
    "05f52f38-58c8-4a26-9357-4df001980345": "cc43b06d-e24a-4b42-8a2f-4112ee58ef8b",  # mopheus
    "67250c5d-e33c-4b03-b526-4428e15bf67d": "1f13a824-b77e-4e08-aa9d-91f7bb4cadb4",  # multica
    "a0c388a2-1566-4be2-968a-a48812ecd4d8": "b509f58b-95e8-4967-bedb-70af95d707f1",  # axon-cli
}
PROJECT_ID_MAP_MAC = {v: k for k, v in PROJECT_ID_MAP_WIN.items()}

# Historical directory renames: old directory slug -> current slug.
SLUG_RENAMES = [("CascadeProjects/moclaw", "CascadeProjects/mopheus")]

# Generic path prefix rules per target side (applied in one pass, longest first).
# Bare-path rules carry a lookbehind so they never re-match inside an already
# remapped URI ('c%3A/Users/kamus' or 'c:/Users/kamus'), keeping the set
# idempotent across repeated repairs; the heal rules undo that exact
# duplication if it was written by an earlier non-idempotent run.
BARE_WIN_USERS = re.compile(rb"(?<![%:A-Za-z0-9])/Users/kamus")
BARE_WIN_HOME = re.compile(rb"(?<![%:A-Za-z0-9])/home/kamus")
PATH_RULES_WIN = [
    ("c%3Ac:/", "c%3A/"),
    ("c:c:/", "c:/"),
    ("file:///Users/kamus", "file:///c%3A/Users/kamus"),
    ("file:///home/kamus", "file:///c%3A/Users/kamus"),
    (BARE_WIN_USERS, b"c:/Users/kamus"),
    (BARE_WIN_HOME, b"c:/Users/kamus"),
]
PATH_RULES_MAC = [
    ("file:///c%3A/Users/kamus", "file:///Users/kamus"),
    ("file:///c:/Users/kamus", "file:///Users/kamus"),
    ("file:///C%3A/Users/kamus", "file:///Users/kamus"),
    ("file:///C:/Users/kamus", "file:///Users/kamus"),
    ("c:/Users/kamus", "/Users/kamus"),
    ("C:/Users/kamus", "/Users/kamus"),
]

PEER_OF = {"win": "mac", "mac": "win"}


def is_local_side(side: str) -> bool:
    """Check whether this machine matches the requested side."""
    if side == "win":
        return os.name == "nt"
    return sys.platform == "darwin"


def load_db_rows(db_path: Path) -> Tuple[List[dict], List[dict]]:
    """Load summaries and FTS metadata rows from a summaries database.

    Args:
        db_path: Path to conversation_summaries.db (live or backup).

    Returns:
        Tuple of (summaries rows, FTS chunk_index=-1 rows) as dicts.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        summaries = [dict(r) for r in conn.execute("SELECT * FROM conversation_summaries;")]
        fts = [
            dict(r)
            for r in conn.execute(
                "SELECT conversation_id, archived, project, workspace, title "
                "FROM conversations_fts WHERE chunk_index = -1;"
            )
        ]
    finally:
        conn.close()
    for r in summaries:
        if isinstance(r.get("raw_summary"), bytes):
            r["raw_summary_b64"] = base64.b64encode(r["raw_summary"]).decode("ascii")
        elif isinstance(r.get("raw_summary"), str):
            r["raw_summary_b64"] = base64.b64encode(
                r["raw_summary"].encode("utf-8", "surrogateescape")
            ).decode("ascii")
        else:
            r["raw_summary_b64"] = ""
        r.pop("raw_summary", None)
    return summaries, fts


def cmd_snapshot(args) -> int:
    """Fetch the LOCAL machine's pristine ground truth as a peer snapshot JSON."""
    agy_dir = get_antigravity_dir()
    bak_path = agy_dir / DEFAULT_LOCAL_BAK[args.side]
    if not bak_path.exists():
        print(f"Error: local pristine backup not found: {bak_path}", file=sys.stderr)
        return 1
    summaries, fts = load_db_rows(bak_path)

    pb_entries: Dict[str, str] = {}
    pb_path = agy_dir / "agyhub_summaries_proto.pb"
    if pb_path.exists():
        from antigravity_sync.proto_utils import read_agyhub_proto_file

        for cid, raw in read_agyhub_proto_file(pb_path).items():
            pb_entries[cid] = base64.b64encode(raw).decode("ascii")

    snapshot = {
        "side": args.side,
        "source_bak": bak_path.name,
        "summaries": summaries,
        "fts": fts,
        "pb_entries": pb_entries,
    }
    out_path = Path(args.output).expanduser()
    out_path.write_text(json.dumps(snapshot), encoding="utf-8")
    print(f"[+] Snapshot of side '{args.side}' written to {out_path} "
          f"({len(summaries)} summaries, {len(fts)} fts rows, {len(pb_entries)} pb entries)")
    return 0


def derive_pid_slugs(bak_summaries: List[dict], bak_fts: List[dict]) -> Dict[str, Tuple[str, str]]:
    """Derive project_id -> (fts project, fts workspace) from pristine native rows.

    Args:
        bak_summaries: Pristine summaries rows.
        bak_fts: Pristine FTS metadata rows.

    Returns:
        Dict mapping project UUID to its native FTS slug pair.
    """
    fts_by_id = {r["conversation_id"]: r for r in bak_fts}
    pid_slugs: Dict[str, Tuple[str, str]] = {}
    for r in bak_summaries:
        pid = r.get("project_id") or ""
        f = fts_by_id.get(r["conversation_id"])
        if not f:
            continue
        proj = f.get("project") or ""
        if pid and pid != "outside-of-project" and proj and pid not in pid_slugs:
            pid_slugs[pid] = (proj, f.get("workspace") or "")
    return pid_slugs


def repair_side(side: str, peer_snapshot_path: Path, apply: bool) -> int:
    """Repair the local machine's Antigravity metadata for peer-native conversations.

    Args:
        side: 'win' or 'mac' - the side being repaired (must match this machine).
        peer_snapshot_path: Peer snapshot JSON with the other machine's ground truth.
        apply: Whether to write changes (False = dry run).

    Returns:
        Process exit code.
    """
    if not is_local_side(side):
        print(f"Error: side '{side}' does not match this machine.", file=sys.stderr)
        return 1

    agy_dir = get_antigravity_dir()
    live_db = agy_dir / "conversation_summaries.db"
    live_pb = agy_dir / "agyhub_summaries_proto.pb"
    conv_dir = agy_dir / "conversations"
    bak_path = agy_dir / DEFAULT_LOCAL_BAK[side]
    if not live_db.exists() or not bak_path.exists():
        print("Error: live db or pristine backup missing.", file=sys.stderr)
        return 1

    peer = json.loads(peer_snapshot_path.read_text(encoding="utf-8"))
    peer_side = peer.get("side")
    if peer_side != PEER_OF[side]:
        print(f"Error: snapshot side '{peer_side}' does not match expected '{PEER_OF[side]}'.", file=sys.stderr)
        return 1

    project_id_map = PROJECT_ID_MAP_WIN if side == "win" else PROJECT_ID_MAP_MAC
    path_rules = PATH_RULES_WIN if side == "win" else PATH_RULES_MAC
    replacements = [
        (o.encode("utf-8"), n.encode("utf-8")) if isinstance(o, str) else (o, n)
        for o, n in path_rules
    ]
    replacements += [(o.encode("utf-8"), n.encode("utf-8")) for o, n in SLUG_RENAMES]
    replacements += [
        (s.encode("utf-8"), t.encode("utf-8"))
        for s, t in project_id_map.items()
    ]
    blob_replacer = build_one_pass_replacer(replacements)

    # Native slug pairs for mapped project ids, from the local pristine backup.
    local_bak_summaries, local_bak_fts = load_db_rows(bak_path)
    pid_slugs = derive_pid_slugs(local_bak_summaries, local_bak_fts)

    peer_summaries = {r["conversation_id"]: r for r in peer["summaries"]}
    peer_fts = {r["conversation_id"]: r for r in peer["fts"]}
    peer_pb = peer.get("pb_entries", {})

    conn = sqlite3.connect(str(live_db))
    conn.row_factory = sqlite3.Row
    live_rows = {r["conversation_id"]: dict(r) for r in conn.execute("SELECT * FROM conversation_summaries;")}
    live_fts = {
        r["conversation_id"]: dict(r)
        for r in conn.execute("SELECT conversation_id, archived, project, workspace, title FROM conversations_fts WHERE chunk_index=-1;")
    }
    conn.close()

    plan: List[str] = []
    stats = {"summaries_updated": 0, "summaries_inserted": 0, "fts_updated": 0,
             "blobs_patched": 0, "pb_entries": 0}
    titles: Dict[str, str] = {}

    for cid, peer_row in sorted(peer_summaries.items()):
        peer_f = peer_fts.get(cid)
        live_row = live_rows.get(cid)

        # Title: peer FTS title first, then peer summaries title, then preview.
        title = (peer_f and peer_f.get("title")) or peer_row.get("title") or peer_row.get("preview") or ""
        preview = (live_row.get("preview") if live_row else None) or title

        # Project id: mapped to the local UUID when the project exists locally.
        src_pid = peer_row.get("project_id") or ""
        mapped_pid = project_id_map.get(src_pid, src_pid)
        if mapped_pid in ("", "outside-of-project"):
            mapped_pid = "outside-of-project"

        # Workspace URIs: one-pass path + rename replacement.
        uris = peer_row.get("workspace_uris") or ""
        new_uris = blob_replacer(uris.encode("utf-8")).decode("utf-8") if uris else uris

        # Raw summary: peer live pb entry first, then peer bak column; patched.
        # Field 1 of the raw summary carries the title the LanguageServer
        # re-applies when the conversation is opened - keep it in sync with
        # the repaired title so opening a session does not revert it.
        raw_b64 = peer_pb.get(cid) or peer_row.get("raw_summary_b64") or ""
        raw_bytes = base64.b64decode(raw_b64) if raw_b64 else b""
        new_raw = transform_proto_bytes(raw_bytes, replacements) if raw_bytes else b""
        if new_raw and title:
            new_raw = set_leading_string_field(new_raw, title)
        titles[cid] = title

        if live_row is None:
            plan.append(f"INSERT summaries {cid[:8]} pid={mapped_pid[:8]} title={title[:40]!r}")
            stats["summaries_inserted"] += 1
            if apply:
                full = dict(peer_row)
                full.update({
                    "title": title, "preview": preview, "project_id": mapped_pid,
                    "workspace_uris": new_uris, "raw_summary": new_raw,
                    "app_data_dir": "antigravity",
                })
                full.pop("raw_summary_b64", None)
                cols = ", ".join(full.keys())
                marks = ", ".join("?" for _ in full)
                conn = sqlite3.connect(str(live_db))
                conn.execute(f"INSERT OR REPLACE INTO conversation_summaries ({cols}) VALUES ({marks});",
                             tuple(full.values()))
                conn.commit()
                conn.close()
            continue

        changes = []
        if live_row.get("title") != title:
            changes.append(("title", title))
        if live_row.get("preview") != preview:
            changes.append(("preview", preview))
        if live_row.get("project_id") != mapped_pid:
            changes.append(("project_id", mapped_pid))
        if live_row.get("workspace_uris") != new_uris:
            changes.append(("workspace_uris", new_uris))
        if new_raw and live_row.get("raw_summary") != new_raw:
            changes.append(("raw_summary", new_raw))
        if changes:
            stats["summaries_updated"] += 1
            plan.append(f"UPDATE summaries {cid[:8]}: " + ", ".join(c[0] for c in changes))
            if apply:
                conn = sqlite3.connect(str(live_db))
                set_sql = ", ".join(f"{c[0]} = ?" for c in changes)
                conn.execute(
                    f"UPDATE conversation_summaries SET {set_sql} WHERE conversation_id = ?;",
                    tuple([c[1] for c in changes] + [cid]),
                )
                conn.commit()
                conn.close()

        # FTS metadata row (only when the conversation has one, matching the
        # peer's native state - some conversations legitimately lack FTS rows).
        if peer_f and cid in live_fts:
            if mapped_pid in pid_slugs:
                proj, ws = pid_slugs[mapped_pid]
            elif mapped_pid in ("outside-of-project", ""):
                proj, ws = "", ""
            else:
                proj, ws = "", ""
            lf = live_fts[cid]
            fts_changes = []
            if lf.get("project") != proj:
                fts_changes.append(("project", proj))
            if lf.get("workspace") != ws:
                fts_changes.append(("workspace", ws))
            if (lf.get("title") or "") != title:
                fts_changes.append(("title", title))
            if lf.get("archived") != peer_f.get("archived"):
                fts_changes.append(("archived", peer_f.get("archived")))
            if fts_changes:
                stats["fts_updated"] += 1
                plan.append(f"UPDATE fts {cid[:8]}: " + ", ".join(c[0] for c in fts_changes))
                if apply:
                    conn = sqlite3.connect(str(live_db))
                    set_sql = ", ".join(f"{c[0]} = ?" for c in fts_changes)
                    conn.execute(
                        f"UPDATE conversations_fts SET {set_sql} WHERE conversation_id = ? AND chunk_index = -1;",
                        tuple([c[1] for c in fts_changes] + [cid]),
                    )
                    conn.commit()
                    conn.close()

    # Patch trajectory_metadata_blob in conversation databases.
    blob_backup: Dict[str, str] = {}
    if apply:
        blob_backup_path = agy_dir / f"blob_backup_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    for cid in sorted(peer_summaries):
        db_file = conv_dir / f"{cid}.db"
        if not db_file.exists():
            continue
        conn = sqlite3.connect(str(db_file))
        try:
            row = conn.execute("SELECT data FROM trajectory_metadata_blob WHERE id='main';").fetchone()
        except sqlite3.DatabaseError:
            row = None
        finally:
            conn.close()
        if not row or not row[0]:
            continue
        blob = row[0] if isinstance(row[0], bytes) else row[0].encode("utf-8", "surrogateescape")
        new_blob = transform_proto_bytes(blob, replacements)
        if new_blob != blob:
            stats["blobs_patched"] += 1
            blob_backup[f"{cid}.db"] = base64.b64encode(blob).decode("ascii")
            plan.append(f"PATCH blob {cid[:8]} ({len(blob)} -> {len(new_blob)} bytes)")
            if apply:
                changed = patch_conversation_blob(
                    db_file, project_id_map=project_id_map,
                    remap_workspaces=path_rules + SLUG_RENAMES,
                )
                if not changed:
                    print(f"Warning: blob patch reported no change for {cid[:8]}", file=sys.stderr)

    if apply and blob_backup:
        blob_backup_path.write_text(json.dumps(blob_backup), encoding="utf-8")
        print(f"[+] Original blobs saved to {blob_backup_path}")

    # Patch agyhub_summaries_proto.pb entries for peer conversations.
    pb_updates: Dict[str, bytes] = {}
    for cid in sorted(peer_summaries):
        raw_b64 = peer_pb.get(cid) or peer_summaries[cid].get("raw_summary_b64") or ""
        if not raw_b64:
            continue
        raw = base64.b64decode(raw_b64)
        new_raw = transform_proto_bytes(raw, replacements)
        title = titles.get(cid) or ""
        if new_raw and title:
            new_raw = set_leading_string_field(new_raw, title)
        pb_updates[cid] = new_raw
    if pb_updates:
        stats["pb_entries"] = len(pb_updates)
        plan.append(f"PATCH pb entries: {len(pb_updates)}")
        if apply:
            if live_pb.exists():
                backup_file(live_pb)
            merge_agyhub_proto_file(live_pb, pb_updates)

    print(f"\n=== Repair plan for side '{side}' ({'APPLY' if apply else 'DRY-RUN'}) ===")
    print(f"summaries: {stats['summaries_updated']} updated, {stats['summaries_inserted']} inserted")
    print(f"fts rows:  {stats['fts_updated']} updated")
    print(f"blobs:     {stats['blobs_patched']} patched")
    print(f"pb:        {stats['pb_entries']} entries")
    for line in plan[:40]:
        print(f"  {line}")
    if len(plan) > 40:
        print(f"  ... and {len(plan) - 40} more")

    if apply:
        verify_side(side, agy_dir)
    return 0


def verify_side(side: str, agy_dir: Path) -> None:
    """Print post-repair verification counts for the local machine.

    Args:
        side: 'win' or 'mac'.
        agy_dir: Antigravity data directory.
    """
    conn = sqlite3.connect(str(agy_dir / "conversation_summaries.db"))
    try:
        total = conn.execute("SELECT count(*) FROM conversations_fts WHERE chunk_index=-1;").fetchone()[0]
        unarch = conn.execute(
            "SELECT count(*) FROM conversations_fts WHERE chunk_index=-1 AND project='mopheus' AND archived=0;"
        ).fetchone()[0]
        arch = conn.execute(
            "SELECT count(*) FROM conversations_fts WHERE chunk_index=-1 AND project='mopheus' AND archived=1;"
        ).fetchone()[0]
        empty = conn.execute(
            "SELECT count(*) FROM conversations_fts WHERE chunk_index=-1 AND project='';"
        ).fetchone()[0]
        uuid_projected = conn.execute(
            "SELECT count(*) FROM conversations_fts WHERE chunk_index=-1 AND project LIKE '%-%-%-%';"
        ).fetchone()[0]
        print(f"\n=== Verification ({side}) ===")
        print(f"fts total: {total} | mopheus unarchived: {unarch} | mopheus archived: {arch}")
        print(f"empty project rows: {empty} | uuid-polluted project rows: {uuid_projected}")
    finally:
        conn.close()


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entrypoint."""
    import argparse

    parser = argparse.ArgumentParser(prog="repair_metadata", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_snap = sub.add_parser("snapshot", help="Dump this machine's pristine ground truth to JSON")
    p_snap.add_argument("--side", choices=["win", "mac"], required=True)
    p_snap.add_argument("-o", "--output", required=True)

    p_rep = sub.add_parser("repair", help="Repair local metadata using a peer snapshot")
    p_rep.add_argument("--side", choices=["win", "mac"], required=True)
    p_rep.add_argument("--peer-snapshot", required=True)
    p_rep.add_argument("--apply", action="store_true", help="Write changes (default: dry run)")

    args = parser.parse_args(argv)
    if args.command == "snapshot":
        return cmd_snapshot(args)
    return repair_side(args.side, Path(args.peer_snapshot).expanduser(), args.apply)


if __name__ == "__main__":
    sys.exit(main())
