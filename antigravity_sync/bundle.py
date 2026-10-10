"""
Packaging and bundle archive management (.agypkg) for conversation data.
"""

import os
import json
import zipfile
import tempfile
import sqlite3
import hashlib
import platform
import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Callable

from antigravity_sync.storage import create_safe_db_snapshot
from antigravity_sync.proto_utils import read_agyhub_proto_file, encode_agyhub_proto


BUNDLE_VERSION = "1.0"


def _calculate_sha256(data: bytes) -> str:
    """Calculate SHA256 hexadecimal hash for given bytes."""
    return hashlib.sha256(data).hexdigest()


def create_bundle(
    antigravity_dir: Path,
    output_bundle_path: Path,
    conversations: List[Dict[str, Any]],
    include_scratch: bool = True,
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
) -> Dict[str, Any]:
    """Create a compressed .agypkg bundle containing specified conversations and metadata.

    Args:
        antigravity_dir: Root Antigravity data directory.
        output_bundle_path: Target path for the output archive.
        conversations: List of conversation metadata dicts to include.
        include_scratch: Whether to include scratch/ directories from brain.
        progress_callback: Optional callback func(item_name, current_index, total_count).

    Returns:
        The generated manifest dictionary.
    """
    output_bundle_path.parent.mkdir(parents=True, exist_ok=True)
    conv_ids = [c["conversation_id"] for c in conversations]
    conv_id_set = set(conv_ids)

    conv_dir = antigravity_dir / "conversations"
    brain_dir = antigravity_dir / "brain"
    summaries_db_path = antigravity_dir / "conversation_summaries.db"

    with tempfile.TemporaryDirectory(prefix="agy_pack_") as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        temp_db_path = temp_dir / "conversation_summaries.db"

        # 1. Create a safe snapshot of the summaries database
        if summaries_db_path.exists():
            create_safe_db_snapshot(summaries_db_path, temp_db_path)

            # Filter the snapshot to only contain the selected conversations
            conn = sqlite3.connect(str(temp_db_path))
            try:
                # Remove unselected conversations from conversation_summaries
                placeholders = ",".join("?" for _ in conv_ids)
                conn.execute(
                    f"DELETE FROM conversation_summaries WHERE conversation_id NOT IN ({placeholders})",
                    conv_ids,
                )
                # Filter conversations_fts if it exists
                has_fts = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversations_fts'"
                ).fetchone()
                if has_fts:
                    conn.execute(
                        f"DELETE FROM conversations_fts WHERE conversation_id NOT IN ({placeholders})",
                        conv_ids,
                    )
                conn.commit()
                conn.execute("VACUUM;")
            finally:
                conn.close()

        # 2. Collect files and calculate sizes
        manifest_conversations = []
        files_to_pack: List[Tuple[Path, str]] = []  # (source_path, archive_arcname)
        total_uncompressed_bytes = 0

        # Include the filtered summaries.db
        if temp_db_path.exists():
            files_to_pack.append((temp_db_path, "conversation_summaries.db"))
            total_uncompressed_bytes += temp_db_path.stat().st_size

        # Include filtered agyhub_summaries_proto.pb if present
        proto_path = antigravity_dir / "agyhub_summaries_proto.pb"
        if proto_path.exists():
            proto_entries = read_agyhub_proto_file(proto_path)
            filtered_entries = {cid: proto_entries[cid] for cid in conv_ids if cid in proto_entries}
            if filtered_entries:
                temp_pb_path = temp_dir / "agyhub_summaries.pb"
                temp_pb_path.write_bytes(encode_agyhub_proto(filtered_entries))
                files_to_pack.append((temp_pb_path, "agyhub_summaries.pb"))
                total_uncompressed_bytes += temp_pb_path.stat().st_size

        # Include per-conversation annotations (title/archived state)
        ann_dir = antigravity_dir / "annotations"
        for cid in conv_id_set:
            ann_file = ann_dir / f"{cid}.pbtxt"
            if ann_file.exists():
                files_to_pack.append((ann_file, f"annotations/{cid}.pbtxt"))
                total_uncompressed_bytes += ann_file.stat().st_size

        total_convs = len(conversations)
        for idx, conv in enumerate(conversations):
            cid = conv["conversation_id"]
            if progress_callback:
                progress_callback(cid, idx + 1, total_convs)

            conv_size = 0

            # Check for conversation db/pb files
            db_file = conv_dir / f"{cid}.db"
            pb_file = conv_dir / f"{cid}.pb"

            if db_file.exists():
                files_to_pack.append((db_file, f"conversations/{cid}.db"))
                conv_size += db_file.stat().st_size
            elif pb_file.exists():
                files_to_pack.append((pb_file, f"conversations/{cid}.pb"))
                conv_size += pb_file.stat().st_size

            # Check for brain directory
            conv_brain_dir = brain_dir / cid
            if conv_brain_dir.is_dir():
                for root, dirs, files in os.walk(conv_brain_dir):
                    rel_dir = Path(root).relative_to(conv_brain_dir)

                    # Filter scratch or temp storage if requested
                    if not include_scratch:
                        dirs[:] = [d for d in dirs if d not in ("scratch", ".tempmediaStorage")]

                    for file in files:
                        file_path = Path(root) / file
                        arcname = f"brain/{cid}/{rel_dir / file}".replace("\\", "/")
                        files_to_pack.append((file_path, arcname))
                        conv_size += file_path.stat().st_size

            total_uncompressed_bytes += conv_size
            conv_meta = dict(conv)
            conv_meta["size_bytes"] = conv_size
            manifest_conversations.append(conv_meta)

        # 3. Construct manifest
        manifest = {
            "version": BUNDLE_VERSION,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "source_platform": platform.system(),
            "conversation_count": len(manifest_conversations),
            "total_uncompressed_bytes": total_uncompressed_bytes,
            "conversations": manifest_conversations,
        }

        manifest_bytes = json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8")
        manifest_path = temp_dir / "manifest.json"
        manifest_path.write_bytes(manifest_bytes)
        files_to_pack.append((manifest_path, "manifest.json"))

        # 4. Write zip package
        with zipfile.ZipFile(str(output_bundle_path), "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for src_p, arc_name in files_to_pack:
                zf.write(str(src_p), arc_name)

        return manifest


def read_bundle_manifest(bundle_path: Path) -> Dict[str, Any]:
    """Read and validate the manifest.json inside an .agypkg bundle without full extraction.

    Args:
        bundle_path: Path to the .agypkg file.

    Returns:
        Parsed manifest dictionary.

    Raises:
        ValueError: If file is invalid, corrupted, or missing manifest.json.
    """
    if not bundle_path.exists():
        raise FileNotFoundError(f"Bundle file not found: {bundle_path}")

    if not zipfile.is_zipfile(str(bundle_path)):
        raise ValueError(f"File is not a valid zip archive or .agypkg bundle: {bundle_path}")

    with zipfile.ZipFile(str(bundle_path), "r") as zf:
        if "manifest.json" not in zf.namelist():
            raise ValueError("Invalid bundle: missing manifest.json")

        manifest_data = zf.read("manifest.json")
        try:
            return json.loads(manifest_data.decode("utf-8"))
        except Exception as e:
            raise ValueError(f"Failed to decode manifest.json: {e}")


def extract_bundle(bundle_path: Path, target_dir: Path) -> Dict[str, Any]:
    """Extract an .agypkg bundle to a target directory and return its manifest.

    Args:
        bundle_path: Path to the .agypkg file.
        target_dir: Directory where files will be unpacked.

    Returns:
        The extracted manifest dictionary.
    """
    manifest = read_bundle_manifest(bundle_path)
    target_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(str(bundle_path), "r") as zf:
        zf.extractall(str(target_dir))

    return manifest
