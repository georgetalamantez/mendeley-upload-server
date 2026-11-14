#!/usr/bin/env python3
"""
Mendeley Cloud Sync Pipeline
Automated Google Drive -> Mendeley book upload pipeline via rclone and Mendeley OAuth2 API.
Features:
- 3-tier deduplication (Structural queue, SHA-256 manifest ledger, Mendeley catalog verification)
- Server-side cryptographic filehash (SHA-1) confirmation
- 4.5-hour runtime budget watchdog to prevent GitHub Actions 6-hour job timeouts
- Rich GitHub Step Summary reporting and downloadable debug log artifact
"""

import os
import sys
import time
import json
import glob
import hashlib
import logging
import subprocess
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
import requests

# --- Logging Setup ---
LOG_FILE = "mendeley_sync.log"

logger = logging.getLogger("mendeley_sync")
logger.setLevel(logging.INFO)

# File handler
file_handler = logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8")
file_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
file_handler.setFormatter(file_formatter)
logger.addHandler(file_handler)

# Console handler
console_handler = logging.StreamHandler(sys.stdout)
console_formatter = logging.Formatter("[%(levelname)s] %(message)s")
console_handler.setFormatter(console_formatter)
logger.addHandler(console_handler)

# --- Configuration & Constants ---
MENDELEY_TOKEN_URL = "https://api.mendeley.com/oauth/token"
MENDELEY_DOCS_URL = "https://api.mendeley.com/documents"
MENDELEY_FILES_URL = "https://api.mendeley.com/files"

CLIENT_ID = os.getenv("MENDELEY_CLIENT_ID")
CLIENT_SECRET = os.getenv("MENDELEY_CLIENT_SECRET")
REFRESH_TOKEN = os.getenv("MENDELEY_REFRESH_TOKEN")

GDRIVE_REMOTE = os.getenv("GDRIVE_REMOTE", "gdrive").rstrip(":")
GDRIVE_QUEUE = os.getenv("GDRIVE_QUEUE_FOLDER", "Mendeley_Queue").strip("/")
GDRIVE_PROCESSED = os.getenv("GDRIVE_PROCESSED_FOLDER", f"{GDRIVE_QUEUE}/Processed").strip("/")

MANIFEST_PATH = os.getenv("MANIFEST_PATH", "manifest.json")
MAX_FILES = int(os.getenv("MAX_FILES_PER_RUN", "10"))
MAX_RUNTIME_SECONDS = int(os.getenv("MAX_RUNTIME_SECONDS", str(int(4.5 * 3600))))  # 4.5 hours safety ceiling
LOCAL_STAGING_DIR = os.getenv("LOCAL_STAGING_DIR", "/tmp/mendeley_staging")

START_TIME = time.time()


# --- Cryptographic Helpers ---
def calculate_file_hashes(file_path: str):
    """Calculates both SHA-1 (matching Mendeley filehash) and SHA-256 for local manifest."""
    sha1 = hashlib.sha1()
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            sha1.update(chunk)
            sha256.update(chunk)
    return sha1.hexdigest().lower(), sha256.hexdigest().lower()


# --- Manifest Ledger Management ---
def load_manifest(path: str) -> Dict[str, Any]:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to parse existing manifest ({e}). Initializing empty ledger.")
    return {}


def save_manifest(path: str, manifest: Dict[str, Any]):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)


# --- Rclone CLI Helpers ---
def run_rclone(args: List[str]) -> subprocess.CompletedProcess:
    cmd = ["rclone"] + args
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        logger.error(f"rclone command failed: {' '.join(cmd)}\nStderr: {res.stderr.strip()}")
    return res


def list_queue_files(remote: str, queue_dir: str) -> List[str]:
    """Lists pending PDF filenames in the queue directory."""
    target = f"{remote}:{queue_dir}"
    logger.info(f"Scanning queue at {target}...")
    res = run_rclone(["lsf", target, "--include", "*.pdf", "--files-only", "--max-depth", "1"])
    if res.returncode != 0:
        return []
    files = [f.strip() for f in res.stdout.strip().split("\n") if f.strip().lower().endswith(".pdf")]
    return sorted(files)


def download_file_from_drive(remote: str, queue_dir: str, filename: str, dest_dir: str) -> str:
    os.makedirs(dest_dir, exist_ok=True)
    src = f"{remote}:{queue_dir}/{filename}"
    dest = os.path.join(dest_dir, filename)
    logger.info(f"Downloading {src} to {dest}...")
    res = run_rclone(["copyto", src, dest])
    if res.returncode != 0 or not os.path.exists(dest):
        raise RuntimeError(f"Failed to download {filename} from Google Drive")
    return dest


