import csv
import hashlib
import io
import json
import math
import re
import shutil
import subprocess
from pathlib import Path

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
    color_ratio = float(np.mean(hsv[:, :, 1] >= 24))
    contrast = float(gray.std())

    return {
        "width": width,
        "height": height,
        "area": width * height,
        "white_ratio": white_ratio,
        "color_ratio": color_ratio,
        "contrast": contrast,
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

        if confidence < 18 or width <= 0 or height <= 0:
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
    box_area = sum(item["width"] * item["height"] for item in words)
    coverage = box_area / max(1, width * height)

    return words, chars, coverage


def looks_like_document(path, image):
    stats = image_stats(image)
    width = stats["width"]
    height = stats["height"]
    portrait_ratio = max(width, height) / max(1, min(width, height))

    words, chars, coverage = text_metrics(path, image)

    a4_like = 1.25 <= portrait_ratio <= 1.70

    document = (
        chars >= 220
        or coverage >= 0.12
        or (
            a4_like
            and stats["white_ratio"] >= 0.42
            and chars >= 80
        )
        or (
            stats["white_ratio"] >= 0.68
            and chars >= 60
        )
    )

    return document, words, chars, coverage, stats


def embedded_candidate(path):
    try:
        image = Image.open(path).convert("RGB")
    except Exception:
        return None

    stats = image_stats(image)

    if stats["width"] < 140 or stats["height"] < 100:
        return None

    if stats["area"] < 50_000:
        return None

    ratio = max(
        stats["width"] / stats["height"],
        stats["height"] / stats["width"],
    )

    if ratio > 6.0:
        return None

    if stats["contrast"] < 4.0:
        return None

    document, _, chars, coverage, stats = looks_like_document(
        path,
        image,
    )

    if document:
        print(
            "   ↪ scartata pagina/modulo:",
            path.name,
            "testo=",
            chars,
            "copertura=",
            round(coverage, 3),
        )
        return None

    return {
        "image": image,
        "area": stats["area"],
        "color_ratio": stats["color_ratio"],
    }


def extract_embedded_images(pdf, recall_id):
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

    if result.returncode != 0:
        return []

    candidates = []

    for path in sorted(folder.glob("img-*.png")):
        item = embedded_candidate(path)

        if item:
            candidates.append(item)

    if not candidates:
        return []

    candidates.sort(
        key=lambda item: (
            item["area"],
            item["color_ratio"],
        ),
        reverse=True,
    )

    largest_area = candidates[0]["area"]

    meaningful = [
        item
        for item in candidates
        if item["area"] >= max(45_000, int(largest_area * 0.08))
    ]

    unique = []
    seen = set()

    for item in meaningful:
        digest = normalized_digest(item["image"])

        if digest in seen:
            continue

        seen.add(digest)
        unique.append(item["image"])

        if len(unique) >= 8:
            break

    return unique


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
        "2",
        "-r",
        "180",
        "-png",
        str(pdf),
        str(prefix),
    ])

    if result.returncode != 0:
        return []

    return sorted(folder.glob("page-*.png"))


def word_mask(shape, words):
    height, width = shape[:2]
    mask = np.zeros((height, width), dtype=np.uint8)

    for item in words:
        x1 = max(0, item["left"] - 8)
        y1 = max(0, item["top"] - 6)
        x2 = min(width, item["left"] + item["width"] + 8)
        y2 = min(height, item["top"] + item["height"] + 6)

        cv2.rectangle(
            mask,
            (x1, y1),
            (x2, y2),
            255,
            -1,
        )

    if np.any(mask):
        kernel = cv2.getStructuringElement(
            cv2.MORPH_RECT,
            (9, 5),
        )
        mask = cv2.dilate(mask, kernel, iterations=1)

    return mask


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


