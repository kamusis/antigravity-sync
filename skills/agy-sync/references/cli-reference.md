# Antigravity Sync CLI Reference

Detailed command-line flag documentation and invocation examples for `antigravity-sync`.

## Global Options

- `-v, --version`: Display tool version and exit.
- `-h, --help`: Display command summary.

---

## 1. `sync`

Bidirectionally synchronize conversation sessions with a remote host via SSH.

```bash
python3 -m antigravity_sync sync --remote <host> [options]
```

### Arguments

| Flag | Type | Description |
|---|---|---|
| `--remote` | string (required) | SSH hostname, IP, or `~/.ssh/config` alias (e.g. `macbook`, `user@192.168.1.50`). |
| `--dry-run` | flag | Simulate sync: shows sessions to push/pull, workspace mappings, and conflicts without writing any data. |
| `--push-only` | flag | Only push local conversations to remote host. Skips pulling remote sessions. |
| `--pull-only` | flag | Only pull remote conversations to local machine. Skips pushing local sessions. |
| `--no-scratch` | flag | Exclude `.system_generated/scratch/` and temporary artifacts from bundles to save transfer time. |
| `--force` | flag | Proceed with synchronization even if an active Antigravity process is detected. (Caution: risk of WAL locks). |
| `--dir` | path | Custom path to local Antigravity directory (defaults to `~/.gemini/antigravity`). |

---

## 2. `export`

Export conversations and metadata from local runtime into a compressed `.agypkg` bundle archive.

```bash
python3 -m antigravity_sync export [options]
```

### Arguments

| Flag | Type | Description |
|---|---|---|
| `-o, --output` | path | Target bundle path. Defaults to `agy_backup_<YYYYMMDD_HHMMSS>.agypkg` in current directory. |
| `--since` | date (`YYYY-MM-DD`) | Filter conversations modified on or after this date. |
| `--workspace` | string | Filter conversations whose workspace path contains this substring. |
| `--id` | string | Filter specific conversation UUIDs (can be repeated or comma-separated). |
| `--no-scratch` | flag | Exclude scratch files and temporary logs. |
| `--force` | flag | Export even if Antigravity is currently running. |
| `--dir` | path | Custom Antigravity data directory. |

---

## 3. `import`

Import and incrementally merge an `.agypkg` archive into the target Antigravity runtime.

```bash
python3 -m antigravity_sync import <bundle_path> [options]
```

### Arguments

| Flag | Type | Description |
|---|---|---|
| `bundle` | path (positional) | Path to `.agypkg` file. |
| `--dry-run` | flag | Validate bundle, check conflicts, and preview additions without modifying target DB. |
| `--mode` | `merge` \| `overwrite` | Import strategy. `merge` (default) non-destructively upserts sessions; `overwrite` replaces overlapping sessions. |
| `--remap-workspace` | `OLD=NEW` | Remap workspace path strings embedded in summaries and trajectory blobs. Can be repeated. |
| `--remap-project-id` | `OLD=NEW` | Remap 36-char project UUIDs between machines. Can be repeated. |
| `--no-backup` | flag | Disable automatic safety backup of `conversation_summaries.db`. Not recommended. |
| `--force` | flag | Force import even if Antigravity process is detected running. |
| `--dir` | path | Custom target Antigravity directory. |

---

## 4. `inspect`

Inspect an exported `.agypkg` bundle or local Antigravity runtime status without modifying data.

```bash
# Inspect local runtime:
python3 -m antigravity_sync inspect --local

# Inspect bundle archive:
python3 -m antigravity_sync inspect <bundle_path>
```

---

## 5. `list`

List conversation summaries from local database or a bundle archive.

```bash
python3 -m antigravity_sync list [options]
```

### Arguments

| Flag | Type | Description |
|---|---|---|
| `-f, --file` | path | Read and list conversations from a `.agypkg` bundle instead of local database. |
| `-n, --limit` | integer | Maximum number of conversations to display (default: 20). |
| `--since` | date (`YYYY-MM-DD`) | Filter conversations modified on or after this date. |
| `--workspace` | string | Filter conversations by workspace keyword. |
| `--dir` | path | Custom local Antigravity data directory. |
