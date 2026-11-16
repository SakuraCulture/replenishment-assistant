"""Build an independent repair copy. Embedded business data and source remain exact."""
import argparse
import hashlib
from pathlib import Path


def build(source, target):
    source, target = Path(source).resolve(), Path(target).resolve()
    if source == target or target.exists():
        raise ValueError('请选择新的修复副本位置，保留原HTML')
    text = source.read_text(encoding='utf-8')
    storage = Path(__file__).with_name('dashboard_storage.js').read_text(encoding='utf-8')
    start = text.index('function readActions()')
    end = text.index('const initialDates=', start)
    text = text[:start] + storage + '\n\n' + text[end:]
    old = "if(imported.length){const keys=new Set(DATA.records.map(r=>r[0]+'|'+r[1]+'|'+r[2]));for(const r of imported){const k=r[0]+'|'+r[1]+'|'+r[2];if(!keys.has(k)){DATA.records.push(r);keys.add(k)}}}"
    if text.count(old) != 1:
        raise ValueError('原看板恢复逻辑已变化，请重新核对')
    text = text.replace(old, 'DATA.records=mergedRecords(imported);')
    start = text.index('function handleCSV(e){', text.index('function parseCSV('))
    end = text.index('\ninit();', start)
    text = text[:start] + text[end:]
    text = text.replace('\ninit();', '\ninit();\ninitStorageControls();')
    text = text.replace('saveActions(list);$(\'actionTarget\')', 'if(!saveActions(list))return;$(\'actionTarget\')')
    text = text.replace('导入后续日明细 CSV', '导入新增或更正明细 CSV')
    text = text.replace('通过顶部导入按钮追加；', '通过顶部导入按钮新增或更正；')
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('x', encoding='utf-8', newline='\n') as out:
        out.write(text)
    return {'source': str(source), 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
            'target': str(target), 'target_sha256': hashlib.sha256(target.read_bytes()).hexdigest()}


if __name__ == '__main__':
    import json
    p = argparse.ArgumentParser()
    p.add_argument('source', type=Path);p.add_argument('target', type=Path)
    a = p.parse_args();print(json.dumps(build(a.source, a.target), ensure_ascii=False))
