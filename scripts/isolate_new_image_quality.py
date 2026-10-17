"""Run the costly product-photo quality pipeline on newly acquired recall IDs only.

Prepare snapshots the live 20-item list and its images, replaces recalls.json
temporarily with only selected IDs. Complete reattaches the reviewed new rows
in original RSS order and restores all pre-existing image bytes unchanged.
A failed quality pipeline never publishes because the workflow uses set -e.
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

SAFE = Path(".incremental-image-safety")
REC = Path("recalls.json")
IMG = Path("images")
SHEETS = Path("lavoro/schede.json")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")


def prepare(rec=REC, img=IMG, sheets=SHEETS, safe=SAFE):
    if safe.exists():
        raise RuntimeError("Backup temporaneo già esistente: interrompo per sicurezza")
    all_data = read(rec)
    requested = read(sheets)
    ids = [str(x.get("id", "") or "").strip() for x in requested]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("Selezione incrementale vuota/duplicata: interrompo")
    existing = {x["id"]: x for x in all_data.get("recalls", [])}
    if not set(ids).issubset(existing):
        raise RuntimeError("ID nuovo assente dal database assemblato")
    safe.mkdir(parents=True)
    write(safe / "full.json", all_data)
    write(safe / "selected-ids.json", ids)
    shutil.copytree(img, safe / "images", dirs_exist_ok=True)
    subset = dict(all_data)
    subset["recalls"] = [existing[rid] for rid in ids]
    subset["totale"] = len(ids)
    subset["pass"] = sum(x.get("stato") == "PASS" for x in subset["recalls"])
    subset["daVerificare"] = subset["totale"] - subset["pass"]
    write(rec, subset)
    print(f"Foto: solo {len(ids)} richiami nuovi/incompleti in esame; "
          f"{len(all_data['recalls'])-len(ids)} schede precedenti protette")


def complete(rec=REC, img=IMG, safe=SAFE):
    full = read(safe / "full.json")
    ids = read(safe / "selected-ids.json")
    reviewed = read(rec)
    selected = {x["id"]: x for x in reviewed.get("recalls", [])}
    if set(ids) != set(selected) or len(selected) != len(reviewed.get("recalls", [])):
        raise ValueError("QA ha perso/aggiunto un ID: pubblicazione interrotta")
    before_ids = [x["id"] for x in full.get("recalls", [])]
    if not set(ids).issubset(before_ids):
        raise ValueError("Backup incoerente: pubblicazione interrotta")
    original_by_id = {x["id"]: x for x in full["recalls"]}
    out = dict(full)
    out["recalls"] = [
        selected[x["id"]] if x["id"] in selected else x
        for x in full["recalls"]
    ]
    out["totale"] = len(out["recalls"])
    out["pass"] = sum(x.get("stato") == "PASS" for x in out["recalls"])
    out["daVerificare"] = out["totale"] - out["pass"]

    # Restore the byte-for-byte original product photos for all untouched
    # recalls. QA tools may remove unreferenced PNGs from the temp subset.
    original_images = safe / "images"
    img.mkdir(parents=True, exist_ok=True)
    new_files = {
        str(row.get("immagine", "") or "").rsplit("/", 1)[-1]
        for row in selected.values()
        if row.get("immagine")
    }
    for source in original_images.glob("*"):
        if not source.is_file():
            continue
        target = img / source.name
        if source.name in new_files and target.exists():
            # New reviewed photo takes precedence on the SAME selected ID.
            continue
        shutil.copy2(source, target)

    for row in out["recalls"]:
        rid = row["id"]
        if rid not in selected and row != original_by_id[rid]:
            raise RuntimeError("Dati di un richiamo precedente alterati")
        url = str(row.get("immagine", "") or "")
        if url and "/images/" in url:
            name = url.rsplit("/", 1)[-1].split("?", 1)[0]
            if not (img / name).is_file():
                raise RuntimeError(f"Foto associata ma mancante: {rid} {name}")

    write(rec, out)
    shutil.rmtree(safe)
    print(f"Foto: {len(selected)} richiami verificati, "
          f"{len(out['recalls'])-len(selected)} schede e foto precedenti preservate")


def self_test():
    with tempfile.TemporaryDirectory() as root:
        wd=Path(root)
        rec=wd/"recalls.json"
        img=wd/"images"
        safe=wd/"safe"
        sheets=wd/"schede.json"
        img.mkdir()
        (img/"old-1234567890.png").write_bytes(b"old-picture")
        original={"version":2,"generatedAt":"original",
                  "recalls":[
                      {"id":"nuovo","prodotto":"Nuovo","immagine":"","stato":"PASS"},
                      {"id":"vecchio","motivo":"PDF verificato","stato":"PASS",
                       "immagine":"https://example.org/images/old-1234567890.png"}
                  ]}
        write(rec,original)
        write(sheets,[{"id":"nuovo"}])
        prepare(rec,img,sheets,safe)
        assert [x["id"] for x in read(rec)["recalls"]]==["nuovo"]
        (img/"old-1234567890.png").unlink()
        new={"recalls":[{"id":"nuovo","prodotto":"Nuovo","stato":"PASS",
                         "immagine":"https://example.org/images/new-1234567890.png"}]}
        (img/"new-1234567890.png").write_bytes(b"new-picture")
        write(rec,new)
        complete(rec,img,safe)
        result=read(rec)
        assert result["recalls"][1] == original["recalls"][1]
        assert result["recalls"][0]["immagine"].endswith("new-1234567890.png")
        assert (img/"old-1234567890.png").read_bytes()==b"old-picture"
        assert result["totale"]==2
        assert not safe.exists()
    print("PASS: verifica foto solo nuove, rollback completo e integrità vecchie immagini")


if __name__=="__main__":
    if "--self-test" in sys.argv:
        self_test()
    elif len(sys.argv)==2 and sys.argv[1]=="prepare":
        prepare()
    elif len(sys.argv)==2 and sys.argv[1]=="complete":
        complete()
    else:
        raise SystemExit("Uso: isolate_new_image_quality.py prepare|complete|--self-test")
