// Selezione pura dei richiami da elaborare: nessun accesso a rete o Firebase.
// Gli ID sono gli ultimi segmenti della URL ufficiale, non il nome del prodotto.
export function recallId(urlString) {
  try {
    const url = new URL(String(urlString || ""));
    const slug = (url.pathname.split("/").filter(Boolean).at(-1) || "")
      .toLowerCase().replace(/[^a-z0-9_-]/g, "-").replace(/-+/g, "-")
      .replace(/^-|-$/g, "");
    return slug;
  } catch {
    return "";
  }
}

export function selectPending(items, previousData, maxItems=50) {
  if (!Array.isArray(items) || items.length === 0) {
    throw new Error("Feed ufficiale senza richiami: nessuna sostituzione del database");
  }
  if (!Number.isInteger(maxItems) || maxItems < 1) throw new Error("Limite RSS non valido");
  const unique = [];
  const seen = new Set();
  for (const item of items) {
    const id = recallId(item?.link);
    if (!id || seen.has(id)) continue;
    if (!String(item.link).includes("/ext-avviso-sicurezza-alimentare/")) continue;
    seen.add(id);
    unique.push({...item, id});
    if (unique.length >= maxItems) break;
  }
  if (!unique.length) throw new Error("Nessun avviso alimentare valido nell'RSS");

  const previous = new Map(
    (Array.isArray(previousData?.recalls) ? previousData.recalls : [])
      .filter(x => x && x.id)
      .map(x => [String(x.id), x])
  );

  const pending = unique.filter(item => {
    const old = previous.get(item.id);
    if (!old) return true;
    const method = String(old.metodoEstrazione || "").toUpperCase();
    if (method === "RSS" || String(old.criterioMatch || "").toUpperCase() === "RSS_MINISTERO") return true;
    // Richiami effettivamente incompleti, senza PDF ufficiale, possono essere ritentati.
    return !String(old.pdfMinistero || "").trim();
  });

  return {feed:unique, pending, reused:unique.length-pending.length};
}

function ensure(x, msg) { if (!x) throw new Error("SELF-TEST: " + msg); }
if (process.argv.includes("--self-test")) {
  const items = [
    {link:"https://www.salute.gov.it/new/it/ext-avviso-sicurezza-alimentare/nuovo"},
    {link:"https://www.salute.gov.it/new/it/ext-avviso-sicurezza-alimentare/vecchio"},
    {link:"https://www.salute.gov.it/new/it/ext-avviso-sicurezza-alimentare/vecchio"},
    {link:"https://www.salute.gov.it/new/it/ext-avviso-sicurezza-alimentare/provvisorio"},
  ];
  const old={recalls:[
    {id:"vecchio",pdfMinistero:"https://salute.gov.it/old.pdf",stato:"PASS",metodoEstrazione:"TESTO_DIGITALE"},
    {id:"provvisorio",pdfMinistero:"",metodoEstrazione:"RSS"}
  ]};
  const one=selectPending(items,old);
  ensure(one.feed.length===3, "ordine/duplicati RSS");
  ensure(one.pending.map(x=>x.id).join(",")==="nuovo,provvisorio", "seleziona soltanto nuovo e incompleto");
  ensure(one.reused===1, "conteggio riutilizzati");
  const done=selectPending(items,{recalls:[...old.recalls,{id:"nuovo",pdfMinistero:"https://salute.gov.it/new.pdf",metodoEstrazione:"TESTO_DIGITALE"}]});
  ensure(done.pending.length===1 && done.pending[0].id==="provvisorio", "nessuna ripetizione di notifiche");
  let rejected=false;try {selectPending([],old)} catch {rejected=true}
  ensure(rejected,"non sovrascrivere con RSS vuoto");
  console.log("PASS selectPending: deduplica RSS, prioritizza nuovi, preserva completi e feed vuoto");
}
