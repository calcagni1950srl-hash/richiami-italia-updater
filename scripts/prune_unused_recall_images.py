"""Elimina SOLO PNG non referenziati dal database pubblicato Richiami Italia.

Non modifica recalls.json né notification-state.json. La validazione è
fail-closed: nessuna cancellazione se manca un'immagine ancora referenziata,
il database è vuoto oppure una URL immagine è inattesa. Solo images/*.png.
"""
import argparse
import json
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse


def plan_cleanup(database_path: Path, images_dir: Path):
    data = json.loads(database_path.read_text(encoding="utf-8"))
    rows = data.get("recalls")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Database vuoto/non valido: pulizia bloccata")
    if not images_dir.is_dir():
        raise ValueError("Cartella immagini assente: pulizia bloccata")

    ids = [str(row.get("id", "") or "").strip() for row in rows]
    if any(not rid for rid in ids) or len(ids) != len(set(ids)):
        raise ValueError("ID mancanti o duplicati: pulizia bloccata")

    protected = set()
    for row in rows:
        url = str(row.get("immagine", "") or "").strip()
        if not url:
            continue
        parsed = urlparse(url)
        name = Path(parsed.path).name
        if (
            parsed.scheme != "https"
            or parsed.hostname != "raw.githubusercontent.com"
            or "/richiami-italia-updater/" not in parsed.path
            or not parsed.path.endswith("/images/" + name)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*-[0-9a-f]{10}\.png", name)
        ):
            raise ValueError(f"Foto fuori formato o repository per {row['id']}: {url}")
        protected.add(name)

    if not protected:
        raise ValueError("Nessuna foto referenziata: evito cancellazioni massive")

    actual = {p.name for p in images_dir.glob("*.png") if p.is_file()}
    missing = protected - actual
    if missing:
        raise ValueError("Foto di richiami ATTIVI mancanti: " + ", ".join(sorted(missing)))
    unused = sorted(actual - protected)
    return len(rows), len(protected), unused


def self_test():
    with tempfile.TemporaryDirectory() as root:
        root = Path(root)
        folder = root / "images"
        folder.mkdir()
        keep_name = "attuale-0123456789.png"
        old_name = "uscito-fedcba9876.png"
        (folder / keep_name).write_bytes(b"foto attuale invariata")
        (folder / old_name).write_bytes(b"foto richiamo vecchio")
        (folder / "readme.txt").write_text("non PNG, non eliminare", encoding="utf-8")
        db = root / "recalls.json"
        url = ("https://raw.githubusercontent.com/"
               "calcagni1950srl-hash/richiami-italia-updater/refs/heads/main/images/"
               + keep_name)
        db.write_text(json.dumps({"recalls": [{"id": "attuale", "immagine": url}]}), encoding="utf-8")
        count, protected, unused = plan_cleanup(db, folder)
        assert count == protected == 1 and unused == [old_name]
        assert (folder / old_name).exists(), "modalita lettura deve preservare file"
        for name in unused:
            (folder / name).unlink()
        assert (folder / keep_name).read_bytes() == b"foto attuale invariata"
        assert (folder / "readme.txt").exists()
        db.write_text(json.dumps({"recalls": [{"id": "attuale", "immagine": url},
                                               {"id": "nuovo", "immagine": ""}]}), encoding="utf-8")
        assert plan_cleanup(db, folder) == (2, 1, [])
        (folder / keep_name).unlink()
        try:
            plan_cleanup(db, folder)
        except ValueError as e:
            assert "mancanti" in str(e)
        else:
            raise AssertionError("Una foto attiva assente deve bloccare la pulizia")
        db.write_text(json.dumps({"recalls": []}), encoding="utf-8")
        try:
            plan_cleanup(db, folder)
        except ValueError:
            pass
        else:
            raise AssertionError("Database vuoto non deve poter cancellare foto")
    print("PASS: elimina solo immagini orfane; preserva schede correnti e blocca input incompleti")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="rimuove gli orfani")
    parser.add_argument("--self-test", action="store_true", help="test senza modificare il repository")
    parser.add_argument("--database", default="recalls.json")
    parser.add_argument("--images", default="images")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    db_path = Path(args.database)
    image_dir = Path(args.images)
    rows, protected, unused = plan_cleanup(db_path, image_dir)
    print(f"Richiami conservati: {rows}; foto protette: {protected}; PNG orfani: {len(unused)}")
    for name in unused:
        print(("RIMUOVO" if args.apply else "DA RIMUOVERE") + ":", name)
    if args.apply:
        for name in unused:
            (image_dir / name).unlink()
        print(f"Pulizia completata: {len(unused)} immagini orfane eliminate.")
    else:
        print("Anteprima soltanto: nessuna eliminazione eseguita.")


if __name__ == "__main__":
    main()
