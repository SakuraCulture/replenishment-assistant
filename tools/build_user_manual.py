"""Render the maintained Chinese guide into offline HTML and a searchable PDF.

HTML uses only bundled relative assets. PDF dependencies are authoring tools,
not application dependencies. Pass --pdf only after screenshot verification.
"""
import argparse
import html
import re
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
FOLDER=ROOT/'docs/manual'


def sections():
    text=(FOLDER/'guide.md').read_text('utf-8')
    parts=re.split(r'^## (.+)$',text,flags=re.M)
    return parts[0],[(parts[i],parts[i+1].strip()) for i in range(1,len(parts),2)]


def inline(text):
    return re.sub(r'`([^`]+)`',r'<code>\1</code>',html.escape(text))


def content(text):
    result=[];list_type=None
    for line in text.splitlines()+['']:
        kind='ol' if re.match(r'^\d+\. ',line) else 'ul' if line.startswith('- ') else None
        if kind!=list_type and list_type:result.append('</'+list_type+'>');list_type=None
        if kind:
            if not list_type:result.append('<'+kind+'>');list_type=kind
            result.append('<li>'+inline(re.sub(r'^(\d+\. |- )','',line))+'</li>');continue
        image=re.fullmatch(r'!\[(.*?)\]\((.*?)\)',line)
        if image:
            result.append('<figure><button class="zoom" aria-label="放大图片"><img loading="lazy" src="'+html.escape(image[2])+'" alt="'+html.escape(image[1])+'"></button><figcaption>点击查看原图 · 实际程序，隔离演示数据</figcaption></figure>')
        elif line.startswith('### '):result.append('<h3>'+inline(line[4:])+'</h3>')
        elif line.startswith('# '):result.append('<h1>'+inline(line[2:])+'</h1>')
        elif line:result.append('<p'+(' class="caption"' if line.startswith('图 ') else '')+'>'+inline(line)+'</p>')
    return '\n'.join(result)


