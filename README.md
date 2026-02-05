# Mendeley Upload Server

A local web server to import PDF files into your Mendeley library.

## Prerequisites

- [Python 3.x](https://www.python.org/downloads/)
- Mendeley Account

## Installation

1. Open a terminal in this directory.
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Set up environment variables:
   - Copy `.env.example` to `.env`.
   - Open `.env` and add your Mendeley API credentials.

## Usage

### Option 1: Automatic Start (Windows)
Double-click `start_server.bat` to install dependencies and start the server automatically.
This will also open the dashboard in your default browser.

### Option 2: Manual Start
Run the following command in your terminal:
```bash
uvicorn main:app --reload
```
Then visit `http://localhost:8000` in your browser.

## How it Works

1. **Authentication**: The server uses a **RefreshToken** stored in environment variables to authenticate with the Mendeley API.
2. **File scanning**: When you click "Start Upload", it scans the configured directory for PDF files.
3. **Upload Logic**:
   - Creates a metadata entry (Document) in Mendeley using the filename as the title.
   - Uploads the PDF file content and attaches it to the Document ID.
4. **Logging**: All successes and failures are logged to `mendeley_uploader.log`.

## Maintenance

### Credentials & Security
Credentials are stored in`.env` file using environment variables.

**If the server fails with "Authentication Failed":**
The `REFRESH_TOKEN` has likely expired or been revoked. You must generate a new one.

1. Run the helper script included in this folder:
   ```bash
   python get_new_token.py
   ```
2. Follow the on-screen instructions to authorize the app in your browser.
3. Paste the code back into the terminal.
4. Copy the new **Refresh Token** output.
5. Update your `.env` file with the new `MENDELEY_REFRESH_TOKEN`.
6. Restart the server.

### Adding Features
- The frontend is in `static/index.html` (Vanilla JS/HTML).
- The backend logic is in `main.py` (FastAPI).

## Troubleshooting

- **502 Bad Gateway**: Usually means Mendeley rejected the specific file (too large, corrupt, or rate limited). Check `mendeley_uploader.log`.
- **400 Bad Request**: Often a header issue (fixed in current version).
- **Stuck Progress**: The server retries automatically, but if it hangs, check the console output.
