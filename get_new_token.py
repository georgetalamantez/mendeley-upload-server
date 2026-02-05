import requests
import json
import os
import webbrowser

from dotenv import load_dotenv

load_dotenv()

# Configuration
CLIENT_ID = os.getenv("MENDELEY_CLIENT_ID")
CLIENT_SECRET = os.getenv("MENDELEY_CLIENT_SECRET")
REDIRECT_URI = os.getenv("MENDELEY_REDIRECT_URI", "http://localhost:8585/callback")
AUTH_URL = "https://api.mendeley.com/oauth/authorize"
TOKEN_URL = "https://api.mendeley.com/oauth/token"

if not CLIENT_ID or not CLIENT_SECRET:
    print("Error: MENDELEY_CLIENT_ID or MENDELEY_CLIENT_SECRET not found in .env")
    exit(1)

def get_new_token():
    print("=" * 60)
    print("Mendeley Token Generator")
    print("=" * 60)
    print("This script will help you generate a new Refresh Token.")
    
    # 1. Authorize
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "all"
    }
    
    auth_request_url = requests.Request('GET', AUTH_URL, params=params).prepare().url
    
    print("\n1. Opening browser to authorize application...")
    print(f"   URL: {auth_request_url}")
    webbrowser.open(auth_request_url)
    
    print("\n2. After authorizing, you will be redirected to a URL (e.g., localhost... or error page).")
    print("   Look at the URL bar in your browser.")
    print("   Copy the 'code' parameter value from the URL.")
    print("   Example: http://localhost:8585/callback?code=YOUR_CODE_HERE\n")
    
    code = input(">> Paste the code here: ").strip()
    
    if not code:
        print("Error: No code provided.")
        return

    # 2. Exchange for Token
    print("\n3. Exchanging code for tokens...")
    
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET
    }
    
    try:
        response = requests.post(TOKEN_URL, data=payload)
        
        if response.status_code == 200:
            data = response.json()
            refresh_token = data.get("refresh_token")
            access_token = data.get("access_token")
            
            print("\n" + "=" * 60)
            print("SUCCESS! Credentials Obtained")
            print("=" * 60)
            print("\nREFRESH TOKEN (Copy this to main.py):")
            print("-" * 20)
            print(refresh_token)
            print("-" * 20)
            
            # Optional: Update main.py automatically?
            # For safety, we just print it.
            
        else:
            print(f"\nError exchanging token: {response.status_code}")
            print(response.text)
            
    except Exception as e:
        print(f"\nException occurred: {e}")

if __name__ == "__main__":
    get_new_token()
    input("\nPress Enter to exit...")