def html_manual():
    intro,chapters=sections()
    nav=''.join(f'<a href="#c{i:02d}">{html.escape(title)}</a>' for i,(title,_) in enumerate(chapters,1))
    bodies=''.join(f'<section id="c{i:02d}"><h2>{html.escape(title)}</h2>{content(body)}<a class="back" href="#top">回到目录 ↑</a></section>' for i,(title,body) in enumerate(chapters,1))
    style='''*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:#f3f6f7;color:#213b41;font:17px/1.85 "Microsoft YaHei","Noto Sans SC",sans-serif}a{color:#086f65}aside{position:fixed;inset:0 auto 0 0;width:275px;padding:24px 20px;background:#123b3d;color:white;overflow:auto}aside a{display:block;color:#e2efea;font-size:14px;text-decoration:none;padding:7px 0;border-bottom:1px solid #ffffff18}aside strong{font-size:24px}main{margin-left:275px;padding:34px 4vw;max-width:1450px}h1{font-size:40px;line-height:1.3;margin:18px 0}h2{font-size:27px;line-height:1.4;color:#0b635d;margin:0 0 22px}header,section{background:white;border:1px solid #d9e5e5;border-radius:14px;padding:32px 36px;margin-bottom:28px;box-shadow:0 5px 15px #163b4210}section{scroll-margin-top:20px}p{margin:12px 0}li{margin:9px 0}code{background:#eff4f4;padding:2px 5px;border-radius:4px;overflow-wrap:anywhere}input{width:100%;font:inherit;padding:12px 16px;border:1px solid #aac2bf;border-radius:8px;margin:16px 0}figure{margin:24px 0 8px}.zoom{border:1px solid #cbdad8;background:white;width:100%;padding:0;cursor:zoom-in}.zoom img{width:100%;height:auto;display:block}.caption,figcaption{font-size:14px;color:#5a7075}figcaption{margin-top:5px}.back{display:inline-block;margin-top:16px;font-size:14px}.flow{display:flex;flex-wrap:wrap;gap:8px;margin:22px 0}.flow span{background:#e2f2eb;padding:8px 14px;border-radius:6px;font-weight:bold}.tag{letter-spacing:2px;color:#10776b;font-size:13px}dialog{max-width:96vw;max-height:96vh;padding:12px;border:0;border-radius:10px}dialog img{max-width:none;display:block}dialog .scroll{max-width:92vw;max-height:86vh;overflow:auto}dialog::backdrop{background:#102c34cc}dialog button{font:inherit;float:right;margin-bottom:8px}#count{color:#526c70;font-size:14px}@media(max-width:950px){aside{position:static;width:auto;max-height:320px}main{margin:0;padding:18px}header,section{padding:22px}h1{font-size:32px}}@media print{aside,input,.back,.zoom:after,#count{display:none}main{margin:0;padding:0}section,header{box-shadow:none;break-inside:avoid;border:0}body{font-size:11pt}}'''
    script='''const search=document.querySelector('input');search.addEventListener('input',()=>{const terms=search.value.trim().toLowerCase().split(/\\s+/).filter(Boolean);let count=0;document.querySelectorAll('section').forEach(s=>{s.hidden=!terms.every(t=>s.textContent.toLowerCase().includes(t));if(!s.hidden)count++;});document.querySelector('#count').textContent=terms.length?'找到 '+count+' 个章节；清空搜索显示全部':'共 26 个任务章节，所有内容可离线阅读';});const zoom=document.querySelector('dialog');document.querySelectorAll('.zoom').forEach(b=>b.onclick=()=>{const src=b.querySelector('img');zoom.querySelector('img').src=src.src;zoom.querySelector('img').alt=src.alt;zoom.showModal();});zoom.querySelector('button').onclick=()=>zoom.close();'''
    page='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>补货助手 0.48.0 · 新人使用手册</title><style>'+style+'</style><aside><strong>新人从这里开始</strong><p>补货助手 · 0.48.0</p>'+nav+'</aside><main id="top"><header><span class="tag">操作指南 / 报错恢复 / 离线可用</span>'+content(intro)+'<div class="flow"><span>准备数据</span><span>→ 检查与计算</span><span>→ 人工核对</span><span>→ 签发导出</span><span>→ 回传 / 付款 / 收货</span></div><p>独立分析路径：门店缺货率 → 采集留存 → 切面筛选 → Excel / AI 分析。</p><input aria-label="搜索任务或报错" type="search" placeholder="输入想做的事或报错，例如：回传、历史只读、恢复、缺货率"><div id="count">共 26 个任务章节，所有内容可离线阅读</div><p><a href="补货助手_0.48.0_新人使用手册.pdf">打开可搜索 PDF</a> · <a href="screenshot-index.json">截图来源索引</a></p></header>'+bodies+'</main><dialog><button>关闭原图</button><div class="scroll"><img alt=""></div></dialog><script>'+script+'</script></html>'
    page=page.replace('26 个任务章节',f'{len(chapters)} 个业务阶段').replace('新人使用手册','新人业务上手手册')
    page=page.replace('<span>准备数据</span><span>→ 检查与计算</span><span>→ 人工核对</span><span>→ 签发导出</span><span>→ 回传 / 付款 / 收货</span>',
        '<span>确定本批门店</span><span>→ 备数与审核</span><span>→ 签发交门店</span><span>→ 回传与仓分配</span><span>→ 付款与采购</span><span>→ 实收与复盘</span>')
    page=page.replace('独立分析路径：门店缺货率 → 采集留存 → 切面筛选 → Excel / AI 分析。',
        '每日巡店路线：留存观察 → 找出缺货商品 → 核对原因依据 → 安排跟进 → 下次复查。')
    page=page.replace('h2{font-size:', 'h3{font-size:21px;color:#145d57;margin:26px 0 12px}h2{font-size:')
    (FOLDER/'index.html').write_text(page,encoding='utf-8',newline='\n')


