"""
Unit and integration tests for antigravity-sync.
"""

import os
import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from antigravity_sync.storage import (
    get_antigravity_dir,
    list_conversations_from_db,
    create_safe_db_snapshot,
)
from antigravity_sync.bundle import (
    create_bundle,
    read_bundle_manifest,
    extract_bundle,
)
from antigravity_sync.merger import (
    merge_summaries_db,
    import_bundle,
    patch_conversation_blob,
)
from antigravity_sync.proto_utils import (
    build_one_pass_replacer,
    transform_proto_bytes,
    encode_varint,
)


def init_mock_antigravity_db(db_path: Path, conversations: list) -> None:
    """Initialize a mock conversation_summaries.db with conversation_summaries and conversations_fts."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE conversation_summaries (
            conversation_id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            preview TEXT NOT NULL DEFAULT '',
            step_count INTEGER NOT NULL DEFAULT 0,
            last_modified_time datetime NOT NULL,
            workspace_uris TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            project_id TEXT NOT NULL DEFAULT '',
            agent_name TEXT NOT NULL DEFAULT '',
            parent_conversation_id TEXT NOT NULL DEFAULT '',
            nesting_depth INTEGER NOT NULL DEFAULT 0,
            battle_id TEXT NOT NULL DEFAULT '',
            winning_conversation_id TEXT NOT NULL DEFAULT '',
            not_fully_idle numeric NOT NULL DEFAULT false,
            killed numeric NOT NULL DEFAULT false,
            last_user_input_time datetime,
            last_user_input_step_index INTEGER NOT NULL DEFAULT -1,
            app_data_dir TEXT NOT NULL DEFAULT '',
            raw_summary BLOB,
            group_id TEXT NOT NULL DEFAULT ''
        );
    """)

    cursor.execute("""
        CREATE VIRTUAL TABLE conversations_fts USING fts5(
            conversation_id UNINDEXED,
            chunk_index UNINDEXED,
            chunk_first_step UNINDEXED,
            chunk_last_step UNINDEXED,
            last_modified_time UNINDEXED,
            step_count UNINDEXED,
            last_step_created_at UNINDEXED,
            archived UNINDEXED,
            user_step_byte_offsets UNINDEXED,
            agent_step_byte_offsets UNINDEXED,
            title,
            workspace,
            project,
            user_body,
            agent_body
        );
    """)

    for conv in conversations:
        cursor.execute("""
            INSERT INTO conversation_summaries (
                conversation_id, title, preview, step_count, last_modified_time, workspace_uris
            ) VALUES (?, ?, ?, ?, ?, ?);
        """, (
            conv["conversation_id"],
            conv["title"],
            conv.get("preview", ""),
            conv.get("step_count", 1),
            conv["last_modified_time"],
            conv["workspace_uris"],
        ))

        cursor.execute("""
            INSERT INTO conversations_fts (
                conversation_id, chunk_index, chunk_first_step, chunk_last_step,
                last_modified_time, step_count, last_step_created_at, archived,
                user_step_byte_offsets, agent_step_byte_offsets,
                title, workspace, project, user_body, agent_body
            ) VALUES (?, 0, 0, 1, ?, 1, ?, 0, '', '', ?, ?, '', 'test user', 'test agent');
        """, (
            conv["conversation_id"],
            conv["last_modified_time"],
            conv["last_modified_time"],
            conv["title"],
            conv["workspace_uris"],
        ))

    conn.commit()
    conn.close()


