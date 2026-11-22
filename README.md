# Mendeley Upload Server

A containerized web application and automated pipeline to import PDF files (including nested subfolders) into your Mendeley library directly from your browser.

## Features

- **Browser Folder & File Picker**: Click **Browse Folder (with Subfolders)** or drag & drop folders/PDFs directly from your computer into the browser UI (no server filesystem path required).
- **Recursive Subfolder Support**: Automatically scans nested subfolders while skipping `Processed/` and `Quarantined/` directories.
- **Deduplication & Integrity Verification**:
  - Local SHA-256 content-hash deduplication via `manifest.json`
  - Optional Mendeley catalog title pre-flight check
  - Server-side SHA-1 `filehash` confirmation
- **Per-File Queue & Progress UI**: Real-time progress bar, per-file status badges, live upload log, **Stop**, and **Retry Failed** controls.
- **Optional Password Gate**: Set `APP_PASSWORD` when hosting publicly behind HTTPS/Nginx (leave unset for local-only use).

## Quick Start (Docker Container)

1. Inject or export your Mendeley credentials (`MENDELEY_CLIENT_ID`, `MENDELEY_CLIENT_SECRET`, `MENDELEY_REFRESH_TOKEN`) — and optionally `APP_PASSWORD` if exposing over the internet.
2. Build and start the container:
   ```bash
   docker compose up -d --build
   ```
3. Open your browser to `http://localhost:8096` (or your reverse-proxied HTTPS URL).
4. Click **Browse Folder (with Subfolders)**, pick any folder on your computer, and click **Upload to Mendeley**.

## Local Python Usage (Without Docker)

```bash
pip install -r requirements.txt
uvicorn main:app --host 127.0.0.1 --port 8000
```

Then open `http://127.0.0.1:8000` in your browser.

## Automated Google Drive Pipeline (`sync_drive_to_mendeley.py`)

Recursively scans watched Google Drive folders (`Mendeley_Queue`, `books1/chapters`) while excluding `Processed/` and `Quarantined/`, uploads new PDFs to Mendeley, records them in `manifest.json`, and moves processed files flat into each queue's `Processed/` folder.

## Refreshing Your Mendeley Token

If authentication fails because your refresh token expired or was revoked:
1. Run `python get_new_token.py`
2. Authorize in your browser and paste the returned code
3. Update `MENDELEY_REFRESH_TOKEN` in your secret manager / environment and restart the container.
