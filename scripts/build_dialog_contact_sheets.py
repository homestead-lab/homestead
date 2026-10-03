"""Build paired desktop/mobile cards, contact sheets and a local review gallery.

Requires Pillow. First run audit_dialogs.mjs with DIALOG_OUTPUT pointing to
<root>/dark and <root>/light and HOMESTEAD_AUDIT_THEME set accordingly.
"""
import argparse
import html
import json
import math
import zipfile
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

parser = argparse.ArgumentParser()
parser.add_argument('root', type=Path)
parser.add_argument('--kind', choices=('dialogs','pages'), default='dialogs')
args = parser.parse_args()
root = args.root
kind_label = 'page' if args.kind == 'pages' else 'dialog'
font_root = Path('C:/Windows/Fonts')
def font(size, bold=False):
    paths = [font_root/('segoeuib.ttf' if bold else 'segoeui.ttf'), Path('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')]
    return ImageFont.truetype(str(next(p for p in paths if p.exists())), size)

reports = {theme:json.loads((root/theme/'report.json').read_text(encoding='utf-8')) for theme in ('dark','light')}
names = sorted({r['name'] for r in reports['dark']})
lookup = {(theme,r['name'],r['label']):r for theme,rows in reports.items() for r in rows}
for theme in reports:
    assert {r['name'] for r in reports[theme]} == set(names), 'Theme inventories differ'
    for name in names:
        for device in ('desktop','mobile'):
            row = lookup[theme,name,device]
            assert not row['overflow'] and not row['tiny'], f'Layout check failed: {name} {theme} {device}'
            assert (root/theme/f'{name}-{device}.png').exists()