class TestAntigravitySync(unittest.TestCase):
    """Test suite for antigravity-sync components."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        # Setup source mock Antigravity directory
        self.src_dir = self.root / "src_antigravity"
        self.src_db_path = self.src_dir / "conversation_summaries.db"
        self.conv1_id = "11111111-1111-4111-8111-111111111111"
        self.conv2_id = "22222222-2222-4222-8222-222222222222"

        init_mock_antigravity_db(self.src_db_path, [
            {
                "conversation_id": self.conv1_id,
                "title": "Debug SwissQL Query",
                "preview": "Investigating query bug",
                "step_count": 12,
                "last_modified_time": "2026-09-01T10:00:00Z",
                "workspace_uris": '["file:///C:/Users/alice/projects/swissql"]',
            },
            {
                "conversation_id": self.conv2_id,
                "title": "Setup Docker compose",
                "preview": "Creating compose file",
                "step_count": 5,
                "last_modified_time": "2026-09-15T15:30:00Z",
                "workspace_uris": '["file:///C:/Users/alice/projects/infra"]',
            },
        ])

        # Create mock conversation and brain files
        conv_dir = self.src_dir / "conversations"
        conv_dir.mkdir(parents=True, exist_ok=True)
        (conv_dir / f"{self.conv1_id}.db").write_text("mock sqlite 1", encoding="utf-8")
        (conv_dir / f"{self.conv2_id}.db").write_text("mock sqlite 2", encoding="utf-8")

        brain_dir = self.src_dir / "brain"
        c1_brain = brain_dir / self.conv1_id
        (c1_brain / ".system_generated" / "logs").mkdir(parents=True, exist_ok=True)
        (c1_brain / ".system_generated" / "logs" / "transcript.jsonl").write_text('{"step": 1}\n', encoding="utf-8")
        (c1_brain / "scratch").mkdir(parents=True, exist_ok=True)
        (c1_brain / "scratch" / "temp.py").write_text("print('hello')", encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_list_conversations(self):
        """Test listing conversations with filters."""
        convs = list_conversations_from_db(self.src_db_path)
        self.assertEqual(len(convs), 2)

        # Filter by workspace
        convs_swissql = list_conversations_from_db(self.src_db_path, workspace="swissql")
        self.assertEqual(len(convs_swissql), 1)
        self.assertEqual(convs_swissql[0]["conversation_id"], self.conv1_id)

        # Filter by since
        convs_since = list_conversations_from_db(self.src_db_path, since="2026-09-10")
        self.assertEqual(len(convs_since), 1)
        self.assertEqual(convs_since[0]["conversation_id"], self.conv2_id)

    def test_create_and_read_bundle(self):
        """Test exporting into an .agypkg bundle and inspecting manifest."""
        convs = list_conversations_from_db(self.src_db_path)
        bundle_path = self.root / "export.agypkg"

        manifest = create_bundle(
            antigravity_dir=self.src_dir,
            output_bundle_path=bundle_path,
            conversations=convs,
            include_scratch=False,
        )

        self.assertTrue(bundle_path.exists())
        self.assertEqual(manifest["conversation_count"], 2)

        # Read manifest from zip
        read_manifest = read_bundle_manifest(bundle_path)
        self.assertEqual(read_manifest["version"], "1.0")
        self.assertEqual(read_manifest["conversation_count"], 2)

    def test_import_and_merge(self):
        """Test importing bundle into target machine with workspace remapping."""
        convs = list_conversations_from_db(self.src_db_path)
        bundle_path = self.root / "export.agypkg"
        create_bundle(self.src_dir, bundle_path, convs)

        # Setup target mock directory with an existing conversation
        target_dir = self.root / "target_antigravity"
        target_db_path = target_dir / "conversation_summaries.db"
        conv3_id = "33333333-3333-4333-8333-333333333333"

        init_mock_antigravity_db(target_db_path, [
            {
                "conversation_id": conv3_id,
                "title": "Existing Target Conversation",
                "preview": "Should be preserved",
                "step_count": 8,
                "last_modified_time": "2026-08-01T12:00:00Z",
                "workspace_uris": '["file:///D:/Users/bob/projects/myapp"]',
            }
        ])

        # Execute import with path remapping
        report = import_bundle(
            bundle_path=bundle_path,
            target_dir=target_dir,
            mode="merge",
            remap_workspaces=[("C:/Users/alice", "D:/Users/bob")],
            dry_run=False,
            create_backup=True,
        )

        self.assertEqual(report["total_incoming"], 2)
        self.assertEqual(report["new_conversations"], 2)
        self.assertIsNotNone(report["backup_path"])

        # Verify target database contains 3 conversations in total
        target_convs = list_conversations_from_db(target_db_path)
        self.assertEqual(len(target_convs), 3)

        # Verify workspace URI remapping occurred
        target_c1 = [c for c in target_convs if c["conversation_id"] == self.conv1_id][0]
        self.assertIn("D:/Users/bob/projects/swissql", target_c1["workspace_uris"])
        self.assertNotIn("C:/Users/alice", target_c1["workspace_uris"])

        # Verify physical files were deployed
        self.assertTrue((target_dir / "conversations" / f"{self.conv1_id}.db").exists())
        self.assertTrue((target_dir / "brain" / self.conv1_id / ".system_generated" / "logs" / "transcript.jsonl").exists())

    def test_dry_run_mode(self):
        """Test dry run mode does not write anything to disk."""
        convs = list_conversations_from_db(self.src_db_path)
        bundle_path = self.root / "export.agypkg"
        create_bundle(self.src_dir, bundle_path, convs)

        target_dir = self.root / "empty_target"
        report = import_bundle(
            bundle_path=bundle_path,
            target_dir=target_dir,
            dry_run=True,
        )

        self.assertEqual(report["total_incoming"], 2)
        self.assertTrue(report["dry_run"])
        # Target directory should not exist or be empty
        self.assertFalse((target_dir / "conversation_summaries.db").exists())

    def test_cli_workflow(self):
        """Test full end-to-end CLI workflow via main()."""
        from antigravity_sync.cli import main as cli_main
        import io
        from contextlib import redirect_stdout

        out_bundle = self.root / "cli_bundle.agypkg"
        target_dir = self.root / "cli_target"

        f = io.StringIO()
        with redirect_stdout(f):
            # 1. Export
            rc = cli_main(["export", "--dir", str(self.src_dir), "-o", str(out_bundle), "--force"])
            self.assertEqual(rc, 0)
            self.assertTrue(out_bundle.exists())

            # 2. Inspect
            rc = cli_main(["inspect", str(out_bundle)])
            self.assertEqual(rc, 0)

            # 3. List bundle
            rc = cli_main(["list", "-f", str(out_bundle)])
            self.assertEqual(rc, 0)

            # 4. Import
            rc = cli_main(["import", str(out_bundle), "--dir", str(target_dir), "--force"])
            self.assertEqual(rc, 0)

        # Verify target received files and summaries
        self.assertTrue((target_dir / "conversation_summaries.db").exists())
        convs = list_conversations_from_db(target_dir / "conversation_summaries.db")
        self.assertEqual(len(convs), 2)


def encode_pb_string(field_num: int, payload: bytes) -> bytes:
    """Encode a single length-delimited protobuf field."""
    return encode_varint((field_num << 3) | 2) + encode_varint(len(payload)) + payload


def make_trajectory_blob(uri: str, project_uuid: str) -> bytes:
    """Build a minimal trajectory_metadata_blob protobuf value for tests."""
    inner = encode_pb_string(1, uri.encode("utf-8")) + encode_pb_string(18, project_uuid.encode("utf-8"))
    return encode_pb_string(1, inner)


def make_conversation_db(db_path: Path, uri: str, project_uuid: str) -> None:
    """Create a minimal conversation <id>.db containing a trajectory blob."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE trajectory_metadata_blob (id TEXT PRIMARY KEY DEFAULT 'main', data BLOB);"
    )
    conn.execute(
        "INSERT INTO trajectory_metadata_blob (id, data) VALUES ('main', ?);",
        (make_trajectory_blob(uri, project_uuid),),
    )
    conn.commit()
    conn.close()


