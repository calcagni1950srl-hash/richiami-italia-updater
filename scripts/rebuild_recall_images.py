import csv
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Compatibilità anche con vecchi tentativi del workflow che installavano
# soltanto Pillow. Il workflow corrente installa già questi pacchetti.
try:
    import cv2
    import numpy as np
except ModuleNotFoundError:
    subprocess.check_call(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--quiet",
            "numpy",
            "opencv-python-headless",
        ]
    )
    import cv2
    import numpy as np

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


def image_stats(image):
    rgb = image.convert("RGB")
    width, height = rgb.size

    sample = rgb.copy()
    sample.thumbnail((500, 500))
    arr = np.asarray(sample)

    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)

    white_ratio = float(np.mean(np.all(arr >= 242, axis=2)))
    color_ratio = float(np.mean(hsv[:, :, 1] >= 20))
    contrast = float(gray.std())

    edges = cv2.Canny(gray, 60, 150)
    edge_ratio = float(np.mean(edges > 0))

    return {
        "width": width,
        "height": height,
        "area": width * height,
        "white_ratio": white_ratio,
        "color_ratio": color_ratio,
        "contrast": contrast,
        "edge_ratio": edge_ratio,
    }


def ocr_layout(path):
    result = run([
        "tesseract",
        str(path),
        "stdout",
        "-l",
        "ita",
        "--psm",
        "11",
        "tsv",
    ])

    words = []

    if not result.stdout.strip():
        return words

    reader = csv.DictReader(
        io.StringIO(result.stdout),
        delimiter="\t",
    )

    for row in reader:
        text = str(row.get("text", "") or "").strip()

        if not text:
            continue

        try:
            confidence = float(row.get("conf", "-1") or -1)
            left = int(row.get("left", "0") or 0)
            top = int(row.get("top", "0") or 0)
            width = int(row.get("width", "0") or 0)
            height = int(row.get("height", "0") or 0)
        except (TypeError, ValueError):
            continue

        if confidence < 15 or width <= 0 or height <= 0:
            continue

        words.append({
            "text": text,
            "left": left,
            "top": top,
            "width": width,
            "height": height,
            "confidence": confidence,
        })

    return words


def text_metrics(path, image):
    width, height = image.size
    words = ocr_layout(path)
    chars = sum(len(item["text"]) for item in words)

    box_area = 0

    for item in words:
        x1 = max(0, item["left"])
        y1 = max(0, item["top"])
        x2 = min(width, item["left"] + item["width"])
        y2 = min(height, item["top"] + item["height"])

        if x2 > x1 and y2 > y1:
            box_area += (x2 - x1) * (y2 - y1)

    coverage = min(1.0, box_area / max(1, width * height))
    return words, chars, coverage


def document_like(path, image):
    stats = image_stats(image)
    width = stats["width"]
    height = stats["height"]
    ratio = max(width, height) / max(1, min(width, height))
    a4_like = 1.25 <= ratio <= 1.75

    words, chars, coverage = text_metrics(path, image)

    is_document = (
        coverage >= 0.18
        or (
            a4_like
            and stats["white_ratio"] >= 0.55
            and coverage >= 0.028
            and chars >= 90
        )
        or (
            stats["white_ratio"] >= 0.78
            and coverage >= 0.02
            and chars >= 70
        )
    )

    return is_document, words, chars, coverage, stats


def word_mask(shape, words, pad_x=9, pad_y=7):
    height, width = shape[:2]
    mask = np.zeros((height, width), dtype=np.uint8)

    for item in words:
        x1 = max(0, item["left"] - pad_x)
        y1 = max(0, item["top"] - pad_y)
        x2 = min(width, item["left"] + item["width"] + pad_x)
        y2 = min(height, item["top"] + item["height"] + pad_y)

        cv2.rectangle(mask, (x1, y1), (x2, y2), 255, -1)

    return mask


