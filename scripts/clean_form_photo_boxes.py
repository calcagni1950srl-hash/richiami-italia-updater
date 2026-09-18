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
WORK_DIR = ROOT / 'form-photo-clean'
IMAGES_DIR = Path('images')
RECALLS_FILE = Path('recalls.json')
WORK_DIR.mkdir(parents=True, exist_ok=True)


def run(cmd):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, errors='ignore')


def ocr_words(path):
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
        out.append((text,left,top,width,height))
    return out


def remove_long(mask):
    h,w = mask.shape
    horizontal = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT,(max(50,w//15),1)))
    vertical = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT,(1,max(50,h//18))))
    lines = cv2.bitwise_or(horizontal, vertical)
    return cv2.bitwise_and(mask, cv2.bitwise_not(lines))


def image_stats(image):
    arr = np.asarray(image.convert('RGB'))
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
    return {
        'white': float(np.mean(np.all(arr >= 244, axis=2))),
        'color': float(np.mean(hsv[:,:,1] >= 20)),
        'contrast': float(gray.std()),
        'edge': float(np.mean(cv2.Canny(gray,45,135) > 0)),
    }


def plausible(image):
    w,h = image.size
    if w < 120 or h < 90:
        return False
    ratio = max(w,h) / max(1,min(w,h))
    if ratio > 5.0:
        return False
    s = image_stats(image)
    if s['white'] > 0.93 or s['contrast'] < 7:
        return False
    return True


def caption_candidates(page_path):
    pil = Image.open(page_path).convert('RGB')
    arr = np.asarray(pil)
    h,w = arr.shape[:2]
    words = ocr_words(page_path)
    captions = [(l,t,ww,hh) for txt,l,t,ww,hh in words if 'inser' in txt.lower()]
    candidates = []

    for l,t,ww,hh in captions[:4]:
        left = (l + ww/2) < w/2
        sx1 = int(.04*w) if left else int(.51*w)
        sx2 = int(.49*w) if left else int(.96*w)
        y2 = max(int(.65*h), t - 3)
        y1 = max(int(.55*h), y2 - int(.31*h))
        if y2-y1 < 100:
            continue

        roi = arr[y1:y2, sx1:sx2]
        rg = cv2.cvtColor(roi, cv2.COLOR_RGB2GRAY)
        rh = cv2.cvtColor(roi, cv2.COLOR_RGB2HSV)
        text_mask = np.zeros(rg.shape, np.uint8)
        for txt2,l2,t2,w2,h2 in words:
            if l2+w2 < sx1 or l2 > sx2 or t2+h2 < y1 or t2 > y2:
                continue
            x1=max(0,l2-sx1-5); yy1=max(0,t2-y1-4)
            x2=min(sx2-sx1,l2+w2-sx1+5); yy2=min(y2-y1,t2+h2-y1+4)
            cv2.rectangle(text_mask,(x1,yy1),(x2,yy2),255,-1)

        nonwhite = ((rg < 246).astype(np.uint8) * 255)
        nonwhite = remove_long(nonwhite)
        colorful = ((rh[:,:,1] >= 10).astype(np.uint8) * 255)
        edges = cv2.Canny(rg,40,130)
        visual = cv2.bitwise_or(nonwhite,colorful)
        visual = cv2.bitwise_or(visual,edges)
        visual[text_mask > 0] = 0
        visual = cv2.morphologyEx(visual,cv2.MORPH_OPEN,
            cv2.getStructuringElement(cv2.MORPH_RECT,(3,3)),iterations=1)
        visual = cv2.morphologyEx(visual,cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_RECT,(15,15)),iterations=2)
        visual = cv2.dilate(visual,cv2.getStructuringElement(cv2.MORPH_RECT,(5,5)),iterations=1)

        contours,_ = cv2.findContours(visual,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        region_area = (sx2-sx1)*(y2-y1)
        for contour in contours:
            x,y,bw,bh = cv2.boundingRect(contour)
            area = bw*bh
            if bw < 90 or bh < 70 or area < region_area*.025:
                continue
            if max(bw/bh,bh/bw) > 4.8:
                continue
            px=max(4,int(bw*.018)); py=max(4,int(bh*.018))
            X1=max(sx1,sx1+x-px); Y1=max(y1,y1+y-py)
            X2=min(sx2,sx1+x+bw+px); Y2=min(y2,y1+y+bh+py)
            crop = pil.crop((X1,Y1,X2,Y2)).convert('RGB')
            if not plausible(crop):
                continue
            s=image_stats(crop)
            score = math.log1p(area) + s['color']*3 + s['edge']*5 - s['white']*.6
            candidates.append((score,crop))

    return sorted(candidates, reverse=True, key=lambda x:x[0])


def slot_candidates(page_path):
    pil = Image.open(page_path).convert('RGB')
    arr = np.asarray(pil)
    h,w = arr.shape[:2]
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
    words = ocr_words(page_path)
    text = np.zeros((h,w),np.uint8)
    for _,l,t,ww,hh in words:
        cv2.rectangle(text,(max(0,l-6),max(0,t-4)),(min(w,l+ww+6),min(h,t+hh+4)),255,-1)

    nonwhite = ((gray < 246).astype(np.uint8)*255)
    nonwhite = remove_long(nonwhite)
    colorful = ((hsv[:,:,1] >= 12).astype(np.uint8)*255)
    edges = cv2.Canny(gray,45,135)
    visual = cv2.bitwise_or(nonwhite,colorful)
    visual = cv2.bitwise_or(visual,edges)
    visual[text > 0] = 0
    visual = cv2.morphologyEx(visual,cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT,(3,3)),iterations=1)
    visual = cv2.morphologyEx(visual,cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT,(21,21)),iterations=2)
    visual = cv2.dilate(visual,cv2.getStructuringElement(cv2.MORPH_RECT,(7,7)),iterations=1)

    slots = [
        (int(.035*w),int(.61*h),int(.50*w),int(.98*h)),
        (int(.50*w),int(.61*h),int(.965*w),int(.98*h)),
    ]
    candidates=[]
    for sx1,sy1,sx2,sy2 in slots:
        roi=visual[sy1:sy2,sx1:sx2]
        contours,_=cv2.findContours(roi,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        slot_area=(sx2-sx1)*(sy2-sy1)
        for contour in contours:
            x,y,bw,bh=cv2.boundingRect(contour)
            area=bw*bh
            if bw<90 or bh<70 or area<slot_area*.025:
                continue
            if max(bw/bh,bh/bw)>4.8:
                continue
            px=max(4,int(bw*.025)); py=max(4,int(bh*.025))
            X1=max(sx1,sx1+x-px); Y1=max(sy1,sy1+y-py)
            X2=min(sx2,sx1+x+bw+px); Y2=min(sy2,sy1+y+bh+py)
            crop=pil.crop((X1,Y1,X2,Y2)).convert('RGB')
            if not plausible(crop):
                continue
            s=image_stats(crop)
            score=math.log1p(area)+s['color']*3+s['edge']*4-s['white']*.5
            candidates.append((score,crop))
    return sorted(candidates,reverse=True,key=lambda x:x[0])


def clean_margins(image):
    img=image.convert('RGB')
    s=image_stats(img)
    w,h=img.size
    if s['white'] > .45 and s['color'] < .15:
        return img.crop((int(w*.04),int(h*.20),int(w*.96),int(h*.90)))
    if s['white'] > .45:
        return img.crop((int(w*.01),int(h*.04),int(w*.99),int(h*.87)))
    return img.crop((int(w*.01),int(h*.03),int(w*.99),int(h*.99)))


def render_first_page(pdf, recall_id):
    folder=WORK_DIR/recall_id
    folder.mkdir(parents=True,exist_ok=True)
    path=folder/'page.png'
    res=run(['pdftoppm','-f','1','-singlefile','-r','200','-png',str(pdf),str(folder/'page')])
    return path if res.returncode==0 and path.exists() else None


def versioned(recall_id,image):
    buf=io.BytesIO(); image.save(buf,format='PNG',optimize=True)
    data=buf.getvalue(); dig=hashlib.sha256(data).hexdigest()[:10]
    return f'{recall_id}-{dig}.png',data


with RECALLS_FILE.open('r',encoding='utf-8') as f:
    database=json.load(f)
recalls=database if isinstance(database,list) else database.get('recalls',[])
changed=False

for recall in recalls:
    rid=str(recall.get('id','') or '').strip()
    if not rid:
        continue
    pdf=PDF_DIR/f'{rid}.pdf'
    if not pdf.exists():
        continue
    page=render_first_page(pdf,rid)
    if page is None:
        continue

    candidates=caption_candidates(page)
    if not candidates:
        candidates=slot_candidates(page)
    if not candidates:
        continue

    crop=clean_margins(candidates[0][1])
    if not plausible(crop):
        continue

    filename,data=versioned(rid,crop)
    (IMAGES_DIR/filename).write_bytes(data)
    key='immagine' if 'immagine' in recall or 'imageUrl' not in recall else 'imageUrl'
    new_url=('https://raw.githubusercontent.com/calcagni1950srl-hash/'
             'richiami-italia-updater/refs/heads/main/images/'+filename)
    if str(recall.get(key,'') or '') != new_url:
        recall[key]=new_url
        changed=True

    print('✅ Foto modulo ripulita:',rid,crop.size)

if changed:
    RECALLS_FILE.write_text(json.dumps(database,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
