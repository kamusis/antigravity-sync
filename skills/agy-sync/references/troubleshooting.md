# Antigravity Sync Troubleshooting Guide

Common issues, root causes, and remediation steps when syncing Antigravity sessions across machines.

---

## 1. Antigravity Process Running (`Warning: Antigravity process appears to be currently running`)

### Symptom
Tool outputs:
```text
Warning: Antigravity is running. Modifying active databases is risky.
Please close Antigravity before importing, or use --force.
```

### Cause
The Antigravity LanguageServer daemon maintains open SQLite file handles with WAL (`write-ahead logging`) locks. Writing while it runs can lead to `sqlite3.OperationalError: database is locked`, or worse, the daemon might overwrite recently merged records from its in-memory cache when exiting later.

### Remediation
1. Notify the user to quit Antigravity (or close the IDE window).
2. Check if processes have exited:
   ```bash
   python3 -m antigravity_sync inspect --local
   ```
3. Once `Status: STOPPED`, proceed with the sync or import operation.
4. Only use `--force` if the user explicitly acknowledges the risk or is running a strictly read-only command.

---

## 2. Remote Machine Setup Error (`Failed to query remote conversations` / `command not found`)

### Symptom
Remote SSH command fails:
```text
python3: No module named antigravity_sync
# or
Failed to query remote conversations: ...
```

### Cause
The remote machine does not have the `antigravity-sync` package installed or available in its Python environment / PATH.

### Remediation
1. Verify SSH key and connection:
   ```bash
   ssh <remote_host> "python3 --version"
   ```
2. Verify package or repository on remote:
   ```bash
   ssh <remote_host> "python3 -m antigravity_sync --version"
   ```
3. If missing, clone or copy the repository to the remote machine, or install it:
   ```bash
   scp -r /path/to/antigravity-sync <remote_host>:~/projects/
   ```

---

## 3. Imported Conversations Lost Grouping / Show as "Untitled"

### Symptom
Conversations are present in SQLite, but the Antigravity UI displays them under `outside of project` or with fallback titles like `Untitled` or raw prompts instead of original project names.

### Cause
Antigravity's LanguageServer parses `trajectory_metadata_blob` protobuf bytes stored inside `conversations/<uuid>.db`. If the source machine had a different project UUID or directory path (e.g. `C:/Users/alice/projects/app` vs `/home/bob/projects/app`), the LanguageServer fails to map the blob to any registered local project and falls back to `outside-of-project`.

### Remediation
1. Use `--remap-workspace` and `--remap-project-id` during import:
   ```bash
   python3 -m antigravity_sync import bundle.agypkg \
       --remap-workspace "OLD_PATH=NEW_PATH" \
       --remap-project-id "OLD_UUID=NEW_UUID"
   ```
2. If already imported without remapping, run the metadata repair script:
   ```bash
   # 1. On machine A (source of truth):
   python3 scripts/repair_metadata.py snapshot --side win -o win_snapshot.json
   # 2. On machine B:
   python3 scripts/repair_metadata.py repair --side mac --peer-snapshot win_snapshot.json --apply
   ```

---

## 4. SQLite WAL Locking or "database disk image is malformed"

### Symptom
`sqlite3.DatabaseError: file is not a database` or `database is locked`.

### Cause
An abnormal crash or abrupt termination while Antigravity was running left `.db-wal` or `.db-shm` files inconsistent.

### Remediation
1. Ensure no Antigravity process is running.
2. Check for WAL files:
   ```bash
   ls -la ~/.gemini/antigravity/*.db*
   ```
3. Run SQLite checkpoint to flush WAL into the main database:
   ```bash
   python3 -c "import sqlite3; conn = sqlite3.connect('~/.gemini/antigravity/conversation_summaries.db'); conn.execute('PRAGMA wal_checkpoint(TRUNCATE);'); conn.close()"
   ```
4. If a safety backup was created earlier, restore from `conversation_summaries.db.backup_<timestamp>`.
