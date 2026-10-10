"""Full verification for a repaired side (run on the machine itself)."""
import base64
import json
import os
import re
import sqlite3
import sys
import io
from pathlib import Path
from collections import Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from antigravity_sync.proto_utils import parse_agyhub_proto
from antigravity_sync.storage import get_antigravity_dir

side = sys.argv[1]
snap_path = Path(sys.argv[2])

UUID_RE = re.compile(rb"\x92\x01\x24([0-9a-fA-F-]{36})")
LOCAL_UUID = {"win": "cc43b06d-e24a-4b42-8a2f-4112ee58ef8b", "mac": "05f52f38-58c8-4a26-9357-4df001980345"}[side]
PEER_UUID = {"win": "05f52f38-58c8-4a26-9357-4df001980345", "mac": "cc43b06d-e24a-4b42-8a2f-4112ee58ef8b"}[side]

agy = get_antigravity_dir()
snap = json.loads(snap_path.read_text(encoding="utf-8"))
peer_ids = {r["conversation_id"] for r in snap["summaries"]}
peer_fts = {r["conversation_id"]: r for r in snap["fts"]}

conn = sqlite3.connect(str(agy / "conversation_summaries.db")); conn.row_factory = sqlite3.Row
fts = [dict(r) for r in conn.execute("SELECT conversation_id, archived, project, workspace, title FROM conversations_fts WHERE chunk_index=-1")]
sums = {r["conversation_id"]: dict(r) for r in conn.execute("SELECT conversation_id, title, project_id FROM conversation_summaries")}
conn.close()
fts_by_id = {r["conversation_id"]: r for r in fts}

print(f"=== FTS project distribution ({side}) ===")
c = Counter(r["project"] for r in fts)
for k, n in sorted(c.items(), key=lambda x: -x[1]):
    print(f"  {k!r:<26} {n}")

print(f"\n=== blob census: peer convs ({side}) ===")
conv_dir = agy / "conversations"
c2 = Counter()
peer_uuid_left = []
for cid in sorted(peer_ids):
    db = conv_dir / f"{cid}.db"
    if not db.exists():
        c2["<no .db>"] += 1
        continue
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute("SELECT data FROM trajectory_metadata_blob WHERE id='main'").fetchone()
    except sqlite3.DatabaseError:
        row = None
    conn.close()
    if not row or not row[0]:
        c2["<no blob>"] += 1
        continue
    blob = row[0] if isinstance(row[0], bytes) else row[0].encode("utf-8", "surrogateescape")
    hits = [m.group(1).decode()[:8] for m in UUID_RE.finditer(blob)]
    c2["+".join(sorted(set(hits))) if hits else "<no uuid>"] += 1
    if PEER_UUID.encode() in blob:
        peer_uuid_left.append(cid)
print(" ", dict(c2))
print(f"  peer uuid remaining in blobs: {len(peer_uuid_left)}")

print(f"\n=== title check: peer convs vs peer bak ===")
mismatch = 0
for cid in sorted(peer_ids):
    pf = peer_fts.get(cid)
    lf = fts_by_id.get(cid)
    if not pf or not lf:
        continue
    if (pf.get("title") or "") != (lf.get("title") or ""):
        mismatch += 1
print(f"  title mismatches vs peer bak: {mismatch}")
n_empty = sum(1 for cid in peer_ids if not (sums.get(cid, {}).get("title") or ""))
print(f"  peer convs with empty summaries title: {n_empty}")

print(f"\n=== pb entries for peer convs ({side}) ===")
pb = parse_agyhub_proto((agy / "agyhub_summaries_proto.pb").read_bytes())
present = sum(1 for cid in peer_ids if cid in pb)
peer_uuid_in_pb = sum(1 for cid in peer_ids if cid in pb and PEER_UUID.encode() in pb[cid])
print(f"  present: {present}/{len(peer_ids)}, peer-uuid remaining inside pb raws: {peer_uuid_in_pb}")

print(f"\n=== local native convs untouched check ===")
local_native = [r for r in fts if r["conversation_id"] not in peer_ids]
c3 = Counter(r["project"] for r in local_native)
print(f"  local native FTS rows: {len(local_native)} by project: {dict(c3)}")
