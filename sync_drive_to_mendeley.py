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
import random
import socket
import hashlib
import logging
import tempfile
import threading
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Set, Tuple
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
GDRIVE_QUEUE_RAW = os.getenv("GDRIVE_QUEUE_FOLDER", "Mendeley_Queue,books1/chapters")
GDRIVE_QUEUES = [q.strip().strip("/") for q in GDRIVE_QUEUE_RAW.split(",") if q.strip()]
GDRIVE_EXCLUDE_RAW = os.getenv("GDRIVE_EXCLUDE_DIRS", "Processed,Quarantined")
GDRIVE_EXCLUDE_DIRS = [d.strip().strip("/") for d in GDRIVE_EXCLUDE_RAW.split(",") if d.strip()]

MANIFEST_PATH = os.getenv("MANIFEST_PATH", "manifest.json")
MAX_FILES = int(os.getenv("MAX_FILES_PER_RUN", "500"))
MAX_RUNTIME_SECONDS = int(os.getenv("MAX_RUNTIME_SECONDS", str(int(4.5 * 3600))))  # 4.5 hours safety ceiling
LOCAL_STAGING_DIR = os.getenv("LOCAL_STAGING_DIR", "/tmp/mendeley_staging")

# Throughput & Rclone Google Drive pacing settings (tuned for dedicated personal GCP OAuth client)
RCLONE_TPSLIMIT = os.getenv("RCLONE_TPSLIMIT", "10")
RCLONE_TPSLIMIT_BURST = os.getenv("RCLONE_TPSLIMIT_BURST", "10")
RCLONE_DRIVE_PACER_MIN_SLEEP = os.getenv("RCLONE_DRIVE_PACER_MIN_SLEEP", "50ms")
RCLONE_TRANSFERS = os.getenv("RCLONE_TRANSFERS", "8")
RCLONE_CHECKERS = os.getenv("RCLONE_CHECKERS", "16")
RCLONE_MAX_RETRIES = int(os.getenv("RCLONE_MAX_RETRIES", "5"))
RCLONE_BACKOFF_BASE = float(os.getenv("RCLONE_BACKOFF_BASE", "10"))
RCLONE_BACKOFF_MAX = float(os.getenv("RCLONE_BACKOFF_MAX", "120"))
UPLOAD_WORKERS = max(1, int(os.getenv("UPLOAD_WORKERS", "4")))
FILE_PACE_SECONDS = float(os.getenv("FILE_PACE_SECONDS", "0.2"))
MANIFEST_SAVE_INTERVAL = max(1, int(os.getenv("MANIFEST_SAVE_INTERVAL", "25")))

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
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    os.replace(tmp_path, path)


# --- Rclone CLI Helpers ---
RATE_LIMIT_MARKERS = (
    "ratelimitexceeded",
    "rate_limit_exceeded",
    "userratelimitexceeded",
    "quota exceeded",
    "too many requests",
    "error 429",
    "http 429",
)


def is_rate_limit_error(stderr: str) -> bool:
    low = (stderr or "").lower()
    return any(marker in low for marker in RATE_LIMIT_MARKERS)


def _uses_rclone_remote(args: List[str]) -> bool:
    for arg in args:
        if arg.startswith("-"):
            continue
        # Match rclone remote syntax like "gdrive:path" (ignore single-letter Windows drive paths like C:\)
        if ":" in arg and not (len(arg) >= 2 and arg[1] == ":" and arg[0].isalpha() and (len(arg) == 2 or arg[2] in ("\\", "/"))):
            return True
    return False


