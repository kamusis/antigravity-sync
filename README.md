# antigravity-sync

A lightweight, zero-dependency CLI tool to export, migrate, and incrementally merge conversation histories across machines for the **Antigravity CLI / runtime** (`~/.gemini/antigravity`).

## Key Features

- **Zero External Dependencies**: Built 100% on the Python Standard Library (`sqlite3`, `zipfile`, `argparse`).
- **Non-Destructive by Default**: Uses SQLite `UPSERT` and `ATTACH` for atomic incremental merging.
- **Cross-Platform**: Supports Windows, macOS, and Linux with automated workspace path remapping (`--remap-workspace`).
- **Safe SQLite WAL Handling**: Automatic checkpoints and SQLite backup API snapshots prevent dirty reads or locks.
- **Protects Machine State**: Never modifies or overwrites target machine-specific identifiers (`installation_id`, `installation_uuid`).

---

## Installation

```bash
cd antigravity-sync
pip install -e .
```

Or run directly without installation:

```bash
python -m antigravity_sync --help
```

---

## Typical Workflows

### Daily: keep two machines in sync

```bash
# 1. Close Antigravity on BOTH machines (the LanguageServer flushes state on
#    exit and would overwrite merged data otherwise)
# 2. From either machine:
python -m antigravity_sync sync --remote macbook
# 3. Reopen Antigravity
```

The sync is bidirectional and incremental: it compares conversation sets and
`last_modified_time`, pushes/pulls only what differs, and automatically aligns
workspace paths, project UUIDs, FTS slugs, trajectory blobs, titles, and
per-conversation annotations across platforms. Run `--dry-run` first to preview.

### Migrate to a new machine

```bash
antigravity-sync export -o conversations.agypkg      # on the source machine
scp conversations.agypkg newmachine:/tmp/
antigravity-sync import /tmp/conversations.agypkg \
    --remap-workspace "C:/Users/alice=D:/Users/bob"  # on the target machine
```

### Something looks wrong (lost grouping / titles)

```bash
# Check the local state (counts, blob census, titles, pb cache)
python scripts/verify_repair.py win <peer_snapshot.json>

# Rebuild metadata from pristine backups + a peer snapshot (dry-run by default)
python scripts/repair_metadata.py snapshot --side win -o peer.json   # on the peer
python scripts/repair_metadata.py repair --side win --peer-snapshot peer.json --apply
```

See `docs/conversation-title-debug-and-fix.md` for how the metadata layers work
and why the repair tool patches each of them.

---

## Usage

### 1. Export Conversations

Export conversations into a compressed `.agypkg` bundle archive.

```bash
# Export all conversations
antigravity-sync export

# Export with custom output bundle path
antigravity-sync export -o ~/my_conversations.agypkg

# Export only sessions modified after a specific date
antigravity-sync export --since 2026-09-01

# Export only sessions from a specific workspace
antigravity-sync export --workspace "swissql"

# Exclude temporary scratch files to save space
antigravity-sync export --no-scratch
```

### 2. Inspect a Bundle or Local Runtime

```bash
# Inspect an exported bundle without extracting it
antigravity-sync inspect ~/my_conversations.agypkg

# Inspect the local Antigravity installation
antigravity-sync inspect --local
```

### 3. List Conversations

```bash
# List local conversations
antigravity-sync list

# List conversations inside a bundle
antigravity-sync list -f ~/my_conversations.agypkg
```

### 4. Import & Merge into Target Machine

```bash
# Simulate the import first (dry-run)
antigravity-sync import ~/my_conversations.agypkg --dry-run

# Incrementally merge into target machine with workspace path remapping
antigravity-sync import ~/my_conversations.agypkg \
    --remap-workspace "C:/Users/alice=D:/Users/bob"

# Remap project UUIDs across machines (e.g. same project recreated on both)
antigravity-sync import ~/my_conversations.agypkg \
    --remap-project-id "05f52f38-58c8-4a26-9357-4df001980345=cc43b06d-e24a-4b42-8a2f-4112ee58ef8b"

# Full overwrite mode (automatically creates a safety backup of target database)
antigravity-sync import ~/my_conversations.agypkg --mode overwrite
```

The importer keeps imported conversations visible and grouped on the target machine:

- **Trajectory blob patching**: each deployed `conversations/<id>.db` has its embedded
  `trajectory_metadata_blob` rewritten with the target machine's project UUIDs and
  workspace paths (protobuf length prefixes recomputed), so the local LanguageServer
  recognizes them instead of resetting the grouping on rescan.
- **FTS slug alignment**: `conversations_fts.project` / `.workspace` store directory
  slugs (not UUIDs); they are aligned with the target machine's own grouping for the
  mapped project, and cleared for `outside-of-project` conversations.
- **Title backfill**: missing titles are filled from the source FTS title first and
  only fall back to `preview` (raw prompt) last.
- **Historical renames**: source slugs whose project maps to a target project are
  rewritten to the current target slug (e.g. `moclaw` -> `mopheus`).

### 5. Bidirectional Sync via SSH

```bash
# Dry-run: see what would be pushed/pulled
antigravity-sync sync --remote macbook --dry-run

# Full bidirectional sync (auto-detects platform path & project alignment)
antigravity-sync sync --remote macbook

# One-directional variants
antigravity-sync sync --remote macbook --push-only
antigravity-sync sync --remote macbook --pull-only
```

Both machines must have this repository at `~/CascadeProjects/antigravity-sync`
(or adjust the paths in `cli.py`). Close Antigravity on both machines before syncing.

### 6. Repair Metadata from Pristine Backups

If a previous import left conversations ungrouped or untitled (e.g. the LanguageServer
reset them), rebuild both machines' metadata from the pristine backups:

```bash
# 1. On machine A: dump its pristine ground truth to a snapshot JSON
python scripts/repair_metadata.py snapshot --side win -o win_snapshot.json

# 2. On machine B: dry-run, then apply the repair using A's snapshot
python scripts/repair_metadata.py repair --side mac --peer-snapshot win_snapshot.json           # dry-run
python scripts/repair_metadata.py repair --side mac --peer-snapshot win_snapshot.json --apply

# 3. Verify counts, blob census, titles, and protobuf cache on either machine
python scripts/verify_repair.py win <peer_snapshot.json>
```

The repair patches `conversation_summaries`, the FTS index, per-conversation
trajectory blobs, and `agyhub_summaries_proto.pb`, saving blob originals to a
timestamped JSON backup before writing.

---

## Internal Bundle Specification (`.agypkg`)

The `.agypkg` bundle is a standard Deflate ZIP archive containing:

```text
my_conversations.agypkg
├── manifest.json                  # Metadata, versions, checksums, conversation inventory
├── conversation_summaries.db      # Sliced SQLite metadata database with FTS5 search index
├── agyhub_summaries.pb            # Filtered protobuf cache of raw summaries (if present)
├── conversations/                 # Individual conversation SQLite databases (<uuid>.db)
└── brain/                         # Complete transcripts, plans, artifacts, and logs
    └── <uuid>/
        ├── .system_generated/logs/transcript.jsonl
        └── *.md
```

## License

MIT