class TestPathReplacement(unittest.TestCase):
    """Tests for single-pass path replacement and protobuf blob transformation."""

    def test_one_pass_replacer_no_self_corruption(self):
        """Overlapping rules must not re-match their own output."""
        replacer = build_one_pass_replacer([
            (b"file:///Users/kamus", b"file:///c%3A/Users/kamus"),
            (b"/Users/kamus", b"c:/Users/kamus"),
        ])
        result = replacer(b"file:///Users/kamus/CascadeProjects/mopheus and /Users/kamus/tmp")
        self.assertEqual(
            result,
            b"file:///c%3A/Users/kamus/CascadeProjects/mopheus and c:/Users/kamus/tmp",
        )

    def test_transform_proto_bytes_reencodes_lengths(self):
        """UUID and path replacements inside nested protobuf must stay wire-valid."""
        blob = make_trajectory_blob(
            "file:///Users/kamus/CascadeProjects/mopheus",
            "05f52f38-58c8-4a26-9357-4df001980345",
        )
        replacements = [
            (b"05f52f38-58c8-4a26-9357-4df001980345", b"cc43b06d-e24a-4b42-8a2f-4112ee58ef8b"),
            (b"file:///Users/kamus", b"file:///c%3A/Users/kamus"),
            (b"/Users/kamus", b"c:/Users/kamus"),
        ]
        out = transform_proto_bytes(blob, replacements)
        self.assertIn(b"cc43b06d-e24a-4b42-8a2f-4112ee58ef8b", out)
        self.assertIn(b"file:///c%3A/Users/kamus/CascadeProjects/mopheus", out)
        self.assertNotIn(b"/Users/kamus", out.replace(b"c%3A/Users/kamus", b""))
        # Output must still parse as valid protobuf with the same field layout.
        from antigravity_sync.proto_utils import _parse_fields_strict
        fields = _parse_fields_strict(out)
        self.assertIsNotNone(fields)
        inner = fields[0][2]
        inner_fields = _parse_fields_strict(inner)
        self.assertEqual(inner_fields[0][2], b"file:///c%3A/Users/kamus/CascadeProjects/mopheus")
        self.assertEqual(inner_fields[1][2], b"cc43b06d-e24a-4b42-8a2f-4112ee58ef8b")

    def test_transform_proto_bytes_roundtrip_identity(self):
        """No replacements means byte-identical output."""
        blob = make_trajectory_blob("file:///Users/kamus/x", "05f52f38-58c8-4a26-9357-4df001980345")
        self.assertEqual(transform_proto_bytes(blob, []), blob)


