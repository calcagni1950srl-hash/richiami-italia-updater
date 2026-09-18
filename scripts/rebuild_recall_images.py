import csv
import hashlib
import io
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import cv2
    import numpy as np
except ModuleNotFoundError:
    subprocess.check_call([
        sys.executable,
        "-m",
        "pip",
        "install",
        "--quiet",
        "numpy",
        "opencv-python-headless",
    ])
    import cv2
    import numpy as np

from PIL import Image


ROOT = Path(".quality")
PDF_DIR = ROOT / "pdf"
WORK_DIR = ROOT / "work"
IMAGES_DIR = Path("images")
RECALLS_FILE = Path("recalls.json")

PDF_DIR.mkdir(parents=True, exist_ok=True)
WORK_DIR.mkdir(parents=True, exist_ok=True)
IMAGES_DIR.mkdir(parents=True, exist_ok=True)

STOPWORDS = {
    "della", "delle", "degli", "dello", "dalla", "dalle", "dagli",
    "dallo", "dell", "con", "senza", "sottovuoto", "prodotto",
    "richiamo", "alimentare", "lotto", "peso", "netto", "kg", "gr",
    "spa", "srl", "s.p.a", "s.r.l", "italia", "italiano", "italiana",
}


def run(command):
    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="ignore",
    )


def normalize_token(value):
    cleaned = "".join(ch.lower() if ch.isalnum() else " " for ch in str(value or ""))
    return {
        token
        for token in cleaned.split()
        if len(token) >= 4 and token not in STOPWORDS
    }


def recall_tokens(recall):
    tokens = set()
    for key in ("marca", "prodotto", "brand", "productName"):
        tokens |= normalize_token(recall.get(key, ""))
    return tokens


def image_key(recall):
    if "immagine" in recall:
        return "immagine"
    if "imageUrl" in recall:
        return "imageUrl"
    return "immagine"


