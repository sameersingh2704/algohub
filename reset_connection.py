"""
reset_connection.py — One-time fix for Angel One 429 "Connection Limit Exceeded"

Run this ONCE when the algo is stopped and you keep getting 429 errors:

    python reset_connection.py

What it does:
  1. Logs in to Angel One to get a fresh JWT token
  2. Calls terminateSession() — forces Angel One to drop all WebSocket
     connections associated with your API key / client code
  3. Waits 5 seconds for the server to register the close
  4. Logs in again to verify connectivity is restored

After this completes, run 'python main.py' as normal.
"""

import asyncio
import sys
import time
from pathlib import Path

import yaml
from SmartApi import SmartConnect
import pyotp


def load_creds() -> dict:
    path = Path("config/credentials.yaml")
    if not path.exists():
        print("ERROR: config/credentials.yaml not found")
        sys.exit(1)
    with open(path) as f:
        return yaml.safe_load(f)["angel_one"]


def login(creds: dict) -> SmartConnect:
    api = SmartConnect(api_key=creds["api_key"])
    totp = pyotp.TOTP(creds["totp_secret"]).now()
    data = api.generateSession(creds["client_id"], creds["client_password"], totp)
    if not data or data.get("status") is False:
        raise RuntimeError(f"Login failed: {data.get('message', 'unknown')}")
    print(f"  Logged in as {creds['client_id']}")
    return api


def main() -> None:
    print("\n=== Angel One Connection Reset ===\n")
    creds = load_creds()

    # Step 1: login
    print("Step 1: Logging in...")
    try:
        api = login(creds)
    except Exception as e:
        print(f"  ERROR: {e}")
        print("\nCould not log in. Check credentials and network, then try again.")
        sys.exit(1)

    # Step 2: terminate session (drops all WebSocket connections on Angel One side)
    print("Step 2: Terminating session to clear WebSocket connection slots...")
    try:
        api.terminateSession(creds["client_id"])
        print("  Session terminated.")
    except Exception as e:
        print(f"  WARNING: terminateSession returned an error (may still have worked): {e}")

    # Step 3: wait for Angel One server to register the close
    print("Step 3: Waiting 5 seconds for connections to drain on Angel One's server...")
    for i in range(5, 0, -1):
        print(f"  {i}...", end="\r", flush=True)
        time.sleep(1)
    print("  Done.          ")

    # Step 4: login again to confirm connectivity is restored
    print("Step 4: Verifying fresh login works...")
    try:
        api2 = login(creds)
        feed_token = api2.getfeedToken()
        print(f"  Fresh login OK. Feed token received.")
        # Immediately terminate this verification session too — we don't want to
        # leave it open, since main.py will create its own session on startup.
        try:
            api2.terminateSession(creds["client_id"])
        except Exception:
            pass
    except Exception as e:
        print(f"  ERROR on fresh login: {e}")
        print("\nFresh login failed. You may need to wait a few more minutes.")
        sys.exit(1)

    print("\n✓ Connection reset complete.")
    print("  You can now run: python main.py\n")


if __name__ == "__main__":
    main()