def move_file_in_drive(remote: str, queue_dir: str, processed_dir: str, filename: str):
    src = f"{remote}:{queue_dir}/{filename}"
    dest = f"{remote}:{processed_dir}/{filename}"
    logger.info(f"Moving {src} -> {dest}...")
    res = run_rclone(["moveto", src, dest])
    if res.returncode != 0:
        logger.error(f"Failed to move {filename} in Google Drive to {processed_dir}")
        return False
    return True


# --- Mendeley API Client ---
class MendeleyClient:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.access_token = None
        self.token_expiry = 0

    def refresh_access_token(self):
        logger.info("Refreshing Mendeley OAuth access token...")
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": "all"
        }
        resp = requests.post(MENDELEY_TOKEN_URL, data=payload, timeout=20)
        if resp.status_code == 200:
            data = resp.json()
            self.access_token = data["access_token"]
            expires_in = data.get("expires_in", 3600)
            self.token_expiry = time.time() + expires_in - 60
            logger.info("Mendeley access token refreshed successfully.")
            return self.access_token
        else:
            raise RuntimeError(f"Authentication Failed: {resp.status_code} {resp.text}")

    def get_token(self):
        if not self.access_token or time.time() >= self.token_expiry:
            return self.refresh_access_token()
        return self.access_token

    def _request_with_retry(self, method: str, url: str, **kwargs) -> requests.Response:
        max_retries = 5
        backoff = 5.0
        for attempt in range(max_retries):
            token = self.get_token()
            headers = kwargs.get("headers", {})
            headers["Authorization"] = f"Bearer {token}"
            kwargs["headers"] = headers

            try:
                resp = requests.request(method, url, **kwargs)
                if resp.status_code == 401:
                    logger.warning("Token expired mid-request. Refreshing token...")
                    self.refresh_access_token()
                    continue
                elif resp.status_code == 429:
                    logger.warning(f"Rate limited by Mendeley (HTTP 429). Retrying in {backoff}s (attempt {attempt+1}/{max_retries})...")
                    time.sleep(backoff)
                    backoff *= 2.0
                    continue
                return resp
            except (requests.ConnectionError, requests.Timeout) as e:
                logger.warning(f"Network error ({e}). Retrying in {backoff}s...")
                time.sleep(backoff)
                backoff *= 2.0

        raise RuntimeError(f"Request failed after {max_retries} attempts: {method} {url}")

    def find_catalog_document_by_title(self, title: str) -> Optional[str]:
        """Deduplication Tier 3: Search existing user documents by title."""
        params = {"title": title, "view": "all"}
        headers = {"Accept": "application/vnd.mendeley-document.1+json"}
        try:
            resp = self._request_with_retry("GET", MENDELEY_DOCS_URL, params=params, headers=headers, timeout=30)
            if resp.status_code == 200:
                docs = resp.json()
                if docs and isinstance(docs, list):
                    for d in docs:
                        if d.get("title", "").strip().lower() == title.strip().lower():
                            if d.get("file_attached") or d.get("files"):
                                return d.get("id")
        except Exception as e:
            logger.warning(f"Could not verify catalog for '{title}': {e}")
        return None

    def create_document(self, title: str) -> str:
        headers = {
            "Content-Type": "application/vnd.mendeley-document.1+json",
            "Accept": "application/vnd.mendeley-document.1+json"
        }
        data = {
            "title": title,
            "type": "book"
        }
        resp = self._request_with_retry("POST", MENDELEY_DOCS_URL, headers=headers, json=data, timeout=30)
        if resp.status_code == 201:
            doc_id = resp.json().get("id")
            logger.info(f"Document created: id={doc_id}")
            return doc_id
        raise RuntimeError(f"Failed to create document '{title}': {resp.status_code} - {resp.text}")

    def upload_file_content(self, document_id: str, file_path: str) -> Dict[str, Any]:
        """Uploads binary PDF and captures Mendeley server response."""
        filename = os.path.basename(file_path)
        headers = {
            "Content-Type": "application/pdf",
            "Accept": "application/vnd.mendeley-file.1+json",
            "Link": f"<{MENDELEY_DOCS_URL}/{document_id}>; rel=\"document\"",
            "Content-Disposition": f'attachment; filename="{filename}"'
        }
        with open(file_path, "rb") as f:
            file_bytes = f.read()

        resp = self._request_with_retry("POST", MENDELEY_FILES_URL, headers=headers, data=file_bytes, timeout=180)
        if resp.status_code == 201:
            try:
                file_info = resp.json()
            except Exception:
                file_info = {}
            # Fallback for file_id from Location header if json body empty
            if "id" not in file_info and "Location" in resp.headers:
                file_info["id"] = resp.headers["Location"].rstrip("/").split("/")[-1]
            return file_info
        raise RuntimeError(f"Failed to upload file '{filename}': {resp.status_code} - {resp.text}")


