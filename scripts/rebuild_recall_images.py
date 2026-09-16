import hashlib
import io
import json
import math
import re
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageOps, ImageStat


ROOT = Path(".quality")
PDF_DIR = ROOT / "pdf"
WORK_DIR = ROOT / "work"
IMAGES_DIR = Path("images")
RECALLS_FILE = Path("recalls.json")

PDF_DIR.mkdir(parents=True, exist_ok=True)
WORK_DIR.mkdir(parents=True, exist_ok=True)
IMAGES_DIR.mkdir(parents=True, exist_ok=True)


def run(command):
    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="ignore",
    )


def current_image_path(recall):
    url = str(recall.get("immagine", "") or "")
    match = re.search(r"/images/([^?/#]+\.png)", url)

    if not match:
        return None

    path = IMAGES_DIR / match.group(1)
    return path if path.exists() else None


def image_candidate(path):
    try:
        original = Image.open(path)
        width, height = original.size
    except Exception:
        return None

    if width < 140 or height < 100:
        return None

    area = width * height

    if area < 50_000:
        return None

    ratio = max(width / height, height / width)

    if ratio > 6.0:
        return None

    try:
        if path.stat().st_size < 4_000:
            return None
    except OSError:
        return None

    # Scartiamo soltanto maschere quasi uniformi. Non imponiamo controlli
    # severi su marca, OCR, colore o nitidezza: l'obiettivo è mostrare
    # tutte le foto ufficiali per intero, senza ritagliarle.
    gray = original.convert("L")
    stddev = ImageStat.Stat(gray).stddev[0]

    if stddev < 4.0:
        return None

    image = original.convert("RGB")

    return {
        "path": path,
        "image": image,
        "area": area,
    }


def normalized_digest(image):
    canvas = Image.new("RGB", (96, 96), "white")
    fitted = ImageOps.contain(
        image.convert("RGB"),
        (92, 92),
        Image.Resampling.LANCZOS,
    )
    x = (96 - fitted.width) // 2
    y = (96 - fitted.height) // 2
    canvas.paste(fitted, (x, y))
    return hashlib.sha256(canvas.tobytes()).hexdigest()


def extract_embedded_images(pdf, recall_id):
    folder = WORK_DIR / recall_id

    if folder.exists():
        shutil.rmtree(folder)

    folder.mkdir(parents=True, exist_ok=True)
    prefix = folder / "img"

    result = run([
        "pdfimages",
        "-png",
        str(pdf),
        str(prefix),
    ])

    if result.returncode != 0:
        return []

    candidates = []

    for path in sorted(folder.glob("img-*.png")):
        item = image_candidate(path)
        if item:
            candidates.append(item)

    if not candidates:
        return []

    candidates.sort(
        key=lambda item: item["area"],
        reverse=True,
    )

    largest_area = candidates[0]["area"]

    # Manteniamo tutte le immagini di dimensioni significative rispetto
    # alla foto principale; questo elimina soprattutto piccoli loghi e icone.
    meaningful = [
        item
        for item in candidates
        if item["area"] >= max(50_000, int(largest_area * 0.10))
    ]

    unique = []
    seen = set()

    for item in meaningful:
        digest = normalized_digest(item["image"])

        if digest in seen:
            continue

        seen.add(digest)
        unique.append(item["image"])

        # Evita collage ingestibili in casi anomali, ma conserva normalmente
        # tutte le viste prodotto utili presenti nei moduli ufficiali.
        if len(unique) >= 8:
            break

    return unique


def render_full_first_page(pdf, recall_id):
    folder = WORK_DIR / recall_id / "fallback"
    folder.mkdir(parents=True, exist_ok=True)
    prefix = folder / "page"

    result = run([
        "pdftoppm",
        "-f",
        "1",
        "-singlefile",
        "-r",
        "200",
        "-png",
        str(pdf),
        str(prefix),
    ])

    page = folder / "page.png"

    if result.returncode != 0 or not page.exists():
        return []

    try:
        return [Image.open(page).convert("RGB")]
    except Exception:
        return []


