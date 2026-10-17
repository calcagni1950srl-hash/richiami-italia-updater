"""Assembla gli ID correnti dell'RSS con le sole schede appena elaborate.

L'elaborazione costosa lavora solo sui nuovi PDF; i vecchi richiami sono
copiati senza cambiare lotto, motivo, fotografia, note o provenienza.
Fallisce senza pubblicare nulla se un ID nuovo non è stato prodotto.
"""
import json
import sys
import tempfile
from pathlib import Path


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def assemble(previous, generated, feed_items):
    if not isinstance(feed_items, list) or not feed_items:
        raise ValueError("Elenco RSS vuoto/non valido: pubblicazione impedita")
    old = {str(x.get("id", "")): x for x in previous.get("recalls", []) if x.get("id")}
    new = {str(x.get("id", "")): x for x in generated.get("recalls", []) if x.get("id")}
    ids = [str(x.get("id", "") or "") for x in feed_items]
    if not all(ids) or len(ids) != len(set(ids)):
        raise ValueError("ID RSS vuoti o duplicati: pubblicazione impedita")
    if any(i not in ids for i in new):
        raise ValueError("Generazione contiene ID non presenti nel feed")
    merged = []
    for item in feed_items:
        rid = str(item["id"])
        if rid in new:
            merged.append(new[rid])
        elif rid in old:
            merged.append(old[rid])
        else:
            raise ValueError("Richiamo assente dai dati esistenti e dalla nuova estrazione: " + rid)
    out = dict(generated)
    out["recalls"] = merged
    out["totale"] = len(merged)
    out["pass"] = sum(str(x.get("stato", "")).upper() == "PASS" for x in merged)
    out["daVerificare"] = len(merged) - out["pass"]
    print(f"Assemblaggio RSS: {len(new)} elaborati, {len(merged)-len(new)} preservati, {len(merged)} totali")
    return out


def self_test():
    old = {"recalls": [
        {"id":"vecchio","motivo":"Testo PDF verificato", "immagine":"https://example.com/images/vecchio-1234567890.png",
         "lotto":"ABC","note":["Fotografia completa estratta dal PDF ufficiale"],"stato":"PASS"},
        {"id":"ultimo","motivo":"vecchio", "stato":"DA_VERIFICARE"},
    ]}
    new={"generatedAt":"2026-10-17T10:00:00Z","recalls":[{"id":"nuovo","motivo":"Salmonella", "stato":"PASS"}]}
    feed=[{"id":"nuovo"},{"id":"vecchio"},{"id":"ultimo"}]
    out=assemble(old,new,feed)
    assert [x["id"] for x in out["recalls"]]==["nuovo","vecchio","ultimo"]
    assert out["recalls"][1]==old["recalls"][0], "foto/motivo/lotti precedenti alterati"
    assert out["totale"]==3 and out["pass"]==2 and out["daVerificare"]==1
    assert out["generatedAt"]==new["generatedAt"]
    assert assemble(old,{"recalls":[]},[{"id":"vecchio"}])["recalls"]==[old["recalls"][0]]
    bad=False
    try: assemble(old,new,[{"id":"vecchio"}])
    except ValueError: bad=True
    assert bad, "nuovo non presente nel feed"
    bad=False
    try: assemble(old,{"recalls":[]},[{"id":"inesistente"}])
    except ValueError: bad=True
    assert bad, "perdita silenziosa ID"
    bad=False
    try: assemble(old,new,[])
    except ValueError: bad=True
    assert bad, "feed vuoto"
    print("PASS assemble: dati precedenti immutati, ordine RSS, nuovi integrati, errori bloccanti")


def main():
    if "--self-test" in sys.argv:
        self_test()
        return
    if len(sys.argv)!=4:
        raise SystemExit("Uso: assemble_incremental_recalls.py PRECEDENTE GENERATO FEED_INDEX")
    prev, generated, feed = map(load,sys.argv[1:4])
    out = assemble(prev, generated, feed)
    Path(sys.argv[2]).write_text(json.dumps(out,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")


if __name__=="__main__":
    main()