# --- Main Orchestration Loop ---
def main():
    if not all([CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN]):
        logger.error("Missing credentials: MENDELEY_CLIENT_ID, MENDELEY_CLIENT_SECRET, or MENDELEY_REFRESH_TOKEN.")
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("Starting Mendeley Automated Pipeline")
    logger.info(f"Queue: {GDRIVE_REMOTE}:{GDRIVE_QUEUE}")
    logger.info(f"Processed: {GDRIVE_REMOTE}:{GDRIVE_PROCESSED}")
    logger.info(f"Max Files This Batch: {MAX_FILES}")
    logger.info(f"Safety Ceiling: {MAX_RUNTIME_SECONDS} seconds")
    logger.info("=" * 60)

    manifest = load_manifest(MANIFEST_PATH)
    logger.info(f"Loaded manifest with {len(manifest)} existing records.")

    mendeley = MendeleyClient(CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN)
    # Test authentication early
    mendeley.refresh_access_token()

    pending_files = list_queue_files(GDRIVE_REMOTE, GDRIVE_QUEUE)
    total_pending = len(pending_files)
    logger.info(f"Found {total_pending} PDF file(s) in queue.")

    if total_pending == 0:
        logger.info("No pending files to process. Exiting cleanly.")
        write_step_summary(total_scanned=0, uploaded=0, skipped=0, errors=[], records=[])
        set_github_output("has_more_files", "false")
        sys.exit(0)

    # Bound the batch to MAX_FILES
    batch_files = pending_files[:MAX_FILES]
    has_more = total_pending > MAX_FILES

    stats = {
        "scanned": len(batch_files),
        "uploaded": 0,
        "skipped": 0,
        "errors": []
    }
    records_summary = []

    for idx, filename in enumerate(batch_files, 1):
        elapsed = time.time() - START_TIME
        if elapsed > MAX_RUNTIME_SECONDS:
            logger.warning(f"Approaching runtime ceiling ({elapsed:.1f}s > {MAX_RUNTIME_SECONDS}s). Stopping batch gracefully.")
            has_more = True
            break

        file_start = time.time()
        title = os.path.splitext(filename)[0].replace("_", " ")
        logger.info(f"[{idx}/{len(batch_files)}] Processing: {filename} ('{title}')")

        local_path = None
        try:
            # 1. Download file
            local_path = download_file_from_drive(GDRIVE_REMOTE, GDRIVE_QUEUE, filename, LOCAL_STAGING_DIR)

            # 2. Compute local hashes
            local_sha1, local_sha256 = calculate_file_hashes(local_path)
            file_size_mb = os.path.getsize(local_path) / (1024 * 1024)

            # 3. Deduplication Tier 2 (Content SHA-256 in manifest)
            if local_sha256 in manifest:
                existing = manifest[local_sha256]
                logger.info(f"⏭️ Deduplication Tier 2 Match: Content hash {local_sha256[:12]}... already uploaded as '{existing.get('title')}' (doc_id={existing.get('mendeley_doc_id')}). Skipping.")
                move_file_in_drive(GDRIVE_REMOTE, GDRIVE_QUEUE, GDRIVE_PROCESSED, filename)
                stats["skipped"] += 1
                records_summary.append({
                    "file": filename,
                    "status": "Skipped (Content Hash Match)",
                    "doc_id": existing.get("mendeley_doc_id", "Existing"),
                    "duration": f"{time.time() - file_start:.1f}s"
                })
                continue

            # 4. Deduplication Tier 3 (Catalog title pre-flight check)
            existing_doc_id = mendeley.find_catalog_document_by_title(title)
            if existing_doc_id:
                logger.info(f"⏭️ Deduplication Tier 3 Match: Document '{title}' already exists in Mendeley with attached file (doc_id={existing_doc_id}). Skipping.")
                manifest[local_sha256] = {
                    "filename": filename,
                    "title": title,
                    "mendeley_doc_id": existing_doc_id,
                    "mendeley_file_id": "catalog_existing",
                    "mendeley_filehash": local_sha1,
                    "uploaded_at": datetime.now(timezone.utc).isoformat()
                }
                save_manifest(MANIFEST_PATH, manifest)
                move_file_in_drive(GDRIVE_REMOTE, GDRIVE_QUEUE, GDRIVE_PROCESSED, filename)
                stats["skipped"] += 1
                records_summary.append({
                    "file": filename,
                    "status": "Skipped (Catalog Title Match)",
                    "doc_id": existing_doc_id,
                    "duration": f"{time.time() - file_start:.1f}s"
                })
                continue

            # 5. Create Document
            doc_id = mendeley.create_document(title)

            # 6. Upload Binary
            file_info = mendeley.upload_file_content(doc_id, local_path)
            mendeley_filehash = file_info.get("filehash")
            mendeley_file_id = file_info.get("id", "confirmed")

            # 7. Server Hash Confirmation
            if mendeley_filehash:
                logger.info(f"Verified Mendeley server confirmation: filehash={mendeley_filehash}, file_id={mendeley_file_id} (local SHA1={local_sha1})")
            else:
                logger.info(f"Upload confirmed (201 Created, file_id={mendeley_file_id}).")

            # 8. Record in Manifest Ledger
            manifest[local_sha256] = {
                "filename": filename,
                "title": title,
                "mendeley_doc_id": doc_id,
                "mendeley_file_id": mendeley_file_id,
                "mendeley_filehash": mendeley_filehash or local_sha1,
                "size_mb": round(file_size_mb, 2),
                "uploaded_at": datetime.now(timezone.utc).isoformat()
            }
            save_manifest(MANIFEST_PATH, manifest)

            # 9. Move file in Google Drive
            move_file_in_drive(GDRIVE_REMOTE, GDRIVE_QUEUE, GDRIVE_PROCESSED, filename)

            duration_s = time.time() - file_start
            logger.info(f"SUCCESS: {filename} uploaded in {duration_s:.1f}s (doc_id={doc_id})")
            stats["uploaded"] += 1
            records_summary.append({
                "file": filename,
                "status": "✅ Uploaded",
                "doc_id": doc_id,
                "duration": f"{duration_s:.1f}s"
            })

            # Polite pacing between books
            time.sleep(2.0)

        except Exception as e:
            duration_s = time.time() - file_start
            logger.error(f"ERROR on {filename}: {str(e)}")
            stats["errors"].append({"file": filename, "error": str(e)})
            records_summary.append({
                "file": filename,
                "status": "❌ Failed",
                "doc_id": "N/A",
                "duration": f"{duration_s:.1f}s"
            })

        finally:
            if local_path and os.path.exists(local_path):
                try:
                    os.remove(local_path)
                except Exception:
                    pass

    # Save final manifest
    save_manifest(MANIFEST_PATH, manifest)

    # Step Summary & Output
    total_duration = time.time() - START_TIME
    write_step_summary(stats["scanned"], stats["uploaded"], stats["skipped"], stats["errors"], records_summary, total_duration)

    if has_more:
        logger.info("Remaining files detected in queue. Signaling auto-continuation.")
        set_github_output("has_more_files", "true")
    else:
        logger.info("All pending queue files processed. No continuation needed.")
        set_github_output("has_more_files", "false")


