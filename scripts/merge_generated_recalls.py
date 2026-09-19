import json
import sys
from pathlib import Path


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    generated_path = Path(
        sys.argv[1] if len(sys.argv) > 1 else ".generated-recalls.json"
    )
    latest_path = Path(
        sys.argv[2] if len(sys.argv) > 2 else "recalls.json"
    )

    if not generated_path.exists():
        raise SystemExit(f"File generato assente: {generated_path}")
    if not latest_path.exists():
        raise SystemExit(f"Database corrente assente: {latest_path}")

    generated = load_json(generated_path)
    latest = load_json(latest_path)

    generated_recalls = generated.get("recalls", []) or []
    latest_recalls = latest.get("recalls", []) or []

    latest_by_id = {
        str(item.get("id", "") or "").strip(): item
        for item in latest_recalls
        if str(item.get("id", "") or "").strip()
    }

    generated_ids = set()

    for item in generated_recalls:
        rid = str(item.get("id", "") or "").strip()
        if not rid:
            continue

        generated_ids.add(rid)
        latest_item = latest_by_id.get(rid) or {}
        latest_image = str(
            latest_item.get("immagine", "") or ""
        ).strip()

        # Il workflow database non deve mai sostituire una foto
        # già validata dal workflow qualità immagini.
        if latest_image and "/images/" in latest_image:
            item["immagine"] = latest_image

        # Se il dato ufficiale appena estratto è ancora vuoto ma il
        # database corrente contiene un valore valido, non peggioriamo
        # la scheda già pubblicata.
        for key in (
            "marca",
            "prodotto",
            "lotto",
            "tmc",
            "produttore",
            "motivo",
            "dataPubblicazione",
            "urlMinistero",
            "pdfMinistero",
        ):
            current_value = str(latest_item.get(key, "") or "").strip()
            generated_value = str(item.get(key, "") or "").strip()

            if current_value and not generated_value:
                item[key] = latest_item[key]

        # Non rimettere una scheda completa nello stato provvisorio RSS.
        if str(latest_item.get("stato", "") or "").upper() == "PASS":
            generated_reason = str(item.get("motivo", "") or "").strip()
            generated_lot = str(item.get("lotto", "") or "").strip()
            generated_tmc = str(item.get("tmc", "") or "").strip()

            if not generated_reason or not (generated_lot or generated_tmc):
                for key in (
                    "criterioMatch",
                    "stato",
                    "metodoEstrazione",
                    "note",
                ):
                    if key in latest_item:
                        item[key] = latest_item[key]

    # Preserva soltanto richiami RSS arrivati mentre il run completo
    # era già in corso, così non vengono persi per una race condition.
    carry = []

    for item in latest_recalls:
        rid = str(item.get("id", "") or "").strip()
        method = str(
            item.get("metodoEstrazione", "") or ""
        ).strip().upper()

        if rid and rid not in generated_ids and method == "RSS":
            carry.append(item)
            generated_ids.add(rid)

    if carry:
        generated_recalls = carry + generated_recalls
        print(
            "Richiami RSS arrivati durante il run preservati:",
            len(carry),
        )

    generated["recalls"] = generated_recalls
    generated["totale"] = len(generated_recalls)
    generated["pass"] = sum(
        1
        for item in generated_recalls
        if str(item.get("stato", "") or "").strip().upper() == "PASS"
    )
    generated["daVerificare"] = (
        generated["totale"] - generated["pass"]
    )

    latest_path.write_text(
        json.dumps(
            generated,
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    print(
        "Merge completato:",
        generated["totale"],
        "richiami,",
        generated["pass"],
        "PASS,",
        generated["daVerificare"],
        "da verificare",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
