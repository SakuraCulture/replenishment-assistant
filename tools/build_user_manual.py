"""Render the maintained business guide as offline HTML and searchable PDF.

Authoring dependencies stay outside the application runtime. Every screenshot
retains its provenance in the manual index and the adjacent business caption.
"""
import argparse
import html
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / 'docs/manual'
VERSION = json.loads((ROOT / 'src/version.json').read_text('utf-8'))['version']
PDF_NAME = f'补货助手_{VERSION}_新人业务上手手册.pdf'


def sections():
    parts = re.split(r'^## (.+)$', (FOLDER / 'guide.md').read_text('utf-8'), flags=re.M)
    return parts[0], [(parts[i], parts[i + 1].strip()) for i in range(1, len(parts), 2)]


def inline(text):
    text = html.escape(text)
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    return re.sub(r'\[([^\]]+)\]\((#[\w-]+|[\w./-]+)\)', r'<a href="\2">\1</a>', text)


def blocks(text):
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip():
            continue
        pic = re.fullmatch(r'!\[(.*?)\]\((.*?)\)', line)
        if pic:
            caption = ''
            j = i
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and lines[j].startswith('图 '):
                caption = lines[j]
                i = j + 1
            yield 'image', (pic[1], pic[2], caption)
        elif line.startswith('|'):
            rows = [line]
            while i < len(lines) and lines[i].startswith('|'):
                rows.append(lines[i])
                i += 1
            values = [[s.strip() for s in r.strip().strip('|').split('|')] for r in rows]
            yield 'table', [r for r in values if not all(re.fullmatch(r':?-{3,}:?', s) for s in r)]
        elif line.startswith('### '):
            yield 'h3', line[4:]
        elif line.startswith('# '):
            yield 'h1', line[2:]
        elif line == '{{weight-practice}}':
            yield 'practice', ''
        elif re.match(r'^\d+\. ', line):
            yield 'ol', re.sub(r'^\d+\. ', '', line)
        elif line.startswith('- '):
            yield 'ul', line[2:]
        else:
            yield 'p', line


def practice():
    return '''<form class="practice" aria-label="权重练习计算器"><h4>练习：检查三段权重合计</h4>
<p>只在本页计算，不连接补货助手，不保存设置。单位为百分比。</p>
<div class="practice-inputs"><label>最近7天 w1（%）<input name="w1" type="number" min="0" max="100" step="0.1" value="60"></label><label>此前7天 w2（%）<input name="w2" type="number" min="0" max="100" step="0.1" value="20"></label></div>
<output aria-live="polite"></output><div class="bars" aria-hidden="true"><span></span><span></span><span></span></div>
<p><button type="button" data-weights="60,20">快销标准</button> <button type="button" data-weights="40,30">日百标准</button> <button type="button" data-weights="60,40">合法边界</button> <button type="button" data-weights="60,60">错误示例</button></p></form>'''


def content(text, prefix='intro'):
    result = []
    list_type = None
    sub = 0
    for kind, value in list(blocks(text)) + [('end', '')]:
        if kind != list_type and list_type:
            result.append('</' + list_type + '>')
            list_type = None
        if kind in ('ol', 'ul'):
            if not list_type:
                result.append('<' + kind + '>')
                list_type = kind
            result.append('<li>' + inline(value) + '</li>')
        elif kind == 'image':
            title, src, caption = value
            result.append(f'<figure><button class="zoom" aria-label="放大图片：{html.escape(title)}"><img loading="lazy" src="{html.escape(src)}" alt="{html.escape(title)}"></button><figcaption>{inline(caption)} · 点击放大原图</figcaption></figure>')
        elif kind == 'table':
            head, *rows = value
            result.append('<div class="table-scroll" tabindex="0" role="region" aria-label="参考表"><table><thead><tr>' + ''.join('<th scope="col">' + inline(s) + '</th>' for s in head) + '</tr></thead><tbody>')
            result.extend('<tr>' + ''.join('<td>' + inline(s) + '</td>' for s in row) + '</tr>' for row in rows)
            result.append('</tbody></table></div>')
        elif kind == 'h3':
            sub += 1
            result.append(f'<h3 id="{prefix}-s{sub:02d}">{inline(value)}</h3>')
        elif kind == 'h1':
            result.append('<h1>' + inline(value) + '</h1>')
        elif kind == 'p':
            result.append('<p>' + inline(value) + '</p>')
        elif kind == 'practice':
            result.append(practice())
    return '\n'.join(result)


