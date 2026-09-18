import json
from datetime import datetime, timezone
from pathlib import Path

PENDING_PATH = Path("notification-pending.json")
RECALLS_PATH = Path("recalls.json")


def load_json(path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


pending = load_json(PENDING_PATH, {})
new_items = pending.get("newItems", []) or []

recalls = load_json(
    RECALLS_PATH,
    {
        "version": 2,
        "generatedAt": "",
        "source": "Ministero della Salute",
        "feed": "RSS_avvisi_richiami_osa.xml",
        "totale": 0,
        "pass": 0,
        "daVerificare": 0,
        "recalls": [],
    },
)

existing_ids = {
    str(item.get("id", "") or "").strip()
    for item in recalls.get("recalls", [])
    if str(item.get("id", "") or "").strip()
}

provisional = []

for item in new_items:
    rid = str(item.get("id", "") or "").strip()
    if not rid or rid in existing_ids:
        continue

    title = (
        str(item.get("title", "") or "").strip()
        or rid.replace("-", " ").title()
    )

    provisional.append(
        {
            "id": rid,
            "marca": "",
            "prodotto": title,
            "lotto": "",
            "tmc": "",
            "produttore": "",
            "motivo": "Dati del richiamo in aggiornamento",
            "dataPubblicazione": str(item.get("pubDate", "") or "").strip(),
            "urlMinistero": str(item.get("link", "") or "").strip(),
            "pdfMinistero": "",
            "immagine": "",
            "criterioMatch": "RSS_MINISTERO",
            "stato": "DA_VERIFICARE",
            "metodoEstrazione": "RSS",
            "note": [
                "Richiamo rilevato dal feed ufficiale; dettagli e foto in aggiornamento"
            ],
        }
    )
    existing_ids.add(rid)

if not provisional:
    print("Nessun nuovo richiamo da aggiungere alla lista.")
    raise SystemExit(0)

recalls["recalls"] = provisional + (recalls.get("recalls", []) or [])
recalls["totale"] = len(recalls["recalls"])
recalls["pass"] = sum(
    1
    for item in recalls["recalls"]
    if str(item.get("stato", "") or "").strip().upper() == "PASS"
)
recalls["daVerificare"] = recalls["totale"] - recalls["pass"]
recalls["generatedAt"] = datetime.now(timezone.utc).isoformat()

RECALLS_PATH.write_text(
    json.dumps(recalls, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)

print("✅ Richiami provvisori aggiunti alla lista:", len(provisional))
for item in provisional:
    print(" -", item["id"], "-", item["prodotto"])
