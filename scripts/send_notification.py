import hashlib
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
state = load_json(
    STATE_PATH,
    {"version": 3, "notifiedIds": []},
)

pending_items = pending.get("newItems", []) or []

existing_ids = {
    str(value).strip()
    for value in (state.get("notifiedIds", []) or [])
    if str(value).strip()
}

new_items = [
    item
    for item in pending_items
    if str(item.get("id", "") or "").strip()
    and str(item.get("id", "") or "").strip() not in existing_ids
]

if not new_items:
    RESULT_PATH.write_text(
        json.dumps(
            {
                "sentCount": 0,
                "firstAttemptCount": 0,
                "messageId": "",
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    state["version"] = 3
    state.pop("deliveryAttempts", None)
    state.pop("lastNotificationAttempt", None)
    state["lastCheckedAt"] = (
        pending.get("checkedAt")
        or datetime.now(timezone.utc).isoformat()
    )
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("Nessun nuovo richiamo: nessuna notifica da inviare.")
    raise SystemExit(0)

service_account_json = os.environ.get(
    "FIREBASE_SERVICE_ACCOUNT", ""
).strip()
if not service_account_json:
    raise SystemExit(
        "ERRORE: secret FIREBASE_SERVICE_ACCOUNT mancante."
    )

try:
    service_account_info = json.loads(service_account_json)
except json.JSONDecodeError as error:
    raise SystemExit(
        "ERRORE: FIREBASE_SERVICE_ACCOUNT non contiene JSON valido."
    ) from error

project_id = str(
    service_account_info.get("project_id", "")
).strip()
if not project_id:
    raise SystemExit(
        "ERRORE: project_id assente nel service account Firebase."
    )

credentials = service_account.Credentials.from_service_account_info(
    service_account_info,
    scopes=["https://www.googleapis.com/auth/firebase.messaging"],
)
credentials.refresh(Request())

if len(new_items) == 1:
    item = new_items[0]
    title = "Nuovo richiamo alimentare"
    feed_title = str(item.get("title", "") or "").strip()
    body = (
        feed_title
        or "Il Ministero della Salute ha pubblicato un nuovo richiamo."
    )
else:
    title = f"{len(new_items)} nuovi richiami alimentari"
    body = "Il Ministero della Salute ha pubblicato nuovi richiami."

ids = [
    str(item.get("id", "") or "").strip()
    for item in new_items
    if str(item.get("id", "") or "").strip()
]

stable_key = "|".join(sorted(ids))
tag_hash = hashlib.sha256(
    stable_key.encode("utf-8")
).hexdigest()[:16]
notification_tag = f"richiami_{tag_hash}"

endpoint = (
    f"https://fcm.googleapis.com/v1/projects/"
    f"{project_id}/messages:send"
)

payload = {
    "message": {
        "topic": "richiami",
        "notification": {
            "title": title,
            "body": body,
        },
        "android": {
            "priority": "high",
            "collapse_key": notification_tag,
            # È Firebase a ritentare la consegna quando il telefono è
            # temporaneamente offline. Il server non invia più copie
            # duplicate dello stesso richiamo nei controlli successivi.
            "ttl": "86400s",
            "notification": {
                "sound": "default",
                "tag": notification_tag,
                "notification_priority": "PRIORITY_MAX",
                "default_sound": True,
                "default_vibrate_timings": True,
            },
        },
        "data": {
            "tipo": "nuovi_richiami",
            "numero": str(len(new_items)),
            "richiamo_id": (
                ids[0] if len(ids) == 1 else ""
            ),
            "url_ministero": (
                str(new_items[0].get("link", "") or "")
                if len(new_items) == 1
                else ""
            ),
            "notification_group": notification_tag,
        },
    }
}

# Una sola richiesta per evento. Un timeout può avvenire DOPO che FCM ha
# già accettato il messaggio: ritentare qui può quindi creare duplicati.
# I controlli successivi sono gestiti dallo stato persistente.
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
except requests.RequestException as error:
    raise SystemExit(
        "ERRORE Firebase: esito invio incerto; nessun retry immediato. "
        + str(error)
    ) from error

print("Firebase HTTP:", response.status_code)
if not response.ok:
    print("Risposta Firebase:", response.text)
    raise SystemExit(
        "ERRORE durante l'invio Firebase. "
        "Stato notifiche NON aggiornato."
    )

message_id = ""
try:
    message_id = str(
        response.json().get("name", "")
    ).strip()
except Exception:
    pass

existing_ids.update(ids)

state["version"] = 3
state["notifiedIds"] = sorted(existing_ids)
state.pop("deliveryAttempts", None)
state.pop("lastNotificationAttempt", None)
state["lastCheckedAt"] = (
    pending.get("checkedAt")
    or datetime.now(timezone.utc).isoformat()
)
state["lastNotificationAt"] = (
    datetime.now(timezone.utc).isoformat()
)
state["lastNotificationIds"] = ids
state["lastFirebaseMessageId"] = message_id

STATE_PATH.write_text(
    json.dumps(
        state,
        ensure_ascii=False,
        indent=2,
    ) + "\n",
    encoding="utf-8",
)

RESULT_PATH.write_text(
    json.dumps(
        {
            "sentCount": len(new_items),
            "firstAttemptCount": len(new_items),
            "messageId": message_id,
        },
        ensure_ascii=False,
        indent=2,
    ) + "\n",
    encoding="utf-8",
)

print("✅ Unica notifica Firebase accettata dal topic richiami.")
print("Titolo:", title)
print("Testo:", body)
print("Tag/collapse key:", notification_tag)
if message_id:
    print("Message ID:", message_id)