def remove_form_lines(nonwhite):
    height, width = nonwhite.shape

    horizontal = cv2.morphologyEx(
        nonwhite,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(
            cv2.MORPH_RECT,
            (max(45, width // 18), 1),
        ),
    )

    vertical = cv2.morphologyEx(
        nonwhite,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(
            cv2.MORPH_RECT,
            (1, max(45, height // 22)),
        ),
    )

    lines = cv2.bitwise_or(horizontal, vertical)
    return cv2.bitwise_and(nonwhite, cv2.bitwise_not(lines))


def box_iou(a, b):
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b

    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)

    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0

    intersection = (ix2 - ix1) * (iy2 - iy1)
    area_a = (ax2 - ax1) * (ay2 - ay1)
    area_b = (bx2 - bx1) * (by2 - by1)

    return intersection / max(1, area_a + area_b - intersection)


def clamp_box(box, width, height):
    x1, y1, x2, y2 = box
    return (
        max(0, min(width - 1, x1)),
        max(0, min(height - 1, y1)),
        max(1, min(width, x2)),
        max(1, min(height, y2)),
    )


def expand_box(box, width, height, fraction=0.06):
    x1, y1, x2, y2 = box
    bw = x2 - x1
    bh = y2 - y1
    px = max(10, int(bw * fraction))
    py = max(10, int(bh * fraction))

    return clamp_box(
        (x1 - px, y1 - py, x2 + px, y2 + py),
        width,
        height,
    )


def trim_white(image, threshold=250):
    rgb = image.convert("RGB")
    arr = np.asarray(rgb)
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    mask = gray < threshold

    if not np.any(mask):
        return rgb

    ys, xs = np.where(mask)
    x1 = int(xs.min())
    x2 = int(xs.max()) + 1
    y1 = int(ys.min())
    y2 = int(ys.max()) + 1

    width, height = rgb.size
    return rgb.crop(
        expand_box((x1, y1, x2, y2), width, height, 0.05)
    )


def candidate_crop(page_path, pil, box, temp_index):
    width, height = pil.size
    box = expand_box(box, width, height, 0.07)
    crop = pil.crop(box).convert("RGB")
    crop = trim_white(crop)
    stats = image_stats(crop)

    if stats["width"] < 110 or stats["height"] < 80:
        return None

    if stats["area"] < 18_000:
        return None

    if stats["contrast"] < 5.0:
        return None

    temp = page_path.parent / f"{page_path.stem}-candidate-{temp_index:03d}.png"
    crop.save(temp)

    _, chars, coverage = text_metrics(temp, crop)
    temp.unlink(missing_ok=True)

    # Il testo stampato sulla confezione è ammesso. Scartiamo invece i crop
    # che sono chiaramente soprattutto parti del modulo o tabelle.
    if (
        stats["white_ratio"] >= 0.72
        and coverage >= 0.10
        and chars >= 65
    ):
        return None

    if coverage >= 0.24 and chars >= 100:
        return None

    score = (
        stats["color_ratio"] * 2.0
        + min(stats["contrast"] / 65.0, 1.2)
        + min(stats["edge_ratio"] * 7.0, 1.0)
        + min(stats["area"] / 500_000.0, 0.8)
        - stats["white_ratio"] * 0.25
        - coverage * 0.9
    )

    return {
        "image": crop,
        "box": box,
        "score": score,
        "coverage": coverage,
        "chars": chars,
    }


def contour_regions(page_path):
    pil = Image.open(page_path).convert("RGB")
    rgb = np.asarray(pil)
    height, width = rgb.shape[:2]
    page_area = width * height

    words = ocr_layout(page_path)
    text = word_mask(rgb.shape, words)

    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)

    nonwhite = (gray < 250).astype(np.uint8) * 255
    residual = remove_form_lines(nonwhite)
    residual[text > 0] = 0

    colorful = (hsv[:, :, 1] >= 15).astype(np.uint8) * 255
    colorful[text > 0] = 0

    edges = cv2.Canny(gray, 45, 125)
    edges[text > 0] = 0

    visual = cv2.bitwise_or(residual, colorful)
    visual = cv2.bitwise_or(visual, edges)

    visual = cv2.morphologyEx(
        visual,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (25, 25)),
        iterations=2,
    )
    visual = cv2.dilate(
        visual,
        cv2.getStructuringElement(cv2.MORPH_RECT, (13, 13)),
        iterations=1,
    )

    contours, _ = cv2.findContours(
        visual,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )

    candidates = []

    for index, contour in enumerate(contours):
        x, y, w, h = cv2.boundingRect(contour)
        area = w * h

        if w < 100 or h < 75:
            continue

        if area < page_area * 0.004:
            continue

        if area > page_area * 0.58:
            continue

        ratio = max(w / h, h / w)

        if ratio > 5.5:
            continue

        item = candidate_crop(
            page_path,
            pil,
            (x, y, x + w, y + h),
            index,
        )

        if item:
            candidates.append(item)

    candidates.sort(key=lambda item: item["score"], reverse=True)

    selected = []

    for item in candidates:
        if any(
            box_iou(item["box"], other["box"]) > 0.48
            for other in selected
        ):
            continue

        selected.append(item)

        if len(selected) >= 6:
            break

    return selected


def low_text_window_regions(page_path):
    """Fallback finale: cerca finestre con contenuto visivo ma poco testo.

    Non restituisce mai la pagina intera. Serve per scansioni dove la foto non
    è un oggetto PDF separato e non ha un bordo netto rilevabile.
    """

    pil = Image.open(page_path).convert("RGB")
    rgb = np.asarray(pil)
    height, width = rgb.shape[:2]

    words = ocr_layout(page_path)
    text = word_mask(rgb.shape, words, pad_x=5, pad_y=4)

    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    edges = cv2.Canny(gray, 50, 140)

    candidates = []
    index = 0

    # Più formati per intercettare foto grandi, medie e quadrate.
    shapes = [
        (0.52, 0.42),
        (0.45, 0.38),
        (0.38, 0.34),
        (0.34, 0.28),
        (0.30, 0.24),
    ]

    for wf, hf in shapes:
        ww = max(160, int(width * wf))
        hh = max(130, int(height * hf))

        step_x = max(70, int(ww * 0.28))
        step_y = max(70, int(hh * 0.28))

        for y1 in range(0, max(1, height - hh + 1), step_y):
            for x1 in range(0, max(1, width - ww + 1), step_x):
                x2 = min(width, x1 + ww)
                y2 = min(height, y1 + hh)

                region = np.s_[y1:y2, x1:x2]
                text_ratio = float(np.mean(text[region] > 0))
                white_ratio = float(np.mean(gray[region] >= 247))
                color_ratio = float(np.mean(hsv[region][..., 1] >= 17))
                edge_ratio = float(np.mean(edges[region] > 0))
                contrast = float(gray[region].std())

                if white_ratio > 0.94:
                    continue

                if contrast < 6.0 and color_ratio < 0.03:
                    continue

                # Penalizzazione molto forte del testo del modulo.
                score = (
                    color_ratio * 2.2
                    + edge_ratio * 5.5
                    + min(contrast / 70.0, 1.0)
                    + (1.0 - white_ratio) * 0.35
                    - text_ratio * 3.8
                )

                if score < 0.22:
                    continue

                item = candidate_crop(
                    page_path,
                    pil,
                    (x1, y1, x2, y2),
                    1000 + index,
                )
                index += 1

                if item:
                    # Il punteggio della finestra guida il fallback più della
                    # sola dimensione del crop.
                    item["score"] += score
                    candidates.append(item)

    candidates.sort(key=lambda item: item["score"], reverse=True)

    selected = []

    for item in candidates:
        if any(
            box_iou(item["box"], other["box"]) > 0.38
            for other in selected
        ):
            continue

        selected.append(item)

        if len(selected) >= 3:
            break

    return selected


def render_pages(pdf, recall_id):
    folder = WORK_DIR / recall_id / "pages"

    if folder.exists():
        shutil.rmtree(folder)

    folder.mkdir(parents=True, exist_ok=True)
    prefix = folder / "page"

    result = run([
        "pdftoppm",
        "-f",
        "1",
        "-l",
        "3",
        "-r",
        "200",
        "-png",
        str(pdf),
        str(prefix),
    ])

    if result.returncode != 0:
        return []

    return sorted(folder.glob("page-*.png"))


def segment_image_source(image, recall_id, source_index):
    folder = WORK_DIR / recall_id / "page-like"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"source-{source_index:03d}.png"
    image.convert("RGB").save(path)

    candidates = contour_regions(path)

    if not candidates:
        candidates = low_text_window_regions(path)

    return [item["image"] for item in candidates]


def extract_images_from_pdf(pdf, recall_id):
    folder = WORK_DIR / recall_id / "embedded"

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

    direct_images = []
    page_like_images = []

    if result.returncode == 0:
        for path in sorted(folder.glob("img-*.png")):
            try:
                image = Image.open(path).convert("RGB")
            except Exception:
                continue

            stats = image_stats(image)

            if stats["width"] < 80 or stats["height"] < 60:
                continue

            if stats["area"] < 8_000 or stats["contrast"] < 3.0:
                continue

            is_document, _, chars, coverage, stats = document_like(
                path,
                image,
            )

            if is_document:
                page_like_images.append(image)
                print(
                    "   ↪ pagina/modulo da segmentare:",
                    path.name,
                    "testo=",
                    chars,
                    "copertura=",
                    round(coverage, 3),
                )
            else:
                direct_images.append(image)

    # Duplicati rimossi prima di valutare dimensioni relative.
    unique_direct = []
    seen = set()

    for image in direct_images:
        digest = normalized_digest(image)

        if digest in seen:
            continue

        seen.add(digest)
        unique_direct.append(image)

    if unique_direct:
        areas = [image.width * image.height for image in unique_direct]
        largest = max(areas)

        meaningful = [
            image
            for image in unique_direct
            if image.width * image.height >= max(8_000, int(largest * 0.025))
        ]

        if meaningful:
            return meaningful[:8], "foto incorporate"

    # Prima proviamo a segmentare eventuali scansioni estratte direttamente.
    segmented = []
    seen_segmented = set()

    for index, image in enumerate(page_like_images):
        for crop in segment_image_source(image, recall_id, index):
            digest = normalized_digest(crop)

            if digest in seen_segmented:
                continue

            seen_segmented.add(digest)
            segmented.append(crop)

            if len(segmented) >= 8:
                return segmented, "foto estratte dalla scansione"

    # Infine renderizziamo le pagine PDF: utile quando la foto non è un
    # oggetto raster separato (es. PDF impaginati o immagini incorporate in
    # una scansione complessiva).
    for page_path in render_pages(pdf, recall_id):
        candidates = contour_regions(page_path)

        if not candidates:
            candidates = low_text_window_regions(page_path)

        for item in candidates:
            crop = item["image"]
            digest = normalized_digest(crop)

            if digest in seen_segmented:
                continue

            seen_segmented.add(digest)
            segmented.append(crop)

            if len(segmented) >= 8:
                break

        if len(segmented) >= 8:
            break

    return segmented, "foto estratte dalla scansione"


def make_full_image_sheet(images):
    unique = []
    seen = set()

    for image in images:
        if image is None:
            continue

        image = image.convert("RGB")
        digest = normalized_digest(image)

        if digest in seen:
            continue

        seen.add(digest)
        unique.append(image)

    images = unique[:8]

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
    image.save(buffer, format="PNG", optimize=True)
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
        source_images, source_label = extract_images_from_pdf(
            pdf,
            recall_id,
        )

    old_url = str(recall.get("immagine", "") or "")
    notes = recall.setdefault("note", [])

    for marker in [
        "Immagine scartata dal controllo qualità",
        "Immagine prodotto non estratta",
        "Immagine non disponibile nel PDF ufficiale",
        "Foto prodotto non individuata nel PDF ufficiale",
    ]:
        remove_note(notes, marker)

    sheet = make_full_image_sheet(source_images)

    if sheet is None:
        if old_url:
            recall["immagine"] = ""
            changed = True

        marker = "Foto prodotto non individuata nel PDF ufficiale"

        if marker not in notes:
            notes.append(marker)
            changed = True

        print("⚠️ Nessuna foto prodotto individuata:", recall_id)
        continue

    filename, png_bytes = versioned_filename(recall_id, sheet)
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
        "✅ Foto prodotto:",
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
    print("✅ Foto prodotto aggiornate senza pagine testuali.")
else:
    print("✅ Nessuna modifica necessaria alle foto prodotto.")
