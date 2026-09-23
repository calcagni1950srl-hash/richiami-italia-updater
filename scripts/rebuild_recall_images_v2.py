import csv
import hashlib
import io
import json
import math
import shutil
import subprocess
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

ROOT = Path('.quality')
PDF_DIR = ROOT / 'pdf'
WORK_DIR = ROOT / 'work-v2'
IMAGES_DIR = Path('images')
RECALLS_FILE = Path('recalls.json')

PDF_DIR.mkdir(parents=True, exist_ok=True)
WORK_DIR.mkdir(parents=True, exist_ok=True)
IMAGES_DIR.mkdir(parents=True, exist_ok=True)

STOPWORDS = {
    'della','delle','degli','dello','dalla','dalle','dagli','dallo','dell',
    'con','senza','sottovuoto','prodotto','richiamo','alimentare','lotto',
    'peso','netto','kg','gr','spa','srl','italia','italiano','italiana'
}


def run(cmd):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, errors='ignore')


def normalize_tokens(value):
    cleaned = ''.join(ch.lower() if ch.isalnum() else ' ' for ch in str(value or ''))
    return {t for t in cleaned.split() if len(t) >= 4 and t not in STOPWORDS}


def recall_tokens(recall):
    out = set()
    for key in ('marca','prodotto','brand','productName'):
        out |= normalize_tokens(recall.get(key, ''))
    return out


def image_key(recall):
    if 'immagine' in recall:
        return 'immagine'
    if 'imageUrl' in recall:
        return 'imageUrl'
    return 'immagine'


def image_stats(image):
    rgb = image.convert('RGB')
    sample = rgb.copy()
    sample.thumbnail((700, 700), Image.Resampling.LANCZOS)
    arr = np.asarray(sample)
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
    edges = cv2.Canny(gray, 55, 145)
    black = np.all(arr <= 18, axis=2)
    white = np.all(arr >= 244, axis=2)
    row_mean = gray.mean(axis=1)
    row_std = gray.std(axis=1)
    col_mean = gray.mean(axis=0)
    col_std = gray.std(axis=0)
    band_rows = float(np.mean((row_mean < 28) & (row_std < 8)))
    band_cols = float(np.mean((col_mean < 28) & (col_std < 8)))
    return {
        'width': rgb.width,
        'height': rgb.height,
        'area': rgb.width * rgb.height,
        'white_ratio': float(np.mean(white)),
        'black_ratio': float(np.mean(black)),
        'color_ratio': float(np.mean(hsv[:, :, 1] >= 20)),
        'contrast': float(gray.std()),
        'edge_ratio': float(np.mean(edges > 0)),
        'banding': max(band_rows, band_cols),
    }