def make_full_image_sheet(images):
    images = [image.convert("RGB") for image in images if image is not None]

    if not images:
        return None

    count = len(images)

    if count == 1:
        canvas = Image.new("RGB", (1200, 1200), "white")
        fitted = ImageOps.contain(
            images[0],
            (1120, 1120),
            Image.Resampling.LANCZOS,
        )
        x = (1200 - fitted.width) // 2
        y = (1200 - fitted.height) // 2
        canvas.paste(fitted, (x, y))
        return canvas

    columns = 2
    rows = math.ceil(count / columns)

    width = 1200
    height = max(1200, rows * 570 + 60)
    outer = 30
    gap = 20

    tile_width = (width - outer * 2 - gap) // columns
    tile_height = (height - outer * 2 - gap * (rows - 1)) // rows

    canvas = Image.new("RGB", (width, height), "white")

    for index, image in enumerate(images):
        row = index // columns
        column = index % columns

        fitted = ImageOps.contain(
            image,
            (tile_width - 24, tile_height - 24),
            Image.Resampling.LANCZOS,
        )

        tile_x = outer + column * (tile_width + gap)
        tile_y = outer + row * (tile_height + gap)

        x = tile_x + (tile_width - fitted.width) // 2
        y = tile_y + (tile_height - fitted.height) // 2

        canvas.paste(fitted, (x, y))

    return canvas


def versioned_filename(recall_id, image):
    buffer = io.BytesIO()

    image.save(
        buffer,
        format="PNG",
        optimize=True,
    )

    data = buffer.getvalue()
    digest = hashlib.sha256(data).hexdigest()[:10]

    return f"{recall_id}-{digest}.png", data


def remove_note(notes, text):
    while text in notes:
        notes.remove(text)


with RECALLS_FILE.open("r", encoding="utf-8") as handle:
    database = json.load(handle)

recalls = database.get("recalls", [])
accepted_names = set()
changed = False


for recall in recalls:
    recall_id = str(recall.get("id", "")).strip()

    if not recall_id:
        continue

    pdf = PDF_DIR / f"{recall_id}.pdf"
    source_images = []
    source_label = ""

    if pdf.exists():
        source_images = extract_embedded_images(
            pdf,
            recall_id,
        )

        if source_images:
            source_label = "immagini incorporate"
        else:
            # Se il PDF è una scansione unica e non contiene immagini
            # separabili, mostriamo l'intera prima pagina. In questo modo
            # la foto resta comunque visibile e non viene tagliata.
            source_images = render_full_first_page(
                pdf,
                recall_id,
            )
            source_label = "pagina PDF completa"

    if not source_images:
        existing = current_image_path(recall)

        if existing:
            try:
                source_images = [
                    Image.open(existing).convert("RGB")
                ]
                source_label = "immagine precedente"
            except Exception:
                source_images = []

    old_url = str(recall.get("immagine", "") or "")
    notes = recall.setdefault("note", [])

    remove_note(
        notes,
        "Immagine scartata dal controllo qualità",
    )
    remove_note(
        notes,
        "Immagine prodotto non estratta",
    )

    sheet = make_full_image_sheet(source_images)

    if sheet is None:
        if old_url:
            recall["immagine"] = ""
            changed = True

        marker = "Immagine non disponibile nel PDF ufficiale"

        if marker not in notes:
            notes.append(marker)
            changed = True

        print("⚠️ Nessuna immagine disponibile:", recall_id)
        continue

    remove_note(
        notes,
        "Immagine non disponibile nel PDF ufficiale",
    )

    filename, png_bytes = versioned_filename(
        recall_id,
        sheet,
    )

    target = IMAGES_DIR / filename
    target.write_bytes(png_bytes)
    accepted_names.add(filename)

    new_url = (
        "https://raw.githubusercontent.com/"
        "calcagni1950srl-hash/"
        "richiami-italia-updater/"
        "refs/heads/main/images/"
        f"{filename}"
    )

    if old_url != new_url:
        recall["immagine"] = new_url
        changed = True

    print(
        "✅ Immagine completa:",
        recall_id,
        "-",
        source_label,
        "- viste:",
        len(source_images),
    )


for path in IMAGES_DIR.glob("*.png"):
    if path.name not in accepted_names:
        path.unlink(missing_ok=True)
        changed = True


if changed:
    RECALLS_FILE.write_text(
        json.dumps(
            database,
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    print("✅ Immagini complete aggiornate.")
else:
    print("✅ Nessuna modifica necessaria alle immagini.")
