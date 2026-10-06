"""Gets a long-lived Threads token for ShibRadar (one-time setup).

Run locally — never in GitHub Actions, never commit the output.

  1) python tools/threads_token.py url
       Prints the login link. Open it logged in as @ShibRadar and accept.
       You are sent to the ShibRadar site, which shows the CODE.

  2) python tools/threads_token.py code <CODE>
       Exchanges the code for a long-lived token (60 days), checks it and
       prints the two GitHub Secrets to create: THREADS_ACCESS_TOKEN and a
       random THREADS_TOKEN_KEY. From then on the bot renews the token itself.

  Alternative to 1+2, if you generated a token in the Meta dashboard:
     python tools/threads_token.py token <SHORT_LIVED_TOKEN>

The App ID and App Secret are read from THREADS_APP_ID / THREADS_APP_SECRET
or asked interactively (the secret is not echoed).
"""

import os
import sys
import getpass
import secrets
from urllib.parse import urlencode

import requests

REDIRECT_URI = "https://shibradar.github.io/shibradar/"
SCOPES = "threads_basic,threads_content_publish"
GRAPH = "https://graph.threads.net"


def app_id():
    return os.getenv("THREADS_APP_ID") or input("Threads App ID: ").strip()


def app_secret():
    return os.getenv("THREADS_APP_SECRET") or getpass.getpass("Threads App Secret: ").strip()


def check(r):
    if not r.ok:
        sys.exit(f"Error {r.status_code}: {r.text}")
    return r.json()


def long_lived(short_token, secret):
    data = check(requests.get(f"{GRAPH}/access_token", params={
        "grant_type": "th_exchange_token",
        "client_secret": secret,
        "access_token": short_token,
    }, timeout=30))
    return data["access_token"], data.get("expires_in", 0)


def report(token, expires_in):
    me = check(requests.get(f"{GRAPH}/v1.0/me", params={
        "fields": "id,username", "access_token": token,
    }, timeout=30))
    print(f"\nOK — account @{me['username']} (id {me['id']}), "
          f"valid for ~{expires_in // 86400} days.\n")
    print("Create these two GitHub Secrets "
          "(repository > Settings > Secrets and variables > Actions):\n")
    print("THREADS_ACCESS_TOKEN")
    print(token)
    print("\nTHREADS_TOKEN_KEY")
    print(secrets.token_urlsafe(32))
    print("\nThe key encrypts the token the bot renews by itself. Keep both private;")
    print("if you ever replace the token, the same key can stay.\n")


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("url", "code", "token"):
        sys.exit(__doc__)

    step = sys.argv[1]

    if step == "url":
        print(f"https://threads.net/oauth/authorize?" + urlencode({
            "client_id": app_id(),
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "response_type": "code",
        }))
        return

    if len(sys.argv) < 3:
        sys.exit(__doc__)
    value = sys.argv[2].strip().removesuffix("#_")
    cid, secret = app_id(), app_secret()

    if step == "code":
        short = check(requests.post(f"{GRAPH}/oauth/access_token", data={
            "client_id": cid,
            "client_secret": secret,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
            "code": value,
        }, timeout=30))["access_token"]
    else:
        short = value

    report(*long_lived(short, secret))


if __name__ == "__main__":
    main()
