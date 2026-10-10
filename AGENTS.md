# AGENTS.md

Operational knowledge for working on this repository and the Antigravity runtime data.

## Verification

```bash
python -m unittest tests.test_sync        # unit tests (no pytest needed)
python scripts/repair_metadata.py repair --side win --peer-snapshot <json>   # dry-run by default, --apply to write
python scripts/verify_repair.py win <peer_snapshot.json>
python -m antigravity_sync sync --remote macbook --dry-run
```

## Critical runtime facts (learned the hard way)

1. **Never modify `~/.gemini/antigravity/**` while `language_server` is running.**
   It holds conversation state in memory and flushes on exit, overwriting any
   external change. Kill it first (`Stop-Process -Name language_server -Force`,
   `pkill -x language_server` on macOS), verify, then patch.

2. **Storage map of a conversation's display metadata:**
   - `conversation_summaries.db` / `conversations_fts` (chunk_index=-1): the sidebar
     list query source. `project`/`workspace` columns store directory SLUGS
     (`'mopheus'`), not project UUIDs.
   - `conversations/<id>.db` -> `trajectory_metadata_blob` (id='main', column `data`):
     protobuf embedding the project UUID (field 18, 36 bytes) and workspace URIs
     (length-prefixed). The LanguageServer reads it on startup/rescan and resets
     grouping when it contains a foreign machine's UUID. Patch with
     `transform_proto_bytes` (recomputes length prefixes; paths change length).
   - `agyhub_summaries_proto.pb` + `conversation_summaries.raw_summary`: protobuf
     cache. Field 1 = title, field 15.1 = persisted title state.
   - `annotations/<id>.pbtxt`: **authoritative display title store** (text proto:
     `title:"..."`, `archived:true`, `last_user_view_time:{...}`). If the title is
     missing here, the LanguageServer re-derives it from the first user message
     every time the conversation is opened and stomps all layers above. This is
     why bundles must include `annotations/`.

3. **Path replacement rules must be idempotent.** Bare-path rules
   (`/Users/kamus -> c:/Users/kamus`) contain their own output; use the
   lookbehind regex variants in `workspace_mapper.build_alignment_mappings`
   and apply replacements in one pass (see `build_one_pass_replacer`).

4. **LanguageServer logs**: `C:\Users\kamus\AppData\Roaming\Antigravity\logs\language_server.log`
   (macOS: `~/Library/Application Support/Antigravity/logs/`). The `[Summaries]
   reconcile` line at startup and `cascade_run_state` lines on conversation open
   are the fastest way to observe what the server reads/writes.

5. `conversation_summaries.title` being empty is NORMAL for conversations whose
   title equals their first user message (the server clears it); the UI list only
   reads the FTS title. Do not "fix" it.
