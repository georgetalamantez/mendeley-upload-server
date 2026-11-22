import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import main as server_mod


class TestBrowserUploadAPI(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.manifest_file = os.path.join(self.tmpdir.name, "manifest.json")
        self.orig_manifest = server_mod.MANIFEST_PATH
        server_mod.MANIFEST_PATH = self.manifest_file
        self.orig_app_pwd = os.environ.pop("APP_PASSWORD", None)
        self.client = TestClient(server_mod.app)

    def tearDown(self):
        server_mod.MANIFEST_PATH = self.orig_manifest
        if self.orig_app_pwd is not None:
            os.environ["APP_PASSWORD"] = self.orig_app_pwd
        else:
            os.environ.pop("APP_PASSWORD", None)
        self.tmpdir.cleanup()

    def test_health_and_frontend_served(self):
        res = self.client.get("/api/health")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"status": "ok"})

        index_res = self.client.get("/")
        self.assertEqual(index_res.status_code, 200)
        self.assertIn("Browse Folder (with Subfolders)", index_res.text)
        self.assertIn("webkitdirectory", index_res.text)

    def test_upload_pdf_and_deduplicate_by_manifest_hash(self):
        pdf_bytes = b"%PDF-1.4\nhello neuroanatomy\n"
        expected_sha1 = hashlib.sha1(pdf_bytes).hexdigest().lower()
        expected_sha256 = hashlib.sha256(pdf_bytes).hexdigest().lower()

        with mock.patch.object(server_mod, "get_access_token", return_value="fake-tok"), \
             mock.patch.object(server_mod, "find_catalog_document_by_title", return_value=None), \
             mock.patch.object(server_mod, "create_document", return_value="doc-abc-123") as mock_create, \
             mock.patch.object(
                 server_mod,
                 "upload_file_bytes",
                 return_value={"id": "file-xyz", "filehash": expected_sha1},
             ) as mock_upload:
            res = self.client.post(
                "/api/upload-file",
                files={"file": ("01_Zenker\u2019s_Diverticulum.pdf", pdf_bytes, "application/pdf")},
                data={
                    "relative_path": "Books/GI/01_Zenker\u2019s_Diverticulum.pdf",
                    "check_manifest": "true",
                    "check_catalog": "true",
                    "doc_type": "book",
                },
            )

        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["status"], "uploaded")
        self.assertEqual(body["document_id"], "doc-abc-123")
        self.assertTrue(body["hash_verified"])
        self.assertEqual(body["manifest_count"], 1)
        mock_create.assert_called_once()
        mock_upload.assert_called_once()

        # Verify manifest.json written
        with open(self.manifest_file, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertIn(expected_sha256, manifest)
        self.assertEqual(manifest[expected_sha256]["mendeley_doc_id"], "doc-abc-123")

        # Second upload of identical bytes should be skipped immediately without calling Mendeley
        with mock.patch.object(server_mod, "get_access_token", side_effect=AssertionError("should not auth")):
            res2 = self.client.post(
                "/api/upload-file",
                files={"file": ("copy.pdf", pdf_bytes, "application/pdf")},
                data={"relative_path": "AnotherSub/copy.pdf", "check_manifest": "true"},
            )
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(res2.json()["status"], "skipped")
        self.assertEqual(res2.json()["reason"], "duplicate_hash")

    def test_rejects_non_pdf_and_empty_file(self):
        res_txt = self.client.post(
            "/api/upload-file",
            files={"file": ("notes.txt", b"hello", "text/plain")},
        )
        self.assertEqual(res_txt.status_code, 400)

        res_empty = self.client.post(
            "/api/upload-file",
            files={"file": ("empty.pdf", b"", "application/pdf")},
        )
        self.assertEqual(res_empty.status_code, 400)

    def test_sanitize_header_filename_handles_unicode(self):
        ascii_name, utf8_name = server_mod.sanitize_header_filename("005_Zenker\u2019s \"Test\".pdf")
        ascii_name.encode("latin-1")  # Must not raise UnicodeEncodeError
        self.assertTrue(ascii_name.endswith(".pdf"))
        self.assertIn("%E2%80%99", utf8_name)

    def test_password_gate_when_enabled(self):
        os.environ["APP_PASSWORD"] = "super-secret-test"
        # Unauthenticated GET / returns login page
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Enter Mendeley Uploader", res.text)

        # Unauthenticated API call returns 401
        res_api = self.client.get("/api/info")
        self.assertEqual(res_api.status_code, 401)

        # Health endpoint stays open for Docker healthchecks
        res_health = self.client.get("/api/health")
        self.assertEqual(res_health.status_code, 200)

        # Wrong password rejected
        bad_login = self.client.post("/login", data={"password": "wrong"})
        self.assertEqual(bad_login.status_code, 401)

        # Correct password sets cookie and unlocks API
        ok_login = self.client.post("/login", data={"password": "super-secret-test"}, follow_redirects=False)
        self.assertEqual(ok_login.status_code, 303)
        res_info = self.client.get("/api/info")
        self.assertEqual(res_info.status_code, 200)
        self.assertTrue(res_info.json()["auth_enabled"])


if __name__ == "__main__":
    unittest.main()