class TestBlobPatching(unittest.TestCase):
    """Tests for trajectory_metadata_blob patching during import."""

    def test_patch_conversation_blob(self):
        """patch_conversation_blob rewrites UUID and paths in place."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "conv.db"
            make_conversation_db(
                db_path,
                "file:///Users/kamus/CascadeProjects/mopheus",
                "05f52f38-58c8-4a26-9357-4df001980345",
            )
            changed = patch_conversation_blob(
                db_path,
                project_id_map={"05f52f38-58c8-4a26-9357-4df001980345": "cc43b06d-e24a-4b42-8a2f-4112ee58ef8b"},
                remap_workspaces=[("file:///Users/kamus", "file:///c%3A/Users/kamus")],
            )
            self.assertTrue(changed)
            conn = sqlite3.connect(str(db_path))
            blob = conn.execute("SELECT data FROM trajectory_metadata_blob WHERE id='main'").fetchone()[0]
            conn.close()
            self.assertIn(b"cc43b06d-e24a-4b42-8a2f-4112ee58ef8b", blob)
            self.assertIn(b"file:///c%3A/Users/kamus/CascadeProjects/mopheus", blob)
            self.assertNotIn(b"05f52f38", blob)

    def test_import_bundle_patches_deployed_blob(self):
        """import_bundle patches trajectory_metadata_blob of deployed .db files."""
        src_dir = Path(tempfile.mkdtemp())
        try:
            conv_id = "44444444-4444-4444-8444-444444444444"
            conv_dir = src_dir / "conversations"
            conv_dir.mkdir(parents=True)
            make_conversation_db(
                conv_dir / f"{conv_id}.db",
                "file:///Users/kamus/CascadeProjects/mopheus",
                "05f52f38-58c8-4a26-9357-4df001980345",
            )
            db_path = src_dir / "conversation_summaries.db"
            init_mock_antigravity_db(db_path, [{
                "conversation_id": conv_id,
                "title": "Mac Session",
                "preview": "hello",
                "step_count": 1,
                "last_modified_time": "2026-10-01T10:00:00Z",
                "workspace_uris": '["file:///Users/kamus/CascadeProjects/mopheus"]',
            }])
            bundle_path = src_dir / "b.agypkg"
            create_bundle(src_dir, bundle_path, list_conversations_from_db(db_path))

            target_dir = Path(tempfile.mkdtemp()) / "target"
            import_bundle(
                bundle_path=bundle_path,
                target_dir=target_dir,
                project_id_map={"05f52f38-58c8-4a26-9357-4df001980345": "cc43b06d-e24a-4b42-8a2f-4112ee58ef8b"},
                remap_workspaces=[("file:///Users/kamus", "file:///c%3A/Users/kamus")],
                create_backup=False,
            )
            deployed = target_dir / "conversations" / f"{conv_id}.db"
            self.assertTrue(deployed.exists())
            conn = sqlite3.connect(str(deployed))
            blob = conn.execute("SELECT data FROM trajectory_metadata_blob WHERE id='main'").fetchone()[0]
            conn.close()
            self.assertIn(b"cc43b06d-e24a-4b42-8a2f-4112ee58ef8b", blob)
            self.assertNotIn(b"05f52f38", blob)
        finally:
            import shutil
            shutil.rmtree(src_dir, ignore_errors=True)


class TestMergeSlugAndTitle(unittest.TestCase):
    """Tests for FTS slug alignment and title backfill in merge_summaries_db."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.target_pid = "cc43b06d-e24a-4b42-8a2f-4112ee58ef8b"
        self.source_pid = "05f52f38-58c8-4a26-9357-4df001980345"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _make_db(self, db_path: Path, convs, fts_rows):
        init_mock_antigravity_db(db_path, convs)
        conn = sqlite3.connect(str(db_path))
        for conv in convs:
            conn.execute(
                "UPDATE conversation_summaries SET project_id=? WHERE conversation_id=?;",
                (conv.get("project_id", ""), conv["conversation_id"]),
            )
        # Mirror the real runtime layout: chunk_index=-1 is the metadata row
        # carrying title/workspace/project.
        for row in fts_rows:
            conn.execute(
                "UPDATE conversations_fts SET chunk_index=-1, title=?, workspace=?, project=? WHERE conversation_id=?;",
                (row["title"], row["workspace"], row["project"], row["conversation_id"]),
            )
        conn.commit()
        conn.close()

    def test_fts_project_aligned_by_target_native_slug(self):
        """FTS project/workspace must follow the target slug for the mapped project."""
        target_db = self.root / "target.db"
        native_id = "55555555-5555-4555-8555-555555555555"
        self._make_db(
            target_db,
            [{
                "conversation_id": native_id,
                "title": "Native",
                "preview": "p",
                "step_count": 1,
                "last_modified_time": "2026-10-01T10:00:00Z",
                "workspace_uris": '["file:///c%3A/Users/kamus/CascadeProjects/mopheus"]',
                "project_id": self.target_pid,
            }],
            [{"conversation_id": native_id, "title": "Native", "workspace": "mopheus", "project": "mopheus"}],
        )
        source_db = self.root / "source.db"
        incoming_id = "66666666-6666-4666-8666-666666666666"
        self._make_db(
            source_db,
            [{
                "conversation_id": incoming_id,
                "title": "",
                "preview": "raw prompt text",
                "step_count": 1,
                "last_modified_time": "2026-10-02T10:00:00Z",
                "workspace_uris": '["file:///Users/kamus/CascadeProjects/moclaw"]',
                "project_id": self.source_pid,
            }],
            [{"conversation_id": incoming_id, "title": "Real Session Title", "workspace": "moclaw", "project": "moclaw"}],
        )

        merge_summaries_db(
            target_db,
            source_db,
            remap_workspaces=[
                ("file:///Users/kamus", "file:///c%3A/Users/kamus"),
                ("CascadeProjects/moclaw", "CascadeProjects/mopheus"),
            ],
            project_id_map={self.source_pid: self.target_pid},
        )

        conn = sqlite3.connect(str(target_db))
        conn.row_factory = sqlite3.Row
        try:
            s = dict(conn.execute("SELECT * FROM conversation_summaries WHERE conversation_id=?", (incoming_id,)).fetchone())
            f = dict(conn.execute("SELECT * FROM conversations_fts WHERE conversation_id=? AND chunk_index=-1", (incoming_id,)).fetchone())
        finally:
            conn.close()
        self.assertEqual(s["project_id"], self.target_pid)
        self.assertEqual(s["title"], "Real Session Title")
        self.assertEqual(f["project"], "mopheus")
        self.assertEqual(f["workspace"], "mopheus")
        self.assertEqual(f["title"], "Real Session Title")
        self.assertIn("CascadeProjects/mopheus", s["workspace_uris"])
        self.assertNotIn("moclaw", s["workspace_uris"])

    def test_outside_project_ungrouped(self):
        """Conversations mapped to outside-of-project get empty FTS slugs."""
        target_db = self.root / "target.db"
        native_id = "77777777-7777-4777-8777-777777777777"
        self._make_db(
            target_db,
            [{
                "conversation_id": native_id,
                "title": "Native",
                "preview": "p",
                "step_count": 1,
                "last_modified_time": "2026-10-01T10:00:00Z",
                "workspace_uris": '["file:///c%3A/Users/kamus/CascadeProjects/mopheus"]',
                "project_id": self.target_pid,
            }],
            [{"conversation_id": native_id, "title": "Native", "workspace": "mopheus", "project": "mopheus"}],
        )
        source_db = self.root / "source.db"
        incoming_id = "88888888-8888-4888-8888-888888888888"
        self._make_db(
            source_db,
            [{
                "conversation_id": incoming_id,
                "title": "T",
                "preview": "p",
                "step_count": 1,
                "last_modified_time": "2026-10-02T10:00:00Z",
                "workspace_uris": '["file:///Users/kamus/CascadeProjects/ruflo"]',
                "project_id": self.source_pid,
            }],
            [{"conversation_id": incoming_id, "title": "T", "workspace": "ruflo", "project": "ruflo"}],
        )

        merge_summaries_db(
            target_db,
            source_db,
            remap_workspaces=[("file:///Users/kamus", "file:///c%3A/Users/kamus")],
            project_id_map={self.source_pid: "outside-of-project"},
        )

        conn = sqlite3.connect(str(target_db))
        try:
            f = conn.execute(
                "SELECT project, workspace FROM conversations_fts WHERE conversation_id=? AND chunk_index=-1",
                (incoming_id,),
            ).fetchone()
            s_pid = conn.execute(
                "SELECT project_id FROM conversation_summaries WHERE conversation_id=?", (incoming_id,)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(s_pid, "outside-of-project")
        self.assertEqual(f, ("", ""))


if __name__ == "__main__":
    unittest.main()
