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

# Full overwrite mode (automatically creates a safety backup of target database)
antigravity-sync import ~/my_conversations.agypkg --mode overwrite
```

---

## Internal Bundle Specification (`.agypkg`)

The `.agypkg` bundle is a standard Deflate ZIP archive containing:

```text
my_conversations.agypkg
├── manifest.json                  # Metadata, versions, checksums, conversation inventory
├── conversation_summaries.db      # Sliced SQLite metadata database with FTS5 search index
├── conversations/                 # Individual conversation SQLite databases (<uuid>.db)
└── brain/                         # Complete transcripts, plans, artifacts, and logs
    └── <uuid>/
        ├── .system_generated/logs/transcript.jsonl
        └── *.md
```

## License

MIT
