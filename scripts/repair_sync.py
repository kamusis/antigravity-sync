"""
Repair script to restore pristine conversation metadata and sync agyhub protobuf cache
between Windows and MacBook installations.
"""

import os
import sys
import json
import base64
import sqlite3
import subprocess
from pathlib import Path

from antigravity_sync.proto_utils import (
    read_agyhub_proto_file,
    merge_agyhub_proto_file,
)
from antigravity_sync.storage import get_antigravity_dir


def repair_both():
    win_dir = get_antigravity_dir()
    win_db_path = win_dir / "conversation_summaries.db"
    win_pb_path = win_dir / "agyhub_summaries_proto.pb"

    # 1. Load Windows pristine sessions from bak
    win_bak_path = win_dir / "conversation_summaries.db.bak_20261010_231228"
    if not win_bak_path.exists():
        print(f"Error: {win_bak_path} not found!", file=sys.stderr)
        return 1

    conn_w = sqlite3.connect(str(win_bak_path))
    conn_w.row_factory = sqlite3.Row
    win_sessions = {
        r["conversation_id"]: dict(r)
        for r in conn_w.execute("SELECT * FROM conversation_summaries").fetchall()
    }
    conn_w.close()

    # 2. Load MacBook pristine sessions from remote bak
    print("[*] Fetching pristine MacBook sessions from macbook...")
    mac_script = """
import sqlite3, os, json, base64
from pathlib import Path
p_mac_bak = Path.home() / ".gemini/antigravity/conversation_summaries.db.bak_20261010_231113"
conn_m = sqlite3.connect(str(p_mac_bak))
conn_m.row_factory = sqlite3.Row
rows = conn_m.execute("SELECT * FROM conversation_summaries").fetchall()
data = []
for r in rows:
    d = dict(r)
    if d.get("raw_summary"):
        d["raw_summary"] = base64.b64encode(d["raw_summary"]).decode("ascii")
    data.append(d)
conn_m.close()
print(json.dumps(data))
"""
    p = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "macbook", "python3 -"],
        input=mac_script,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    if p.returncode != 0:
        print(f"Error querying macbook: {p.stderr}", file=sys.stderr)
        return 1

    mac_sessions_list = json.loads(p.stdout)
    mac_sessions = {r["conversation_id"]: r for r in mac_sessions_list}
    print(f"[+] Loaded {len(win_sessions)} Windows sessions and {len(mac_sessions)} MacBook sessions.")

    # 3. Update Windows database and protobuf cache
    print("[*] Repairing Windows Antigravity database & agyhub cache...")
    conn_live = sqlite3.connect(str(win_db_path))
    cursor = conn_live.cursor()

    win_new_proto_entries = {}

    for cid, m_row in mac_sessions.items():
        title = m_row.get("title") or ""
        preview = m_row.get("preview") or ""
        if not title and preview:
            title = preview
        if not preview and title:
            preview = title

        # Project ID mapping for Windows
        pid = m_row.get("project_id") or ""
        uris = m_row.get("workspace_uris") or ""

        if "mopheus" in uris:
            pid = "cc43b06d-e24a-4b42-8a2f-4112ee58ef8b"
            uris = '["file:///c%3A/Users/kamus/CascadeProjects/mopheus"]'
        elif "axon-cli" in uris:
            pid = "b509f58b-95e8-4967-bedb-70af95d707f1"
            uris = '["file:///c%3A/Users/kamus/CascadeProjects/axon-cli"]'
        elif "multica" in uris:
            pid = "1f13a824-b77e-4e08-aa9d-91f7bb4cadb4"
            uris = '["file:///c%3A/Users/kamus/CascadeProjects/multica"]'
        else:
            uris = uris.replace("/Users/kamus", "c:/Users/kamus")

        raw_summary_bytes = (
            base64.b64decode(m_row["raw_summary"]) if m_row.get("raw_summary") else b""
        )
        if raw_summary_bytes:
            win_new_proto_entries[cid] = raw_summary_bytes

        cursor.execute(
            """
            UPDATE conversation_summaries 
            SET title = ?, preview = ?, project_id = ?, workspace_uris = ?, raw_summary = ?
            WHERE conversation_id = ?
            """,
            (title, preview, pid, uris, raw_summary_bytes, cid),
        )

        cursor.execute(
            """
            UPDATE conversations_fts 
            SET title = ?, project = ?, workspace = ?
            WHERE conversation_id = ?
            """,
            (title, pid, uris, cid),
        )

    conn_live.commit()
    try:
        conn_live.execute("PRAGMA wal_checkpoint(PASSIVE);")
    except Exception:
        pass
    conn_live.close()

    # Merge into Windows agyhub_summaries_proto.pb
    if win_new_proto_entries:
        merged_count = merge_agyhub_proto_file(win_pb_path, win_new_proto_entries)
        print(f"[+] Updated Windows agyhub_summaries_proto.pb with {len(win_new_proto_entries)} entries.")

    print("[+] Windows database successfully repaired!")

    # 4. Update MacBook database and protobuf cache
    print("[*] Repairing MacBook Antigravity database & agyhub cache...")
    win_export_data = []
    for cid, w_row in win_sessions.items():
        title = w_row.get("title") or ""
        preview = w_row.get("preview") or ""
        if not title and preview:
            title = preview
        if not preview and title:
            preview = title

        pid = w_row.get("project_id") or ""
        uris = w_row.get("workspace_uris") or ""

        if "mopheus" in uris:
            pid = "05f52f38-58c8-4a26-9357-4df001980345"
            uris = '["file:///Users/kamus/CascadeProjects/mopheus"]'
        elif "axon-cli" in uris:
            pid = "a0c388a2-1566-4be2-968a-a48812ecd4d8"
            uris = '["file:///Users/kamus/CascadeProjects/axon-cli"]'
        elif "multica" in uris:
            pid = "67250c5d-e33c-4b03-b526-4428e15bf67d"
            uris = '["file:///Users/kamus/CascadeProjects/multica"]'
        else:
            uris = (
                uris.replace("file:///c%3A/Users/kamus", "file:///Users/kamus")
                .replace("file:///c:/Users/kamus", "file:///Users/kamus")
                .replace("c:/Users/kamus", "/Users/kamus")
            )

        raw_b64 = (
            base64.b64encode(w_row["raw_summary"]).decode("ascii")
            if w_row.get("raw_summary")
            else ""
        )

        win_export_data.append(
            {
                "conversation_id": cid,
                "title": title,
                "preview": preview,
                "project_id": pid,
                "workspace_uris": uris,
                "raw_summary": raw_b64,
            }
        )

    mac_repair_script = f"""
import sqlite3, os, json, base64
from pathlib import Path
import sys

sys.path.insert(0, str(Path.home() / "CascadeProjects/antigravity-sync"))
from antigravity_sync.proto_utils import merge_agyhub_proto_file

win_data = {json.dumps(win_export_data)}

p_mac_db = Path.home() / ".gemini/antigravity/conversation_summaries.db"
p_mac_pb = Path.home() / ".gemini/antigravity/agyhub_summaries_proto.pb"

conn = sqlite3.connect(str(p_mac_db))
cursor = conn.cursor()

mac_proto_entries = {{}}

for row in win_data:
    cid = row["conversation_id"]
    title = row["title"]
    preview = row["preview"]
    pid = row["project_id"]
    uris = row["workspace_uris"]
    raw_bytes = base64.b64decode(row["raw_summary"]) if row["raw_summary"] else b""
    if raw_bytes:
        mac_proto_entries[cid] = raw_bytes

    cursor.execute('''
        UPDATE conversation_summaries
        SET title = ?, preview = ?, project_id = ?, workspace_uris = ?, raw_summary = ?
        WHERE conversation_id = ?
    ''', (title, preview, pid, uris, raw_bytes, cid))

    cursor.execute('''
        UPDATE conversations_fts
        SET title = ?, project = ?, workspace = ?
        WHERE conversation_id = ?
    ''', (title, pid, uris, cid))

conn.commit()
try:
    conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
except Exception:
    pass
conn.close()

if mac_proto_entries:
    merge_agyhub_proto_file(p_mac_pb, mac_proto_entries)

print(f"MacBook updated successfully with {{len(win_data)}} sessions and {{len(mac_proto_entries)}} proto entries.")
"""
    p2 = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "macbook", "python3 -"],
        input=mac_repair_script,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    if p2.returncode != 0:
        print(f"Error repairing macbook: {p2.stderr}", file=sys.stderr)
        return 1

    print(f"[+] {p2.stdout.strip()}")
    print("\n[+] SUCCESS! Both machines are completely repaired and aligned!")
    return 0


if __name__ == "__main__":
    sys.exit(repair_both())
