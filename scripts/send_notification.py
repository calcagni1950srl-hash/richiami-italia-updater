import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from google.auth.transport.requests import Request
from google.oauth2 import service_account

STATE_PATH = Path("notification-state.json")
PENDING_PATH = Path("notification-pending.json")
RESULT_PATH = Path("notification-result.json")


def load_json(path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


pending = load_json(PENDING_PATH, {})
state = load_json(STATE_PATH, {"version": 1, "notifiedIds": []})

pending_items = pending.get("newItems", []) or []

existing_ids = {
    str(value).strip()
    for value in (state.get("notifiedIds", []) or [])
    if str(value).strip()
}

# Ricalcola i nuovi elementi usando lo stato più recente presente su main.
# Questo evita doppie notifiche se un run era partito da un commit vecchio.
new_items = [
    item
    for item in pending_items
    if str(item.get("id", "") or "").strip()
    and str(item.get("id", "") or "").strip() not in existing_ids
]

feed_ids = [
    str(value).strip()
    for value in (pending.get("feedIds", []) or [])
    if str(value).strip()
]

def save_state(message_id=""):
    merged = sorted(existing_ids.union(feed_ids))
    state["version"] = 1
    state["notifiedIds"] = merged
    state["lastCheckedAt"] = pending.get("checkedAt") or datetime.now(timezone.utc).isoformat()

    if new_items:
        state["lastNotificationAt"] = datetime.now(timezone.utc).isoformat()
        state["lastNotificationIds"] = [
            str(item.get("id", "")).strip()
            for item in new_items
            if str(item.get("id", "")).strip()
        ]
        if message_id:
            state["lastFirebaseMessageId"] = message_id

    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if not new_items:
    RESULT_PATH.write_text(
        json.dumps({"sentCount": 0, "messageId": ""}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("Nessun nuovo richiamo: nessuna notifica da inviare.")
    print("Nessuna modifica a notification-state.json o recalls.json.")
    raise SystemExit(0)

service_account_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "").strip()
if not service_account_json:
    raise SystemExit("ERRORE: secret FIREBASE_SERVICE_ACCOUNT mancante.")

try:
    service_account_info = json.loads(service_account_json)
except json.JSONDecodeError as error:
    raise SystemExit("ERRORE: FIREBASE_SERVICE_ACCOUNT non contiene JSON valido.") from error

project_id = str(service_account_info.get("project_id", "")).strip()
if not project_id:
    raise SystemExit("ERRORE: project_id assente nel service account Firebase.")

credentials = service_account.Credentials.from_service_account_info(
    service_account_info,
    scopes=["https://www.googleapis.com/auth/firebase.messaging"],
)
credentials.refresh(Request())

if len(new_items) == 1:
    item = new_items[0]
    title = "Nuovo richiamo alimentare"
    feed_title = str(item.get("title", "")).strip()
    body = feed_title or "Il Ministero della Salute ha pubblicato un nuovo richiamo."
else:
    title = f"{len(new_items)} nuovi richiami alimentari"
    body = "Il Ministero della Salute ha pubblicato nuovi richiami."

endpoint = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"

payload = {
    "message": {
        "topic": "richiami",
        "notification": {
            "title": title,
            "body": body,
        },
        "android": {
            "priority": "high",
            "notification": {
                "sound": "default",
            },
        },
        "data": {
            "tipo": "nuovi_richiami",
            "numero": str(len(new_items)),
            "richiamo_id": str(new_items[0].get("id", "")) if len(new_items) == 1 else "",
            "url_ministero": str(new_items[0].get("link", "")) if len(new_items) == 1 else "",
        },
    }
}

response = None

for attempt in range(1, 4):
    try:
        response = requests.post(
            endpoint,
            headers={
                "Authorization": "Bearer " + credentials.token,
                "Content-Type": "application/json; charset=UTF-8",
            },
            json=payload,
            timeout=30,
        )

        print(f"Firebase tentativo {attempt}/3 HTTP:", response.status_code)

        if response.ok:
            break

        print("Risposta Firebase:", response.text)
    except requests.RequestException as error:
        print(f"Errore Firebase tentativo {attempt}/3:", error)

    if attempt < 3:
        time.sleep(5)

if response is None or not response.ok:
    raise SystemExit("ERRORE durante l'invio Firebase. Stato notifiche NON aggiornato.")

message_id = ""
try:
    message_id = str(response.json().get("name", "")).strip()
except Exception:
    pass

print("✅ Notifica Firebase inviata al topic richiami.")
print("Titolo:", title)
print("Testo:", body)
if message_id:
    print("Message ID:", message_id)

save_state(message_id)

RESULT_PATH.write_text(
    json.dumps(
        {"sentCount": len(new_items), "messageId": message_id},
        ensure_ascii=False,
        indent=2,
    ) + "\n",
    encoding="utf-8",
)