def normalized_digest(image):
    thumb = image.convert('RGB').copy()
    thumb.thumbnail((96, 96), Image.Resampling.LANCZOS)
    canvas = Image.new('RGB', (96, 96), 'white')
    canvas.paste(thumb, ((96-thumb.width)//2, (96-thumb.height)//2))
    return hashlib.sha256(canvas.tobytes()).hexdigest()


def ocr_layout(path):
    res = run(['tesseract', str(path), 'stdout', '-l', 'ita', '--psm', '11', 'tsv'])
    if not res.stdout.strip():
        return []
    out = []
    reader = csv.DictReader(io.StringIO(res.stdout), delimiter='\t')
    for row in reader:
        text = str(row.get('text','') or '').strip()
        if not text:
            continue
        try:
            conf = float(row.get('conf','-1') or -1)
            left = int(row.get('left','0') or 0)
            top = int(row.get('top','0') or 0)
            width = int(row.get('width','0') or 0)
            height = int(row.get('height','0') or 0)
        except Exception:
            continue
        if conf < 15 or width <= 0 or height <= 0:
            continue
        out.append({'text':text,'left':left,'top':top,'width':width,'height':height})
    return out


def overlap_score(text, wanted):
    found = normalize_tokens(text)
    if not wanted or not found:
        return 0.0
    return len(found & wanted) / max(1, len(wanted))


def word_mask(shape, words, px=7, py=5):
    h, w = shape[:2]
    mask = np.zeros((h,w), np.uint8)
    for x in words:
        x1=max(0,x['left']-px); y1=max(0,x['top']-py)
        x2=min(w,x['left']+x['width']+px); y2=min(h,x['top']+x['height']+py)
        cv2.rectangle(mask,(x1,y1),(x2,y2),255,-1)
    return mask


def remove_long_lines(mask):
    h,w = mask.shape
    horizontal = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT,(max(50,w//15),1)))
    vertical = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT,(1,max(50,h//18))))
    lines = cv2.bitwise_or(horizontal, vertical)
    return cv2.bitwise_and(mask, cv2.bitwise_not(lines))


def expand_box(box, w, h, frac=0.075):
    x1,y1,x2,y2 = box
    bw=max(1,x2-x1); bh=max(1,y2-y1)
    px=max(6,int(bw*frac)); py=max(6,int(bh*frac))
    return (max(0,x1-px), max(0,y1-py), min(w,x2+px), min(h,y2+py))


def primary_visual_crop(image):
    rgb = np.asarray(image.convert('RGB'))
    h,w = rgb.shape[:2]
    if h < 100 or w < 100:
        return image.convert('RGB')
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    edges = cv2.Canny(gray,45,135)
    mask = (((hsv[:,:,1] >= 16) | (gray < 225) | (edges > 0)).astype(np.uint8) * 255)
    mask = remove_long_lines(mask)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT,(11,11)), iterations=2)
    mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_RECT,(7,7)), iterations=1)
    contours,_ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes=[]
    total=w*h
    for c in contours:
        x,y,bw,bh=cv2.boundingRect(c)
        area=bw*bh
        if bw < 55 or bh < 45 or area < total*0.012:
            continue
        region=rgb[y:y+bh,x:x+bw]
        reg_hsv=cv2.cvtColor(region, cv2.COLOR_RGB2HSV)
        reg_gray=cv2.cvtColor(region, cv2.COLOR_RGB2GRAY)
        color=float(np.mean(reg_hsv[:,:,1] >= 16))
        edge=float(np.mean(cv2.Canny(reg_gray,45,135)>0))
        score=area*(0.55 + 0.9*color + min(edge*5,0.8))
        boxes.append((score,(x,y,x+bw,y+bh)))
    if not boxes:
        return image.convert('RGB')
    boxes.sort(reverse=True, key=lambda t:t[0])
    best=boxes[0][1]

    # Se l'area visiva arriva già vicino a un bordo dell'immagine,
    # non stringiamo ulteriormente: potrebbe essere il prodotto intero
    # che tocca il margine e un crop aggressivo lo troncherebbe.
    bx1,by1,bx2,by2=best
    guard_x=max(8,int(w*0.035)); guard_y=max(8,int(h*0.035))
    if bx1 <= guard_x or by1 <= guard_y or bx2 >= w-guard_x or by2 >= h-guard_y:
        return image.convert('RGB')

    x1,y1,x2,y2=expand_box(best,w,h,0.085)
    crop=image.convert('RGB').crop((x1,y1,x2,y2))
    if crop.width < 120 or crop.height < 90 or crop.width*crop.height < total*0.08:
        return image.convert('RGB')
    return crop


def plausible_photo(image, relaxed=False):
    s=image_stats(image)
    ratio=max(s['width'],s['height'])/max(1,min(s['width'],s['height']))
    if s['width'] < (100 if relaxed else 130) or s['height'] < (80 if relaxed else 100):
        return False
    if s['area'] < (16000 if relaxed else 26000):
        return False
    if ratio > (5.2 if relaxed else 4.4):
        return False
    if s['contrast'] < 5.0:
        return False
    if s['banding'] > 0.24:
        return False
    if s['white_ratio'] > (0.91 if relaxed else 0.82):
        return False
    if s['black_ratio'] > 0.58 and s['color_ratio'] < 0.08 and s['banding'] > 0.10:
        return False
    return True


def save_temp_image(image, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    image.convert('RGB').save(path)
    return path


def embedded_candidates(pdf, recall_id, wanted):
    folder=WORK_DIR/recall_id/'embedded'
    if folder.exists(): shutil.rmtree(folder)
    folder.mkdir(parents=True,exist_ok=True)
    prefix=folder/'img'
    res=run(['pdfimages','-all',str(pdf),str(prefix)])
    if res.returncode != 0:
        return []
    candidates=[]
    for path in sorted(folder.glob('img-*.*')):
        try:
            image=Image.open(path).convert('RGB')
        except Exception:
            continue
        if not plausible_photo(image, relaxed=False):
            continue
        tightened=primary_visual_crop(image)
        if plausible_photo(tightened, relaxed=True):
            image=tightened
        s=image_stats(image)
        tmp=save_temp_image(image, folder/(path.stem+'-ocr.png'))
        words=ocr_layout(tmp)
        text=' '.join(x['text'] for x in words)
        rel=overlap_score(text,wanted)
        score=(
            min(math.log10(max(s['area'],1))/6.0,1.1)*1.8
            + s['color_ratio']*1.3
            + min(s['contrast']/70.0,1.0)
            + min(s['edge_ratio']*6.0,0.9)
            - s['white_ratio']*0.75
            - s['banding']*2.0
            + rel*1.5
        )
        candidates.append({'image':image,'score':score,'stats':s,'source':'embedded','relevance':rel})
    out=[]; seen=set()
    for item in sorted(candidates,key=lambda x:x['score'],reverse=True):
        d=normalized_digest(item['image'])
        if d in seen: continue
        seen.add(d); out.append(item)
    return out


def render_pages(pdf, recall_id):
    folder=WORK_DIR/recall_id/'pages'
    if folder.exists(): shutil.rmtree(folder)
    folder.mkdir(parents=True,exist_ok=True)
    res=run(['pdftoppm','-f','1','-l','3','-r','200','-png',str(pdf),str(folder/'page')])
    if res.returncode != 0: return []
    return sorted(folder.glob('page-*.png'))


def page_candidates(page_path, wanted, relaxed=False):
    pil=Image.open(page_path).convert('RGB')
    rgb=np.asarray(pil)
    h,w=rgb.shape[:2]; page_area=w*h
    gray=cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY)
    hsv=cv2.cvtColor(rgb,cv2.COLOR_RGB2HSV)
    words=ocr_layout(page_path)
    text=word_mask(rgb.shape,words,7,5)
    nonwhite=((gray<247).astype(np.uint8)*255)
    nonwhite=remove_long_lines(nonwhite)
    nonwhite[text>0]=0
    colorful=((hsv[:,:,1] >= (12 if relaxed else 17)).astype(np.uint8)*255)
    colorful[text>0]=0
    edges=cv2.Canny(gray,45,135); edges[text>0]=0
    visual=cv2.bitwise_or(nonwhite,colorful)
    visual=cv2.bitwise_or(visual,edges)
    visual=cv2.morphologyEx(visual,cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT,(27 if relaxed else 17,27 if relaxed else 17)),
        iterations=2 if relaxed else 1)
    visual=cv2.dilate(visual,cv2.getStructuringElement(cv2.MORPH_RECT,(9,9)),1)
    contours,_=cv2.findContours(visual,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    results=[]
    for c in contours:
        x,y,bw,bh=cv2.boundingRect(c); area=bw*bh
        if bw < (100 if relaxed else 130) or bh < (80 if relaxed else 100): continue
        if area < page_area*(0.0035 if relaxed else 0.006): continue
        if area > page_area*(0.55 if relaxed else 0.45): continue
        ratio=max(bw/bh,bh/bw)
        if ratio > (5.0 if relaxed else 4.3): continue
        x1,y1,x2,y2=expand_box((x,y,x+bw,y+bh),w,h,0.085)
        crop=pil.crop((x1,y1,x2,y2)).convert('RGB')
        crop=primary_visual_crop(crop)
        if not plausible_photo(crop, relaxed=True): continue
        s=image_stats(crop)
        local=[]
        for wd in words:
            cx=wd['left']+wd['width']/2; cy=wd['top']+wd['height']/2
            if x1<=cx<=x2 and y1<=cy<=y2: local.append(wd['text'])
        rel=overlap_score(' '.join(local),wanted)
        visual_occ=float(np.mean(visual[y1:y2,x1:x2]>0)) if x2>x1 and y2>y1 else 0
        text_occ=float(np.mean(text[y1:y2,x1:x2]>0)) if x2>x1 and y2>y1 else 0
        if s['white_ratio'] > 0.78 and visual_occ < 0.12: continue
        if text_occ > 0.20 and s['white_ratio'] > 0.50: continue
        score=(
            visual_occ*3.0 + s['color_ratio']*1.5 + min(s['edge_ratio']*6.0,1.0)
            + min(s['contrast']/75.0,1.0) - s['white_ratio']*0.65
            - text_occ*1.5 + rel*1.5 + min(math.sqrt(s['area']/page_area),0.7)
        )
        results.append({'image':crop,'score':score,'stats':s,'source':'page-relaxed' if relaxed else 'page','relevance':rel})
    return results


def sliding_window_salvage(page_path, wanted):
    pil=Image.open(page_path).convert('RGB')
    rgb=np.asarray(pil); h,w=rgb.shape[:2]
    gray=cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY)
    hsv=cv2.cvtColor(rgb,cv2.COLOR_RGB2HSV)
    words=ocr_layout(page_path)
    text=word_mask(rgb.shape,words,5,4)
    edges=cv2.Canny(gray,45,135)
    candidates=[]
    shapes=[(0.50,0.42),(0.42,0.34),(0.34,0.30),(0.30,0.24)]
    for wf,hf in shapes:
        ww=max(180,int(w*wf)); hh=max(150,int(h*hf))
        sx=max(90,int(ww*0.35)); sy=max(90,int(hh*0.35))
        for y in range(0,max(1,h-hh+1),sy):
            for x in range(0,max(1,w-ww+1),sx):
                reg=np.s_[y:y+hh,x:x+ww]
                white=float(np.mean(gray[reg]>=247))
                text_r=float(np.mean(text[reg]>0))
                color=float(np.mean(hsv[reg][...,1]>=14))
                edge=float(np.mean(edges[reg]>0))
                contrast=float(gray[reg].std())
                if white>0.90 or text_r>0.13 or contrast<7: continue
                visual=color*1.8+edge*5.0+min(contrast/70,1.0)+(1-white)*0.25-text_r*2.8
                if visual<0.25: continue
                crop=pil.crop((x,y,x+ww,y+hh)).convert('RGB')
                crop=primary_visual_crop(crop)
                if not plausible_photo(crop, relaxed=True): continue
                local=[]
                for wd in words:
                    cx=wd['left']+wd['width']/2; cy=wd['top']+wd['height']/2
                    if x<=cx<=x+ww and y<=cy<=y+hh: local.append(wd['text'])
                rel=overlap_score(' '.join(local),wanted)
                s=image_stats(crop)
                score=visual + rel*1.5 - s['white_ratio']*0.35
                candidates.append({'image':crop,'score':score,'stats':s,'source':'salvage','relevance':rel})
    return candidates


def extract_best(pdf, recall_id, wanted):
    direct=embedded_candidates(pdf,recall_id,wanted)
    if direct:
        return direct[0]['image'],'foto incorporata',direct[0]
    pages=render_pages(pdf,recall_id)
    all_candidates=[]
    for p in pages:
        all_candidates.extend(page_candidates(p,wanted,relaxed=False))
    if not all_candidates:
        for p in pages:
            all_candidates.extend(page_candidates(p,wanted,relaxed=True))
    if not all_candidates:
        for p in pages:
            all_candidates.extend(sliding_window_salvage(p,wanted))
    if not all_candidates:
        return None,'',None
    unique=[]; seen=set()
    for item in sorted(all_candidates,key=lambda x:x['score'],reverse=True):
        d=normalized_digest(item['image'])
        if d in seen: continue
        seen.add(d); unique.append(item)
    best=unique[0]
    return best['image'],'foto recuperata dalla pagina',best


def prepare_output(image):
    # Il candidato è già stato selezionato e ritagliato nella fase di
    # estrazione. Non applichiamo un secondo crop sul contenuto: sui
    # prodotti lunghi poteva eliminare una delle estremità.
    out = image.convert('RGB')
    if max(out.size) > 1800:
        out.thumbnail((1800, 1800), Image.Resampling.LANCZOS)
    return out


def versioned_filename(recall_id,image):
    buf=io.BytesIO(); image.save(buf,format='PNG',optimize=True)
    data=buf.getvalue(); digest=hashlib.sha256(data).hexdigest()[:10]
    return f'{recall_id}-{digest}.png',data


def remove_note(notes,text):
    while text in notes: notes.remove(text)

with RECALLS_FILE.open('r',encoding='utf-8') as f:
    database=json.load(f)
recalls=database if isinstance(database,list) else database.get('recalls',[])
accepted=set(); changed=False; found=0; missing=[]

for recall in recalls:
    rid=str(recall.get('id','') or '').strip()
    if not rid: continue
    pdf=PDF_DIR/f'{rid}.pdf'
    key=image_key(recall); old=str(recall.get(key,'') or '')
    notes=recall.setdefault('note',[]) if isinstance(recall.get('note',[]),list) else []
    if not isinstance(recall.get('note'),list): recall['note']=notes
    for marker in ['Immagine scartata dal controllo qualità','Immagine prodotto non estratta',
                   'Immagine non disponibile nel PDF ufficiale','Foto prodotto non individuata nel PDF ufficiale']:
        remove_note(notes,marker)
    image=label=meta=None
    if pdf.exists():
        image,label,meta=extract_best(pdf,rid,recall_tokens(recall))
    if image is None:
        old_filename=''
        if old and '/images/' in old:
            old_filename=old.rsplit('/',1)[-1].split('?',1)[0].strip()

        if old_filename and (IMAGES_DIR/old_filename).exists():
            accepted.add(old_filename)
            found+=1
            print('✅ Foto esistente preservata:',rid,'->',old_filename)
            continue

        if old:
            recall[key]=''; changed=True
        marker='Foto prodotto non individuata nel PDF ufficiale'
        if marker not in notes: notes.append(marker); changed=True
        missing.append(rid)
        print('⚠️ Nessuna foto dopo 3 tentativi:',rid)
        continue
    out=prepare_output(image)
    if not plausible_photo(out,relaxed=True):
        old_filename=''
        if old and '/images/' in old:
            old_filename=old.rsplit('/',1)[-1].split('?',1)[0].strip()

        if old_filename and (IMAGES_DIR/old_filename).exists():
            accepted.add(old_filename)
            found+=1
            print('✅ Foto esistente preservata dopo candidato non plausibile:',rid)
            continue

        if old:
            recall[key]=''; changed=True
        marker='Foto prodotto non individuata nel PDF ufficiale'
        if marker not in notes: notes.append(marker); changed=True
        missing.append(rid)
        print('⚠️ Foto finale non plausibile:',rid)
        continue
    filename,data=versioned_filename(rid,out)
    (IMAGES_DIR/filename).write_bytes(data); accepted.add(filename); found+=1
    url=('https://raw.githubusercontent.com/calcagni1950srl-hash/'
         'richiami-italia-updater/refs/heads/main/images/'+filename)
    if old!=url: recall[key]=url; changed=True
    s=image_stats(out)
    print(f"✅ {rid}: {label} {out.width}x{out.height} white={s['white_ratio']:.2f} "
          f"color={s['color_ratio']:.2f} band={s['banding']:.2f} score={(meta or {}).get('score',0):.2f}")

referenced=set(accepted)
for recall in recalls:
    key=image_key(recall)
    url=str(recall.get(key,'') or '').strip()
    if url and '/images/' in url:
        name=url.rsplit('/',1)[-1].split('?',1)[0].strip()
        if name:
            referenced.add(name)

for p in IMAGES_DIR.glob('*.png'):
    if p.name not in referenced:
        p.unlink(missing_ok=True); changed=True

if changed:
    RECALLS_FILE.write_text(json.dumps(database,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

print(f'✅ Copertura immagini: {found}/{len(recalls)}')
if missing:
    print('⚠️ Senza foto:', ', '.join(missing))
