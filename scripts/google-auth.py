#!/usr/bin/env python3
"""One-time Google OAuth 2.0 setup for mcp-brain Google Calendar integration.

Run this on any machine with a web browser (your laptop, not the LXC).
It will guide you through the OAuth consent flow and print the
GOOGLE_REFRESH_TOKEN to paste into your .env file on the LXC.

Prerequisites:
  1. Go to https://console.cloud.google.com
  2. Create a new project (or use existing)
  3. Enable the "Google Calendar API"
  4. Go to Credentials → Create Credentials → OAuth 2.0 Client ID
  5. Application type: "Desktop app"
  6. Note the Client ID and Client Secret

Usage:
  python scripts/google-auth.py <client_id> <client_secret>
"""

import json
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"  # Manual copy-paste flow
_SCOPES = "https://www.googleapis.com/auth/calendar.readonly https://www.googleapis.com/auth/calendar.events"


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: python scripts/google-auth.py <client_id> <client_secret>")
        print()
        print("Get credentials from: https://console.cloud.google.com/apis/credentials")
        sys.exit(1)

    client_id = sys.argv[1]
    client_secret = sys.argv[2]

    # Step 1: Generate authorization URL
    auth_params = urlencode({
        "client_id": client_id,
        "redirect_uri": _REDIRECT_URI,
        "response_type": "code",
        "scope": _SCOPES,
        "access_type": "offline",
        "prompt": "consent",
    })
    auth_url = f"{_AUTH_URL}?{auth_params}"

    print()
    print("=" * 60)
    print("  mcp-brain — Google Calendar Authorization")
    print("=" * 60)
    print()
    print("Open this URL in your browser:")
    print()
    print(f"  {auth_url}")
    print()
    print("Sign in with your Google account and click 'Allow'.")
    print("Google will show you an authorization code.")
    print()

    # Step 2: Get the authorization code from user
    auth_code = input("Paste the authorization code here: ").strip()
    if not auth_code:
        print("Error: no authorization code provided.")
        sys.exit(1)

    # Step 3: Exchange code for tokens
    token_body = urlencode({
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": auth_code,
        "redirect_uri": _REDIRECT_URI,
    }).encode()

    req = Request(_TOKEN_URL, data=token_body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"Error exchanging code for token: {e}")
        sys.exit(1)

    refresh_token = data.get("refresh_token")
    if not refresh_token:
        print("Error: no refresh_token in response. Did you set prompt=consent?")
        print(f"Response: {json.dumps(data, indent=2)}")
        sys.exit(1)

    # Step 4: Print the result
    print()
    print("=" * 60)
    print("  Success! Add these to your .env on the LXC:")
    print("=" * 60)
    print()
    print(f"GOOGLE_CLIENT_ID={client_id}")
    print(f"GOOGLE_CLIENT_SECRET={client_secret}")
    print(f"GOOGLE_REFRESH_TOKEN={refresh_token}")
    print()
    print("Then restart mcp-brain:")
    print("  cd /opt/mcp-brain && docker compose down && docker compose up -d")
    print()


if __name__ == "__main__":
    main()
