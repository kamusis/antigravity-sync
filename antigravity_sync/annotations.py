"""
Per-conversation annotation store (annotations/<id>.pbtxt).

The annotation file is the authoritative store for a conversation's display
title (plus archived flag and view timestamps). When the annotation lacks a
title, the LanguageServer re-derives the title from the first user message
every time the conversation is opened, reverting any title repaired only in
the summaries database or protobuf cache.
"""

import re
from pathlib import Path
from typing import Optional

_TITLE_RE = re.compile(r'title:"((?:[^"\\]|\\.)*)"')


def _escape(value: str) -> str:
    """Escape a string for inclusion in a text protobuf literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _unescape(value: str) -> str:
    """Unescape a text protobuf string literal body."""
    return value.replace('\\"', '"').replace("\\\\", "\\")


def get_annotation_title(agy_dir: Path, conversation_id: str) -> Optional[str]:
    """Read the annotated title of a conversation.

    Args:
        agy_dir: Antigravity data directory.
        conversation_id: Conversation UUID.

    Returns:
        The annotated title, or None when absent.
    """
    path = agy_dir / "annotations" / f"{conversation_id}.pbtxt"
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = _TITLE_RE.search(text)
    return _unescape(m.group(1)) if m else None


def set_annotation_title(
    agy_dir: Path,
    conversation_id: str,
    title: str,
    archived: Optional[bool] = None,
) -> bool:
    """Write the conversation title into its annotation file.

    Preserves all other annotation fields; creates the file when missing.

    Args:
        agy_dir: Antigravity data directory.
        conversation_id: Conversation UUID.
        title: Title to store.
        archived: When set, also ensure the archived flag matches.

    Returns:
        True if the file was created or modified.
    """
    if not title:
        return False
    ann_dir = agy_dir / "annotations"
    ann_dir.mkdir(parents=True, exist_ok=True)
    path = ann_dir / f"{conversation_id}.pbtxt"

    text = ""
    if path.exists():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""

    escaped = _escape(title)
    if _TITLE_RE.search(text):
        new_text = _TITLE_RE.sub(f'title:"{escaped}"', text, count=1)
    else:
        prefix = ""
        if archived and re.search(r"\barchived:", text) is None:
            prefix += "archived:true "
        new_text = f'{prefix}title:"{escaped}" {text}'.strip() + "\n"

    if archived is True and "archived:true" not in new_text:
        new_text = "archived:true " + new_text

    if path.exists() and path.read_text(encoding="utf-8") == new_text:
        return False
    path.write_text(new_text, encoding="utf-8")
    return True