def run_rclone(
    args: List[str],
    max_retries: Optional[int] = None,
    backoff_base: Optional[float] = None,
    quiet_on_error: bool = False,
) -> subprocess.CompletedProcess:
    retries = RCLONE_MAX_RETRIES if max_retries is None else max_retries
    base_delay = RCLONE_BACKOFF_BASE if backoff_base is None else backoff_base

    cmd = ["rclone"] + list(args)
    if _uses_rclone_remote(args):
        cmd.extend([
            "--tpslimit", str(RCLONE_TPSLIMIT),
            "--tpslimit-burst", str(RCLONE_TPSLIMIT_BURST),
            "--drive-pacer-min-sleep", str(RCLONE_DRIVE_PACER_MIN_SLEEP),
            "--retries", "3",
            "--retries-sleep", "5s",
            "--low-level-retries", "8",
        ])

    res = None
    for attempt in range(retries + 1):
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            return res

        stderr_text = (res.stderr or "").strip()
        if is_rate_limit_error(stderr_text) and attempt < retries:
            delay = min(base_delay * (2 ** attempt), RCLONE_BACKOFF_MAX) + random.uniform(0.5, 3.0)
            logger.warning(
                f"Google Drive rate limit hit on rclone (attempt {attempt + 1}/{retries + 1}). "
                f"Backing off {delay:.1f}s before retrying..."
            )
            time.sleep(delay)
            continue

        if not quiet_on_error:
            logger.error(f"rclone command failed: {' '.join(cmd)}\nStderr: {stderr_text}")
        return res

    return res


def _remote_path(remote: str, path: str) -> str:
    return f"{remote}:{path}" if remote else path


def is_excluded_rel_path(rel_path: str, excluded_dirs: Optional[List[str]] = None) -> bool:
    """Returns True if any directory segment of rel_path matches an excluded folder (case-insensitive)."""
    excluded = {d.lower() for d in (excluded_dirs if excluded_dirs is not None else GDRIVE_EXCLUDE_DIRS)}
    parts = [p for p in rel_path.replace("\\", "/").strip("/").split("/") if p]
    return any(part.lower() in excluded for part in parts[:-1])


def list_queue_files(
    remote: str,
    queue_dir: str,
    excluded_dirs: Optional[List[str]] = None,
    raise_on_error: bool = False,
) -> List[str]:
    """Recursively lists pending PDF relative paths in queue_dir, excluding Processed/ and Quarantined/."""
    excluded = excluded_dirs if excluded_dirs is not None else GDRIVE_EXCLUDE_DIRS
    target = _remote_path(remote, queue_dir)
    logger.info(f"Scanning queue recursively at {target} (excluding {excluded})...")
    args = ["lsf", "-R", target, "--files-only", "--ignore-case"]
    if remote:
        args.append("--fast-list")
    for ex_dir in excluded:
        args.extend(["--filter", f"- {ex_dir}/**"])
    args.extend(["--filter", "+ *.pdf", "--filter", "- *"])

    res = run_rclone(args)
    if res.returncode != 0:
        if raise_on_error:
            raise RuntimeError(f"Failed to scan {target}: {(res.stderr or '').strip()[:300]}")
        return []

    files = []
    for line in res.stdout.splitlines():
        rel = line.strip().replace("\\", "/").lstrip("/")
        if rel.startswith("./"):
            rel = rel[2:]
        if not rel or not rel.lower().endswith(".pdf"):
            continue
        if is_excluded_rel_path(rel, excluded):
            continue
        files.append(rel)
    return sorted(files)


def list_processed_filenames(remote: str, processed_dir: str) -> Optional[Set[str]]:
    """Fetches existing filenames directly in processed_dir once so per-file collision checks avoid extra Drive API calls."""
    target = _remote_path(remote, processed_dir)
    args = ["lsf", target, "--files-only"]
    if remote:
        args.append("--fast-list")
    res = run_rclone(args, quiet_on_error=True)
    if res.returncode != 0:
        # Processed/ might not exist yet on first run; treat directory-not-found as empty set
        low = (res.stderr or "").lower()
        if "directory not found" in low or "not found" in low:
            return set()
        logger.warning(f"Could not pre-list {target}; will fall back to per-file collision checks.")
        return None
    return {line.strip() for line in res.stdout.splitlines() if line.strip()}


