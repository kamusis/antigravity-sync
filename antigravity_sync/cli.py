"""
Command-line interface entry point for antigravity-sync.
"""

import sys
import argparse
import datetime
from pathlib import Path
from typing import List, Tuple, Optional

from antigravity_sync import __version__
from antigravity_sync.storage import (
    get_antigravity_dir,
    is_antigravity_running,
    list_conversations_from_db,
)
from antigravity_sync.bundle import (
    create_bundle,
    read_bundle_manifest,
)
from antigravity_sync.merger import import_bundle

if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def format_bytes(num_bytes: int) -> str:
    """Format bytes into human-readable string (KB, MB, GB)."""
    for unit in ["B", "KB", "MB", "GB"]:
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:3.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} TB"


def cmd_export(args: argparse.Namespace) -> int:
    """Handle the export command."""
    base_dir = get_antigravity_dir(args.dir)
    if not base_dir.exists():
        print(f"Error: Antigravity data directory not found: {base_dir}", file=sys.stderr)
        return 1

    if is_antigravity_running() and not args.force:
        print("Warning: Antigravity process appears to be currently running.", file=sys.stderr)
        print("To avoid database locks, it is recommended to close Antigravity before exporting.")
        print("Use --force to proceed anyway.\n", file=sys.stderr)
        return 1

    db_path = base_dir / "conversation_summaries.db"
    if not db_path.exists():
        print(f"Error: conversation_summaries.db not found in {base_dir}", file=sys.stderr)
        return 1

    # Resolve target output file
    if args.output:
        output_path = Path(args.output).expanduser().resolve()
    else:
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = Path.cwd() / f"agy_backup_{timestamp}.agypkg"

    # Parse conversation IDs if provided
    conv_ids = None
    if args.id:
        conv_ids = []
        for item in args.id:
            conv_ids.extend([x.strip() for x in item.split(",") if x.strip()])

    print(f"[*] Reading conversation summaries from: {base_dir}")
    conversations = list_conversations_from_db(
        db_path,
        since=args.since,
        workspace=args.workspace,
        conv_ids=conv_ids,
    )

    if not conversations:
        print("No matching conversations found to export.")
        return 0

    print(f"[*] Found {len(conversations)} conversation(s) to pack.")
    print(f"[*] Creating bundle at: {output_path}")

    def progress(cid: str, current: int, total: int):
        print(f"    [{current}/{total}] Packing conversation {cid[:8]}...", end="\r")

    manifest = create_bundle(
        antigravity_dir=base_dir,
        output_bundle_path=output_path,
        conversations=conversations,
        include_scratch=not args.no_scratch,
        progress_callback=progress,
    )
    print()  # newline after progress

    total_size = output_path.stat().st_size
    uncompressed_size = manifest.get("total_uncompressed_bytes", 0)

    print("\n[+] Export successfully completed!")
    print(f"    Archive path:        {output_path}")
    print(f"    Conversations:       {manifest['conversation_count']}")
    print(f"    Compressed size:     {format_bytes(total_size)}")
    print(f"    Uncompressed size:   {format_bytes(uncompressed_size)}")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    """Handle the import command."""
    bundle_path = Path(args.bundle).expanduser().resolve()
    if not bundle_path.exists():
        print(f"Error: Bundle file not found: {bundle_path}", file=sys.stderr)
        return 1

    target_dir = get_antigravity_dir(args.dir)

    if is_antigravity_running() and not args.force and not args.dry_run:
        print("Warning: Antigravity is running. Modifying active databases is risky.", file=sys.stderr)
        print("Please close Antigravity before importing, or use --force.\n", file=sys.stderr)
        return 1

    # Parse workspace remapping pairs
    remap_pairs: List[Tuple[str, str]] = []
    if args.remap_workspace:
        for pair in args.remap_workspace:
            if "=" not in pair:
                print(f"Error: Invalid --remap-workspace argument '{pair}'. Expected OLD=NEW.", file=sys.stderr)
                return 1
            old_p, new_p = pair.split("=", 1)
            remap_pairs.append((old_p.strip(), new_p.strip()))

    print(f"[*] Target Antigravity directory: {target_dir}")
    print(f"[*] Inspecting bundle: {bundle_path}")

    try:
        manifest = read_bundle_manifest(bundle_path)
    except Exception as e:
        print(f"Error: Failed to read bundle: {e}", file=sys.stderr)
        return 1

    print(f"    Version:          {manifest.get('version')}")
    print(f"    Created:          {manifest.get('created_at')}")
    print(f"    Platform:         {manifest.get('source_platform')}")
    print(f"    Total in bundle:  {manifest.get('conversation_count')}")

    if remap_pairs:
        print("[*] Workspace path remapping rules:")
        for old_p, new_p in remap_pairs:
            print(f"    '{old_p}' -> '{new_p}'")

    if args.dry_run:
        print("\n--- [DRY-RUN PLAN] ---")
    else:
        print(f"\n[*] Executing import (mode: {args.mode})...")

    result = import_bundle(
        bundle_path=bundle_path,
        target_dir=target_dir,
        mode=args.mode,
        remap_workspaces=remap_pairs if remap_pairs else None,
        dry_run=args.dry_run,
        create_backup=not args.no_backup,
    )

    if args.dry_run:
        print(f"    Incoming conversations: {result['total_incoming']}")
        print(f"    Existing overlapping:   {result['conflicts']}")
        print(f"    Brand-new to add:       {result['new_conversations']}")
        print("\n[!] Dry run complete. No modifications were made.")
        return 0

    print("\n[+] Import successfully completed!")
    print(f"    Merged conversations: {result['total_incoming']}")
    if result.get("backup_path"):
        print(f"    Safety backup saved:  {result['backup_path']}")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    """Handle the inspect command."""
    if args.local:
        base_dir = get_antigravity_dir(args.dir)
        print(f"[*] Inspecting local Antigravity runtime: {base_dir}")
        if not base_dir.exists():
            print(f"Directory does not exist: {base_dir}", file=sys.stderr)
            return 1

        db_path = base_dir / "conversation_summaries.db"
        convs = list_conversations_from_db(db_path) if db_path.exists() else []

        print(f"    Status:                {'RUNNING' if is_antigravity_running() else 'STOPPED'}")
        print(f"    Indexed Conversations: {len(convs)}")
        if convs:
            workspaces = set()
            for c in convs:
                uris = c.get("workspace_uris") or ""
                if uris:
                    workspaces.add(uris)
            print(f"    Associated Workspaces: {len(workspaces)}")
            print(f"    Latest Session Date:   {convs[0].get('last_modified_time')}")
        return 0

    if not args.target:
        print("Error: Specify a bundle path or use --local to inspect current installation.", file=sys.stderr)
        return 1

    bundle_path = Path(args.target).expanduser().resolve()
    try:
        manifest = read_bundle_manifest(bundle_path)
    except Exception as e:
        print(f"Error inspecting bundle: {e}", file=sys.stderr)
        return 1

    convs = manifest.get("conversations", [])
    print(f"[*] Bundle: {bundle_path.name}")
    print(f"    Version:           {manifest.get('version')}")
    print(f"    Source Platform:   {manifest.get('source_platform')}")
    print(f"    Created At:        {manifest.get('created_at')}")
    print(f"    Conversations:     {manifest.get('conversation_count')}")
    print(f"    Archive Size:      {format_bytes(bundle_path.stat().st_size)}")
    print(f"    Uncompressed:      {format_bytes(manifest.get('total_uncompressed_bytes', 0))}")

    if convs:
        print("\n--- Top Conversations ---")
        for c in convs[:5]:
            title = (c.get("title") or "Untitled").strip().replace("\n", " ")
            if len(title) > 40:
                title = title[:37] + "..."
            print(f"  [{c['conversation_id'][:8]}] {title:<40} (steps: {c.get('step_count', 0):>3}) - {c.get('last_modified_time')}")
        if len(convs) > 5:
            print(f"  ... and {len(convs) - 5} more conversation(s).")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """Handle the list command."""
    if args.file:
        bundle_path = Path(args.file).expanduser().resolve()
        try:
            manifest = read_bundle_manifest(bundle_path)
            convs = manifest.get("conversations", [])
        except Exception as e:
            print(f"Error reading bundle: {e}", file=sys.stderr)
            return 1
    else:
        base_dir = get_antigravity_dir(args.dir)
        db_path = base_dir / "conversation_summaries.db"
        convs = list_conversations_from_db(
            db_path,
            since=args.since,
            workspace=args.workspace,
        )

    if not convs:
        print("No conversations found.")
        return 0

    if args.limit and args.limit > 0:
        convs = convs[:args.limit]

    print(f"{'ID':<10} {'STEPS':<7} {'MODIFIED':<22} {'TITLE'}")
    print("-" * 75)
    for c in convs:
        cid = c["conversation_id"][:8]
        steps = str(c.get("step_count", 0))
        mtime = str(c.get("last_modified_time") or "")[:19]
        title = (c.get("title") or "Untitled").strip().replace("\n", " ")
        if len(title) > 40:
            title = title[:37] + "..."
        print(f"{cid:<10} {steps:<7} {mtime:<22} {title}")

    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """Main CLI entrypoint."""
    parser = argparse.ArgumentParser(
        prog="antigravity-sync",
        description="Cross-machine conversation migration and sync tool for Antigravity runtime.",
    )
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # export
    p_export = subparsers.add_parser("export", help="Export conversations to an .agypkg bundle")
    p_export.add_argument("-o", "--output", help="Path of output bundle file (default: agy_backup_<timestamp>.agypkg)")
    p_export.add_argument("--dir", help="Custom Antigravity data directory")
    p_export.add_argument("--since", help="Filter conversations after this date (YYYY-MM-DD)")
    p_export.add_argument("--workspace", help="Filter by workspace keyword")
    p_export.add_argument("--id", action="append", help="Filter specific conversation UUID(s)")
    p_export.add_argument("--no-scratch", action="store_true", help="Exclude scratch/ and temporary media files")
    p_export.add_argument("--force", action="store_true", help="Proceed even if Antigravity is running")

    # import
    p_import = subparsers.add_parser("import", help="Import an .agypkg bundle into Antigravity")
    p_import.add_argument("bundle", help="Path to .agypkg bundle file")
    p_import.add_argument("--dir", help="Custom target Antigravity data directory")
    p_import.add_argument("--mode", choices=["merge", "overwrite"], default="merge", help="Import mode (default: merge)")
    p_import.add_argument("--remap-workspace", action="append", help="Remap workspace paths: OLD=NEW")
    p_import.add_argument("--dry-run", action="store_true", help="Inspect and simulate import without writing")
    p_import.add_argument("--no-backup", action="store_true", help="Skip creating backup of current summaries.db")
    p_import.add_argument("--force", action="store_true", help="Proceed even if Antigravity is running")

    # inspect
    p_inspect = subparsers.add_parser("inspect", help="Inspect a bundle or local Antigravity installation")
    p_inspect.add_argument("target", nargs="?", help="Path to .agypkg bundle file to inspect")
    p_inspect.add_argument("--local", action="store_true", help="Inspect current local Antigravity storage")
    p_inspect.add_argument("--dir", help="Custom Antigravity data directory")

    # list
    p_list = subparsers.add_parser("list", help="List conversations from local database or a bundle")
    p_list.add_argument("-f", "--file", help="List conversations from an .agypkg bundle instead of local")
    p_list.add_argument("--dir", help="Custom Antigravity data directory")
    p_list.add_argument("--since", help="Filter conversations after this date (YYYY-MM-DD)")
    p_list.add_argument("--workspace", help="Filter by workspace keyword")
    p_list.add_argument("-n", "--limit", type=int, default=20, help="Maximum number of conversations to list (default: 20)")

    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    if args.command == "export":
        return cmd_export(args)
    elif args.command == "import":
        return cmd_import(args)
    elif args.command == "inspect":
        return cmd_inspect(args)
    elif args.command == "list":
        return cmd_list(args)

    return 0


if __name__ == "__main__":
    sys.exit(main())
