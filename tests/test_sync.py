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


if __name__ == "__main__":
    unittest.main()
