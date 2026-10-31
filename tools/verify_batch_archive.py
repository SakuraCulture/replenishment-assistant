"""Frozen-source Excel oracle vs separate/combined exports, no user data access."""
import argparse,hashlib,json,sys,subprocess,time,itertools
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--baseline',type=Path,required=True);ap.add_argument('--results',type=Path,required=True);ap.add_argument('--output',type=Path,required=True);ap.add_argument('--oracle',action='store_true');a=ap.parse_args()
    sys.path[:0]=[str((a.baseline if a.oracle else ROOT)/'src'),str(ROOT/'tools')]
    import work_repository as wr,normal_orders as no,archive_orders as ao,archive_exports as ae,openpyxl
    from parallel_qa import read
    from contextlib import closing
    out=a.output.resolve()
    if a.oracle:
        import history_read
        heads=json.loads((out/'heads.json').read_text(encoding='utf-8'));db=out/'settings/workbench.sqlite3'
        for i,h in enumerate(heads):ae.export(db,h,out/f'old-{i}.xlsx')
        return
    out.mkdir(parents=True,exist_ok=False);work=wr.Repository(out/'settings');repo=no.Repository(work);heads=[]
    for i,name in enumerate(('real_None_True','real_3000_True')):
        result=read(a.results/(name+'.pklz'));day=f'2026-10-{27+i}';payload={'schema':1,'result':result,'new_focus':None};digest=no.fingerprint(result)
        oid=f'qa-{i}';number=f'BH-{day}-{result["store"]}-ZC'
        with closing(work.connect()) as db,db:
            db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',(day,result['store'],'normal_model',oid,number,1))
            db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',(oid,1,digest,'fixed',no.encode(payload)))
        for wh in ao.scopes(result['rows']):heads.append({'day':day,'store':result['store'],'name':result['store_name'],'oid':oid,'number':ao.number(number,wh),'revision':1,'digest':digest,'warehouse':wh})
    (out/'heads.json').write_text(json.dumps(heads,ensure_ascii=False),encoding='utf-8')
    before=hashlib.sha256(work.path.read_bytes()).hexdigest();start=time.perf_counter()
    subprocess.run([sys.executable,__file__,'--baseline',str(a.baseline.resolve()),'--results',str(a.results.resolve()),'--output',str(out),'--oracle'],check=True)
    old_seconds=time.perf_counter()-start
    import archive_batch as batch
    reports=[];sentinel=object()
    def trip(c):return c.value,c.data_type,c.number_format
    for kind,sheet in [('calculation',0),('signed',2)]:
        for mode in ('separate','combined'):
            start=time.perf_counter();plan=batch.prepare(work.path,heads,kind,mode);prepared=time.perf_counter()-start
            result=batch.execute(work.path,plan,out);elapsed=time.perf_counter()-start
            if result['failed'] or result['skipped'] or result['cancelled']:raise AssertionError(result)
            count=0;row_count=0
            for f in result['files']:
                book=openpyxl.load_workbook(f['path'],read_only=True);ws=book.active
                if book.sheetnames!=[batch.TITLES[kind]]:raise AssertionError('unexpected sheets')
                it=ws.iter_rows();headers=[c.value for c in next(it)];keys=batch.columns(headers[6:])
                for head in f['heads']:
                    index=next(i for i,h in enumerate(heads) if batch.key(h)==batch.key(head));old=openpyxl.load_workbook(out/f'old-{index}.xlsx',read_only=True);old_it=old.worksheets[sheet].iter_rows();old_keys=batch.columns([c.value for c in next(old_it)]);positions={k:i for i,k in enumerate(old_keys)}
                    order=repo.get(head['day'],head['store'],1);identity=batch.identity(order,head['warehouse'])
                    for old_row in old_it:
                        row=next(it,sentinel)
                        if row is sentinel:raise AssertionError('missing row')
                        if [c.value for c in row[:6]]!=identity:raise AssertionError('wrong order identity')
                        if len(row)>len(headers):raise AssertionError('extra columns')
                        for column,k in enumerate(keys,6):
                            c=row[column] if column<len(row) else None
                            expected=trip(old_row[positions[k]]) if k in positions else (None,'n','General')
                            actual=trip(c) if c is not None else (None,'n','General')
                            if actual!=expected:raise AssertionError((kind,mode,head['number'],k,actual,expected))
                            count+=1
                        row_count+=1
                    old.close()
                if next(it,sentinel) is not sentinel:raise AssertionError('extra row')
                book.close()
            reports.append({'kind':kind,'mode':mode,'files':len(result['files']),'rows':row_count,'strict_cells':count,'prepare_seconds':prepared,'prepare_and_export_seconds':elapsed,'differences':0})
    after=hashlib.sha256(work.path.read_bytes()).hexdigest()
    if before!=after:raise AssertionError('source archive DB mutated')
    report={'passed':True,'baseline':str(a.baseline),'scenarios':reports,'baseline_four_three_sheet_exports_seconds':old_seconds,'snapshot_db_unchanged':True,'allowed_changes':['one content type per workbook','separate or same-warehouse concatenation','six order identity columns','union of historical columns with blank absent values','autofilter and freeze panes'],'business_value_type_number_format_order_differences':0,'timing_note':'single-run QA timings, not a controlled speedup benchmark; legacy exports three content types at once'}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print('passed',sum(r['strict_cells'] for r in reports),'strict cells')

if __name__=='__main__':main()