def pdf_manual():
    from reportlab.platypus import BaseDocTemplate,PageTemplate,Frame,Paragraph,Spacer,PageBreak,Image,NextPageTemplate,KeepTogether
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.pagesizes import A4,landscape
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.colors import HexColor
    from reportlab.lib.utils import ImageReader
    from pypdf import PdfReader
    pdfmetrics.registerFont(TTFont('Chinese',r'C:\Windows\Fonts\simhei.ttf'))
    font='Chinese';portrait=A4;wide=landscape(A4)
    styles={
        'body':ParagraphStyle('Body',fontName=font,fontSize=10.5,leading=18,spaceAfter=10,wordWrap='CJK',textColor=HexColor('#213b41')),
        'title':ParagraphStyle('Title',fontName=font,fontSize=25,leading=34,spaceAfter=20,textColor=HexColor('#09665e'),wordWrap='CJK'),
        'heading':ParagraphStyle('Heading',fontName=font,fontSize=19,leading=28,spaceAfter=16,textColor=HexColor('#09665e'),wordWrap='CJK'),
        'caption':ParagraphStyle('Caption',fontName=font,fontSize=9,leading=14,spaceAfter=12,textColor=HexColor('#526d71'),wordWrap='CJK'),
        'contents':ParagraphStyle('Contents',fontName=font,fontSize=10,leading=18,spaceAfter=5,wordWrap='CJK'),
        'subheading':ParagraphStyle('Subheading',fontName=font,fontSize=13,leading=21,spaceBefore=14,spaceAfter=9,keepWithNext=True,textColor=HexColor('#09665e'),wordWrap='CJK'),
    }
    def plain(text):return html.escape(text.replace('`','')).replace('—','-').replace('–','-')
    def para(text,style='body'):return Paragraph(plain(text),styles[style])
    target=FOLDER/'补货助手_0.48.0_新人使用手册.pdf'
    def footer(canvas,doc):
        width,height=canvas._pagesize;canvas.saveState();canvas.setStrokeColor(HexColor('#bed1cf'));canvas.line(38,31,width-38,31)
        canvas.setFont(font,8);canvas.setFillColor(HexColor('#526d71'));canvas.drawString(38,19,'补货助手 0.48.0 | 新人使用手册 | 隔离演示截图')
        canvas.drawRightString(width-38,19,str(doc.page));canvas.restoreState()
    class Document(BaseDocTemplate):
        def afterFlowable(self,flow):
            if hasattr(flow,'bookmark'):
                self.canv.bookmarkPage(flow.bookmark);self.canv.addOutlineEntry(flow.getPlainText(),flow.bookmark,level=0,closed=False)
    doc=Document(str(target),pagesize=portrait,title='补货助手 0.48.0 新人使用手册',author='优团 · 补货助手',leftMargin=42,rightMargin=42,topMargin=42,bottomMargin=45)
    doc.addPageTemplates([PageTemplate(id='text',frames=[Frame(42,45,portrait[0]-84,portrait[1]-87,id='body',leftPadding=0,rightPadding=0,topPadding=0,bottomPadding=0)],pagesize=portrait,onPage=footer),
                          PageTemplate(id='picture',frames=[Frame(32,42,wide[0]-64,wide[1]-74,id='pic',leftPadding=0,rightPadding=0,topPadding=0,bottomPadding=0)],pagesize=wide,onPage=footer)])
    intro,chapters=sections();story=[para('补货助手 · 新人业务上手手册','title'),para('从一批补货到实收复盘 · 从每日缺货到跟进清单'),Spacer(1,20)]
    for line in intro.splitlines():
        if line and not line.startswith('#'):story.append(para(line))
    story+=[Spacer(1,18),para('先认业务阶段，再看下一步交接。','heading'),para('每一步问：资料齐了吗？结果核对了吗？交给谁继续？'),para('签发已提交 ≠ 文件已生成。已受理 ≠ 已完成。缺失事实 ≠ 0。')]
    story.append(PageBreak());story.append(para('任务目录','heading'))
    for i,(title,_) in enumerate(chapters,1):story.append(Paragraph(f'<link href="#c{i:02d}" color="#09665e">{plain(title)}</link>',styles['contents']))
    for i,(title,body) in enumerate(chapters,1):
        story.append(PageBreak());heading=para(title,'heading');heading.bookmark=f'c{i:02d}';story.append(heading)
        images=[];captions=[]
        for line in body.splitlines():
            image=re.fullmatch(r'!\[(.*?)\]\((.*?)\)',line)
            if image:images.append((image[1],FOLDER/image[2]));continue
            if line.startswith('图 '):captions.append(line);continue
            if line.startswith('### '):story.append(para(line[4:],'subheading'))
            elif line:story.append(para(line))
        for n,(title,path) in enumerate(images):
            if not path.is_file():raise FileNotFoundError(path)
            story.extend([NextPageTemplate('picture'),PageBreak(),para(title,'heading')])
            width,height=ImageReader(str(path)).getSize();scale=min((wide[0]-70)/width,420/height)
            story.append(Image(str(path),width*scale,height*scale))
            story.append(Spacer(1,8));story.append(para(captions[n] if n<len(captions) else '实际 EXE；隔离演示数据。','caption'))
            story.append(NextPageTemplate('text'))
    doc.build(story)
    reader=PdfReader(target);text='\n'.join(p.extract_text() or '' for p in reader.pages)
    assert len(reader.outline)==len(chapters)
    for token in ('导出任务与恢复','系统关联信息','门店缺货率','STale_CONTEXT'.upper(),'全部初始化'):
        assert token in text,token
    print(f'PDF: {len(reader.pages)} pages, {len(text)} searchable characters, {target}')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--pdf',action='store_true');args=parser.parse_args()
    html_manual()
    if args.pdf:pdf_manual()
