import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import cv2
import numpy as np
from PIL import Image

RECALLS = Path("recalls.json")
IMAGES = Path("images")
BACKUP_ROOT = Path(".quality/pre-existing")
BACKUP_RECALLS = BACKUP_ROOT / "recalls.json"
BACKUP_IMAGES = BACKUP_ROOT / "images"

FORM_PHRASES = (
    "non consumare il prodotto",
    "inserire immagine",
    "restituirlo presso",
    "avvertenze",
    "motivo del richiamo",
    "acquistato",
    "riconsegnare il prodotto",
    "procedere al suo utilizzo",
)


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def image_filename(url: str) -> str:
    url = str(url or "").strip()
    if not url or "/images/" not in url:
        return ""
    return Path(urlparse(url).path).name


def ocr_text(path: Path) -> str:
    try:
        result = subprocess.run(
            ["tesseract", str(path), "stdout", "-l", "ita", "--psm", "11"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="ignore",
            timeout=45,
        )
        return result.stdout.lower()
    except Exception:
        return ""


def analyse(path: Path) -> dict:
    try:
        image = Image.open(path).convert("RGB")
    except Exception:
        return {
            "valid": False,
            "severe": True,
            "score": -999.0,
            "reason": "file immagine non leggibile",
        }

    arr = np.asarray(image)
    h, w = arr.shape[:2]
    if h <= 0 or w <= 0:
        return {
            "valid": False,
            "severe": True,
            "score": -999.0,
            "reason": "dimensioni nulle",
        }

    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
    edges = cv2.Canny(gray, 55, 145)

    red = arr[:, :, 0].astype(np.int16)
    green = arr[:, :, 1].astype(np.int16)
    blue = arr[:, :, 2].astype(np.int16)

    white_ratio = float(np.mean(np.all(arr >= 244, axis=2)))
    pale_blue_ratio = float(
        np.mean(
            (blue > 220)
            & (green > 210)
            & (red > 180)
            & ((blue - red) > 8)
        )
    )
    color_ratio = float(np.mean(hsv[:, :, 1] >= 20))
    contrast = float(gray.std())
    edge_ratio = float(np.mean(edges > 0))
    blur = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    ratio = max(w, h) / max(1, min(w, h))
    area = w * h

    text = ocr_text(path)
    form_hits = [phrase for phrase in FORM_PHRASES if phrase in text]
    ocr_words = re.findall(r"[a-zà-ÿ0-9]{3,}", text, flags=re.IGNORECASE)

    # Firma tipica della pagina/modulo Ministero: molto spazio bianco
    # accompagnato da una fascia azzurra ampia. È esattamente il difetto
    # che aveva lasciato visibili i moduli dei due salami.
    form_signature = white_ratio > 0.35 and pale_blue_ratio > 0.10

    # Ritaglio testuale del modulo: fondo molto chiaro, quasi nessun colore,
    # più parole OCR e formato orizzontale. È il caso del falso "POMODORO
    # CILIEGINO" che non conteneva alcuna foto del prodotto.
    text_form_signature = (
        white_ratio > 0.55
        and color_ratio < 0.08
        and len(ocr_words) >= 3
        and ratio > 1.6
    )

    severe_reasons = []
    if w < 120 or h < 90 or area < 15000:
        severe_reasons.append("immagine troppo piccola")
    if ratio > 5.2:
        severe_reasons.append("rapporto dimensioni anomalo")
    if white_ratio > 0.94:
        severe_reasons.append("immagine quasi vuota")
    if form_hits:
        severe_reasons.append("testo del modulo Ministero")
    if form_signature:
        severe_reasons.append("firma grafica del modulo Ministero")
    if text_form_signature:
        severe_reasons.append("ritaglio testuale del modulo")

    score = (
        5.0
        + (1.0 - white_ratio) * 2.0
        + min(contrast / 70.0, 1.0)
        + min(edge_ratio * 8.0, 1.0)
        + min(math.log10(max(area, 1)) / 6.0, 1.0)
        + min(math.log10(max(blur, 1.0)) / 4.0, 1.0)
        - max(0.0, ratio - 3.0) * 2.0
    )

    if form_hits:
        score -= 30.0
    if form_signature:
        score -= 15.0
    if text_form_signature:
        score -= 25.0

    return {
        "valid": True,
        "severe": bool(severe_reasons),
        "score": float(score),
        "reason": ", ".join(severe_reasons),
        "width": w,
        "height": h,
        "white": white_ratio,
        "pale_blue": pale_blue_ratio,
        "color": color_ratio,
        "ocr_words": len(ocr_words),
        "contrast": contrast,
        "edge": edge_ratio,
        "blur": blur,
        "area": area,
        "form_hits": form_hits,
    }


def url_for(filename: str) -> str:
    return (
        "https://raw.githubusercontent.com/"
        "calcagni1950srl-hash/richiami-italia-updater/"
        "refs/heads/main/images/"
        + filename
    )


def discard_severe_new_image(item: dict, rid: str) -> bool:
    image = str(item.get("immagine", "") or "").strip()
    name = image_filename(image)
    if not name:
        return False

    path = IMAGES / name
    if not path.exists():
        return False

    quality = analyse(path)
    if not quality["severe"]:
        return False

    item["immagine"] = ""
    path.unlink(missing_ok=True)
    print(
        "🗑️ Scarto falsa foto senza precedente valido:",
        rid,
        quality.get("reason", ""),
        f"score={quality.get('score', -999.0):.2f}",
    )
    return True


def select_best() -> None:
    if not BACKUP_RECALLS.exists() or not BACKUP_IMAGES.exists():
        print("Nessun backup pre-esistente: selezione comparativa saltata.")
        return

    current = load(RECALLS)
    previous = load(BACKUP_RECALLS)

    current_items = current.get("recalls", []) or []
    previous_by_id = {
        str(item.get("id", "") or "").strip(): item
        for item in (previous.get("recalls", []) or [])
        if str(item.get("id", "") or "").strip()
    }

    restored = 0
    kept_new = 0
    unchanged = 0

    for item in current_items:
        rid = str(item.get("id", "") or "").strip()
        if not rid:
            continue

        previous_item = previous_by_id.get(rid)
        if not previous_item:
            if not discard_severe_new_image(item, rid):
                kept_new += 1
            continue

        old_url = str(previous_item.get("immagine", "") or "").strip()
        new_url = str(item.get("immagine", "") or "").strip()

        old_name = image_filename(old_url)
        new_name = image_filename(new_url)

        if not old_name:
            if not discard_severe_new_image(item, rid):
                kept_new += 1
            continue

        old_path = BACKUP_IMAGES / old_name
        if not old_path.exists():
            kept_new += 1
            continue

        if old_name == new_name and (IMAGES / new_name).exists():
            unchanged += 1
            continue

        old_quality = analyse(old_path)

        new_path = IMAGES / new_name if new_name else None
        new_quality = (
            analyse(new_path)
            if new_path is not None and new_path.exists()
            else {
                "valid": False,
                "severe": True,
                "score": -999.0,
                "reason": "nuova immagine assente",
            }
        )

        choose_old = False

        # Mai sostituire una foto pulita con una foto palesemente peggiore.
        if not old_quality["severe"] and new_quality["severe"]:
            choose_old = True

        # Se entrambe sono pulite, cambiamo foto solo quando il nuovo
        # candidato ha un vantaggio reale. Questo evita oscillazioni casuali
        # della pipeline fra due ritagli equivalenti.
        elif not old_quality["severe"] and not new_quality["severe"]:
            old_w = int(old_quality.get("width", 0) or 0)
            old_h = int(old_quality.get("height", 0) or 0)
            new_w = int(new_quality.get("width", 0) or 0)
            new_h = int(new_quality.get("height", 0) or 0)
            old_area = max(1, old_w * old_h)
            new_area = max(1, new_w * new_h)

            # Completezza prima della sola nitidezza: un ritaglio può avere
            # edge/blur migliori ma mostrare soltanto un dettaglio della
            # confezione. Se la foto di partenza è già pulita, non accettiamo
            # un candidato che ne conserva meno del 35% dell'area E riduce
            # entrambe le dimensioni sotto il 60%.
            drastic_crop = (
                old_area >= 100_000
                and new_area < old_area * 0.35
                and new_w < old_w * 0.60
                and new_h < old_h * 0.60
            )

            if drastic_crop:
                choose_old = True
            else:
                choose_old = new_quality["score"] < old_quality["score"] + 0.35

        # Se la vecchia foto è contaminata e la nuova è pulita, la nuova
        # vince sempre.
        elif old_quality["severe"] and not new_quality["severe"]:
            choose_old = False

        # Se entrambe sono problematiche, teniamo quella con meno danni.
        else:
            choose_old = old_quality["score"] >= new_quality["score"]

        if choose_old:
            target = IMAGES / old_name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old_path, target)
            item["immagine"] = old_url or url_for(old_name)
            restored += 1
            print(
                "↩️ Mantengo foto precedente:",
                rid,
                f"old={old_quality['score']:.2f}",
                f"new={new_quality['score']:.2f}",
                old_quality.get("reason", ""),
                "|",
                new_quality.get("reason", ""),
            )
        else:
            kept_new += 1
            print(
                "✅ Mantengo nuova foto:",
                rid,
                f"old={old_quality['score']:.2f}",
                f"new={new_quality['score']:.2f}",
                old_quality.get("reason", ""),
                "|",
                new_quality.get("reason", ""),
            )

    RECALLS.write_text(
        json.dumps(current, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # Elimina solo file non più referenziati dal JSON finale.
    referenced = {
        image_filename(item.get("immagine", ""))
        for item in current_items
    }
    referenced.discard("")

    for path in IMAGES.glob("*.png"):
        if path.name not in referenced:
            path.unlink(missing_ok=True)

    print("Foto precedenti ripristinate:", restored)
    print("Foto nuove mantenute:", kept_new)
    print("Foto invariate:", unchanged)


def validate() -> None:
    data = load(RECALLS)
    recalls = data.get("recalls", []) or []
    if not recalls:
        raise SystemExit("ERRORE: recalls.json non contiene richiami")

    with_pdf = 0
    present = 0
    missing = []
    cleared_broken = []
    cleared_bad = []

    for item in recalls:
        rid = str(item.get("id", "") or "").strip()
        pdf = str(item.get("pdfMinistero", "") or "").strip()
        image = str(item.get("immagine", "") or "").strip()

        if pdf:
            with_pdf += 1

        if not image:
            if pdf:
                missing.append(rid)
            continue

        name = image_filename(image)
        path = IMAGES / name if name else None

        # Un URL senza file locale produrrebbe il riquadro bianco nell'app.
        # Non blocchiamo gli altri richiami: svuotiamo soltanto questa foto.
        if not name or path is None or not path.exists():
            item["immagine"] = ""
            cleared_broken.append((rid, name or image, "file assente"))
            if pdf:
                missing.append(rid)
            continue

        q = analyse(path)

        if not q["valid"]:
            item["immagine"] = ""
            cleared_broken.append((rid, name, q["reason"]))
            if pdf:
                missing.append(rid)
            continue

        # Una falsa foto (testo/modulo/residui gravi) riguarda solo il
        # singolo richiamo. La eliminiamo dal JSON ma NON fermiamo la
        # pubblicazione delle immagini valide degli altri richiami.
        if q["severe"]:
            item["immagine"] = ""
            cleared_bad.append((rid, name, q["reason"], q["score"]))
            if pdf:
                missing.append(rid)
            continue

        present += 1

    # Scriviamo subito il JSON sanificato: nessun URL rotto e nessuna
    # immagine bocciata possono arrivare all'app.
    RECALLS.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # Eliminiamo soltanto PNG non più referenziati dal JSON finale.
    referenced = {
        image_filename(item.get("immagine", ""))
        for item in recalls
    }
    referenced.discard("")

    for path in IMAGES.glob("*.png"):
        if path.name not in referenced:
            path.unlink(missing_ok=True)

    # Deduplica dell'elenco mancanti solo per il riepilogo.
    missing = list(dict.fromkeys(missing))
    coverage = present / max(1, with_pdf)

    print("Richiami:", len(recalls))
    print("Richiami con PDF:", with_pdf)
    print("Foto pulite e realmente presenti:", present)
    print("Richiami senza foto:", len(missing))
    print("URL/file rotti rimossi:", len(cleared_broken))
    print("Foto non valide rimosse:", len(cleared_bad))
    print("Copertura foto pulite:", f"{coverage:.1%}")

    if missing:
        print("Senza foto (non bloccano gli altri richiami):")
        for rid in missing:
            print(" -", rid)

    if cleared_broken:
        print("URL/file rotti rimossi dal singolo richiamo:")
        for rid, name, reason in cleared_broken:
            print(" -", rid, "->", name, reason)

    if cleared_bad:
        print("Foto bocciate rimosse dal singolo richiamo:")
        for rid, name, reason, score in cleared_bad:
            print(" -", rid, "->", name, reason, f"score={score:.2f}")

    # La copertura resta un indicatore diagnostico, non un motivo per
    # bloccare l'intero aggiornamento. Una sola foto problematica non deve
    # impedire la pubblicazione delle altre foto corrette.
    if coverage < 0.90:
        print("⚠️ Copertura foto sotto il 90%: aggiornamento pubblicato comunque.")

    print("✅ Validazione immagini completata senza bloccare richiami validi")

def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "validate"
    if mode == "select":
        select_best()
        return
    if mode == "validate":
        validate()
        return
    raise SystemExit("Uso: image_quality_guard.py [select|validate]")


if __name__ == "__main__":
    main()
