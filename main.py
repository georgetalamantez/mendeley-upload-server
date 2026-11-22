import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests
import uvicorn
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from auth_gate import (
    COOKIE_NAME,
    PasswordGateMiddleware,
    get_expected_password,
    get_expected_token,
    is_auth_enabled,
    render_login_page,
)

# --- Configuration ---
load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

MENDELEY_CLIENT_ID = os.getenv("MENDELEY_CLIENT_ID")
MENDELEY_CLIENT_SECRET = os.getenv("MENDELEY_CLIENT_SECRET")
MENDELEY_REFRESH_TOKEN = os.getenv("MENDELEY_REFRESH_TOKEN")
MENDELEY_REDIRECT_URI = os.getenv("MENDELEY_REDIRECT_URI", "http://localhost:8585/callback")

MANIFEST_PATH = os.getenv("MANIFEST_PATH", os.path.join(BASE_DIR, "manifest.json"))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "100"))
EXCLUDED_DIRS = {
    d.strip().lower()
    for d in os.getenv("UPLOADER_EXCLUDE_DIRS", "Processed,Quarantined").split(",")
    if d.strip()
}

TOKEN_URL = "https://api.mendeley.com/oauth/token"
DOCUMENTS_URL = "https://api.mendeley.com/documents"
FILES_URL = "https://api.mendeley.com/files"
VALID_DOC_TYPES = {"book", "journal", "report", "generic", "book_section", "conference_proceedings", "thesis", "working_paper"}

# --- Logging Setup ---
LOG_FILE = os.getenv("LOG_FILE", os.path.join(BASE_DIR, "mendeley_uploader.log"))


class MemoryHandler(logging.Handler):
    def __init__(self, capacity: int = 1000):
        super().__init__()
        self.capacity = capacity
        self.logs: List[str] = []
        self._lock = threading.Lock()

    def emit(self, record):
        log_entry = self.format(record)
        with self._lock:
            self.logs.append(log_entry)
            if len(self.logs) > self.capacity:
                self.logs.pop(0)

    def clear(self):
        with self._lock:
            self.logs.clear()

    def snapshot(self) -> List[str]:
        with self._lock:
            return list(self.logs)


logger = logging.getLogger("mendeley_uploader")
logger.setLevel(logging.INFO)
logger.propagate = False

