import hashlib
import io
import json
import subprocess
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

PDF_DIR = Path('.quality/pdf')
IMAGES_DIR = Path('images')
RECALLS_FILE = Path('recalls.json')
WORK_DIR = Path('.quality/finalize')
WORK_DIR.mkdir(parents=True, exist_ok=True)


def run(cmd):
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors='ignore',
    )


def image_text(path):
    result = run([
        'tesseract', str(path), 'stdout', '-l', 'ita', '--psm', '11'
    ])
    return result.stdout.lower()


def crop_photo_inside_form(image):
    """Rimuove bordo/modulo quando una foto valida è dentro un riquadro del form."""
    img = image.convert('RGB')
    arr = np.asarray(img)
    h, w = arr.shape[:2]

    if w < 180 or h < 140:
        return None

    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
    r = arr[:, :, 0].astype(np.int16)
    g = arr[:, :, 1].astype(np.int16)
    b = arr[:, :, 2].astype(np.int16)

    white_ratio = float(np.mean(np.all(arr >= 244, axis=2)))
    pale_blue_ratio = float(np.mean(
        (b > 220) & (g > 210) & (r > 180) & ((b - r) > 8)
    ))
    red_frame = (
        (r > 80) &
        (r > g * 1.35) &
        (r > b * 1.35) &
        (g < 180)
    )
    red_ratio = float(np.mean(red_frame))

    # Interveniamo solo su immagini che hanno una firma tipica del modulo:
    # molto bianco + fascia azzurra o bordo rosso.
    if white_ratio < 0.30:
        return None
    if pale_blue_ratio < 0.012 and red_ratio < 0.002:
        return None

    mask = (
        (gray < 244) |
        (hsv[:, :, 1] >= 12)
    ).astype(np.uint8) * 255

    # Il bordo rosso del riquadro unisce artificialmente foto e spazio bianco:
    # lo togliamo prima di cercare la vera area fotografica.
    mask[red_frame] = 0
    mask = cv2.morphologyEx(
        mask,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)),
        iterations=1,
    )

    contours, _ = cv2.findContours(
        mask,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )

    candidates = []
    image_area = w * h
    for contour in contours:
        x, y, bw, bh = cv2.boundingRect(contour)
        area = bw * bh
        if bw < max(80, int(w * 0.12)):
            continue
        if bh < max(100, int(h * 0.25)):
            continue
        if area < image_area * 0.05:
            continue
        if max(bw / max(1, bh), bh / max(1, bw)) > 5.0:
            continue
        candidates.append((area, x, y, bw, bh))

    if not candidates:
        return None

    _, x, y, bw, bh = max(candidates)
    # Se il contenuto fotografico arriva quasi al bordo, evitare un crop
    # stretto: è un segnale che il prodotto potrebbe essere già a filo.
    edge_x = max(6, int(w * 0.025))
    edge_y = max(6, int(h * 0.025))
    if x <= edge_x or y <= edge_y or x + bw >= w - edge_x or y + bh >= h - edge_y:
        return None

    pad_x = max(8, int(bw * 0.06))
    pad_y = max(8, int(bh * 0.06))

    x1 = max(0, x - pad_x)
    y1 = max(0, y - pad_y)
    x2 = min(w, x + bw + pad_x)
    y2 = min(h, y + bh + pad_y)

    crop = img.crop((x1, y1, x2, y2)).convert('RGB')

    # Evita correzioni che di fatto non ritagliano nulla.
    if crop.width * crop.height > image_area * 0.85:
        return None
    if crop.width < 150 or crop.height < 150:
        return None

    return crop


def versioned_filename(recall_id, image):
    buffer = io.BytesIO()
    image.save(buffer, format='PNG', optimize=True)
    data = buffer.getvalue()
    digest = hashlib.sha256(data).hexdigest()[:10]
    return f'{recall_id}-{digest}.png', data


def render_first_page(pdf, recall_id):
    folder = WORK_DIR / recall_id
    folder.mkdir(parents=True, exist_ok=True)
    prefix = folder / 'page'
    result = run([
        'pdftoppm', '-f', '1', '-singlefile', '-r', '160', '-png',
        str(pdf), str(prefix)
    ])
    path = folder / 'page.png'
    if result.returncode != 0 or not path.exists():
        return None
    return path


def recover_standard_left_photo(pdf, recall_id):
    page_path = render_first_page(pdf, recall_id)
    if page_path is None:
        return None

    page = Image.open(page_path).convert('RGB')
    w, h = page.size

    # Riquadro fotografico standard del modulo Ministero.
    # Il fondo del riquadro viene fermato prima della didascalia
    # "Inserire immagine uno" per non trascinare testo del modulo.
    crop = page.crop((
        int(0.060 * w),
        int(0.685 * h),
        int(0.495 * w),
        int(0.865 * h),
    )).convert('RGB')

    # Togliamo solo pochi pixel di bordo del riquadro, senza toccare
    # il prodotto vero e proprio.
    cw, ch = crop.size
    crop = crop.crop((
        int(0.005 * cw),
        int(0.005 * ch),
        int(0.995 * cw),
        int(0.995 * ch),
    ))

    if crop.width < 220 or crop.height < 140:
        return None

    return crop


with RECALLS_FILE.open('r', encoding='utf-8') as handle:
    database = json.load(handle)

recalls = database if isinstance(database, list) else database.get('recalls', [])
changed = False

for recall in recalls:
    recall_id = str(recall.get('id', '') or '').strip()
    if not recall_id:
        continue

    key = 'immagine' if 'immagine' in recall or 'imageUrl' not in recall else 'imageUrl'
    image_url = str(recall.get(key, '') or '').strip()
    if not image_url or '/images/' not in image_url:
        continue

    filename = image_url.rsplit('/', 1)[-1]
    current = IMAGES_DIR / filename
    if not current.exists():
        continue

    text = image_text(current)

    # Se la foto è dentro un riquadro del modulo, ritagliamo prima la vera
    # area fotografica interna. Questo elimina fascia azzurra, bordo rosso
    # e spazio bianco senza tagliare il prodotto.
    current_image = Image.open(current).convert('RGB')
    replacement = crop_photo_inside_form(current_image)

    # Se nella foto finale sono rimaste frasi tipiche del modulo,
    # significa che abbiamo incluso ancora parte del foglio.
    contaminated = (
        'non consumare il prodotto' in text
        or 'inserire immagine' in text
        or 'restituirlo presso' in text
        or 'avvertenze' in text
        or 'motivo del richiamo' in text
        or 'acquistato' in text
        or 'riconsegnare il prodotto' in text
        or 'procedere al suo utilizzo' in text
    )

    if replacement is None and not contaminated:
        continue

    if replacement is None:
        pdf = PDF_DIR / f'{recall_id}.pdf'
        if not pdf.exists():
            continue
        replacement = recover_standard_left_photo(pdf, recall_id)
        if replacement is None:
            continue

    new_name, png_bytes = versioned_filename(recall_id, replacement)
    target = IMAGES_DIR / new_name
    target.write_bytes(png_bytes)

    new_url = (
        'https://raw.githubusercontent.com/'
        'calcagni1950srl-hash/'
        'richiami-italia-updater/'
        'refs/heads/main/images/'
        f'{new_name}'
    )

    if image_url != new_url:
        recall[key] = new_url
        changed = True

    if current.name != new_name:
        current.unlink(missing_ok=True)

    print('✅ Residuo del modulo rimosso:', recall_id, replacement.size)

if changed:
    RECALLS_FILE.write_text(
        json.dumps(database, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
else:
    print('✅ Nessun residuo testuale del modulo da correggere.')
