import hashlib
import io
import json
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps, ImageStat


ROOT = Path(".quality")
PDF_DIR = ROOT / "pdf"
WORK_DIR = ROOT / "work"
IMAGES_DIR = Path("images")
RECALLS_FILE = Path("recalls.json")

PDF_DIR.mkdir(parents=True, exist_ok=True)
WORK_DIR.mkdir(parents=True, exist_ok=True)
IMAGES_DIR.mkdir(parents=True, exist_ok=True)


STOPWORDS = {
    "della", "delle", "dello", "degli", "dell", "alla", "alle", "allo", "agli",
    "con", "senza", "sotto", "sottovuoto", "prodotto", "prodotti", "alimentare",
    "alimentari", "richiamo", "rischio", "marca", "marchio", "nome", "sede",
    "societa", "soc", "srl", "srls", "spa", "sasu", "snc", "italia", "italy",
    "francia", "france", "via", "rue", "kg", "gr", "grammi", "grammo", "litri",
    "litro", "ml", "pz", "conf", "confezione", "confezioni", "neutre",
}

DOCUMENT_WORDS = {
    "richiamo", "denominazione", "lotto", "produttore", "stabilimento",
    "motivo", "marchio", "scadenza", "conservazione", "ministero", "salute",
    "modello", "segnalazione",
}


def run(command):
    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="ignore",
    )