def normalized_digest(image):
    thumb = image.convert("RGB").copy()
    thumb.thumbnail((96, 96), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (96, 96), "white")
    canvas.paste(thumb, ((96 - thumb.width) // 2, (96 - thumb.height) // 2))
    return hashlib.sha256(canvas.tobytes()).hexdigest()


def image_stats(image):
    rgb = image.convert("RGB")
    width, height = rgb.size
    sample = rgb.copy()
    sample.thumbnail((600, 600), Image.Resampling.LANCZOS)
    arr = np.asarray(sample)
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
    edges = cv2.Canny(gray, 55, 145)

    return {
        "width": width,
        "height": height,
        "area": width * height,
        "white_ratio": float(np.mean(np.all(arr >= 244, axis=2))),
        "color_ratio": float(np.mean(hsv[:, :, 1] >= 20)),
        "contrast": float(gray.std()),
        "edge_ratio": float(np.mean(edges > 0)),
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

    if not result.stdout.strip():
        return []

    words = []
    reader = csv.DictReader(io.StringIO(result.stdout), delimiter="\t")

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


def words_text(words):
    return " ".join(item["text"] for item in words)


def text_metrics(words, width, height):
    chars = sum(len(item["text"]) for item in words)
    box_area = 0
    for item in words:
        x1 = max(0, item["left"])
        y1 = max(0, item["top"])
        x2 = min(width, item["left"] + item["width"])
        y2 = min(height, item["top"] + item["height"])
        if x2 > x1 and y2 > y1:
            box_area += (x2 - x1) * (y2 - y1)
    return chars, min(1.0, box_area / max(1, width * height))


def word_mask(shape, words, pad_x=8, pad_y=6):
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
        cv2.getStructuringElement(cv2.MORPH_RECT, (max(55, width // 16), 1)),
    )
    vertical = cv2.morphologyEx(
        nonwhite,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(55, height // 20))),
    )
    lines = cv2.bitwise_or(horizontal, vertical)
    return cv2.bitwise_and(nonwhite, cv2.bitwise_not(lines))


def overlap_score(text, wanted_tokens):
    if not wanted_tokens:
        return 0.0
    found = normalize_token(text)
    if not found:
        return 0.0
    return len(found & wanted_tokens) / max(1, len(wanted_tokens))


def looks_like_document(path, image):
    stats = image_stats(image)
    words = ocr_layout(path)
    chars, coverage = text_metrics(words, image.width, image.height)
    ratio = max(image.width, image.height) / max(1, min(image.width, image.height))
    a4_like = 1.25 <= ratio <= 1.75

    result = (
        coverage >= 0.17
        or (a4_like and stats["white_ratio"] >= 0.48 and chars >= 80)
        or (stats["white_ratio"] >= 0.75 and chars >= 55)
    )
    return result, words, chars, coverage, stats


def direct_candidate(path, wanted_tokens):
    try:
        image = Image.open(path).convert("RGB")
    except Exception:
        return None, None

    stats = image_stats(image)
    if stats["width"] < 110 or stats["height"] < 80:
        return None, None
    if stats["area"] < 20_000 or stats["contrast"] < 4.0:
        return None, None

    is_document, words, chars, coverage, stats = looks_like_document(path, image)
    if is_document:
        return None, image

    relevance = overlap_score(words_text(words), wanted_tokens)
    score = (
        min(math.log10(max(stats["area"], 1)) / 6.0, 1.1) * 2.0
        + stats["color_ratio"] * 1.5
        + min(stats["contrast"] / 70.0, 1.0)
        + min(stats["edge_ratio"] * 6.0, 0.9)
        - stats["white_ratio"] * 0.45
        + relevance * 1.2
    )

    return {
        "image": image,
        "score": score,
        "stats": stats,
        "relevance": relevance,
        "source": path.name,
    }, None


def render_pages(pdf, recall_id):
    folder = WORK_DIR / recall_id / "pages"
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True, exist_ok=True)
    prefix = folder / "page"

    result = run([
        "pdftoppm",
        "-f", "1",
        "-l", "3",
        "-r", "200",
        "-png",
        str(pdf),
        str(prefix),
    ])
    if result.returncode != 0:
        return []
    return sorted(folder.glob("page-*.png"))


def clamp_box(box, width, height):
    x1, y1, x2, y2 = box
    return (
        max(0, min(width - 1, x1)),
        max(0, min(height - 1, y1)),
        max(1, min(width, x2)),
        max(1, min(height, y2)),
    )


def expand_box(box, width, height, fraction=0.04):
    x1, y1, x2, y2 = box
    bw = max(1, x2 - x1)
    bh = max(1, y2 - y1)
    px = max(8, int(bw * fraction))
    py = max(8, int(bh * fraction))
    return clamp_box((x1 - px, y1 - py, x2 + px, y2 + py), width, height)


def words_in_box(words, box):
    x1, y1, x2, y2 = box
    selected = []
    for item in words:
        cx = item["left"] + item["width"] / 2
        cy = item["top"] + item["height"] / 2
        if x1 <= cx <= x2 and y1 <= cy <= y2:
            selected.append(item)
    return selected


def visual_mask_for_page(rgb, words, aggressive=False):
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    text = word_mask(rgb.shape, words, pad_x=8, pad_y=6)

    nonwhite = (gray < 246).astype(np.uint8) * 255
    residual = remove_form_lines(nonwhite)
    residual[text > 0] = 0

    colorful = (hsv[:, :, 1] >= 18).astype(np.uint8) * 255
    colorful[text > 0] = 0

    edges = cv2.Canny(gray, 50, 135)
    edges[text > 0] = 0

    visual = cv2.bitwise_or(residual, colorful)
    visual = cv2.bitwise_or(visual, edges)

    visual = cv2.morphologyEx(
        visual,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)),
        iterations=1,
    )

    kernel = (17, 17) if not aggressive else (29, 29)
    iterations = 1 if not aggressive else 2
    visual = cv2.morphologyEx(
        visual,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, kernel),
        iterations=iterations,
    )
    visual = cv2.dilate(
        visual,
        cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9)),
        iterations=1,
    )
    return visual, text, gray, hsv