def html_manual():
    intro, chapters = sections()
    nav = ''.join(f'<a href="#c{i:02d}">{html.escape(title)}</a>' for i, (title, _) in enumerate(chapters, 1))
    bodies = []
    for i, (title, body) in enumerate(chapters, 1):
        jumps = ''
        if i >= 17:
            subs = [v for k, v in blocks(body) if k == 'h3']
            jumps = '<nav class="chapter-nav" aria-label="本章任务">' + ''.join(f'<a href="#c{i:02d}-s{n:02d}">{html.escape(s)}</a>' for n, s in enumerate(subs, 1)) + '</nav>'
        bodies.append(f'<section id="c{i:02d}"><h2>{html.escape(title)}</h2>{jumps}{content(body, f"c{i:02d}")}<a class="back" href="#top">回到目录 ↑</a></section>')
    style = '''*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:#f3f6f7;color:#213b41;font:17px/1.85 "Microsoft YaHei","Noto Sans SC",sans-serif}a{color:#086f65}aside{position:fixed;inset:0 auto 0 0;width:285px;padding:24px 20px;background:#123b3d;color:white;overflow:auto}aside a{display:block;color:#e2efea;font-size:14px;text-decoration:none;padding:7px 0;border-bottom:1px solid #ffffff18}aside strong{font-size:24px}main{margin-left:285px;padding:34px 4vw;max-width:1550px;min-width:0}h1{font-size:38px;line-height:1.4;margin:18px 0}h2{font-size:27px;line-height:1.45;color:#0b635d;margin:0 0 22px}h3{font-size:21px;color:#145d57;margin:30px 0 12px;scroll-margin-top:22px}h4{margin:0;font-size:19px}header,section{background:white;border:1px solid #d9e5e5;border-radius:14px;padding:32px 36px;margin-bottom:28px;box-shadow:0 5px 15px #163b4210;min-width:0}section{scroll-margin-top:20px}p{margin:12px 0;overflow-wrap:anywhere}li{margin:9px 0;overflow-wrap:anywhere}code{background:#eff4f4;padding:2px 5px;border-radius:4px;overflow-wrap:anywhere}input{width:100%;min-width:0;font:inherit;padding:12px 16px;border:1px solid #aac2bf;border-radius:8px;margin:16px 0}figure{margin:24px 0 12px}.zoom{border:1px solid #cbdad8;background:white;width:100%;padding:0;cursor:zoom-in}.zoom img{width:100%;height:auto;display:block}figcaption{font-size:14px;color:#52696e;margin-top:8px}.back{display:inline-block;margin-top:18px;font-size:14px}.tag{letter-spacing:2px;color:#10776b;font-size:13px}dialog{max-width:96vw;max-height:96vh;padding:12px;border:0;border-radius:10px}dialog img{max-width:none;display:block}dialog .scroll{max-width:92vw;max-height:86vh;overflow:auto}dialog::backdrop{background:#102c34cc}button{font:inherit;border:1px solid #9cbfba;border-radius:6px;padding:6px 12px;background:white;color:#125c56;cursor:pointer}dialog button{float:right;margin-bottom:8px}#count{color:#526c70;font-size:14px}.flow{display:flex;flex-wrap:wrap;gap:8px;margin:20px 0}.flow span{background:#e2f2eb;padding:8px 12px;border-radius:6px;font-weight:bold}.chapter-nav{display:grid;gap:5px;padding:16px 20px;background:#f1f7f5;border-left:4px solid #1d7f72;margin-bottom:25px;font-size:15px}.table-scroll{overflow-x:auto;margin:20px 0;border:1px solid #cbdcd8;border-radius:8px}table{width:100%;border-collapse:collapse;font-size:15px;line-height:1.7;min-width:570px}th,td{vertical-align:top;padding:12px 14px;text-align:left;border-bottom:1px solid #dce7e4;overflow-wrap:anywhere}th{background:#e1efea;color:#124c45}tbody tr:nth-child(even){background:#f7faf9}.practice{padding:22px;border:1px solid #96bdb4;border-radius:10px;background:#f1f8f5;margin:24px 0}.practice-inputs{display:flex;gap:20px;flex-wrap:wrap}.practice-inputs label{flex:1 1 210px}.practice input{margin:6px 0 14px}output{display:block;font-weight:bold;padding:10px 0}.bars{height:14px;display:flex;background:#d9e3e0;border-radius:7px;overflow:hidden}.bars span:nth-child(1){background:#116f64}.bars span:nth-child(2){background:#66a394}.bars span:nth-child(3){background:#b7d3c7}.practice[data-valid="false"] output{color:#a63224}:focus-visible{outline:3px solid #df8d20;outline-offset:3px}@media(max-width:950px){aside{position:static;width:auto;max-height:340px}main{margin:0;padding:16px}header,section{padding:22px}h1{font-size:30px}h2{font-size:24px}}@media print{aside,input,.back,#count,dialog,.practice{display:none}main{margin:0;padding:0}header,section{box-shadow:none;border:0}body{font-size:11pt}}'''
    script = '''const search=document.querySelector('#manual-search');const sections=[...document.querySelectorAll('section')];function filter(){const terms=search.value.trim().toLowerCase().split(/\\s+/).filter(Boolean);let count=0;sections.forEach(s=>{s.hidden=!terms.every(t=>s.textContent.toLowerCase().includes(t));if(!s.hidden)count++;});document.querySelector('#count').textContent=terms.length?'找到 '+count+' 个章节；清空搜索显示全部':'共 CHAPTER_COUNT 个业务阶段，所有内容可离线阅读';}search.addEventListener('input',filter);document.querySelectorAll('a[href^="#"]').forEach(a=>a.addEventListener('click',()=>{search.value='';filter();}));const zoom=document.querySelector('dialog');document.querySelectorAll('.zoom').forEach(b=>b.addEventListener('click',()=>{const src=b.querySelector('img');zoom.querySelector('img').src=src.src;zoom.querySelector('img').alt=src.alt;zoom.showModal();}));zoom.querySelector('button').onclick=()=>zoom.close();document.querySelectorAll('.practice').forEach(f=>{const w1=f.elements.w1,w2=f.elements.w2;function calc(){const a=w1.valueAsNumber,b=w2.valueAsNumber,c=100-a-b;const valid=[a,b].every(v=>Number.isFinite(v)&&v>=0&&v<=100)&&c>=-1e-9;f.dataset.valid=String(valid);const r=v=>Math.round(v*100)/100;f.querySelector('output').textContent=Number.isFinite(c)?'自动 w3 = '+r(c)+'%；'+(valid?'权重合计合法；是否适合本店仍需试算。':'不合法：两段非负且合计不能超过100%。'):'请完整填写两段权重。';f.querySelectorAll('.bars span').forEach((s,i)=>s.style.width=(valid?[a,b,Math.max(0,c)][i]:0)+'%');}f.addEventListener('submit',e=>e.preventDefault());[w1,w2].forEach(x=>x.addEventListener('input',calc));f.querySelectorAll('[data-weights]').forEach(b=>b.onclick=()=>{[w1.value,w2.value]=b.dataset.weights.split(',');calc();});calc();});'''.replace('CHAPTER_COUNT', str(len(chapters)))
    page = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
    page += f'<title>补货助手 {VERSION} · 新人业务上手手册</title><style>{style}</style><aside><strong>新人从这里开始</strong><p>补货助手 · {VERSION}</p>{nav}</aside><main id="top"><header><span class="tag">业务引导 / 参数详解 / 单号维护 / 离线可用</span>'
    page += content(intro) + '<div class="flow"><span>定门店与参数</span><span>→ 备数与审核</span><span>→ 签发交门店</span><span>→ 回传与仓分配</span><span>→ 付款与采购</span><span>→ 实收与复盘</span></div>'
    page += f'<input id="manual-search" aria-label="搜索任务或报错" type="search" placeholder="输入：参数、权重、系统默认、翱象采购单号、TASK_NOT_FOUND"><div id="count">共 {len(chapters)} 个业务阶段，所有内容可离线阅读</div><p><a href="{PDF_NAME}">打开可搜索 PDF</a> · <a href="screenshot-index.json">截图来源索引</a></p></header>'
    page += ''.join(bodies) + '</main><dialog aria-label="原图预览"><button>关闭原图</button><div class="scroll"><img alt=""></div></dialog><script>' + script + '</script></html>'
    (FOLDER / 'index.html').write_text(page, encoding='utf-8', newline='\n')