def normalize(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def tokens(value):
    return [
        token
        for token in normalize(value).split()
        if len(token) >= 4
        and token not in STOPWORDS
        and not token.isdigit()
    ]


def ocr_text(path):
    result = run([
        "tesseract",
        str(path),
        "stdout",
        "-l",
        "ita",
        "--psm",
        "11",
    ])
    return result.stdout.strip()


def fuzzy_token_match(target, words):
    if target in words:
        return True

    if len(target) >= 6:
        for word in words:
            if len(word) < 4:
                continue

            if target in word or word in target:
                ratio = min(len(target), len(word)) / max(len(target), len(word))
                if ratio >= 0.72:
                    return True

    return False


def identity_score(recall, text):
    ocr_words = set(normalize(text).split())
    brand_tokens = tokens(recall.get("marca", ""))
    product_tokens = tokens(recall.get("prodotto", ""))

    brand_hits = sum(
        fuzzy_token_match(token, ocr_words)
        for token in brand_tokens
    )

    product_hits = sum(
        fuzzy_token_match(token, ocr_words)
        for token in product_tokens
    )

    strong_product = any(
        len(token) >= 7 and fuzzy_token_match(token, ocr_words)
        for token in product_tokens
    )

    verified = (
        brand_hits >= 1
        or product_hits >= 2
        or strong_product
        or (len(product_tokens) == 1 and product_hits == 1)
    )

    score = brand_hits * 4.0 + product_hits * 2.0

    return verified, score, brand_hits, product_hits


def image_metrics(image):
    rgb = image.convert("RGB")
    gray = rgb.convert("L")

    detail = ImageStat.Stat(gray).stddev[0]

    edges = gray.filter(ImageFilter.FIND_EDGES)
    edge_detail = ImageStat.Stat(edges).stddev[0]

    small = rgb.copy()
    small.thumbnail((300, 300))
    pixels = list(small.getdata())

    white = 0
    colorful = 0

    for red, green, blue in pixels:
        if red >= 244 and green >= 244 and blue >= 244:
            white += 1

        if max(red, green, blue) - min(red, green, blue) >= 22:
            colorful += 1

    total = max(1, len(pixels))

    return {
        "detail": detail,
        "edge": edge_detail,
        "white": white / total,
        "colorful": colorful / total,
    }


def candidate_from(path, recall):
    try:
        image = Image.open(path).convert("RGB")
    except Exception:
        return None

    width, height = image.size

    if width < 260 or height < 160 or width * height < 100_000:
        return None

    ratio = max(width / height, height / width)

    if ratio > 4.0:
        return None

    try:
        if path.stat().st_size < 12_000:
            return None
    except OSError:
        return None

    metrics = image_metrics(image)

    # Controlli prudenziali: immagini troppo uniformi o prive di bordi
    # sono spesso miniature compresse, sfocate o elementi grafici del modulo.
    if metrics["detail"] < 14.0:
        return None

    if metrics["edge"] < 7.0:
        return None

    if metrics["white"] > 0.88:
        return None

    text = ocr_text(path)
    normalized_ocr = normalize(text)
    ocr_words = normalized_ocr.split()

    document_hits = sum(
        1
        for word in DOCUMENT_WORDS
        if word in ocr_words
    )

    # Non pubblichiamo scansioni/moduli al posto della confezione.
    if document_hits >= 4 and len(normalized_ocr) > 120:
        return None

    identity_ok, identity, brand_hits, product_hits = identity_score(
        recall,
        text,
    )

    photo_strength = (
        metrics["colorful"] * 2.4
        + min(metrics["detail"] / 55.0, 1.5)
        + min(metrics["edge"] / 35.0, 1.5)
        - metrics["white"] * 0.8
    )

    return {
        "path": path,
        "image": image,
        "identity_ok": identity_ok,
        "identity": identity,
        "brand_hits": brand_hits,
        "product_hits": product_hits,
        "photo_strength": photo_strength,
        "area": width * height,
    }


def extract_embedded_images(pdf, recall):
    folder = WORK_DIR / recall["id"]

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

    found = []

    for path in sorted(folder.glob("img-*.png")):
        candidate = candidate_from(path, recall)
        if candidate:
            found.append(candidate)

    return found


def choose_candidate(candidates):
    if not candidates:
        return None

    verified = [
        item
        for item in candidates
        if item["identity_ok"]
    ]

    if verified:
        verified.sort(
            key=lambda item: (
                item["identity"],
                item["photo_strength"],
                item["area"],
            ),
            reverse=True,
        )

        return verified[0]

    # Regola prudente: senza conferma testuale accettiamo soltanto
    # una singola immagine fotografica molto forte e sufficientemente grande.
    # In caso di dubbio è preferibile mostrare "Foto non disponibile".
    if len(candidates) == 1:
        item = candidates[0]

        if (
            item["photo_strength"] >= 1.55
            and item["area"] >= 350_000
        ):
            return item

    return None


def make_centered_square(image):
    # Non tagliamo mai la foto. La inseriamo intera in una tela bianca
    # quadrata con margine, così nell'app risulta sempre centrata.
    rgb = image.convert("RGB")
    fitted = ImageOps.contain(
        rgb,
        (1000, 1000),
        Image.Resampling.LANCZOS,
    )

    canvas = Image.new(
        "RGB",
        (1100, 1100),
        "white",
    )

    x = (canvas.width - fitted.width) // 2
    y = (canvas.height - fitted.height) // 2

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


def current_image_path(recall):
    url = str(recall.get("immagine", "") or "")
    match = re.search(r"/images/([^?/#]+\.png)", url)

    if not match:
        return None

    path = IMAGES_DIR / match.group(1)

    return path if path.exists() else None


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
    chosen = None

    if pdf.exists():
        candidates = extract_embedded_images(
            pdf,
            recall,
        )

        chosen = choose_candidate(candidates)

    # Se il PDF non è stato scaricato per un problema temporaneo,
    # controlliamo almeno l'immagine già presente nel repository.
    if chosen is None and not pdf.exists():
        existing = current_image_path(recall)

        if existing:
            candidate = candidate_from(
                existing,
                recall,
            )

            if candidate and (
                candidate["identity_ok"]
                or (
                    candidate["photo_strength"] >= 1.65
                    and candidate["area"] >= 400_000
                )
            ):
                chosen = candidate

    old_url = str(recall.get("immagine", "") or "")

    if chosen is None:
        if old_url:
            recall["immagine"] = ""
            changed = True

        notes = recall.setdefault("note", [])
        marker = "Immagine scartata dal controllo qualità"

        if marker not in notes:
            notes.append(marker)
            changed = True

        print("🚫 Foto scartata:", recall_id)
        continue

    centered = make_centered_square(
        chosen["image"]
    )

    filename, png_bytes = versioned_filename(
        recall_id,
        centered,
    )

    target = IMAGES_DIR / filename
    target.write_bytes(png_bytes)
    accepted_names.add(filename)

    # Il nome include l'hash dell'immagine: quando cambia la foto cambia anche
    # l'URL e Coil non può riutilizzare una vecchia immagine dalla cache.
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

    notes = recall.setdefault("note", [])
    bad_marker = "Immagine scartata dal controllo qualità"

    if bad_marker in notes:
        notes.remove(bad_marker)
        changed = True

    print(
        "✅ Foto accettata:",
        recall_id,
        "brand_hits=",
        chosen["brand_hits"],
        "product_hits=",
        chosen["product_hits"],
        "strength=",
        round(chosen["photo_strength"], 2),
    )


# Rimuoviamo le vecchie immagini non più referenziate, incluse quelle senza
# versione generate dall'updater principale. Il JSON resta la fonte di verità.
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

    print("✅ Controllo qualità immagini completato con modifiche.")
else:
    print("✅ Controllo qualità immagini: nessuna modifica necessaria.")
