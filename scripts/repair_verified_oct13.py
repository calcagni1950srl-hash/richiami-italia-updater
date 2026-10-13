"""Correzione verificata di due avvisi del 13/10/2026 da PDF ufficiali.
Usa soltanto i PDF scaricati; non accede alle notifiche.
"""
import hashlib
import io
import json
import re
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from PIL import Image

PDF_DIR = Path(".quality/pdf")
IMAGES = Path("images")
DATA = Path("recalls.json")
RAW = ("https://raw.githubusercontent.com/calcagni1950srl-hash/"
       "richiami-italia-updater/refs/heads/main/images/")
B = "perfetto-moka-macinato-aromatizzato-al-caramello"
C = "cooperlat-soc-coop-agr"


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def reason_from_pdf(pdf):
    text = run("pdftotext", "-layout", str(pdf), "-")
    if not text.strip():
        with TemporaryDirectory() as tmp:
            page = Path(tmp) / "page"
            run("pdftoppm", "-f", "1", "-l", "1", "-scale-to", "1800",
                "-singlefile", "-png", str(pdf), str(page))
            text = run("tesseract", str(page.with_suffix(".png")), "stdout",
                       "-l", "ita", "--psm", "6")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    for idx, line in enumerate(lines):
        match = re.search(r"(?i)\bmotivo\s+del\s+richiamo\s*:\s*(.*)", line)
        if not match:
            continue
        pieces = [match.group(1).strip()] if match.group(1).strip() else []
        for row in lines[idx + 1:idx + 7]:
            if re.match(r"(?i)^(avvertenz|fotografia|foto del prodotto|inserire immagine)", row):
                break
            if row:
                pieces.append(row)
        reason = " ".join(pieces).strip()
        if len(reason) >= 12:
            return reason
    raise RuntimeError("Motivo ufficiale non rilevato nel PDF: " + str(pdf))


def photo_bialetti(pdf):
    with TemporaryDirectory() as tmp:
        prefix = Path(tmp) / "tile"
        run("pdfimages", "-png", str(pdf), str(prefix))
        parts = [Image.open(p).convert("RGB") for p in sorted(Path(tmp).glob("tile-*.png"))]
        if len(parts) != 6 or len({p.width for p in parts}) != 1 or max(p.height for p in parts) > 210:
            raise RuntimeError("Immagine PDF Bialetti cambiata: verifica manuale richiesta")
        image = Image.new("RGB", (parts[0].width, sum(p.height for p in parts)), "white")
        y = 0
        for part in parts:
            image.paste(part, (0, y))
            y += part.height
        return image


def photo_cigno(pdf):
    with TemporaryDirectory() as tmp:
        prefix = Path(tmp) / "page"
        run("pdfimages", "-j", str(pdf), str(prefix))
        pages = []
        for p in sorted(Path(tmp).glob("page-*.jpg")):
            im = Image.open(p).convert("RGB")
            if im.width > 1000 and im.height > 1500:
                pages.append(im)
        if len(pages) != 1:
            raise RuntimeError("Scansione Cigno cambiata: verifica manuale richiesta")
        img = pages[0]
        w, h = img.size
        # Limiti verificati sull'immagine del PDF ufficiale 12 ottobre.
        return img.crop((round(w * .2097), round(h * .704),
                         round(w * .339), round(h * .930)))


def save_photo(rid, image):
    buf = io.BytesIO()
    image.save(buf, "PNG", optimize=True)
    raw = buf.getvalue()
    filename = rid + "-" + hashlib.sha256(raw).hexdigest()[:10] + ".png"
    IMAGES.mkdir(parents=True, exist_ok=True)
    (IMAGES / filename).write_bytes(raw)
    return RAW + filename


def main():
    data = json.loads(DATA.read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in data["recalls"]}
    specs = {
        B: ("1791893295.pdf", "Livelli di ocratossina A sopra i limiti consentiti", photo_bialetti),
        C: ("1791893297.pdf", "per sospetto deterioramento del prodotto", photo_cigno),
    }
    for rid, (suffix, expected, extractor) in specs.items():
        item = by_id[rid]
        if not str(item.get("pdfMinistero", "")).endswith(suffix):
            raise RuntimeError("URL PDF cambiato per " + rid)
        pdf = PDF_DIR / (rid + ".pdf")
        if not pdf.is_file():
            raise RuntimeError("PDF originale mancante: " + str(pdf))
        reason = reason_from_pdf(pdf)
        if reason.casefold() != expected.casefold():
            raise RuntimeError("Motivo PDF inatteso per " + rid + ": " + reason)
        img = extractor(pdf)
        if img.width < 140 or img.height < 350:
            raise RuntimeError("Foto troppo piccola: " + rid)
        item["motivo"] = reason
        item["immagine"] = save_photo(rid, img)
        if rid == C:
            item["prodotto"] = "Latte parzialmente scremato pastorizzato a temperatura elevata"
            item["produttore"] = "Cooperlat Soc. Coop. Agr."
        notes = item.setdefault("note", [])
        for note in ("Motivo specifico verificato nel PDF ufficiale",
                     "Fotografia completa estratta dal PDF ufficiale"):
            if note not in notes:
                notes.append(note)
        print("PDF verificato:", rid, "|", reason, "|", img.size, "|", item["immagine"])
    DATA.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