def paste_fit(canvas, path, box):
    with Image.open(path) as source:
        pic=source.convert('RGB')
        pic.thumbnail((box[2],box[3]),Image.Resampling.LANCZOS)
        canvas.paste(pic,(box[0]+(box[2]-pic.width)//2,box[1]))

for theme in reports:
    folder=root/'cards'/theme;folder.mkdir(parents=True,exist_ok=True)
    bg='#101114' if theme=='dark' else '#eceef2'
    fg='#f4f5f7' if theme=='dark' else '#17191e'
    muted='#a9b0bb' if theme=='dark' else '#555e6c'
    for name in names:
        images=[root/theme/f'{name}-{d}.png' for d in ('desktop','mobile')]
        sizes=[]
        for image_path,width in zip(images,(1560,680)):
            with Image.open(image_path) as im:sizes.append(min(2200,round(im.height*width/im.width)))
        height=max(sizes)+180
        card=Image.new('RGB',(2400,height),bg);draw=ImageDraw.Draw(card)
        title=name.replace('-', ' ').title() if args.kind == 'pages' else lookup[theme,name,'desktop']['title']
        draw.text((40,24),title[:85],font=font(38,True),fill=fg)
        draw.text((40,76),name+'  |  '+theme.title(),font=font(24),fill=muted)
        draw.text((40,119),'DESKTOP · 1440 px viewport',font=font(22,True),fill=muted)
        draw.text((1660,119),'PHONE · 390 px viewport',font=font(22,True),fill=muted)
        paste_fit(card,images[0],(40,160,1560,2200));paste_fit(card,images[1],(1660,160,680,2200))
        card.save(folder/f'{name}.png',compress_level=5)

def sheet(theme, subset, path, heading, page_label):
    cols=3;rows=math.ceil(len(subset)/cols);tilew=1000;tileh=800
    canvas=Image.new('RGB',(cols*tilew,160+rows*tileh),'#e5e8ee');draw=ImageDraw.Draw(canvas)
    draw.text((35,22),heading,font=font(43,True),fill='#172033')
    draw.text((35,83),page_label+' · desktop and phone pairs · open the gallery for full-size cards',font=font(25),fill='#4b576c')
    for i,name in enumerate(subset):
        x=(i%cols)*tilew;y=160+(i//cols)*tileh
        draw.rounded_rectangle((x+16,y+12,x+tilew-16,y+tileh-12),radius=18,fill='#ffffff')
        label=f'{names.index(name)+1:03d}  {name}'
        draw.text((x+34,y+29),label[:65],font=font(23,True),fill='#172033')
        paste_fit(canvas,root/'cards'/theme/f'{name}.png',(x+32,y+77,tilew-64,tileh-105))
    canvas.save(path,compress_level=5)

sheets=[]
for theme in reports:
    for page,offset in enumerate(range(0,len(names),12),1):
        filename=f'contact-{theme}-{page:02}.png';sheets.append(filename)
        sheet(theme,names[offset:offset+12],root/filename,f'Homestead {kind_label} contact sheets',f'{theme.title()} · page {page} of {math.ceil(len(names)/12)}')
sample=[n for n in ('workload-edit','vm-edit','image-update-review','jobs-completed-data-moves','import-copy-review','add-harvester-host') if n in names]
if args.kind == 'pages': sample=[n for n in ('containers','vms','node-page','settings-updates','settings-you','setup') if n in names]
sheet('dark',sample,root/'sample-overview.png',f'Homestead · shared {kind_label} components','Selected samples')

cards=[]
for i,name in enumerate(names,1):
    title=name.replace('-', ' ').title() if args.kind == 'pages' else lookup['dark',name,'desktop']['title']; safe=html.escape(name);label=html.escape(title)
    cards.append(f'''<article data-search="{safe} {label.lower()}"><header><span>{i:03}</span><h2>{label}</h2></header><p>{safe}</p>
    <a class="paired" href="cards/dark/{safe}.png" target="_blank"><img loading="lazy" src="cards/dark/{safe}.png" alt="{label}: desktop and phone" width="1200" height="900"></a>
    <footer><a class="desktop" href="dark/{safe}-desktop.png" target="_blank">Full desktop</a><a class="mobile" href="dark/{safe}-mobile.png" target="_blank">Full phone</a><a class="download" href="cards/dark/{safe}.png" download>Download pair</a></footer></article>''')
gallery='''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Homestead dialog review</title>
<style>*{box-sizing:border-box}body{margin:0;background:#f0f2f6;color:#182235;font:15px system-ui,sans-serif}main{max-width:1800px;margin:auto;padding:28px}h1{margin:0 0 9px;font-size:30px}p{color:#536179}nav{display:flex;gap:12px;align-items:center;flex-wrap:wrap;position:sticky;top:0;background:#f0f2f6;padding:16px 0;z-index:2}input,select{font:inherit;padding:12px;border:1px solid #aab3c2;border-radius:8px}input{flex:1;min-width:200px}a{color:#284fc2}#grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(370px,1fr));gap:20px}article{background:white;border:1px solid #d3d9e2;border-radius:12px;overflow:hidden}article header{display:flex;gap:10px;padding:18px 18px 0;align-items:baseline}article header span{color:#66738a}h2{font-size:17px;margin:0}article p{font-size:12px;padding:0 18px;overflow-wrap:anywhere}.paired{display:block;background:#17191d}.paired img{width:100%;height:390px;object-fit:contain;display:block}footer{display:flex;gap:16px;padding:18px;font-size:13px;flex-wrap:wrap}[hidden]{display:none!important}@media(max-width:500px){main{padding:14px}#grid{grid-template-columns:1fr}.paired img{height:320px}}</style>
<main><h1>Homestead dialog review</h1><p>COUNT dialog states · SHOTS original screenshots · desktop and phone in dark and light themes. These are demo fixtures, not live cluster data.</p>
<p>Includes container updates, Jobs, settled data-move history from PR #206, forms, recovery and blocked states. Click a pair for the full card; use the links for original screenshots.</p>
<nav><input id="search" aria-label="Find a dialog" placeholder="Find a dialog or state…"><label>Theme <select id="theme"><option value="dark">Dark</option><option value="light">Light</option></select></label><a href="contact-sheets.zip" download>Download all contact sheets</a><span id="count"></span></nav><div id="grid">CARDS</div></main>
<script>const articles=[...document.querySelectorAll('article')];document.querySelector('#search').oninput=e=>{const q=e.target.value.toLowerCase();articles.forEach(a=>a.hidden=!a.dataset.search.toLowerCase().includes(q));document.querySelector('#count').textContent=articles.filter(a=>!a.hidden).length+' shown';};document.querySelector('#theme').onchange=e=>{const t=e.target.value;articles.forEach(a=>{const name=a.querySelector('p').textContent;a.querySelector('img').src=`cards/${t}/${name}.png`;a.querySelector('.paired').href=`cards/${t}/${name}.png`;a.querySelector('.download').href=`cards/${t}/${name}.png`;a.querySelector('.desktop').href=`${t}/${name}-desktop.png`;a.querySelector('.mobile').href=`${t}/${name}-mobile.png`;});};document.querySelector('#count').textContent=articles.length+' shown';</script></html>'''
if args.kind == 'pages': gallery=gallery.replace('dialog','page')
(root/'index.html').write_text(gallery.replace('COUNT',str(len(names))).replace('SHOTS',str(len(names)*4)).replace('CARDS','\n'.join(cards)),encoding='utf-8')
with zipfile.ZipFile(root/'contact-sheets.zip','w',compression=zipfile.ZIP_DEFLATED,compresslevel=2) as z:
    for name in sheets:z.write(root/name,name)
    z.write(root/'sample-overview.png','sample-overview.png')
summary={'states':len(names),'original_screenshots':len(names)*4,'paired_cards':len(names)*2,'contact_sheets':len(sheets),'themes':['dark','light'],'viewports':{'desktop':1440,'mobile':390}}
(root/'manifest.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
print(json.dumps(summary))
