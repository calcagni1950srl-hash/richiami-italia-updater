import json
import shutil
from pathlib import Path
from urllib.parse import urlparse

BACKUP_DIR = Path('.quality/images-before-clean')
IMAGES_DIR = Path('images')
RECALLS_FILE = Path('recalls.json')
RAW_BASE = (
    'https://raw.githubusercontent.com/'
    'calcagni1950srl-hash/richiami-italia-updater/'
    'refs/heads/main/images/'
)


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


def backup_candidate_for(recall_id, all_ids):
    """Trova il file del richiamo senza confonderlo con ID figli/simili."""
    candidates = sorted(BACKUP_DIR.glob(f'{recall_id}-*.png'))
    if not candidates:
        return None

    child_prefixes = [
        f'{other}-'
        for other in all_ids
        if other != recall_id and other.startswith(f'{recall_id}-')
    ]

    exact_candidates = [
        path
        for path in candidates
        if not any(path.name.startswith(prefix) for prefix in child_prefixes)
    ]

    if not exact_candidates:
        return None

    # Il backup contiene normalmente un solo file per ID; se ce ne fossero
    # più di uno scegliamo il più recente per mtime.
    return max(exact_candidates, key=lambda path: path.stat().st_mtime)


data = json.loads(RECALLS_FILE.read_text(encoding='utf-8'))
recalls = data if isinstance(data, list) else data.get('recalls', [])
all_ids = {
    str(item.get('id', '') or '').strip()
    for item in recalls
    if str(item.get('id', '') or '').strip()
}

restored = 0
relinked = 0
unresolved = []
changed = False

for recall in recalls:
    rid = str(recall.get('id', '') or '').strip()
    if not rid:
        continue

    key = image_key(recall)
    filename = filename_from_url(recall.get(key, ''))
    if not filename:
        continue

    target = IMAGES_DIR / filename
    if target.exists():
        continue

    # Primo tentativo: stesso identico filename nel backup.
    backup = BACKUP_DIR / filename
    if backup.exists():
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup, target)
        restored += 1
        print(f'✅ Ripristinata foto cancellata: {rid} -> {filename}')
        continue

    # Secondo tentativo: la pulizia ha creato un nuovo hash e poi un ID
    # padre lo ha cancellato. Torniamo alla foto valida generata subito
    # prima della pulizia e riallineiamo anche recalls.json a quel filename.
    candidate = backup_candidate_for(rid, all_ids)
    if candidate is not None:
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        destination = IMAGES_DIR / candidate.name
        shutil.copy2(candidate, destination)

        new_url = RAW_BASE + candidate.name
        if str(recall.get(key, '') or '').strip() != new_url:
            recall[key] = new_url
            changed = True

        relinked += 1
        print(
            f'✅ Foto recuperata e URL riallineato: '
            f'{rid} -> {candidate.name}'
        )
        continue

    unresolved.append((rid, filename))

if changed:
    RECALLS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )

print(f'Foto ripristinate con stesso filename: {restored}')
print(f'Foto recuperate con URL riallineato: {relinked}')
if unresolved:
    print('⚠️ URL immagini ancora senza file locale:')
    for rid, filename in unresolved:
        print(f' - {rid}: {filename}')
