"""
Run this script ONCE locally to generate a Google OAuth token.
Then paste the printed JSON into Streamlit Cloud secrets as GOOGLE_TOKEN_JSON.

Usage:
    python generate_token.py
"""
import json
import os

from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

load_dotenv()

client_config = {
    "installed": {
        "client_id": os.getenv("GOOGLE_API_OAUTH_CLIENT_ID"),
        "client_secret": os.getenv("GOOGLE_API_OAUTH_CLIENT_SECRET"),
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
}

SCOPES = ["https://www.googleapis.com/auth/business.manage"]

flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
creds = flow.run_local_server(port=0)

token_data = {
    "token": creds.token,
    "refresh_token": creds.refresh_token,
    "token_uri": creds.token_uri,
    "client_id": creds.client_id,
    "client_secret": creds.client_secret,
    "scopes": list(creds.scopes) if creds.scopes else [],
}

print("\n--- COPY EVERYTHING BELOW INTO STREAMLIT SECRETS AS GOOGLE_TOKEN_JSON ---\n")
print(f"GOOGLE_TOKEN_JSON = '{json.dumps(token_data)}'")
print("\n--- END ---\n")
