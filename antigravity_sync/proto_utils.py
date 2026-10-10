"""
Protobuf wire-format encoder and decoder for Antigravity agyhub_summaries_proto.pb.

Implemented using Python standard library without external protoc dependencies.
"""

from pathlib import Path
from typing import Dict, Optional, Tuple


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