if not logger.handlers:
    try:
        log_parent = os.path.dirname(os.path.abspath(LOG_FILE))
        if log_parent:
            os.makedirs(log_parent, exist_ok=True)
        file_handler = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
        file_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)
    except Exception:
        pass

    memory_handler = MemoryHandler()
    memory_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    memory_handler.setFormatter(memory_formatter)
    logger.addHandler(memory_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(memory_formatter)
    logger.addHandler(console_handler)
else:
    memory_handler = next(
        (h for h in logger.handlers if isinstance(h, MemoryHandler)),
        MemoryHandler(),
    )

if not all([MENDELEY_CLIENT_ID, MENDELEY_CLIENT_SECRET, MENDELEY_REFRESH_TOKEN]):
    logger.warning("Missing Mendeley credentials in environment variables.")

# --- Thread-safe Token & Manifest State ---
_token_lock = threading.Lock()
_cached_token: Dict[str, Any] = {"access_token": None, "expires_at": 0.0}
_manifest_lock = threading.Lock()


class UploadState:
    is_running = False
    total_files = 0
    processed_files = 0
    current_file = ""
    status_message = "Idle"
    should_stop = False


state = UploadState()


# --- Utility Helpers ---
def sanitize_header_filename(filename: str) -> Tuple[str, str]:
    """Returns (ascii_fallback, rfc5987_utf8) safe for HTTP Content-Disposition headers."""
    clean = os.path.basename(filename.replace("\\", "/")).replace("\r", "").replace("\n", "").strip()
    if not clean:
        clean = "document.pdf"
    if not clean.lower().endswith(".pdf"):
        clean = f"{clean}.pdf"
    normalized = unicodedata.normalize("NFKD", clean).encode("ascii", "ignore").decode("ascii")
    ascii_safe = re.sub(r'["\\]+', "_", normalized).strip() or "document.pdf"
    if not ascii_safe.lower().endswith(".pdf"):
        ascii_safe = f"{ascii_safe}.pdf"
    utf8_encoded = quote(clean, safe="")
    return ascii_safe, utf8_encoded


def title_from_filename(filename: str) -> str:
    base = os.path.basename(filename.replace("\\", "/"))
    stem = os.path.splitext(base)[0]
    cleaned = re.sub(r"[_\s]+", " ", stem).strip()
    return cleaned or "Untitled PDF"


def compute_hashes(data: bytes) -> Tuple[str, str]:
    """Returns (sha1_hex, sha256_hex) for Mendeley verification and manifest deduplication."""
    return hashlib.sha1(data).hexdigest().lower(), hashlib.sha256(data).hexdigest().lower()


def load_manifest(path: Optional[str] = None) -> Dict[str, Any]:
    target = path or MANIFEST_PATH
    with _manifest_lock:
        if os.path.exists(target):
            try:
                with open(target, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data
            except Exception as e:
                logger.warning(f"Could not read manifest at {target}: {e}")
        return {}


def save_manifest_record(sha256_hex: str, record: Dict[str, Any], path: Optional[str] = None) -> int:
    target = path or MANIFEST_PATH
    with _manifest_lock:
        manifest: Dict[str, Any] = {}
        if os.path.exists(target):
            try:
                with open(target, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        manifest = loaded
            except Exception:
                manifest = {}

        manifest[sha256_hex] = record
        parent = os.path.dirname(os.path.abspath(target))
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp_path = f"{target}.tmp.{os.getpid()}.{threading.get_ident()}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, sort_keys=True)
        os.replace(tmp_path, target)
        return len(manifest)


# --- Mendeley API Helpers ---
def get_access_token(force_refresh: bool = False) -> str:
    with _token_lock:
        if not force_refresh and _cached_token["access_token"] and time.time() < _cached_token["expires_at"]:
            return _cached_token["access_token"]

        client_id = os.getenv("MENDELEY_CLIENT_ID") or MENDELEY_CLIENT_ID
        client_secret = os.getenv("MENDELEY_CLIENT_SECRET") or MENDELEY_CLIENT_SECRET
        refresh_token = os.getenv("MENDELEY_REFRESH_TOKEN") or MENDELEY_REFRESH_TOKEN

        if not all([client_id, client_secret, refresh_token]):
            raise RuntimeError("Missing Mendeley OAuth credentials in environment variables.")

        logger.info("Refreshing Mendeley OAuth access token...")
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "all",
        }
        response = requests.post(TOKEN_URL, data=payload, timeout=15)
        if response.status_code == 200:
            data = response.json()
            token = data["access_token"]
            expires_in = int(data.get("expires_in", 3600))
            _cached_token["access_token"] = token
            _cached_token["expires_at"] = time.time() + max(expires_in - 60, 60)
            logger.info("Mendeley access token refreshed.")
            return token

        logger.error(f"Failed to get token: {response.status_code} {response.text}")
        raise RuntimeError(f"Mendeley authentication failed ({response.status_code}): {response.text}")


def _mendeley_request(method: str, url: str, access_token: Optional[str] = None, **kwargs) -> requests.Response:
    max_retries = 5
    backoff = 4.0
    token = access_token or get_access_token()

    for attempt in range(max_retries):
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {token}"
        try:
            resp = requests.request(method, url, headers=headers, **kwargs)
            if resp.status_code == 401 and attempt < max_retries - 1:
                logger.warning("Mendeley token expired mid-request; refreshing...")
                token = get_access_token(force_refresh=True)
                continue
            if resp.status_code == 429 and attempt < max_retries - 1:
                retry_after = resp.headers.get("Retry-After")
                wait_s = float(retry_after) if retry_after and retry_after.isdigit() else backoff
                logger.warning(f"Mendeley rate limit (429). Retrying in {wait_s:.1f}s (attempt {attempt + 1}/{max_retries})...")
                time.sleep(wait_s)
                backoff *= 2.0
                continue
            return resp
        except (requests.ConnectionError, requests.Timeout) as e:
            if attempt >= max_retries - 1:
                raise
            logger.warning(f"Network error contacting Mendeley ({e}). Retrying in {backoff:.1f}s...")
            time.sleep(backoff)
            backoff *= 2.0

    raise RuntimeError(f"Mendeley request failed after {max_retries} attempts: {method} {url}")


def find_catalog_document_by_title(access_token: str, title: str) -> Optional[str]:
    headers = {"Accept": "application/vnd.mendeley-document.1+json"}
    try:
        resp = _mendeley_request(
            "GET",
            DOCUMENTS_URL,
            access_token=access_token,
            params={"title": title, "view": "all"},
            headers=headers,
            timeout=30,
        )
        if resp.status_code == 200:
            docs = resp.json()
            if isinstance(docs, list):
                target = title.strip().lower()
                for doc in docs:
                    if (doc.get("title") or "").strip().lower() == target:
                        if doc.get("file_attached") or doc.get("files"):
                            return doc.get("id")
    except Exception as e:
        logger.warning(f"Catalog title check skipped for '{title}': {e}")
    return None


def create_document(access_token: str, title: str, doc_type: str = "book") -> Optional[str]:
    safe_type = doc_type if doc_type in VALID_DOC_TYPES else "book"
    headers = {
        "Content-Type": "application/vnd.mendeley-document.1+json",
        "Accept": "application/vnd.mendeley-document.1+json",
    }
    data = {"title": title, "type": safe_type}
    try:
        response = _mendeley_request("POST", DOCUMENTS_URL, access_token=access_token, headers=headers, json=data, timeout=30)
        if response.status_code == 201:
            doc_id = response.json()["id"]
            logger.info(f"Document created: id={doc_id} ('{title}')")
            return doc_id
        logger.error(f"Failed to create document '{title}': {response.status_code} - {response.text}")
        return None
    except Exception as e:
        logger.error(f"Exception creating document '{title}': {str(e)}")
        return None


def upload_file_bytes(access_token: str, document_id: str, filename: str, file_bytes: bytes) -> Dict[str, Any]:
    ascii_name, utf8_name = sanitize_header_filename(filename)
    headers = {
        "Content-Type": "application/pdf",
        "Accept": "application/vnd.mendeley-file.1+json",
        "Link": f'<{DOCUMENTS_URL}/{document_id}>; rel="document"',
        "Content-Disposition": f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{utf8_name}',
    }
    response = _mendeley_request("POST", FILES_URL, access_token=access_token, headers=headers, data=file_bytes, timeout=180)
    if response.status_code == 201:
        try:
            info = response.json()
        except Exception:
            info = {}
        if "id" not in info and "Location" in response.headers:
            info["id"] = response.headers["Location"].rstrip("/").split("/")[-1]
        logger.info(f"File uploaded successfully for document {document_id} (file_id={info.get('id', 'confirmed')})")
        return info
    raise RuntimeError(f"Mendeley file upload failed ({response.status_code}): {response.text}")


def upload_file_content(access_token: str, document_id: str, file_path: str) -> bool:
    """Backwards-compatible helper used by local-path batch upload and tests."""
    try:
        with open(file_path, "rb") as f:
            file_bytes = f.read()
        upload_file_bytes(access_token, document_id, os.path.basename(file_path), file_bytes)
        return True
    except Exception as e:
        logger.error(f"Exception uploading file '{file_path}': {str(e)}")
        return False


def collect_pdf_files(path: str, recursive: bool = True, excluded_dirs: Optional[set] = None) -> List[str]:
    """Collects PDF files from a file path or directory (recursively by default, skipping Processed/ and Quarantined/)."""
    excluded = EXCLUDED_DIRS if excluded_dirs is None else {d.lower() for d in excluded_dirs}
    if os.path.isfile(path):
        return [path] if path.lower().endswith(".pdf") else []
    if not os.path.isdir(path):
        return []

    if not recursive:
        return sorted(
            os.path.join(path, fname)
            for fname in os.listdir(path)
            if fname.lower().endswith(".pdf") and os.path.isfile(os.path.join(path, fname))
        )

    collected: List[str] = []
    for root, dirnames, filenames in os.walk(path):
        dirnames[:] = sorted(d for d in dirnames if d.lower() not in excluded)
        for fname in sorted(filenames):
            if fname.lower().endswith(".pdf"):
                collected.append(os.path.join(root, fname))
    return collected


def process_upload_task(path: str, recursive: bool = True):
    logger.info(f"Starting local path upload task for: {path} (recursive={recursive})")
    state.is_running = True
    state.should_stop = False
    state.processed_files = 0
    state.total_files = 0

    try:
        access_token = get_access_token()
    except Exception:
        state.status_message = "Authentication Failed. Check Logs."
        state.is_running = False
        return

    files_to_process = collect_pdf_files(path, recursive=recursive)
    state.total_files = len(files_to_process)
    logger.info(f"Found {state.total_files} PDF files to process.")
    state.status_message = f"Processing directly from {path}"

    is_dir = os.path.isdir(path)

    for idx, file_path in enumerate(files_to_process):
        if state.should_stop:
            logger.info("Process stopped by user.")
            state.status_message = "Stopped."
            break

        filename = os.path.basename(file_path)
        display_name = os.path.relpath(file_path, path) if is_dir else filename
        state.current_file = display_name
        title = os.path.splitext(filename)[0].replace("_", " ")

        logger.info(f"[{idx + 1}/{state.total_files}] Processing: {display_name}")

        try:
            doc_id = create_document(access_token, title)
            if doc_id:
                success = upload_file_content(access_token, doc_id, file_path)
                if success:
                    logger.info(f"SUCCESS: {display_name}")
                else:
                    logger.error(f"FAILURE (upload): {display_name}")
            else:
                logger.error(f"FAILURE (metadata): {display_name}")
        except Exception as e:
            logger.error(f"CRITICAL ERROR on {display_name}: {str(e)}")

        state.processed_files += 1
        time.sleep(1.0)

    state.is_running = False
    state.current_file = ""
    state.status_message = "Completed"
    logger.info("Batch processing finished.")


# --- Application ---
app = FastAPI(title="Mendeley PDF Uploader")
app.add_middleware(PasswordGateMiddleware)


class UploadRequest(BaseModel):
    path: str
    recursive: bool = True


# --- Auth Routes (matching Flashcards App) ---
@app.get("/login", include_in_schema=False)
async def login_get():
    if not is_auth_enabled():
        return RedirectResponse(url="/", status_code=303)
    return HTMLResponse(render_login_page(), status_code=200)


@app.post("/login", include_in_schema=False)
async def login_post(request: Request, password: str = Form("")):
    expected_pwd = get_expected_password()
    if expected_pwd and hmac.compare_digest(password.strip(), expected_pwd):
        prefix = request.headers.get("x-forwarded-prefix", "").rstrip("/")
        resp = RedirectResponse(url=f"{prefix}/", status_code=303)
        is_https = request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https"
        resp.set_cookie(
            key=COOKIE_NAME,
            value=get_expected_token(),
            max_age=86400 * 30,
            httponly=True,
            secure=is_https,
            samesite="lax",
            path="/",
        )
        return resp
    return HTMLResponse(render_login_page("Incorrect password. Please try again."), status_code=401)


@app.get("/logout", include_in_schema=False)
async def logout_get(request: Request):
    prefix = request.headers.get("x-forwarded-prefix", "").rstrip("/")
    resp = RedirectResponse(url=f"{prefix}/", status_code=303)
    resp.delete_cookie(key=COOKIE_NAME, path="/")
    return resp


# --- Health & Info Endpoints ---
@app.get("/api/health")
def health_check():
    return {"status": "ok"}


@app.get("/api/info")
def get_info(verify: bool = Query(False, description="Test Mendeley OAuth token refresh")):
    manifest = load_manifest()
    creds_configured = all([
        os.getenv("MENDELEY_CLIENT_ID") or MENDELEY_CLIENT_ID,
        os.getenv("MENDELEY_CLIENT_SECRET") or MENDELEY_CLIENT_SECRET,
        os.getenv("MENDELEY_REFRESH_TOKEN") or MENDELEY_REFRESH_TOKEN,
    ])
    mendeley_connected: Optional[bool] = None
    mendeley_error: Optional[str] = None
    if verify:
        try:
            get_access_token(force_refresh=False)
            mendeley_connected = True
        except Exception as e:
            mendeley_connected = False
            mendeley_error = str(e)

    return {
        "credentials_configured": creds_configured,
        "mendeley_connected": mendeley_connected,
        "mendeley_error": mendeley_error,
        "manifest_count": len(manifest),
        "auth_enabled": is_auth_enabled(),
        "max_upload_mb": MAX_UPLOAD_MB,
        "excluded_dirs": sorted(EXCLUDED_DIRS),
    }


# --- Browser Direct File / Folder Upload Endpoint ---
@app.post("/api/upload-file")
async def upload_single_pdf(
    file: UploadFile = File(...),
    relative_path: str = Form(""),
    check_manifest: bool = Form(True),
    check_catalog: bool = Form(True),
    doc_type: str = Form("book"),
):
    """Receives a single PDF streamed from the browser folder/file picker and uploads it to Mendeley."""
    started = time.perf_counter()
    orig_name = os.path.basename((file.filename or "document.pdf").replace("\\", "/"))
    display_path = (relative_path or orig_name).replace("\\", "/").lstrip("/")

    if not orig_name.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail=f"Only PDF files are supported (got '{orig_name}').")

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail=f"File '{display_path}' is empty.")

    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    if len(file_bytes) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File '{display_path}' ({len(file_bytes) / (1024 * 1024):.1f} MB) exceeds limit of {MAX_UPLOAD_MB} MB.",
        )

    local_sha1, local_sha256 = compute_hashes(file_bytes)
    size_mb = round(len(file_bytes) / (1024 * 1024), 2)
    title = title_from_filename(orig_name)

    # Deduplication Tier 2: SHA-256 content hash in manifest.json
    if check_manifest:
        manifest = load_manifest()
        if local_sha256 in manifest:
            existing = manifest[local_sha256]
            doc_id = existing.get("mendeley_doc_id", "existing")
            duration_s = round(time.perf_counter() - started, 2)
            logger.info(f"SKIP (hash duplicate): {display_path} -> already in manifest (doc_id={doc_id})")
            return {
                "status": "skipped",
                "reason": "duplicate_hash",
                "message": f"Already uploaded previously (matching SHA-256 hash)",
                "document_id": doc_id,
                "title": existing.get("title", title),
                "filename": orig_name,
                "relative_path": display_path,
                "size_mb": size_mb,
                "duration_s": duration_s,
                "manifest_count": len(manifest),
            }

    try:
        access_token = get_access_token()
    except Exception as e:
        logger.error(f"AUTH ERROR on {display_path}: {e}")
        raise HTTPException(status_code=502, detail=f"Mendeley authentication failed: {e}")

    # Deduplication Tier 3: Mendeley catalog title check
    if check_catalog:
        existing_doc_id = find_catalog_document_by_title(access_token, title)
        if existing_doc_id:
            manifest_count = save_manifest_record(
                local_sha256,
                {
                    "filename": orig_name,
                    "source_path": f"browser/{display_path}",
                    "title": title,
                    "mendeley_doc_id": existing_doc_id,
                    "mendeley_file_id": "catalog_existing",
                    "mendeley_filehash": local_sha1,
                    "size_mb": size_mb,
                    "uploaded_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            duration_s = round(time.perf_counter() - started, 2)
            logger.info(f"SKIP (catalog match): {display_path} -> '{title}' already in Mendeley (doc_id={existing_doc_id})")
            return {
                "status": "skipped",
                "reason": "duplicate_catalog",
                "message": "Already exists in Mendeley catalog with attached PDF",
                "document_id": existing_doc_id,
                "title": title,
                "filename": orig_name,
                "relative_path": display_path,
                "size_mb": size_mb,
                "duration_s": duration_s,
                "manifest_count": manifest_count,
            }

    # Create Mendeley document metadata
    doc_id = create_document(access_token, title, doc_type=doc_type)
    if not doc_id:
        raise HTTPException(status_code=502, detail=f"Failed to create Mendeley document metadata for '{title}'.")

    # Upload PDF binary and verify server SHA-1 hash
    try:
        file_info = upload_file_bytes(access_token, doc_id, orig_name, file_bytes)
    except Exception as e:
        logger.error(f"UPLOAD ERROR on {display_path}: {e}")
        raise HTTPException(status_code=502, detail=str(e))

    server_hash = (file_info.get("filehash") or "").lower()
    file_id = file_info.get("id", "confirmed")
    hash_verified = bool(server_hash) and server_hash == local_sha1
    if server_hash and not hash_verified:
        logger.warning(f"SHA-1 mismatch on {display_path}: local={local_sha1} server={server_hash}")

    manifest_count = save_manifest_record(
        local_sha256,
        {
            "filename": orig_name,
            "source_path": f"browser/{display_path}",
            "title": title,
            "mendeley_doc_id": doc_id,
            "mendeley_file_id": file_id,
            "mendeley_filehash": server_hash or local_sha1,
            "size_mb": size_mb,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    duration_s = round(time.perf_counter() - started, 2)
    logger.info(f"SUCCESS: {display_path} uploaded in {duration_s:.2f}s (doc_id={doc_id}, sha1_verified={hash_verified})")

    return {
        "status": "uploaded",
        "message": "Uploaded to Mendeley" + (" (SHA-1 verified)" if hash_verified else ""),
        "document_id": doc_id,
        "file_id": file_id,
        "filehash": server_hash or local_sha1,
        "hash_verified": hash_verified,
        "title": title,
        "filename": orig_name,
        "relative_path": display_path,
        "size_mb": size_mb,
        "duration_s": duration_s,
        "manifest_count": manifest_count,
    }


# --- Legacy Local-Path Endpoints (backwards-compatible) ---
@app.post("/api/start-upload")
async def start_upload(request: UploadRequest, background_tasks: BackgroundTasks):
    if state.is_running:
        raise HTTPException(status_code=400, detail="Job already running.")
    if not os.path.exists(request.path):
        raise HTTPException(status_code=404, detail="Path not found.")

    background_tasks.add_task(process_upload_task, request.path, request.recursive)
    return {"message": "Upload started", "path": request.path, "recursive": request.recursive}


@app.post("/api/stop")
def stop_upload():
    if state.is_running:
        state.should_stop = True
        return {"message": "Stopping..."}
    return {"message": "Not running"}


@app.get("/api/status")
def get_status():
    return {
        "is_running": state.is_running,
        "total_files": state.total_files,
        "processed_files": state.processed_files,
        "current_file": state.current_file,
        "status_message": state.status_message,
    }


@app.get("/api/logs")
def get_logs():
    return {"logs": memory_handler.snapshot()}


@app.post("/api/logs/clear")
def clear_logs():
    memory_handler.clear()
    return {"message": "Logs cleared"}


# --- Frontend Serving ---
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    @app.get("/index.html", include_in_schema=False)
    async def serve_index():
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")
    uvicorn.run("main:app", host=host, port=port, proxy_headers=True, forwarded_allow_ips="*")
