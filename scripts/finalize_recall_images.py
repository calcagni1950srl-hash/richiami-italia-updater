import hashlib
import io
import json
import subprocess
from pathlib import Path

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
        int(0.025 * cw),
        int(0.025 * ch),
        int(0.990 * cw),
        int(0.990 * ch),
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

    # Se nella foto finale sono rimaste frasi tipiche del modulo,
    # significa che abbiamo incluso ancora parte del foglio.
    contaminated = (
        'non consumare il prodotto' in text
        or 'inserire immagine' in text
        or 'restituirlo presso' in text
    )

    if not contaminated:
        continue

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
