"""Use one verified product photograph when official PDFs provide alternatives.

Only photographs that survived the quality gate may be reused. A photograph
from a different notice is marked as representative, never as proof of the
specific lot or package. No notification state is touched here.
"""
import hashlib
import json
import re
import shutil
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

from image_quality_guard import analyse

RECALLS = Path("recalls.json")
IMAGES = Path("images")
RAW = ("https://raw.githubusercontent.com/calcagni1950srl-hash/"
       "richiami-italia-updater/refs/heads/main/images/")

def normalized(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(x for x in text if not unicodedata.combining(x))
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))

def brand_of(recall):
    val = normalized(recall.get("marca", "") or recall.get("brand", ""))
    return "" if val in ("", "no brand", "senza marca", "no marca") else val

def file_owned_by_recall(recall):
    url = str(recall.get("immagine", "") or "").strip()
    rid = str(recall.get("id", "") or "").strip()
    if "/images/" not in url or not rid:
        return None
    name = Path(urlparse(url).path).name
    if not re.fullmatch(re.escape(rid) + r"-[a-f0-9]{10}\.png", name):
        return None
    path = IMAGES / name
    return path if path.is_file() else None

def main():
    data = json.loads(RECALLS.read_text(encoding="utf-8"))
    rows = data.get("recalls", [])
    validated = []
    for row in rows:
        path = file_owned_by_recall(row)
        if path is None:
            continue
        quality = analyse(path)
        if quality.get("severe") or not quality.get("valid"):
            continue
        validated.append((row, path, quality))

    filled = 0
    for row in rows:
        rid = str(row.get("id", "") or "").strip()
        name = normalized(row.get("prodotto", "") or row.get("productName", ""))
        if not rid or not name or file_owned_by_recall(row) is not None:
            continue
        brand = brand_of(row)
        matches = []
        for source, path, quality in validated:
            if source.get("id") == rid:
                continue
            if normalized(source.get("prodotto", "") or source.get("productName", "")) != name:
                continue
            source_brand = brand_of(source)
            if brand and source_brand and brand != source_brand:
                continue
            # Identical denomination (and non-conflicting brand) required.
            # A provenance link prevents suggesting it proves the target lot.
            matches.append((float(quality.get("score", 0)), source, path))
        if not matches:
            continue
        score, source, source_path = max(matches, key=lambda x: x[0])
        raw = source_path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()[:10]
        new_name = f"{rid}-{digest}.png"
        new_path = IMAGES / new_name
        if not new_path.exists():
            shutil.copyfile(source_path, new_path)
        row["immagine"] = RAW + new_name
        notes = row.setdefault("note", [])
        if not isinstance(notes, list):
            notes = []
            row["note"] = notes
        message = ("Foto rappresentativa dello stesso prodotto dal PDF ufficiale "
                   f"del richiamo {source.get('id')}; non prova la corrispondenza "
                   "del lotto o del confezionamento.")
        if message not in notes:
            notes.append(message)
        print(f"Foto rappresentativa assegnata: {rid} <- {source.get('id')}; "
              f"punteggio qualita' {score:.2f}")
        filled += 1

    if filled:
        RECALLS.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8"
        )
    print(f"Foto rappresentative recuperate da richiami identici: {filled}")

if __name__ == "__main__":
    main()
