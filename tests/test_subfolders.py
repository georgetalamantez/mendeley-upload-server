import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Ensure repository root is importable
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import sync_drive_to_mendeley as sync_mod
import main as server_mod


def _write_file(base: Path, rel: str, content: bytes = b"%PDF-1.4\n") -> Path:
    full = base / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(content)
    return full


class TestDriveSyncSubfolders(unittest.TestCase):
    def test_is_excluded_rel_path(self):
        self.assertTrue(sync_mod.is_excluded_rel_path("Processed/book.pdf"))
        self.assertTrue(sync_mod.is_excluded_rel_path("processed/book.pdf"))
        self.assertTrue(sync_mod.is_excluded_rel_path("SubA/Processed/book.pdf"))
        self.assertTrue(sync_mod.is_excluded_rel_path("Quarantined/bad.pdf"))
        self.assertTrue(sync_mod.is_excluded_rel_path("SubA/quarantined/bad.pdf"))
        self.assertFalse(sync_mod.is_excluded_rel_path("SubA/Processed_Notes/book.pdf"))
        self.assertFalse(sync_mod.is_excluded_rel_path("SubA/Nested/book.pdf"))
        self.assertFalse(sync_mod.is_excluded_rel_path("Processed.pdf"))

    def test_rclone_recursive_list_download_and_flat_move_with_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue_dir = root / "queue"
            staging_dir = root / "staging"
            processed_dir = queue_dir / "Processed"

            _write_file(queue_dir, "top.pdf", b"%PDF top")
            _write_file(queue_dir, "BookA/01_Chapter [1].pdf", b"%PDF bookA ch1")
            _write_file(queue_dir, "BookA/Deep/02_Upper.PDF", b"%PDF bookA ch2")
            _write_file(queue_dir, "BookB/01_Chapter [1].pdf", b"%PDF bookB ch1 DIFFERENT")
            _write_file(queue_dir, "BookA/notes.txt", b"ignore me")
            _write_file(queue_dir, "Processed/already_done.pdf", b"%PDF done")
            _write_file(queue_dir, "BookA/Processed/nested_done.pdf", b"%PDF nested done")
            _write_file(queue_dir, "Quarantined/bad.pdf", b"%PDF bad")
            _write_file(queue_dir, "BookB/Quarantined/bad2.pdf", b"%PDF bad2")

            found = sync_mod.list_queue_files("", str(queue_dir))
            self.assertEqual(
                found,
                [
                    "BookA/01_Chapter [1].pdf",
                    "BookA/Deep/02_Upper.PDF",
                    "BookB/01_Chapter [1].pdf",
                    "top.pdf",
                ],
            )

            # Download nested file into staging and verify cleanup prunes empty subdirs
            local_a = sync_mod.download_file_from_drive("", str(queue_dir), "BookA/01_Chapter [1].pdf", str(staging_dir))
            self.assertTrue(os.path.isfile(local_a))
            self.assertEqual(os.path.basename(local_a), "01_Chapter [1].pdf")
            _, sha256_a = sync_mod.calculate_file_hashes(local_a)
            sync_mod.cleanup_staging_file(local_a, str(staging_dir))
            self.assertFalse(os.path.exists(local_a))
            self.assertFalse((staging_dir / "BookA").exists())

            # Move BookA/01_Chapter [1].pdf flat into Processed/
            ok_a = sync_mod.move_file_in_drive(
                "", str(queue_dir), str(processed_dir), "BookA/01_Chapter [1].pdf", content_hash=sha256_a
            )
            self.assertTrue(ok_a)
            self.assertTrue((processed_dir / "01_Chapter [1].pdf").is_file())
            self.assertEqual((processed_dir / "01_Chapter [1].pdf").read_bytes(), b"%PDF bookA ch1")
            self.assertFalse((queue_dir / "BookA/01_Chapter [1].pdf").exists())

            # Now move BookB/01_Chapter [1].pdf -> same basename collision must NOT overwrite BookA's file!
            local_b = sync_mod.download_file_from_drive("", str(queue_dir), "BookB/01_Chapter [1].pdf", str(staging_dir))
            _, sha256_b = sync_mod.calculate_file_hashes(local_b)
            sync_mod.cleanup_staging_file(local_b, str(staging_dir))

            ok_b = sync_mod.move_file_in_drive(
                "", str(queue_dir), str(processed_dir), "BookB/01_Chapter [1].pdf", content_hash=sha256_b
            )
            self.assertTrue(ok_b)
            expected_b_name = f"01_Chapter [1]_{sha256_b[:8]}.pdf"
            self.assertTrue((processed_dir / "01_Chapter [1].pdf").is_file())
            self.assertEqual((processed_dir / "01_Chapter [1].pdf").read_bytes(), b"%PDF bookA ch1")
            self.assertTrue((processed_dir / expected_b_name).is_file())
            self.assertEqual((processed_dir / expected_b_name).read_bytes(), b"%PDF bookB ch1 DIFFERENT")
            self.assertFalse((queue_dir / "BookB/01_Chapter [1].pdf").exists())

    def test_download_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                sync_mod.download_file_from_drive("", tmp, "../../etc/passwd.pdf", tmp)

    def test_rclone_rate_limit_backoff_and_retry(self):
        self.assertTrue(
            sync_mod.is_rate_limit_error(
                "googleapi: Error 403: Quota exceeded for quota metric 'Queries', rateLimitExceeded"
            )
        )
        self.assertFalse(sync_mod.is_rate_limit_error("file not found"))

        fake_403 = mock.Mock(returncode=1, stdout="", stderr="googleapi: Error 403: Quota exceeded, rateLimitExceeded")
        fake_ok = mock.Mock(returncode=0, stdout="ok.pdf\n", stderr="")

        with mock.patch.object(sync_mod.subprocess, "run", side_effect=[fake_403, fake_403, fake_ok]) as m_run, \
             mock.patch.object(sync_mod.time, "sleep") as m_sleep:
            res = sync_mod.run_rclone(["lsf", "gdrive:Mendeley_Queue"], max_retries=3, backoff_base=5.0)

        self.assertEqual(res.returncode, 0)
        self.assertEqual(m_run.call_count, 3)
        self.assertEqual(m_sleep.call_count, 2)
        # Verify pacing flags were injected for remote call
        called_cmd = m_run.call_args_list[0].args[0]
        self.assertIn("--tpslimit", called_cmd)
        self.assertIn("--drive-pacer-min-sleep", called_cmd)

    def test_resolved_processed_name_uses_known_cache_without_rclone(self):
        cache = {"01_Chapter.pdf"}
        with mock.patch.object(sync_mod, "remote_file_exists", side_effect=AssertionError("should use cache")):
            name = sync_mod.resolve_flattened_processed_name(
                "gdrive", "Mendeley_Queue/Processed", "01_Chapter.pdf", content_hash="abcdef123456", known_processed=cache
            )
        self.assertEqual(name, "01_Chapter_abcdef12.pdf")

    def test_batch_download_from_drive_nested_and_special_chars(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue_dir = root / "queue"
            staging_dir = root / "staging"
            _write_file(queue_dir, "top.pdf", b"%PDF top")
            _write_file(queue_dir, "BookA/01_Chapter [1], Part (A).pdf", b"%PDF special")
            _write_file(queue_dir, "BookB/ignored.pdf", b"%PDF ignored")

            staged = sync_mod.batch_download_from_drive(
                "",
                str(queue_dir),
                ["top.pdf", "BookA/01_Chapter [1], Part (A).pdf"],
                str(staging_dir),
            )
            self.assertEqual(set(staged.keys()), {"top.pdf", "BookA/01_Chapter [1], Part (A).pdf"})
            self.assertEqual(Path(staged["top.pdf"]).read_bytes(), b"%PDF top")
            self.assertEqual(Path(staged["BookA/01_Chapter [1], Part (A).pdf"]).read_bytes(), b"%PDF special")
            self.assertFalse((staging_dir / "BookB").exists())

    def test_move_file_in_drive_uses_rc_daemon_and_falls_back(self):
        fake_daemon = mock.Mock()
        fake_daemon.base_url = "http://127.0.0.1:9999"
        fake_daemon.move_file.return_value = True
        cache = set()

        with mock.patch.object(sync_mod, "run_rclone", side_effect=AssertionError("should use daemon")):
            ok = sync_mod.move_file_in_drive(
                "gdrive",
                "Mendeley_Queue",
                "Mendeley_Queue/Processed",
                "Nested/ch1.pdf",
                content_hash="deadbeef",
                known_processed=cache,
                rc_daemon=fake_daemon,
            )
        self.assertTrue(ok)
        fake_daemon.move_file.assert_called_once_with(
            "gdrive:Mendeley_Queue", "Nested/ch1.pdf", "gdrive:Mendeley_Queue/Processed", "ch1.pdf"
        )
        self.assertIn("ch1.pdf", cache)

        # Verify fallback to subprocess moveto when daemon returns False
        fake_daemon.move_file.return_value = False
        with mock.patch.object(sync_mod, "run_rclone", return_value=mock.Mock(returncode=0)) as m_rc:
            ok2 = sync_mod.move_file_in_drive(
                "gdrive",
                "Mendeley_Queue",
                "Mendeley_Queue/Processed",
                "Nested/ch2.pdf",
                content_hash="cafebabe",
                known_processed=cache,
                rc_daemon=fake_daemon,
            )
        self.assertTrue(ok2)
        m_rc.assert_called_once_with(["moveto", "gdrive:Mendeley_Queue/Nested/ch2.pdf", "gdrive:Mendeley_Queue/Processed/ch2.pdf"])


class TestLocalUploaderSubfolders(unittest.TestCase):
    def test_collect_pdf_files_recursive_and_non_recursive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_file(root, "alpha.pdf")
            _write_file(root, "BETA.PDF")
            _write_file(root, "ignore.txt")
            _write_file(root, "Sub1/gamma.pdf")
            _write_file(root, "Sub1/Deep/delta.PDF")
            _write_file(root, "Processed/skip1.pdf")
            _write_file(root, "Sub1/processed/skip2.pdf")
            _write_file(root, "Quarantined/skip3.pdf")
            _write_file(root, "Sub1/Quarantined/skip4.pdf")

            rec = [os.path.relpath(p, root) for p in server_mod.collect_pdf_files(str(root), recursive=True)]
            self.assertEqual(
                rec,
                [
                    "BETA.PDF",
                    "alpha.pdf",
                    os.path.join("Sub1", "gamma.pdf"),
                    os.path.join("Sub1", "Deep", "delta.PDF"),
                ],
            )

            flat = [os.path.relpath(p, root) for p in server_mod.collect_pdf_files(str(root), recursive=False)]
            self.assertEqual(flat, ["BETA.PDF", "alpha.pdf"])

            single = server_mod.collect_pdf_files(str(root / "Sub1/gamma.pdf"))
            self.assertEqual(single, [str(root / "Sub1/gamma.pdf")])

    def test_process_upload_task_uses_basename_title_and_uploads_nested(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_file(root, "Nested/01_My_Book.pdf", b"%PDF-1.4 test")
            _write_file(root, "Processed/ignored.pdf", b"%PDF-1.4 ignored")

            created_titles = []
            uploaded_paths = []

            def fake_create(token, title):
                created_titles.append(title)
                return "doc-123"

            def fake_upload(token, doc_id, file_path):
                uploaded_paths.append(file_path)
                return True

            with mock.patch.object(server_mod, "get_access_token", return_value="tok"), \
                 mock.patch.object(server_mod, "create_document", side_effect=fake_create), \
                 mock.patch.object(server_mod, "upload_file_content", side_effect=fake_upload), \
                 mock.patch.object(server_mod.time, "sleep"):
                server_mod.process_upload_task(str(root), recursive=True)

            self.assertEqual(created_titles, ["01 My Book"])
            self.assertEqual(uploaded_paths, [str(root / "Nested/01_My_Book.pdf")])
            self.assertEqual(server_mod.state.total_files, 1)
            self.assertEqual(server_mod.state.processed_files, 1)


if __name__ == "__main__":
    unittest.main()