def photo_regions_from_page(page_path):
    pil = Image.open(page_path).convert("RGB")
    rgb = np.asarray(pil)
    height, width = rgb.shape[:2]
    page_area = width * height

    words = ocr_layout(page_path)
    text = word_mask(rgb.shape, words)

    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)

    saturation = hsv[:, :, 1]
    colorful = (saturation >= 18).astype(np.uint8) * 255

    lap = cv2.Laplacian(gray, cv2.CV_32F)
    texture = (np.abs(lap) >= 12).astype(np.uint8) * 255

    nonwhite = (gray <= 246).astype(np.uint8) * 255

    visual = cv2.bitwise_or(
        colorful,
        cv2.bitwise_and(texture, nonwhite),
    )

    visual[text > 0] = 0

    visual = cv2.morphologyEx(
        visual,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (31, 31)),
        iterations=2,
    )
    visual = cv2.dilate(
        visual,
        cv2.getStructuringElement(cv2.MORPH_RECT, (19, 19)),
        iterations=1,
    )

    contours, _ = cv2.findContours(
        visual,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )

    boxes = []

    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        area = w * h

        if w < 240 or h < 170:
            continue

        if area < page_area * 0.025:
            continue

        if area > page_area * 0.68:
            continue

        ratio = max(w / h, h / w)

        if ratio > 4.5:
            continue

        margin = max(12, int(min(w, h) * 0.025))
        x1 = max(0, x - margin)
        y1 = max(0, y - margin)
        x2 = min(width, x + w + margin)
        y2 = min(height, y + h + margin)

        crop = pil.crop((x1, y1, x2, y2)).convert("RGB")
        stats = image_stats(crop)

        if stats["contrast"] < 8.0:
            continue

        # Una zona fotografica può contenere testo della confezione, ma non
        # deve essere soprattutto una porzione del modulo con righe e scritte.
        temp_path = page_path.parent / (
            page_path.stem
            + f"-crop-{len(boxes):02d}.png"
        )
        crop.save(temp_path)

        _, chars, coverage = text_metrics(temp_path, crop)

        if (
            stats["white_ratio"] > 0.70
            and chars > 70
        ):
            temp_path.unlink(missing_ok=True)
            continue

        if coverage > 0.16 and chars > 90:
            temp_path.unlink(missing_ok=True)
            continue

        score = (
            area / page_area
            + stats["color_ratio"] * 1.7
            + min(stats["contrast"] / 80.0, 1.0)
            - stats["white_ratio"] * 0.35
        )

        boxes.append({
            "box": (x1, y1, x2, y2),
            "image": crop,
            "score": score,
            "temp": temp_path,
        })

    boxes.sort(key=lambda item: item["score"], reverse=True)

    selected = []

    for item in boxes:
        if any(
            box_iou(item["box"], other["box"]) > 0.45
            for other in selected
        ):
            item["temp"].unlink(missing_ok=True)
            continue

        selected.append(item)

        if len(selected) >= 6:
            break

    for item in boxes:
        if item not in selected:
            item["temp"].unlink(missing_ok=True)

    return [item["image"] for item in selected]


def extract_scanned_photo_regions(pdf, recall_id):
    images = []
    seen = set()

    for page_path in render_pages(pdf, recall_id):
        for image in photo_regions_from_page(page_path):
            digest = normalized_digest(image)

            if digest in seen:
                continue

            seen.add(digest)
            images.append(image)

            if len(images) >= 8:
                return images

    return images


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
            source_label = "foto incorporate"
        else:
            source_images = extract_scanned_photo_regions(
                pdf,
                recall_id,
            )
            source_label = "foto estratte dalla scansione"

    old_url = str(recall.get("immagine", "") or "")
    notes = recall.setdefault("note", [])

    remove_note(notes, "Immagine scartata dal controllo qualità")
    remove_note(notes, "Immagine prodotto non estratta")
    remove_note(notes, "Immagine non disponibile nel PDF ufficiale")

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

    remove_note(notes, "Foto prodotto non individuata nel PDF ufficiale")

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
