"""Check every PDF page and source image; render pages for human visual QA."""
import argparse
import hashlib
import json
import math
import re
import subprocess
from pathlib import Path


def main():
    from pypdf import PdfReader
    import pdfplumber
    p = argparse.ArgumentParser()
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--poppler', type=Path, required=True)
    a = p.parse_args()
    root = Path(__file__).resolve().parents[1]
    folder = root / 'docs/manual'
    out = a.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    version = json.loads((root/'src/version.json').read_text('utf-8'))['version']
    source = folder / f'补货助手_{version}_新人业务上手手册.pdf'
    guide = (folder/'guide.md').read_text('utf-8')
    index = json.loads((folder/'screenshot-index.json').read_text('utf-8'))
    referenced = re.findall(r'!\[.*?\]\((.*?)\)', guide)
    assert len(referenced) == len(index['images']) == 26
    for item in index['images']:
        file = folder/'images'/item['name']
        assert hashlib.sha256(file.read_bytes()).hexdigest() == item['sha256'], file
        assert 'images/'+item['name'] in referenced, file
    ref = json.loads((folder/'parameter-reference.json').read_text('utf-8'))
    assert len(ref['definitions']) == 28
    assert len(ref['standard']['parameters']) == 15
    # Typography inserts spaces around day counts; compare the same field names
    # without treating those readable spaces as missing parameter coverage.
    guide_names = re.sub(r'\s+', '', guide)
    for d in ref['definitions']:
        assert re.sub(r'\s+', '', d['name']) in guide_names, d['name']
    reader = PdfReader(source)
    chapter_count = len(re.findall(r'^## ', guide, re.M))
    assert len([v for v in reader.outline if not isinstance(v, list)]) == chapter_count == 19
    chars = 0
    issues = []
    page_stats = []
    with pdfplumber.open(source) as doc:
        for n, page in enumerate(doc.pages, 1):
            text = page.extract_text() or ''
            body = [c for c in page.chars if c['bottom'] < page.height-34]
            for c in page.chars:
                if not all(math.isfinite(c[k]) for k in ('x0','x1','top','bottom')) or c['x0'] < -.5 or c['x1'] > page.width+.5 or c['top'] < -.5 or c['bottom'] > page.height+.5:
                    issues.append({'page': n, 'char': c['text'], 'bounds': [c[k] for k in ('x0','x1','top','bottom')]})
            assert len(body) > 8, f'Almost blank body on page {n}'
            assert '\ufffd' not in text
            chars += len(text)
            page_stats.append({'page': n, 'width': page.width, 'height': page.height, 'body_characters': len(body), 'images': len(page.images)})
    assert not issues, issues[:10]
    all_text = '\n'.join(p.extract_text() or '' for p in reader.pages)
    for token in ('系统默认', '沿用门店通用值', '核对已有采购单', '确认关联已核对采购单', '维护聚水潭内部订单号', 'SUPERSEDED', 'w1+w2不能超过1'):
        assert token in all_text, token
    assert '{{weight-practice}}' not in all_text
    assert sum(len(p.images) for p in reader.pages) == 26
    subprocess.run([str(a.poppler), '-r', '95', '-png', str(source), str(out/'page')], check=True, timeout=120)
    report = {'passed': True, 'pages': len(reader.pages), 'chapters': chapter_count, 'images': 26, 'parameter_definitions': 28, 'searchable_characters': chars, 'character_bounds_issues': issues, 'page_stats': page_stats, 'pdf_sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'visual_review': 'pending separate review of rendered pages'}
    (out/'pdf-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='page_stats'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