def candidate_from_box(pil, rgb, visual, text, words, box, wanted_tokens, page_area):
    height, width = rgb.shape[:2]
    box = expand_box(box, width, height, 0.035)
    x1, y1, x2, y2 = box

    region_visual = visual[y1:y2, x1:x2]
    if region_visual.size == 0:
        return None

    ys, xs = np.where(region_visual > 0)
    if len(xs) == 0:
        return None

    vx1 = x1 + int(xs.min())
    vy1 = y1 + int(ys.min())
    vx2 = x1 + int(xs.max()) + 1
    vy2 = y1 + int(ys.max()) + 1
    tight = expand_box((vx1, vy1, vx2, vy2), width, height, 0.06)
    tx1, ty1, tx2, ty2 = tight

    crop = pil.crop(tight).convert("RGB")
    stats = image_stats(crop)
    if stats["width"] < 120 or stats["height"] < 90:
        return None
    if stats["area"] < 24_000 or stats["contrast"] < 5.0:
        return None

    visual_occ = float(np.mean(visual[ty1:ty2, tx1:tx2] > 0))
    text_ratio = float(np.mean(text[ty1:ty2, tx1:tx2] > 0))
    local_words = words_in_box(words, tight)
    chars = sum(len(item["text"]) for item in local_words)
    relevance = overlap_score(words_text(local_words), wanted_tokens)

    ratio = max(crop.width, crop.height) / max(1, min(crop.width, crop.height))
    crop_fraction = stats["area"] / max(1, page_area)

    if stats["white_ratio"] > 0.84:
        return None
    if stats["white_ratio"] > 0.68 and visual_occ < 0.12:
        return None
    if stats["white_ratio"] > 0.58 and chars > 90 and text_ratio > 0.04:
        return None
    if 1.25 <= ratio <= 1.75 and crop_fraction > 0.30 and stats["white_ratio"] > 0.46 and chars > 45:
        return None
    if visual_occ < 0.045:
        return None

    score = (
        visual_occ * 3.0
        + stats["color_ratio"] * 1.6
        + min(stats["edge_ratio"] * 6.0, 1.0)
        + min(stats["contrast"] / 75.0, 1.0)
        + min(math.sqrt(stats["area"] / max(1, page_area)), 0.8)
        - stats["white_ratio"] * 0.75
        - text_ratio * 1.8
        + relevance * 1.2
    )

    return {
        "image": crop,
        "score": score,
        "stats": stats,
        "visual_occ": visual_occ,
        "text_ratio": text_ratio,
        "chars": chars,
        "relevance": relevance,
        "box": tight,
    }


def photo_candidates_from_page(path, wanted_tokens):
    pil = Image.open(path).convert("RGB")
    rgb = np.asarray(pil)
    height, width = rgb.shape[:2]
    page_area = width * height
    words = ocr_layout(path)

    all_candidates = []

    for aggressive in (False, True):
        visual, text, _, _ = visual_mask_for_page(rgb, words, aggressive=aggressive)
        contours, _ = cv2.findContours(visual, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            area = w * h
            if w < 120 or h < 90:
                continue
            if area < page_area * 0.006:
                continue
            if area > page_area * 0.48:
                continue
            ratio = max(w / h, h / w)
            if ratio > 4.8:
                continue

            item = candidate_from_box(
                pil,
                rgb,
                visual,
                text,
                words,
                (x, y, x + w, y + h),
                wanted_tokens,
                page_area,
            )
            if item:
                all_candidates.append(item)

        if all_candidates:
            break

    unique = []
    seen = set()
    for item in sorted(all_candidates, key=lambda x: x["score"], reverse=True):
        digest = normalized_digest(item["image"])
        if digest in seen:
            continue
        seen.add(digest)
        unique.append(item)

    return unique


def extract_best_image_from_pdf(pdf, recall_id, wanted_tokens):
    folder = WORK_DIR / recall_id / "embedded"
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True, exist_ok=True)
    prefix = folder / "img"

    result = run(["pdfimages", "-png", str(pdf), str(prefix)])
    direct = []
    page_like = []

    if result.returncode == 0:
        for path in sorted(folder.glob("img-*.png")):
            item, page_image = direct_candidate(path, wanted_tokens)
            if item:
                direct.append(item)
            elif page_image is not None:
                page_like.append((path.name, page_image))

    if direct:
        direct.sort(key=lambda x: x["score"], reverse=True)
        best = direct[0]
        return best["image"], "foto incorporata", best

    scan_candidates = []
    page_like_dir = WORK_DIR / recall_id / "page-like"
    page_like_dir.mkdir(parents=True, exist_ok=True)
    for index, (_, image) in enumerate(page_like):
        path = page_like_dir / f"source-{index:03d}.png"
        image.save(path)
        for item in photo_candidates_from_page(path, wanted_tokens):
            item["source"] = path.name
            scan_candidates.append(item)

    for page_path in render_pages(pdf, recall_id):
        for item in photo_candidates_from_page(page_path, wanted_tokens):
            item["source"] = page_path.name
            scan_candidates.append(item)

    if not scan_candidates:
        return None, "", None

    scan_candidates.sort(key=lambda x: x["score"], reverse=True)
    best = scan_candidates[0]
    return best["image"], "foto estratta dalla scansione", best