def pdf_manual(report_path=None):
    from reportlab.platypus import BaseDocTemplate, PageTemplate, Frame, Paragraph, Spacer, PageBreak, Image, NextPageTemplate, LongTable, TableStyle, KeepTogether
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.colors import HexColor
    from reportlab.lib.utils import ImageReader
    from pypdf import PdfReader
    pdfmetrics.registerFont(TTFont('Chinese', r'C:\Windows\Fonts\simhei.ttf'))
    font = 'Chinese'
    portrait, wide = A4, landscape(A4)
    styles = {
        'body': ParagraphStyle('Body', fontName=font, fontSize=10.5, leading=17, spaceAfter=9, wordWrap='CJK', textColor=HexColor('#213b41')),
        'title': ParagraphStyle('Title', fontName=font, fontSize=25, leading=35, spaceAfter=20, textColor=HexColor('#09665e'), wordWrap='CJK'),
        'heading': ParagraphStyle('Heading', fontName=font, fontSize=19, leading=28, spaceAfter=16, textColor=HexColor('#09665e'), wordWrap='CJK'),
        'caption': ParagraphStyle('Caption', fontName=font, fontSize=9, leading=14, spaceAfter=10, textColor=HexColor('#526d71'), wordWrap='CJK'),
        'contents': ParagraphStyle('Contents', fontName=font, fontSize=10, leading=17, spaceAfter=5, wordWrap='CJK'),
        'subheading': ParagraphStyle('Subheading', fontName=font, fontSize=13, leading=21, spaceBefore=14, spaceAfter=9, keepWithNext=True, textColor=HexColor('#09665e'), wordWrap='CJK'),
        'cell': ParagraphStyle('Cell', fontName=font, fontSize=9.2, leading=15, wordWrap='CJK', textColor=HexColor('#213b41')),
    }

    def markup(text):
        text = re.sub(r'`([^`]+)`', r'\1', text).replace('—', '-').replace('–', '-').replace('−', '-')
        text = html.escape(text)
        # CJK paragraph wrapping can split a parenthesized identifier despite
        # nobr. Give that identifier its own line in narrow reference cells.
        text = re.sub(r'（([A-Z][A-Z0-9_]{2,20})）', r'<br/>\1', text)
        # Keep short error identifiers searchable as a whole across CJK wraps.
        text = re.sub(r'\b[A-Z][A-Z0-9_]{2,20}\b', r'<nobr>\g<0></nobr>', text)
        return re.sub(r'\[([^\]]+)\]\((#[\w-]+)\)', r'<link href="\2" color="#09665e">\1</link>', text)

    def para(text, style='body'):
        return Paragraph(markup(text), styles[style])

    def footer(canvas, doc):
        width, height = canvas._pagesize
        canvas.saveState()
        canvas.setStrokeColor(HexColor('#bed1cf'))
        canvas.line(38, 31, width - 38, 31)
        canvas.setFont(font, 8)
        canvas.setFillColor(HexColor('#526d71'))
        canvas.drawString(38, 19, f'补货助手 {VERSION} | 新人业务上手手册 | 隔离演示，来源见图注')
        canvas.drawRightString(width - 38, 19, str(doc.page))
        canvas.restoreState()

    locations = []

    class Document(BaseDocTemplate):
        def afterFlowable(self, flow):
            if hasattr(flow, 'bookmark'):
                self.canv.bookmarkPage(flow.bookmark)
                self.canv.addOutlineEntry(flow.getPlainText(), flow.bookmark, level=flow.level, closed=flow.level == 0)
                locations.append({'id': flow.bookmark, 'title': flow.getPlainText(), 'page': self.page, 'level': flow.level})
            if hasattr(flow, 'image_source'):
                locations.append({'image': flow.image_source, 'page': self.page})

    target = FOLDER / PDF_NAME
    doc = Document(str(target), pagesize=portrait, title=f'补货助手 {VERSION} 新人业务上手手册', author='优团 · 补货助手', leftMargin=42, rightMargin=42, topMargin=42, bottomMargin=45)
    doc.addPageTemplates([
        PageTemplate(id='text', frames=[Frame(42, 45, portrait[0]-84, portrait[1]-87, id='body', leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)], pagesize=portrait, onPage=footer),
        PageTemplate(id='picture', frames=[Frame(32, 42, wide[0]-64, wide[1]-74, id='pic', leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)], pagesize=wide, onPage=footer),
    ])
    intro, chapters = sections()
    story = [para('补货助手 · 新人业务上手手册', 'title'), para('从一批补货到实收复盘 | 参数实操与三种单号维护'), Spacer(1, 14)]
    for kind, value in blocks(intro):
        if kind == 'p':
            story.append(para(value))
    story += [Spacer(1, 14), para('先认业务阶段，再看下一步交接。', 'heading'), para('签发已生效与文件已生成分别核对；已受理不等于已完成；缺失事实不能填成 0。')]
    story.append(para('PDF 的完整图示集中在各章末。点击正文“查看图示”或侧边书签跳到原图；图片页可返回本章。离线 HTML 按步骤直接显示图片，适合边看边操作。', 'caption'))
    story += [PageBreak(), para('任务目录', 'heading')]
    for i, (title, _) in enumerate(chapters, 1):
        story.append(Paragraph(f'<link href="#c{i:02d}" color="#09665e">{html.escape(title)}</link>', styles['contents']))
    mode = 'text'
    for i, (title, body) in enumerate(chapters, 1):
        story.extend([NextPageTemplate('text'), PageBreak()])
        mode = 'text'
        heading = para(title, 'heading')
        heading.bookmark, heading.level = f'c{i:02d}', 0
        story.append(heading)
        sub, item_number = 0, 0
        pictures = []
        for kind, value in blocks(body):
            if kind == 'image':
                pictures.append(value)
                picture_id = f'c{i:02d}-pic-{len(pictures):02d}'
                story.append(Paragraph(f'<link href="#{picture_id}" color="#09665e">查看图示：{html.escape(value[0])}（本章图片页）</link>', styles['caption']))
                continue
            if mode == 'picture':
                story.append(PageBreak())
                mode = 'text'
            if kind == 'h3':
                sub += 1
                heading = para(value, 'subheading')
                heading.bookmark, heading.level = f'c{i:02d}-s{sub:02d}', 1
                story.append(heading)
            elif kind == 'table':
                n = len(value[0])
                ratios = {3: [0.25, 0.29, 0.46], 4: [0.2, 0.2, 0.2, 0.4]}.get(n, [1/n]*n)
                table = LongTable([[para(s, 'cell') for s in row] for row in value], colWidths=[r*(portrait[0]-84) for r in ratios], repeatRows=1, hAlign='LEFT')
                table.setStyle(TableStyle([
                    ('BACKGROUND', (0,0), (-1,0), HexColor('#e1efea')), ('ROWBACKGROUNDS', (0,1), (-1,-1), [HexColor('#ffffff'), HexColor('#f6faf8')]),
                    ('LINEBELOW', (0,0), (-1,-1), 0.4, HexColor('#c5d8d2')), ('VALIGN', (0,0), (-1,-1), 'TOP'),
                    ('LEFTPADDING', (0,0), (-1,-1), 7), ('RIGHTPADDING', (0,0), (-1,-1), 7), ('TOPPADDING', (0,0), (-1,-1), 7), ('BOTTOMPADDING', (0,0), (-1,-1), 7),
                ]))
                # Keep short comparisons together; long reference tables retain
                # repeating headers when they genuinely need another page.
                story.extend([KeepTogether([table]) if len(value) <= 6 else table, Spacer(1, 12)])
            elif kind == 'practice':
                story.append(para('练习计算器在离线 HTML 中使用；PDF 按上方四组例子核算。本页练习不会写入软件参数。', 'caption'))
            elif kind in ('p', 'ol', 'ul'):
                if kind == 'ol':
                    item_number += 1
                    text = f'{item_number}. {value}'
                else:
                    item_number = 0
                    text = ('- ' if kind == 'ul' else '') + value
                story.append(para(text))
            if kind not in ('ol',):
                item_number = 0
        for n, (caption_title, src, caption) in enumerate(pictures, 1):
            path = FOLDER / src
            if not path.is_file():
                raise FileNotFoundError(path)
            heading = para(caption_title, 'heading')
            heading.bookmark, heading.level = f'c{i:02d}-pic-{n:02d}', 1
            story.extend([NextPageTemplate('picture'), PageBreak(), heading])
            width, height = ImageReader(str(path)).getSize()
            caption_flow = para(caption or '隔离演示，详细来源见截图索引。', 'caption')
            back_flow = Paragraph(f'<link href="#c{i:02d}" color="#09665e">返回本章正文</link>', styles['caption'])
            frame_width = wide[0]-64
            reserve = sum(flow.wrap(frame_width, 1000)[1] + flow.getSpaceBefore() + flow.getSpaceAfter()
                          for flow in (heading, caption_flow, back_flow)) + 13
            max_height = min(420, wide[1]-74-reserve)
            scale = min((wide[0]-70)/width, max_height/height)
            pic = Image(str(path), width*scale, height*scale)
            pic.image_source = src
            story.extend([pic, Spacer(1, 8), caption_flow, back_flow, NextPageTemplate('text')])
    doc.build(story)
    reader = PdfReader(target)
    text = '\n'.join(p.extract_text() or '' for p in reader.pages)
    for token in ('导出任务与恢复', '系统关联信息', '门店缺货率', 'STALE_CONTEXT', '全部初始化', '核对已有采购单', '维护聚水潭内部订单号', '权重', '沿用门店通用值'):
        assert token in text, token
    report = {'version': VERSION, 'pdf': str(target), 'pages': len(reader.pages), 'characters': len(text), 'chapters': len(chapters), 'locations': locations}
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps({k:v for k,v in report.items() if k != 'locations'}, ensure_ascii=False))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--pdf', action='store_true')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    html_manual()
    if args.pdf:
        pdf_manual(args.report)
