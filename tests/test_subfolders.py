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