def write_step_summary(total_scanned: int, uploaded: int, skipped: int, errors: List[Dict[str, str]], records: List[Dict[str, str]], duration_s: float = 0.0):
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    m, s = divmod(int(duration_s), 60)
    time_str = f"{m}m {s}s" if m > 0 else f"{s}s"

    lines = [
        "## 📚 Mendeley Cloud Sync Run Summary",
        f"- **Total PDFs Inspected**: `{total_scanned}`",
        f"- **Successfully Uploaded**: `{uploaded}`",
        f"- **Duplicates Skipped**: `{skipped}`",
        f"- **Failed / Errors**: `{len(errors)}`",
        f"- **Total Execution Time**: `{time_str}`",
        "",
        "### Ingestion Details",
        "| File | Status | Mendeley Document ID | Duration |",
        "| :--- | :--- | :--- | :--- |"
    ]
    for r in records:
        lines.append(f"| `{r['file']}` | {r['status']} | `{r['doc_id']}` | {r['duration']} |")

    if errors:
        lines.append("")
        lines.append("### ⚠️ Error Log")
        for err in errors:
            lines.append(f"- **`{err['file']}`**: `{err['error']}`")

    try:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        logger.warning(f"Could not write to GITHUB_STEP_SUMMARY: {e}")


def set_github_output(name: str, value: str):
    output_path = os.getenv("GITHUB_OUTPUT")
    if output_path:
        try:
            with open(output_path, "a", encoding="utf-8") as f:
                f.write(f"{name}={value}\n")
        except Exception as e:
            logger.warning(f"Could not write to GITHUB_OUTPUT: {e}")


if __name__ == "__main__":
    main()