def prepare_output(image):
    result = image.convert("RGB").copy()
    if max(result.size) > 1800:
        result.thumbnail((1800, 1800), Image.Resampling.LANCZOS)
    return result


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

if isinstance(database, list):
    recalls = database
else:
    recalls = database.get("recalls", [])

accepted_names = set()
changed = False

for recall in recalls:
    recall_id = str(recall.get("id", "") or "").strip()
    if not recall_id:
        continue

    pdf = PDF_DIR / f"{recall_id}.pdf"
    key = image_key(recall)
    old_url = str(recall.get(key, "") or "")
    notes = recall.setdefault("note", []) if isinstance(recall.get("note", []), list) else []
    if "note" not in recall or not isinstance(recall.get("note"), list):
        recall["note"] = notes

    for marker in [
        "Immagine scartata dal controllo qualità",
        "Immagine prodotto non estratta",
        "Immagine non disponibile nel PDF ufficiale",
        "Foto prodotto non individuata nel PDF ufficiale",
    ]:
        remove_note(notes, marker)

    image = None
    source_label = ""
    meta = None

    if pdf.exists():
        image, source_label, meta = extract_best_image_from_pdf(
            pdf,
            recall_id,
            recall_tokens(recall),
        )

    if image is None:
        old_filename = ""
        if old_url and "/images/" in old_url:
            old_filename = old_url.rsplit("/", 1)[-1].split("?", 1)[0].strip()

        if old_filename and (IMAGES_DIR / old_filename).exists():
            accepted_names.add(old_filename)
            print(
                "✅ Foto esistente preservata:",
                recall_id,
                "-",
                old_filename,
            )
            continue

        if old_url:
            recall[key] = ""
            changed = True

        marker = "Foto prodotto non individuata nel PDF ufficiale"
        if marker not in notes:
            notes.append(marker)
            changed = True

        print("⚠️ Nessuna foto prodotto individuata:", recall_id)
        continue

    output = prepare_output(image)
    filename, png_bytes = versioned_filename(recall_id, output)
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
        recall[key] = new_url
        changed = True

    stats = image_stats(output)
    extra = ""
    if meta:
        extra = (
            f" white={stats['white_ratio']:.2f}"
            f" color={stats['color_ratio']:.2f}"
            f" edge={stats['edge_ratio']:.3f}"
            f" score={meta.get('score', 0):.2f}"
        )

    print(
        "✅ Foto singola:",
        recall_id,
        "-",
        source_label,
        f"- {output.width}x{output.height}",
        extra,
    )

referenced_names = set(accepted_names)

for recall in recalls:
    key = image_key(recall)
    url = str(recall.get(key, "") or "").strip()
    if url and "/images/" in url:
        filename = url.rsplit("/", 1)[-1].split("?", 1)[0].strip()
        if filename:
            referenced_names.add(filename)

for path in IMAGES_DIR.glob("*.png"):
    if path.name not in referenced_names:
        path.unlink(missing_ok=True)
        changed = True

if changed:
    RECALLS_FILE.write_text(
        json.dumps(database, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("✅ Immagini aggiornate: una sola foto per richiamo, nessun collage.")
else:
    print("✅ Nessuna modifica necessaria alle foto prodotto.")