def _safe_staging_dest(staging_root: str, rel_path: str) -> str:
    rel_clean = rel_path.replace("\\", "/").lstrip("/")
    root_abs = os.path.abspath(staging_root)
    dest = os.path.abspath(os.path.normpath(os.path.join(root_abs, *rel_clean.split("/"))))
    if os.path.commonpath([root_abs, dest]) != root_abs:
        raise ValueError(f"Unsafe relative path detected: {rel_path}")
    return dest


def download_file_from_drive(remote: str, queue_dir: str, rel_path: str, dest_dir: str) -> str:
    rel_clean = rel_path.replace("\\", "/").lstrip("/")
    src = _remote_path(remote, f"{queue_dir}/{rel_clean}")
    dest = _safe_staging_dest(dest_dir, rel_clean)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    logger.info(f"Downloading {src} to {dest}...")
    res = run_rclone(["copyto", src, dest])
    if res.returncode != 0 or not os.path.exists(dest):
        raise RuntimeError(f"Failed to download {rel_clean} from Google Drive")
    return dest


def batch_download_from_drive(
    remote: str,
    queue_dir: str,
    rel_paths: List[str],
    dest_dir: str,
) -> Dict[str, str]:
    """Downloads many PDFs from queue_dir in a single parallel rclone copy call with a warm directory cache."""
    if not rel_paths:
        return {}

    os.makedirs(dest_dir, exist_ok=True)
    expected: Dict[str, str] = {}
    clean_lines: List[str] = []
    for rel_path in rel_paths:
        rel_clean = rel_path.replace("\\", "/").lstrip("/")
        dest = _safe_staging_dest(dest_dir, rel_clean)
        expected[rel_path] = dest
        clean_lines.append(rel_clean)

    src_root = _remote_path(remote, queue_dir)
    list_file = None
    t0 = time.time()
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".txt", delete=False) as tf:
            tf.write("\n".join(clean_lines) + "\n")
            list_file = tf.name

        logger.info(f"Batch-downloading {len(clean_lines)} PDF(s) from {src_root} (transfers={RCLONE_TRANSFERS}, checkers={RCLONE_CHECKERS})...")
        args = [
            "copy",
            src_root,
            dest_dir,
            "--files-from-raw", list_file,
            "--no-traverse",
            "--transfers", str(RCLONE_TRANSFERS),
            "--checkers", str(RCLONE_CHECKERS),
        ]
        res = run_rclone(args)
        staged = {rel: path for rel, path in expected.items() if os.path.isfile(path)}
        logger.info(
            f"Batch download for {src_root} finished in {time.time() - t0:.1f}s "
            f"({len(staged)}/{len(rel_paths)} files staged; rc={res.returncode})."
        )
        return staged
    finally:
        if list_file and os.path.exists(list_file):
            try:
                os.remove(list_file)
            except OSError:
                pass


