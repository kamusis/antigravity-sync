"""
Protobuf wire-format encoder and decoder for Antigravity agyhub_summaries_proto.pb.

Implemented using Python standard library without external protoc dependencies.
"""

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def encode_varint(n: int) -> bytes:
    """Encode an integer as a protobuf varint.

    Args:
        n: Unsigned integer to encode.

    Returns:
        Varint-encoded bytes.
    """
    res = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            res.append(b | 0x80)
        else:
            res.append(b)
            break
    return bytes(res)


def decode_varint(data: bytes, offset: int) -> Tuple[int, int]:
    """Decode a varint from bytes starting at offset.

    Args:
        data: Buffer containing bytes.
        offset: Starting offset in buffer.

    Returns:
        Tuple of (decoded_value, new_offset).
    """
    val = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise ValueError("Unexpected end of data while decoding varint")
        b = data[offset]
        offset += 1
        val |= (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            break
    return val, offset


def parse_agyhub_proto(data: bytes) -> Dict[str, bytes]:
    """Parse agyhub_summaries_proto.pb into a dictionary mapping conversation_id to raw_summary.

    The protobuf structure is a repeated message (tag 1, wire_type 2):
        message Entry {
            string conversation_id = 1;
            bytes raw_summary = 2;
        }

    Args:
        data: Raw bytes of agyhub_summaries_proto.pb.

    Returns:
        Dict mapping conversation_id (str) to raw_summary bytes.
    """
    idx = 0
    entries: Dict[str, bytes] = {}

    while idx < len(data):
        tag = data[idx]
        wire_type = tag & 0x07
        field_num = tag >> 3
        idx += 1

        if wire_type != 2 or field_num != 1:
            raise ValueError(f"Unexpected wire_type {wire_type} or field_num {field_num} at {idx-1}")

        length, idx = decode_varint(data, idx)
        msg_bytes = data[idx : idx + length]
        idx += length

        # Parse Entry message fields
        m_idx = 0
        cid: Optional[str] = None
        raw_summary: Optional[bytes] = None

        while m_idx < len(msg_bytes):
            m_tag = msg_bytes[m_idx]
            m_wire = m_tag & 0x07
            m_field = m_tag >> 3
            m_idx += 1

            if m_wire != 2:
                # Unsupported wire type in Entry message
                break

            m_len, m_idx = decode_varint(msg_bytes, m_idx)
            payload = msg_bytes[m_idx : m_idx + m_len]
            m_idx += m_len

            if m_field == 1:
                try:
                    cid = payload.decode("utf-8")
                except UnicodeDecodeError:
                    cid = None
            elif m_field == 2:
                raw_summary = payload

        if cid and raw_summary is not None:
            entries[cid] = raw_summary

    return entries


def encode_agyhub_proto(entries: Dict[str, bytes]) -> bytes:
    """Encode conversation entries into binary agyhub_summaries_proto.pb format.

    Args:
        entries: Dict mapping conversation_id (str) to raw_summary bytes.

    Returns:
        Binary protobuf bytes.
    """
    res = bytearray()
    for cid, raw in entries.items():
        cid_bytes = cid.encode("utf-8")
        # Entry message: field 1 (string cid), field 2 (bytes raw_summary)
        entry_body = (
            bytes([0x0A])
            + encode_varint(len(cid_bytes))
            + cid_bytes
            + bytes([0x12])
            + encode_varint(len(raw))
            + raw
        )
        # Outer message repeated field 1
        res.extend(bytes([0x0A]) + encode_varint(len(entry_body)) + entry_body)
    return bytes(res)


def read_agyhub_proto_file(proto_path: Path) -> Dict[str, bytes]:
    """Read and parse an agyhub_summaries_proto.pb file.

    Args:
        proto_path: Path to the .pb file.

    Returns:
        Dict mapping conversation_id to raw_summary bytes.
    """
    if not proto_path.exists():
        return {}
    return parse_agyhub_proto(proto_path.read_bytes())


def merge_agyhub_proto_file(
    target_path: Path, incoming_entries: Dict[str, bytes]
) -> int:
    """Merge incoming entries into target agyhub_summaries_proto.pb file.

    Args:
        target_path: Path to target .pb file.
        incoming_entries: Dict mapping conversation_id to raw_summary bytes.

    Returns:
        Number of entries merged/updated.
    """
    existing_entries = read_agyhub_proto_file(target_path) if target_path.exists() else {}
    count = len(incoming_entries)
    existing_entries.update(incoming_entries)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(encode_agyhub_proto(existing_entries))
    return count


def _parse_fields_strict(data: bytes) -> Optional[List[Tuple[int, int, object]]]:
    """Parse a protobuf message into (field_number, wire_type, value) tuples.

    Values are ints for wire type 0 and bytes for wire types 1/2/5.
    Returns None if the buffer is not a cleanly terminated protobuf message
    (unknown wire types, truncated fields, or mismatched group markers).

    Args:
        data: Buffer to parse.

    Returns:
        List of field tuples, or None when parsing fails.
    """
    fields: List[Tuple[int, int, object]] = []
    idx = 0
    group_stack: List[int] = []
    try:
        while idx < len(data):
            tag, idx = decode_varint(data, idx)
            field_num, wire_type = tag >> 3, tag & 0x07
            if field_num == 0:
                return None
            if wire_type == 0:
                val, idx = decode_varint(data, idx)
                fields.append((field_num, wire_type, val))
            elif wire_type == 1:
                if idx + 8 > len(data):
                    return None
                fields.append((field_num, wire_type, data[idx:idx + 8]))
                idx += 8
            elif wire_type == 2:
                length, idx = decode_varint(data, idx)
                if idx + length > len(data):
                    return None
                fields.append((field_num, wire_type, data[idx:idx + length]))
                idx += length
            elif wire_type == 3:
                group_stack.append(field_num)
                fields.append((field_num, wire_type, None))
            elif wire_type == 4:
                if not group_stack or group_stack[-1] != field_num:
                    return None
                group_stack.pop()
                fields.append((field_num, wire_type, None))
            elif wire_type == 5:
                if idx + 4 > len(data):
                    return None
                fields.append((field_num, wire_type, data[idx:idx + 4]))
                idx += 4
            else:
                return None
    except ValueError:
        return None
    return fields if not group_stack else None


def _serialize_fields(fields: List[Tuple[int, int, object]]) -> bytes:
    """Serialize parsed field tuples back into protobuf wire format.

    Args:
        fields: List of (field_number, wire_type, value) tuples.

    Returns:
        Re-encoded protobuf bytes with length prefixes recomputed.
    """
    res = bytearray()
    for field_num, wire_type, value in fields:
        res += encode_varint((field_num << 3) | wire_type)
        if wire_type == 0:
            res += encode_varint(value)
        elif wire_type == 2:
            res += encode_varint(len(value)) + value
        elif wire_type in (1, 5):
            res += value
        # Wire types 3/4 (groups) carry no payload of their own.
    return bytes(res)


def build_one_pass_replacer(replacements: List[Tuple[object, bytes]]):
    """Build a single-pass bytes replacer from (pattern, new) pairs.

    Patterns may be plain bytes (escaped literally) or compiled bytes-mode
    regex patterns (used as-is, e.g. with lookbehind assertions). Longer
    patterns are matched first so overlapping rules never corrupt each
    other's output within a pass; rules whose replacement contains the
    pattern itself must use a lookbehind to stay idempotent across runs.

    Args:
        replacements: List of (pattern, new_bytes) pairs.

    Returns:
        A function transforming bytes with all replacements applied in one pass.
    """
    pairs = [(o, n) for o, n in replacements if o and o != n]
    if not pairs:
        return lambda data: data

    def source_len(o):
        return len(o.pattern if isinstance(o, re.Pattern) else o)

    ordered = sorted(pairs, key=lambda p: source_len(p[0]), reverse=True)
    parts = []
    for o, _ in ordered:
        src = o.pattern if isinstance(o, re.Pattern) else re.escape(o)
        # Wrap each alternative in a group so the match callback can tell
        # which rule fired via lastindex. Regex rules must therefore use
        # non-capturing groups (?:...) internally.
        parts.append(b"(" + src + b")")
    pattern = re.compile(b"|".join(parts))
    mapping = {i + 1: n for i, (_, n) in enumerate(ordered)}
    return lambda data: pattern.sub(lambda m: mapping[m.lastindex], data)


def set_summary_title(data: bytes, title: str) -> bytes:
    """Set the conversation title inside a raw_summary protobuf.

    The title lives in two places: top-level field 1 and field 15.1 (the
    persisted title state). When field 15.1 is missing, the LanguageServer
    re-derives the title from the first user message whenever the
    conversation is opened, reverting the displayed title to the raw prompt.

    Args:
        data: Raw protobuf bytes of the summary.
        title: Title string to store.

    Returns:
        Re-encoded protobuf bytes.
    """
    fields = _parse_fields_strict(data)
    if fields is None:
        return data
    title_payload = title.encode("utf-8")
    out = []
    changed = False
    for field_num, wire_type, value in fields:
        if field_num == 1 and wire_type == 2:
            if value != title_payload:
                value, changed = title_payload, True
        elif field_num == 15 and wire_type == 2:
            sub = _parse_fields_strict(value)
            if sub is not None:
                new_sub = []
                sub_changed = False
                has_title = False
                for s_field, s_wt, s_val in sub:
                    if s_field == 1 and s_wt == 2:
                        has_title = True
                        if s_val != title_payload:
                            s_val, sub_changed = title_payload, True
                    new_sub.append((s_field, s_wt, s_val))
                if not has_title:
                    # Insert 15.1 as the first entry to match native layout.
                    new_sub = [(1, 2, title_payload)] + new_sub
                    sub_changed = True
                if sub_changed:
                    value, changed = _serialize_fields(new_sub), True
        out.append((field_num, wire_type, value))
    if not changed:
        return data
    return _serialize_fields(out)


def transform_proto_bytes(data: bytes, replacements: List[Tuple[bytes, bytes]]) -> bytes:
    """Apply byte replacements inside a protobuf message, re-encoding length prefixes.

    Recursively walks the wire format. Length-delimited fields that parse as
    nested messages are descended into; fields that do not are treated as
    string/binary payloads and transformed with a single-pass replacer, so
    replacements that change payload length remain wire-valid.

    Args:
        data: Raw protobuf bytes (e.g. trajectory_metadata_blob value).
        replacements: List of (old_bytes, new_bytes) pairs.

    Returns:
        Transformed bytes (identical structure, updated lengths).
    """
    replace = build_one_pass_replacer(replacements)
    # Length-preserving-only replacer: fallback for payloads that are not
    # parseable protobuf, where changing length would corrupt framing.
    def effective_len(o):
        return len(o.pattern if isinstance(o, re.Pattern) else o)

    equal_len = [(o, n) for o, n in replacements if effective_len(o) == len(n)]
    replace_equal = build_one_pass_replacer(equal_len)

    def walk(buf: bytes, top_level: bool = False) -> bytes:
        fields = _parse_fields_strict(buf)
        if fields is None:
            # Not a protobuf message: treat as opaque payload. Only apply
            # length-preserving replacements to avoid corrupting framing.
            return replace_equal(buf) if top_level else replace(buf)
        out = []
        changed = False
        for field_num, wire_type, value in fields:
            if wire_type == 2:
                new_val = walk(value)
                if new_val != value:
                    value, changed = new_val, True
            out.append((field_num, wire_type, value))
        return _serialize_fields(out) if changed else buf

    return walk(data, top_level=True)
