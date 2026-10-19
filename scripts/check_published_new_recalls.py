"""Controllo di sola lettura prima di notificare i nuovi richiami.

Se tutte le schede sono GIA' pubblicate in recalls.json con PDF e
motivo, non avviare un altro workflow di acquisizione. Non considera
completa una scheda provvisoria RSS. NON modifica alcuno stato.
"""
import json
import os
import sys
from pathlib import Path


def published_pending_ready(pending: dict, published: dict):
    new_items = pending.get("newItems", []) or []
    if not isinstance(new_items, list):
        raise ValueError("notification-pending.newItems non valido")
    published_rows = {
        str(row.get("id", "") or "").strip(): row
        for row in published.get("recalls", []) or []
        if str(row.get("id", "") or "").strip()
    }
    ids = [str(x.get("id", "") or "").strip() for x in new_items]
    if any(not rid for rid in ids) or len(ids) != len(set(ids)):
        raise ValueError("ID dei nuovi richiami non validi o duplicati")
    unready = []
    for rid in ids:
        row = published_rows.get(rid)
        if row is None:
            unready.append((rid, "assente"))
            continue
        method = str(row.get("metodoEstrazione", "") or "").strip().upper()
        if method == "RSS" or str(row.get("criterioMatch", "")).upper() == "RSS_MINISTERO":
            unready.append((rid, "provvisorio RSS"))
            continue
        if not str(row.get("pdfMinistero", "") or "").strip():
            unready.append((rid, "PDF non associato"))
            continue
        if not str(row.get("motivo", "") or "").strip():
            unready.append((rid, "motivo mancante"))
    return not unready, unready, len(ids)


def self_test():
    pending = {"newItems": [{"id": "a"}, {"id": "b"}]}
    old = {"recalls": [{"id": "a", "pdfMinistero": "https://salute.gov.it/a.pdf",
                         "motivo": "Salmonella", "metodoEstrazione": "TESTO_DIGITALE",
                         "stato": "DA_VERIFICARE"},
                        {"id": "b", "pdfMinistero": "https://salute.gov.it/b.pdf",
                         "motivo": "Listeria", "metodoEstrazione": "OCR", "stato": "PASS"}]}
    ok, missing, n = published_pending_ready(pending, old)
    assert ok and not missing and n == 2
    ok, missing, n = published_pending_ready(pending, {"recalls": old["recalls"][:1]})
    assert not ok and missing == [("b", "assente")]
    provisional = {"recalls": [old["recalls"][0], dict(old["recalls"][1], metodoEstrazione="RSS")]}
    ok, missing, _ = published_pending_ready(pending, provisional)
    assert not ok and missing == [("b", "provvisorio RSS")]
    ok, missing, _ = published_pending_ready(pending, {"recalls": [
        old["recalls"][0], dict(old["recalls"][1], pdfMinistero="")]})
    assert not ok and missing == [("b", "PDF non associato")]
    assert published_pending_ready({"newItems": []}, old) == (True, [], 0)
    print("PASS: salta nuovo updater solo con PDF+motivo pubblicati; blocca RSS provvisori")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    else:
        pending = json.loads(Path(sys.argv[1] if len(sys.argv) > 1 else "notification-pending.json").read_text(encoding="utf-8"))
        db = json.loads(Path(sys.argv[2] if len(sys.argv) > 2 else "recalls.json").read_text(encoding="utf-8"))
        ready, missing, count = published_pending_ready(pending, db)
        print("Nuovi richiami rilevati:", count)
        print("Gia' pubblicati con PDF e motivo:", count - len(missing))
        for rid, reason in missing:
            print("DA_COMPLETARE:", rid, "-", reason)
        output = os.environ.get("GITHUB_OUTPUT")
        if output:
            with open(output, "a", encoding="utf-8") as f:
                f.write("ready=" + ("true" if ready else "false") + "\n")
        print("VERIFICA:", "già pronti per notifica" if ready else "aggiornamento ancora necessario")