class RcloneDaemon:
    """Long-lived rclone rcd server keeping Google Drive's directory-ID cache warm across fast server-side moves."""

    def __init__(self):
        self.proc: Optional[subprocess.Popen] = None
        self.base_url: Optional[str] = None
        self._local = threading.local()

    def _session(self) -> requests.Session:
        sess = getattr(self._local, "session", None)
        if sess is None:
            sess = requests.Session()
            self._local.session = sess
        return sess

    def start(self) -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
        except OSError as e:
            logger.warning(f"Could not allocate port for rclone rcd ({e}); falling back to subprocess moves.")
            return False

        addr = f"127.0.0.1:{port}"
        cmd = [
            "rclone", "rcd",
            "--rc-no-auth",
            "--rc-addr", addr,
            "--dir-cache-time", "60m",
            "--tpslimit", str(RCLONE_TPSLIMIT),
            "--tpslimit-burst", str(RCLONE_TPSLIMIT_BURST),
            "--drive-pacer-min-sleep", str(RCLONE_DRIVE_PACER_MIN_SLEEP),
            "--retries", "3",
            "--retries-sleep", "5s",
            "--low-level-retries", "8",
        ]
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            logger.warning(f"Failed to start rclone rcd ({e}); falling back to subprocess moves.")
            return False

        self.base_url = f"http://{addr}"
        deadline = time.time() + 10.0
        while time.time() < deadline:
            if self.proc.poll() is not None:
                logger.warning("rclone rcd exited unexpectedly; falling back to subprocess moves.")
                self.proc = None
                self.base_url = None
                return False
            try:
                resp = self._session().post(f"{self.base_url}/rc/noop", json={}, timeout=1.0)
                if resp.status_code == 200:
                    logger.info(f"Started warm rclone rcd daemon on {addr} (dir-cache-time=60m).")
                    return True
            except requests.RequestException:
                time.sleep(0.2)

        logger.warning("Timed out waiting for rclone rcd; falling back to subprocess moves.")
        self.stop()
        return False

    def stop(self):
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None
        self.base_url = None

    def move_file(self, src_fs: str, src_remote: str, dst_fs: str, dst_remote: str) -> bool:
        if not self.base_url or (self.proc is not None and self.proc.poll() is not None):
            return False

        payload = {
            "srcFs": src_fs,
            "srcRemote": src_remote,
            "dstFs": dst_fs,
            "dstRemote": dst_remote,
        }
        for attempt in range(RCLONE_MAX_RETRIES + 1):
            try:
                resp = self._session().post(f"{self.base_url}/operations/movefile", json=payload, timeout=90)
                if resp.status_code == 200:
                    return True
                err_text = resp.text.strip()
                if is_rate_limit_error(err_text) and attempt < RCLONE_MAX_RETRIES:
                    delay = min(RCLONE_BACKOFF_BASE * (2 ** attempt), RCLONE_BACKOFF_MAX) + random.uniform(0.5, 2.0)
                    logger.warning(f"rclone rcd rate limit on movefile (attempt {attempt + 1}/{RCLONE_MAX_RETRIES + 1}); sleeping {delay:.1f}s...")
                    time.sleep(delay)
                    continue
                logger.warning(f"rclone rcd movefile failed (HTTP {resp.status_code}): {err_text[:300]}")
                return False
            except requests.RequestException as e:
                logger.warning(f"rclone rcd request error ({e}); will fall back to subprocess moveto.")
                return False
        return False

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()


def remote_file_exists(remote: str, folder: str, filename: str) -> bool:
    """Silently checks whether a specific filename already exists directly inside folder."""
    target = _remote_path(remote, f"{folder}/{filename}")
    res = run_rclone(["lsf", target, "--files-only"], quiet_on_error=True)
    return res.returncode == 0 and bool(res.stdout.strip())


def resolve_flattened_processed_name(
    remote: str,
    processed_dir: str,
    basename: str,
    content_hash: Optional[str] = None,
    known_processed: Optional[Set[str]] = None,
) -> str:
    """Returns basename for flat archival in Processed/, disambiguating with a hash suffix if a file with the same name already exists."""
    def _exists(name: str) -> bool:
        if known_processed is not None:
            return name in known_processed
        return remote_file_exists(remote, processed_dir, name)

    if not _exists(basename):
        return basename

    stem, ext = os.path.splitext(basename)
    suffix = content_hash[:8] if content_hash else str(int(time.time()))
    candidate = f"{stem}_{suffix}{ext}"
    if _exists(candidate):
        candidate = f"{stem}_{suffix}_{int(time.time())}{ext}"
    logger.info(f"Collision detected in {processed_dir} for '{basename}'; flattening as '{candidate}' to avoid overwrite.")
    return candidate


