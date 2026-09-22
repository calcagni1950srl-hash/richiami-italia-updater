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
MAX_SUCCESSFUL_ATTEMPTS = 3


def load_json(path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


pending = load_json(PENDING_PATH, {})
state = load_json(
    STATE_PATH,
    {"version": 2, "notifiedIds": [], "deliveryAttempts": {}},
)

pending_items = pending.get("newItems", []) or []

existing_ids = {
    str(value).strip()
    for value in (state.get("notifiedIds", []) or [])
    if str(value).strip()
}

attempts = {
    str(key).strip(): int(value or 0)
    for key, value in (state.get("deliveryAttempts", {}) or {}).items()
    if str(key).strip()
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
                "completedIds": [],
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    state["version"] = 2
    state["lastCheckedAt"] = pending.get("checkedAt") or datetime.now(timezone.utc).isoformat()
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("Nessun nuovo richiamo: nessuna notifica da inviare.")
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

ids = [
    str(item.get("id", "") or "").strip()
    for item in new_items
    if str(item.get("id", "") or "").strip()
]
stable_key = "|".join(sorted(ids))
tag_hash = hashlib.sha256(stable_key.encode("utf-8")).hexdigest()[:16]
notification_tag = f"richiami_{tag_hash}"

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
            "collapse_key": notification_tag,
            "ttl": "3600s",
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
            "richiamo_id": ids[0] if len(ids) == 1 else "",
            "url_ministero": str(new_items[0].get("link", "")) if len(new_items) == 1 else "",
            "retry_group": notification_tag,
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
        print(f"Firebase tentativo HTTP {attempt}/3:", response.status_code)
        if response.ok:
            break
        print("Risposta Firebase:", response.text)
    except requests.RequestException as error:
        print(f"Errore Firebase tentativo HTTP {attempt}/3:", error)

    if attempt < 3:
        time.sleep(5)

if response is None or not response.ok:
    raise SystemExit("ERRORE durante l'invio Firebase. Stato notifiche NON aggiornato.")

message_id = ""
try:
    message_id = str(response.json().get("name", "")).strip()
except Exception:
    pass

first_attempt_count = 0
completed_ids = []
for rid in ids:
    previous = int(attempts.get(rid, 0) or 0)
    if previous == 0:
        first_attempt_count += 1

    current = previous + 1
    if current >= MAX_SUCCESSFUL_ATTEMPTS:
        existing_ids.add(rid)
        attempts.pop(rid, None)
        completed_ids.append(rid)
    else:
        attempts[rid] = current

state["version"] = 2
state["notifiedIds"] = sorted(existing_ids)
state["deliveryAttempts"] = dict(sorted(attempts.items()))
state["lastCheckedAt"] = pending.get("checkedAt") or datetime.now(timezone.utc).isoformat()
state["lastNotificationAt"] = datetime.now(timezone.utc).isoformat()
state["lastNotificationIds"] = ids
state["lastFirebaseMessageId"] = message_id
state["lastNotificationAttempt"] = {
    rid: (
        MAX_SUCCESSFUL_ATTEMPTS
        if rid in completed_ids
        else attempts.get(rid, 0)
    )
    for rid in ids
}

STATE_PATH.write_text(
    json.dumps(state, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)

RESULT_PATH.write_text(
    json.dumps(
        {
            "sentCount": len(new_items),
            "firstAttemptCount": first_attempt_count,
            "messageId": message_id,
            "completedIds": completed_ids,
        },
        ensure_ascii=False,
        indent=2,
    ) + "\n",
    encoding="utf-8",
)

print("✅ Notifica Firebase accettata dal topic richiami.")
print("Titolo:", title)
print("Testo:", body)
print("Tag/collapse key:", notification_tag)
print("Tentativi riusciti per ID:")
for rid in ids:
    done = rid in existing_ids
    count = MAX_SUCCESSFUL_ATTEMPTS if done else attempts.get(rid, 0)
    print(f" - {rid}: {count}/{MAX_SUCCESSFUL_ATTEMPTS}" + (" COMPLETATO" if done else ""))
if message_id:
    print("Message ID:", message_id)
