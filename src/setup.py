import base64
import json
import os
import socket

# Set a process-wide default socket timeout to prevent indefinite socket hangs
socket.setdefaulttimeout(120)

import dotenv
import gspread
import requests

from retry_utils import retry_call
from run_scheduled import run_scheduled
from update import run_update
from train_model import train_model

if not os.environ.get("IS_DOCKER", False):
    dotenv.load_dotenv()
else:
    for key, value in os.environ.items():
        if isinstance(value, str):
            os.environ[key] = value.strip('\'"\r\n')

###
# SimpleFIN setup
###
if not os.path.exists(
    os.path.join(os.environ.get("CONFIG_PATH"), "simplefin_auth.json")
):
    print("No prior SimpleFIN credentials found, creating from token...", flush=True)
    sf_token = os.environ.get("SIMPLEFIN_BRIDGE_TOKEN")
    res = retry_call(requests.post, base64.b64decode(sf_token), timeout=(15, 90))

    if res.status_code != 200:
        print(
            "SimpleFIN setup token invalid. Has it been used already? If so, delete the token from your account, generate a new one, and update .env.",
            flush=True
        )
        exit()
    else:
        print("SimpleFIN setup token valid. Saving access credentials.", flush=True)

    access_url = res.text
    scheme, rest = access_url.split("//", 1)
    auth, rest = rest.split("@", 1)
    url = scheme + "//" + rest + "/accounts"
    username, password = auth.split(":", 1)

    simplefin_data = {"url": url, "username": username, "password": password}

    # Save SimpleFIN access credentials to config file
    with open(
        os.path.join(os.environ.get("CONFIG_PATH"), "simplefin_auth.json"), "w+"
    ) as f:
        f.write(json.dumps(simplefin_data))
        print("SimpleFIN credentials saved.", flush=True)

print("Validating SimpleFIN credentials...", flush=True)
sf_auth = json.load(
    open(os.path.join(os.environ.get("CONFIG_PATH"), "simplefin_auth.json"))
)
res = retry_call(requests.get, sf_auth["url"], auth=(sf_auth["username"], sf_auth["password"]), timeout=(15, 90))
if res.status_code != 200:
    print(
        "SimpleFIN credentials invalid. Please check your credentials and try again. Specific setup information is available in the README.",
        flush=True
    )
    exit()
print("SimpleFIN config validated.", flush=True)

###
# Google Sheets setup
###
print("Validating Google Sheets config...")
try:
    gc = retry_call(gspread.service_account, filename=os.path.join(os.environ.get("CONFIG_PATH"), "google_auth.json"))
    print("Google Sheets API access validated.")
except Exception as e:
    print(
        "Google Sheets API access invalid. Please check your credentials and try again. Specific setup information is available in the README."
    )
    exit()

try:
    sh = retry_call(gc.open_by_key, os.environ.get("SHEET_ID"))
except Exception as e:
    print(
        "Specified sheet ID invalid. Has the sheet been shared with the service account client email?"
    )
    print(f"Details: {e}")
    exit()

print("Google Sheets config validated.")

###
# initial pull
###
print("Building initial ML model for categorization...")
train_model()

print("Performing initial data pull... - Edited 3/3/2026")
run_update(days_to_fetch=int(os.environ.get("SETUP_PULL_PAST_DAYS")))

print("Setup complete!")

if os.environ.get("IS_DOCKER", False):
    print("Initializing scheduled updates...")
    run_scheduled()