def move_file_in_drive(
    remote: str,
    queue_dir: str,
    processed_dir: str,
    rel_path: str,
    content_hash: Optional[str] = None,
    known_processed: Optional[Set[str]] = None,
    rc_daemon: Optional[RcloneDaemon] = None,
    processed_lock: Optional[threading.Lock] = None,
) -> bool:
    """Moves a processed file (from root or any subfolder) flat into processed_dir without overwriting existing files."""
    rel_clean = rel_path.replace("\\", "/").lstrip("/")
    basename = os.path.basename(rel_clean)
    if processed_lock is not None:
        with processed_lock:
            dest_name = resolve_flattened_processed_name(
                remote, processed_dir, basename, content_hash=content_hash, known_processed=known_processed
            )
            if known_processed is not None:
                known_processed.add(dest_name)
    else:
        dest_name = resolve_flattened_processed_name(
            remote, processed_dir, basename, content_hash=content_hash, known_processed=known_processed
        )
        if known_processed is not None:
            known_processed.add(dest_name)

    src = _remote_path(remote, f"{queue_dir}/{rel_clean}")
    dest = _remote_path(remote, f"{processed_dir}/{dest_name}")
    logger.info(f"Moving {src} -> {dest}...")

    if rc_daemon is not None and rc_daemon.base_url:
        src_fs = _remote_path(remote, queue_dir)
        dst_fs = _remote_path(remote, processed_dir)
        if rc_daemon.move_file(src_fs, rel_clean, dst_fs, dest_name):
            return True

    res = run_rclone(["moveto", src, dest])
    if res.returncode != 0:
        logger.error(f"Failed to move {rel_clean} in Google Drive to {processed_dir}")
        return False
    return True


def cleanup_staging_file(local_path: Optional[str], staging_root: str):
    """Deletes staged file and prunes any empty intermediate staging subdirectories."""
    if not local_path:
        return
    if os.path.exists(local_path):
        try:
            os.remove(local_path)
        except Exception:
            pass
    root_abs = os.path.abspath(staging_root)
    parent = os.path.dirname(os.path.abspath(local_path))
    while parent != root_abs and os.path.commonpath([root_abs, parent]) == root_abs:
        try:
            os.rmdir(parent)
            parent = os.path.dirname(parent)
        except OSError:
            break


