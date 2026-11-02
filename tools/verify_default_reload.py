"""Strict fixed-configuration comparison for the 0.26.1 loading-only fix."""
import argparse
import json
from pathlib import Path
import parallel_qa as qa


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--qa',type=Path,required=True)
    p.add_argument('--before',default='before');p.add_argument('--after',default='after')
    p.add_argument('--report',default='equivalence.json');a=p.parse_args()
    root=a.qa.resolve();before=root/a.before;after=root/a.after
    baseline=json.loads((root/'baseline.json').read_text(encoding='utf-8'))
    old=json.loads((before/'capture.json').read_text(encoding='utf-8'))
    new=json.loads((after/'capture.json').read_text(encoding='utf-8'))
    if old['cases']!=new['cases']:raise ValueError('Capture scenario order changed')
    cases=[]
    for name in old['cases']:
        changes=qa.diff(qa.clean(qa.read(before/(name+'.pklz'))),qa.clean(qa.read(after/(name+'.pklz'))))
        cases.append({'case':name,'differences':changes})
    if sorted(p.name for p in before.glob('*.xlsx'))!=sorted(p.name for p in after.glob('*.xlsx')):
        raise ValueError('Export file set changed')
    for file in before.glob('*.xlsx'):
        cases.append({'case':file.name,'differences':qa.diff(qa.excel_snapshot(file),qa.excel_snapshot(after/file.name))})
    if new['source_hashes']!=qa.source_hashes(qa.ROOT):raise ValueError('Source changed after capture')
    changed=[p for p,h in {**baseline['protected'],**baseline['inputs']}.items() if qa.sha(Path(p))!=h]
    core={n:{'before':qa.sha(root/'baseline/src'/n),'after':qa.sha(qa.ROOT/'src'/n)}
          for n in ['model.py','one_box_exception.py','allocation.py','default_config.json','default_must_sell.json']}
    report={'baseline_commit':baseline['baseline'],'strict_values_types_order':True,
            'whitelist':qa.WHITELIST,'additional_ignored_fields':[],
            'cases':cases,'differences':sum(len(c['differences']) for c in cases),
            'core':core,'changed_protected':changed}
    report['passed']=report['differences']==0 and not changed and all(v['before']==v['after'] for v in core.values())
    (root/a.report).write_bytes(json.dumps(report,ensure_ascii=False,indent=2).encode())
    print(json.dumps({k:report[k] for k in ('passed','differences','changed_protected')}))
    if not report['passed']:raise AssertionError('Strict comparison failed; see report')


if __name__=='__main__':main()
