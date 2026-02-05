import os
import glob
import time
import logging
import requests
import json
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List, Optional

# --- Configuration ---
# --- Configuration ---
from dotenv import load_dotenv

load_dotenv()

MENDELEY_CLIENT_ID = os.getenv("MENDELEY_CLIENT_ID")
MENDELEY_CLIENT_SECRET = os.getenv("MENDELEY_CLIENT_SECRET")
MENDELEY_REFRESH_TOKEN = os.getenv("MENDELEY_REFRESH_TOKEN")
MENDELEY_REDIRECT_URI = os.getenv("MENDELEY_REDIRECT_URI", "http://localhost:8585/callback")

if not all([MENDELEY_CLIENT_ID, MENDELEY_CLIENT_SECRET, MENDELEY_REFRESH_TOKEN]):
    print("WARNING: Missing credentials in .env file.")

TOKEN_URL = "https://api.mendeley.com/oauth/token"
DOCUMENTS_URL = "https://api.mendeley.com/documents"
FILES_URL = "https://api.mendeley.com/files"

# --- Logging Setup ---
LOG_FILE = "mendeley_uploader.log"

class MemoryHandler(logging.Handler):
    def __init__(self, capacity=1000):
        super().__init__()
        self.capacity = capacity
        self.logs = []

    def emit(self, record):
        log_entry = self.format(record)
        self.logs.append(log_entry)
        if len(self.logs) > self.capacity:
            self.logs.pop(0)

# Create loggers
logger = logging.getLogger("mendeley_uploader")
logger.setLevel(logging.INFO)

# File handler
file_handler = logging.FileHandler(LOG_FILE, mode='w')
file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(file_formatter)
logger.addHandler(file_handler)

# Memory handler for UI
memory_handler = MemoryHandler()
memory_formatter = logging.Formatter('%(levelname)s: %(message)s')
memory_handler.setFormatter(memory_formatter)
logger.addHandler(memory_handler)

# Console handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(memory_formatter)
logger.addHandler(console_handler)

# --- State ---
class UploadState:
    is_running = False
    total_files = 0
    processed_files = 0
    current_file = ""
    status_message = "Idle"
    should_stop = False

state = UploadState()

# --- Application ---
app = FastAPI()

# Serve static files (Frontend)
app.mount("/static", StaticFiles(directory="static"), name="static")

# --- Helpers ---
def get_access_token():
    logger.info("Refreshing access token...")
    payload = {
        'grant_type': 'refresh_token',
        'refresh_token': MENDELEY_REFRESH_TOKEN,
        'client_id': MENDELEY_CLIENT_ID,
        'client_secret': MENDELEY_CLIENT_SECRET,
        'scope': 'all'
    }
    try:
        response = requests.post(TOKEN_URL, data=payload, timeout=10)
        if response.status_code == 200:
            logger.info("Access token refreshed.")
            return response.json()['access_token']
        else:
            logger.error(f"Failed to get token: {response.status_code} {response.text}")
            raise Exception(f"Authentication Failed: {response.text}")
    except Exception as e:
        logger.error(f"Network error refreshing token: {str(e)}")
        raise

def create_document(access_token, title):
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/vnd.mendeley-document.1+json'
    }
    data = {
        'title': title,
        'type': 'book'
    }
    try:
        response = requests.post(DOCUMENTS_URL, headers=headers, json=data, timeout=30)
        if response.status_code == 201:
            doc_id = response.json()['id']
            logger.info(f"Document created: id={doc_id}")
            return doc_id
        elif response.status_code == 429:
            logger.warning("Rate limit (create doc). Waiting 5s...")
            time.sleep(5)
            # Recursion is dangerous if infinite, but simple here
            return create_document(access_token, title)
        else:
            logger.error(f"Failed to create document '{title}': {response.status_code} - {response.text}")
            return None
    except Exception as e:
        logger.error(f"Exception creating document '{title}': {str(e)}")
        return None

def upload_file_content(access_token, document_id, file_path):
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/pdf',
        'Link': f'<{DOCUMENTS_URL}/{document_id}>; rel="document"',
        'Content-Disposition': f'attachment; filename="{os.path.basename(file_path)}"'
    }
    
    try:
        # Check file size for safety?
        # If huge, might need streaming. Mendeley max is usually 50MB via API?
        # For books > 50MB, simple POST might fail or timeout.
        
        with open(file_path, 'rb') as f:
            file_content = f.read()
            
        response = requests.post(FILES_URL, headers=headers, data=file_content, timeout=120)
        
        if response.status_code == 201:
            logger.info(f"File uploaded successfully for document {document_id}")
            return True
        elif response.status_code == 429:
            logger.warning("Rate limit (upload file). Waiting 5s...")
            time.sleep(5)
            return upload_file_content(access_token, document_id, file_path)
        else:
            logger.error(f"Failed to upload file content: {response.status_code} - {response.text}")
            return False
            
    except Exception as e:
        logger.error(f"Exception uploading file '{file_path}': {str(e)}")
        return False

def process_upload_task(path: str):
    logger.info(f"Starting upload task for path: {path}")
    state.is_running = True
    state.should_stop = False
    state.processed_files = 0
    state.total_files = 0
    
    # 1. Access Token
    try:
        access_token = get_access_token()
    except Exception:
        state.status_message = "Authentication Failed. Check Logs."
        state.is_running = False
        return

    # 2. Collect Files
    files_to_process = []
    if os.path.isfile(path):
        if path.lower().endswith('.pdf'):
            files_to_process.append(path)
    elif os.path.isdir(path):
        files_to_process = glob.glob(os.path.join(path, "*.pdf"))
    
    state.total_files = len(files_to_process)
    logger.info(f"Found {state.total_files} PDF files to process.")
    state.status_message = f"Processing directly from {path}"

    # 3. Process Loop
    for idx, file_path in enumerate(files_to_process):
        if state.should_stop:
            logger.info("Process stopped by user.")
            state.status_message = "Stopped."
            break
            
        filename = os.path.basename(file_path)
        state.current_file = filename
        title = os.path.splitext(filename)[0].replace('_', ' ')
        
        logger.info(f"[{idx+1}/{state.total_files}] Processing: {filename}")
        
        try:
            # Create Doc
            doc_id = create_document(access_token, title)
            if doc_id:
                # Upload File
                success = upload_file_content(access_token, doc_id, file_path)
                if success:
                    logger.info(f"SUCCESS: {filename}")
                else:
                    logger.error(f"FAILURE (upload): {filename}")
            else:
                 logger.error(f"FAILURE (metadata): {filename}")
                 
        except Exception as e:
            logger.error(f"CRITICAL ERROR on {filename}: {str(e)}")

        state.processed_files += 1
        
        # Polite delay
        time.sleep(1.0)
    
    state.is_running = False
    state.current_file = ""
    state.status_message = "Completed"
    logger.info("Batch processing finished.")


# --- API Models ---
class UploadRequest(BaseModel):
    path: str

# --- API Endpoints ---
@app.get("/")
def read_root():
    return {"message": "Mendeley Upload Server Running. Go to /static/index.html"}

@app.post("/api/start-upload")
async def start_upload(request: UploadRequest, background_tasks: BackgroundTasks):
    if state.is_running:
        raise HTTPException(status_code=400, detail="Job already running.")
    
    if not os.path.exists(request.path):
         raise HTTPException(status_code=404, detail="Path not found.")
         
    background_tasks.add_task(process_upload_task, request.path)
    return {"message": "Upload started", "path": request.path}

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
        "status_message": state.status_message
    }

@app.get("/api/logs")
def get_logs():
    return {"logs": memory_handler.logs}