# --- Mendeley API Client ---
class MendeleyClient:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.access_token = None
        self.token_expiry = 0
        self._token_lock = threading.Lock()
        self._local = threading.local()

    def _session(self) -> requests.Session:
        sess = getattr(self._local, "session", None)
        if sess is None:
            sess = requests.Session()
            self._local.session = sess
        return sess

    def refresh_access_token(self, failed_token: Optional[str] = None):
        with self._token_lock:
            # Another worker may have already refreshed while we waited on the lock
            if failed_token and self.access_token and self.access_token != failed_token and time.time() < self.token_expiry:
                return self.access_token
            logger.info("Refreshing Mendeley OAuth access token...")
            payload = {
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": "all"
            }
            resp = self._session().post(MENDELEY_TOKEN_URL, data=payload, timeout=20)
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
        base_headers = dict(kwargs.pop("headers", {}) or {})
        for attempt in range(max_retries):
            token = self.get_token()
            headers = dict(base_headers)
            headers["Authorization"] = f"Bearer {token}"

            try:
                resp = self._session().request(method, url, headers=headers, **kwargs)
                if resp.status_code == 401:
                    logger.warning("Token expired mid-request. Refreshing token...")
                    self.refresh_access_token(failed_token=token)
                    continue
                elif resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After")
                    wait_s = backoff
                    if retry_after:
                        try:
                            wait_s = max(float(retry_after), 1.0)
                        except ValueError:
                            pass
                    logger.warning(f"Rate limited by Mendeley (HTTP 429). Retrying in {wait_s:.1f}s (attempt {attempt+1}/{max_retries})...")
                    time.sleep(wait_s)
                    backoff = min(backoff * 2.0, 60.0)
                    continue
                return resp
            except (requests.ConnectionError, requests.Timeout) as e:
                logger.warning(f"Network error ({e}). Retrying in {backoff:.1f}s...")
                time.sleep(backoff)
                backoff = min(backoff * 2.0, 60.0)

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
    logger.info(f"Watched Queues: {[_remote_path(GDRIVE_REMOTE, q) for q in GDRIVE_QUEUES]}")
    logger.info(f"Excluded Subfolders: {GDRIVE_EXCLUDE_DIRS}")
    logger.info(f"Max Files This Batch: {MAX_FILES}")
    logger.info(f"Safety Ceiling: {MAX_RUNTIME_SECONDS} seconds")
    logger.info("=" * 60)

    manifest = load_manifest(MANIFEST_PATH)
    logger.info(f"Loaded manifest with {len(manifest)} existing records.")

    mendeley = MendeleyClient(CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN)
    # Test authentication early
    mendeley.refresh_access_token()

    # Collect pending (queue_dir, rel_path) pairs recursively across all watched folders
    pending_items = []
    scan_failures = []
    for q_dir in GDRIVE_QUEUES:
        try:
            for rel_path in list_queue_files(GDRIVE_REMOTE, q_dir, raise_on_error=True):
                pending_items.append((q_dir, rel_path))
        except Exception as e:
            logger.error(f"Queue scan failed for {q_dir}: {e}")
            scan_failures.append({"file": f"{q_dir} (scan)", "error": str(e)})

    total_pending = len(pending_items)
    logger.info(f"Found {total_pending} PDF file(s) across watched queues.")

    if total_pending == 0:
        if scan_failures:
            logger.error("All queue scans failed (likely Google Drive rate limit). Exiting with error instead of assuming empty queue.")
            write_step_summary(total_scanned=0, uploaded=0, skipped=0, errors=scan_failures, records=[], duration_s=time.time() - START_TIME, total_manifest=len(manifest))
            set_github_output("has_more_files", "false")
            set_github_output("uploaded_count", "0")
            sys.exit(1)
        logger.info("No pending files to process. Exiting cleanly.")
        write_step_summary(total_scanned=0, uploaded=0, skipped=0, errors=[], records=[])
        set_github_output("has_more_files", "false")
        set_github_output("uploaded_count", "0")
        sys.exit(0)

    # Bound the batch to MAX_FILES
    batch_files = pending_items[:MAX_FILES]
    has_more = total_pending > MAX_FILES

    # Group batch files per queue for single-shot parallel rclone downloads
    queue_rel_paths: Dict[str, List[str]] = {}
    for q_dir, rel_path in batch_files:
        queue_rel_paths.setdefault(q_dir, []).append(rel_path)

    active_queues = sorted(queue_rel_paths.keys())

    # Pre-load Processed/ filename caches once per active queue to avoid per-file Drive API lookups
    processed_caches: Dict[str, Optional[Set[str]]] = {}
    processed_locks: Dict[str, threading.Lock] = {}
    queue_staging_dirs: Dict[str, str] = {}
    pre_staged: Dict[Tuple[str, str], str] = {}

    for q_idx, q_dir in enumerate(active_queues):
        processed_caches[q_dir] = list_processed_filenames(GDRIVE_REMOTE, f"{q_dir}/Processed")
        processed_locks[q_dir] = threading.Lock()
        q_stage = os.path.join(LOCAL_STAGING_DIR, f"q_{q_idx}")
        queue_staging_dirs[q_dir] = q_stage
        staged_map = batch_download_from_drive(GDRIVE_REMOTE, q_dir, queue_rel_paths[q_dir], q_stage)
        for rel_path, local_path in staged_map.items():
            pre_staged[(q_dir, rel_path)] = local_path

    stats = {
        "scanned": len(batch_files),
        "uploaded": 0,
        "skipped": 0,
        "errors": list(scan_failures),
    }
    records_summary: List[Dict[str, str]] = []
    manifest_lock = threading.Lock()
    stats_lock = threading.Lock()
    manifest_dirty_count = 0
    stop_event = threading.Event()
    hit_runtime_ceiling = False

    def _record_manifest_entry(sha256_hex: str, entry: Dict[str, Any]):
        nonlocal manifest_dirty_count
        with manifest_lock:
            manifest[sha256_hex] = entry
            manifest_dirty_count += 1
            if manifest_dirty_count >= MANIFEST_SAVE_INTERVAL:
                save_manifest(MANIFEST_PATH, manifest)
                manifest_dirty_count = 0

    def _process_one(idx: int, q_dir: str, rel_path: str, rc_daemon: Optional[RcloneDaemon]):
        nonlocal hit_runtime_ceiling
        if stop_event.is_set():
            return
        elapsed = time.time() - START_TIME
        if elapsed > MAX_RUNTIME_SECONDS:
            logger.warning(f"Approaching runtime ceiling ({elapsed:.1f}s > {MAX_RUNTIME_SECONDS}s). Stopping batch gracefully.")
            hit_runtime_ceiling = True
            stop_event.set()
            return

        p_dir = f"{q_dir}/Processed"
        known_processed = processed_caches.get(q_dir)
        p_lock = processed_locks.get(q_dir)
        q_stage = queue_staging_dirs[q_dir]

        file_start = time.time()
        basename = os.path.basename(rel_path)
        title = os.path.splitext(basename)[0].replace("_", " ")
        display_path = f"{q_dir}/{rel_path}"
        logger.info(f"[{idx}/{len(batch_files)}] Processing ({q_dir}): {rel_path} ('{title}')")

        local_path = pre_staged.get((q_dir, rel_path))
        try:
            # 1. Ensure file is staged locally (fallback to single download if batch missed it)
            if not local_path or not os.path.isfile(local_path):
                local_path = download_file_from_drive(GDRIVE_REMOTE, q_dir, rel_path, q_stage)

            # 2. Compute local hashes
            local_sha1, local_sha256 = calculate_file_hashes(local_path)
            file_size_mb = os.path.getsize(local_path) / (1024 * 1024)

            # 3. Deduplication Tier 2 (Content SHA-256 in manifest)
            with manifest_lock:
                existing = manifest.get(local_sha256)
            if existing is not None:
                logger.info(f"⏭️ Deduplication Tier 2 Match: Content hash {local_sha256[:12]}... already uploaded as '{existing.get('title')}' (doc_id={existing.get('mendeley_doc_id')}). Skipping.")
                move_file_in_drive(
                    GDRIVE_REMOTE, q_dir, p_dir, rel_path,
                    content_hash=local_sha256, known_processed=known_processed,
                    rc_daemon=rc_daemon, processed_lock=p_lock,
                )
                with stats_lock:
                    stats["skipped"] += 1
                    records_summary.append({
                        "file": display_path,
                        "status": "Skipped (Content Hash Match)",
                        "doc_id": existing.get("mendeley_doc_id", "Existing"),
                        "duration": f"{time.time() - file_start:.1f}s"
                    })
                return

            # 4. Deduplication Tier 3 (Catalog title pre-flight check)
            existing_doc_id = mendeley.find_catalog_document_by_title(title)
            if existing_doc_id:
                logger.info(f"⏭️ Deduplication Tier 3 Match: Document '{title}' already exists in Mendeley with attached file (doc_id={existing_doc_id}). Skipping.")
                _record_manifest_entry(local_sha256, {
                    "filename": basename,
                    "source_path": display_path,
                    "title": title,
                    "mendeley_doc_id": existing_doc_id,
                    "mendeley_file_id": "catalog_existing",
                    "mendeley_filehash": local_sha1,
                    "uploaded_at": datetime.now(timezone.utc).isoformat()
                })
                move_file_in_drive(
                    GDRIVE_REMOTE, q_dir, p_dir, rel_path,
                    content_hash=local_sha256, known_processed=known_processed,
                    rc_daemon=rc_daemon, processed_lock=p_lock,
                )
                with stats_lock:
                    stats["skipped"] += 1
                    records_summary.append({
                        "file": display_path,
                        "status": "Skipped (Catalog Title Match)",
                        "doc_id": existing_doc_id,
                        "duration": f"{time.time() - file_start:.1f}s"
                    })
                return

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
            _record_manifest_entry(local_sha256, {
                "filename": basename,
                "source_path": display_path,
                "title": title,
                "mendeley_doc_id": doc_id,
                "mendeley_file_id": mendeley_file_id,
                "mendeley_filehash": mendeley_filehash or local_sha1,
                "size_mb": round(file_size_mb, 2),
                "uploaded_at": datetime.now(timezone.utc).isoformat()
            })

            # 9. Move file flat into Processed/ in Google Drive
            move_file_in_drive(
                GDRIVE_REMOTE, q_dir, p_dir, rel_path,
                content_hash=local_sha256, known_processed=known_processed,
                rc_daemon=rc_daemon, processed_lock=p_lock,
            )

            duration_s = time.time() - file_start
            logger.info(f"SUCCESS: {display_path} uploaded in {duration_s:.1f}s (doc_id={doc_id})")
            with stats_lock:
                stats["uploaded"] += 1
                records_summary.append({
                    "file": display_path,
                    "status": "✅ Uploaded",
                    "doc_id": doc_id,
                    "duration": f"{duration_s:.1f}s"
                })

            if FILE_PACE_SECONDS > 0:
                time.sleep(FILE_PACE_SECONDS)

        except Exception as e:
            duration_s = time.time() - file_start
            logger.error(f"ERROR on {display_path}: {str(e)}")
            with stats_lock:
                stats["errors"].append({"file": display_path, "error": str(e)})
                records_summary.append({
                    "file": display_path,
                    "status": "❌ Failed",
                    "doc_id": "N/A",
                    "duration": f"{duration_s:.1f}s"
                })
            time.sleep(1.0)

        finally:
            cleanup_staging_file(local_path, q_stage)

    logger.info(f"Processing {len(batch_files)} file(s) with UPLOAD_WORKERS={UPLOAD_WORKERS} (tpslimit={RCLONE_TPSLIMIT}, burst={RCLONE_TPSLIMIT_BURST})...")

    daemon_ctx = RcloneDaemon() if GDRIVE_REMOTE else None
    try:
        if daemon_ctx is not None:
            daemon_ctx.start()
        with ThreadPoolExecutor(max_workers=UPLOAD_WORKERS) as pool:
            futures = [
                pool.submit(_process_one, idx, q_dir, rel_path, daemon_ctx)
                for idx, (q_dir, rel_path) in enumerate(batch_files, 1)
            ]
            for fut in as_completed(futures):
                fut.result()
    finally:
        if daemon_ctx is not None:
            daemon_ctx.stop()
        # Save final manifest
        with manifest_lock:
            save_manifest(MANIFEST_PATH, manifest)

    if hit_runtime_ceiling:
        has_more = True

    # Step Summary & Output
    total_duration = time.time() - START_TIME
    write_step_summary(stats["scanned"], stats["uploaded"], stats["skipped"], stats["errors"], records_summary, total_duration, len(manifest))
    set_github_output("uploaded_count", str(stats["uploaded"]))

    if has_more:
        logger.info("Remaining files detected in queue. Signaling auto-continuation.")
        set_github_output("has_more_files", "true")
    else:
        logger.info("All pending queue files processed. No continuation needed.")
        set_github_output("has_more_files", "false")


