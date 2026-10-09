"""Safe image-only merge: never replace the live recall list.

The image job works on a snapshot. Publish only independently validated
image URLs for the same notice ID/PDF, and keep all concurrently arrived
RSS records, notifications, corrections and live metadata unchanged.
"""
import hashlib
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse


def by_id(data):
    return {
        str(r.get("id", "") or "").strip(): r
        for r in data.get("recalls", [])
        if str(r.get("id", "") or "").strip()
    }


def verified_image(url, rid, candidate_dir):
    url = str(url or "").strip()
    if "/images/" not in url:
        return None
    filename = Path(urlparse(url).path).name
    if not re.fullmatch(re.escape(rid) + r"-[a-f0-9]{10}\.png", filename):
        return None
    source = candidate_dir / filename
    if not source.is_file():
        return None
    if hashlib.sha256(source.read_bytes()).hexdigest()[:10] != filename[-14:-4]:
        return None
    return source


def apply_images(before, repaired, live, candidates, images_dir):
    old_rows, fresh_rows = by_id(before), by_id(repaired)
    changed = []
    for rid, fixed in fresh_rows.items():
        old = old_rows.get(rid)
        actual = by_id(live).get(rid)
        if not old or not actual:
            continue
        # A newer notice with the same slug must not inherit a stale photo.
        if (
            actual.get("pdfMinistero", "") != old.get("pdfMinistero", "")
            or actual.get("lotto", "") != old.get("lotto", "")
        ):
            continue

        old_image = str(old.get("immagine", "") or "").strip()
        new_image = str(fixed.get("immagine", "") or "").strip()
        live_image = str(actual.get("immagine", "") or "").strip()
        if not new_image or new_image == old_image:
            continue
        if live_image not in ("", old_image, new_image):
            # Another completed run already published a different photo.
            continue
        source = verified_image(new_image, rid, candidates)
        if source is None:
            print("Foto ignorata (ID, file o hash non valido):", rid)
            continue

        target = images_dir / source.name
        if not target.exists():
            images_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        if target.read_bytes() != source.read_bytes():
            print("Foto ignorata (collisione nome):", rid)
            continue
        if actual.get("immagine", "") != new_image:
            actual["immagine"] = new_image
            changed.append(rid)

        for note in fixed.get("note", []):
            if not str(note).startswith("Foto rappresentativa dello stesso prodotto"):
                continue
            notes = actual.get("note")
            if not isinstance(notes, list):
                notes = []
                actual["note"] = notes
            if note not in notes:
                notes.append(note)

    return changed


def self_test():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        candidates = root / "candidates"
        candidates.mkdir()
        payload = b"\x89PNG\r\n\x1a\n\x00test"
        digest = hashlib.sha256(payload).hexdigest()[:10]
        filename = f"food-{digest}.png"
        (candidates / filename).write_bytes(payload)
        good_url = f"https://raw.githubusercontent.com/x/y/main/images/{filename}"
        base = {"recalls": [{"id": "food", "pdfMinistero": "p1", "lotto": "L1", "immagine": ""}]}
        out = {"recalls": [{"id": "food", "pdfMinistero": "p1", "lotto": "L1", "immagine": good_url}]}
        latest = {"recalls": [
            {"id": "food", "pdfMinistero": "p1", "lotto": "L1", "immagine": ""},
            {"id": "new-rss", "stato": "RSS", "immagine": ""},
        ]}
        updated = apply_images(base, out, latest, candidates, root / "images")
        assert updated == ["food"]
        assert len(latest["recalls"]) == 2
        assert latest["recalls"][1]["id"] == "new-rss"
        latest["recalls"][0]["pdfMinistero"] = "different"
        latest["recalls"][0]["immagine"] = ""
        assert not apply_images(base, out, latest, candidates, root / "images")
        latest["recalls"][0]["pdfMinistero"] = "p1"
        latest["recalls"][0]["immagine"] = "https://somewhere/newer-validated.png"
        assert not apply_images(base, out, latest, candidates, root / "images")
        print("PASS: nuovi richiami preservati, cambio PDF e immagini concorrenti protetti")


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "--self-test":
        self_test()
        return
    if len(sys.argv) != 5:
        raise SystemExit(
            "Uso: merge_image_updates.py base.json risultato.json live.json candidate_images/"
        )
    base_path, repaired_path, live_path, candidates = map(Path, sys.argv[1:])
    before = json.loads(base_path.read_text(encoding="utf-8"))
    repaired = json.loads(repaired_path.read_text(encoding="utf-8"))
    live = json.loads(live_path.read_text(encoding="utf-8"))
    ids = apply_images(before, repaired, live, candidates, Path("images"))
    if ids:
        live_path.write_text(
            json.dumps(live, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    print(f"Immagini aggiornate senza toccare nuovi richiami: {len(ids)}", ", ".join(ids))


if __name__ == "__main__":
    main()
