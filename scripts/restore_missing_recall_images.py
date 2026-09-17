import json
import shutil
from pathlib import Path
from urllib.parse import urlparse

BACKUP_DIR = Path('.quality/images-before-clean')
IMAGES_DIR = Path('images')
RECALLS_FILE = Path('recalls.json')


def image_key(recall):
    if 'immagine' in recall:
        return 'immagine'
    if 'imageUrl' in recall:
        return 'imageUrl'
    return 'immagine'


def filename_from_url(value):
    value = str(value or '').strip()
    if not value or '/images/' not in value:
        return ''
    return Path(urlparse(value).path).name


data = json.loads(RECALLS_FILE.read_text(encoding='utf-8'))
recalls = data if isinstance(data, list) else data.get('recalls', [])
restored = 0
unresolved = []

for recall in recalls:
    rid = str(recall.get('id', '') or '').strip()
    key = image_key(recall)
    filename = filename_from_url(recall.get(key, ''))
    if not filename:
        continue

    target = IMAGES_DIR / filename
    if target.exists():
        continue

    backup = BACKUP_DIR / filename
    if backup.exists():
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup, target)
        restored += 1
        print(f'✅ Ripristinata foto cancellata per collisione ID: {rid} -> {filename}')
    else:
        unresolved.append((rid, filename))

print(f'Foto ripristinate: {restored}')
if unresolved:
    print('⚠️ URL immagini ancora senza file locale:')
    for rid, filename in unresolved:
        print(f' - {rid}: {filename}')