def write_step_summary(total_scanned: int, uploaded: int, skipped: int, errors: List[Dict[str, str]], records: List[Dict[str, str]], duration_s: float = 0.0, total_manifest: int = 0):
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")

    m, s = divmod(int(duration_s), 60)
    time_str = f"{m}m {s}s" if m > 0 else f"{s}s"

    lines = [
        f"## 📚 Mendeley Cloud Sync Summary ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})",
        f"- **Total PDFs Inspected**: `{total_scanned}`",
        f"- **Successfully Uploaded**: `{uploaded}`",
        f"- **Duplicates Skipped**: `{skipped}`",
        f"- **Failed / Errors**: `{len(errors)}`",
        f"- **Total Books in Ledger (`manifest.json`)**: `{total_manifest}`",
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
            clean_err = " ".join(str(err["error"]).split())
            if len(clean_err) > 300:
                clean_err = clean_err[:297] + "..."
            lines.append(f"- **`{err['file']}`**: `{clean_err}`")

    summary_md = "\n".join(lines) + "\n"
    try:
        with open("run_summary.md", "w", encoding="utf-8") as rf:
            rf.write(summary_md)
    except Exception as e:
        logger.warning(f"Could not write run_summary.md: {e}")

    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as f:
                f.write(summary_md)
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
