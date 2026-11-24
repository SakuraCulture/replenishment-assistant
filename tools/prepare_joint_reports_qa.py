"""Take read-only SQLite backups of the selected published report evidence."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))

from openpyxl import Workbook, load_workbook
from business_daily_sources import file_source, profile, save_profile
from export_files import sha256
from order_migration import pause_imported
from stockout_repository import Repository


def prepare(out, source, reference, business_day, blacklist, must_sell, owners, weeks):
    out.mkdir(parents=True)
    root = out/'settings'
    source = source.resolve(strict=True)
    with closing(sqlite3.connect((source/'workbench.sqlite3').as_uri()+'?mode=ro',uri=True)) as original:
        sku_day = original.execute('SELECT max(day) FROM normal_orders').fetchone()[0]
        root.mkdir()
        with closing(sqlite3.connect(root/'workbench.sqlite3')) as backup:
            original.backup(backup)
    for kind in ('stockout','warehouse_stockout'):
        relative = Path('analysis')/kind/(kind+'.sqlite3')
        (root/relative).parent.mkdir(parents=True)
        with closing(sqlite3.connect((source/relative).as_uri()+'?mode=ro',uri=True)) as original:
            with closing(sqlite3.connect(root/relative)) as backup:
                original.backup(backup)
    pause_imported(root)
    repo = Repository(root)
    with repo.connect() as db:
        selected = db.execute('''SELECT r.basis FROM current_days d JOIN publications p ON p.id=d.publication
                              JOIN runs r ON r.id=p.run WHERE d.day=?''',(business_day,)).fetchone()
        assert selected, business_day
        settings = repo.settings(selected['basis'])
    with closing(load_workbook(reference,read_only=True,data_only=True)) as book:
        tags = {row[0]:row[1:4] for row in book['门店缺货率'].iter_rows(min_row=3,max_col=4,values_only=True) if row[0]}
    roster = out/'验收门店清单.xlsx'
    book = Workbook()
    book.active.append(('门店编码','门店名称','标签','运营负责人','主仓','从仓'))
    for entry in settings['stores']:
        code = entry['code']
        _,tag,owner = tags[code]
        route = settings['routes'][code]
        book.active.append((code,entry['name'],tag,owner,route['primary'],route.get('secondary')))
    book.save(roster)
    saved = profile(root,business_day)
    saved.update(order_cutoff=business_day+'T23:59:59+08:00')
    refs = {'stores':roster, 'blacklist':blacklist, 'must_sell':must_sell, 'owners':owners}
    for role,name in refs.items():
        saved['files'][role] = file_source(root,name,sheets=['黑名单采购状态'] if role=='blacklist' else None)
    save_profile(root,saved)
    (out/'synthetic.json').write_text(json.dumps({'synthetic':True,'isolated':True,'live_platform':False,
                                                  'data':'read-only backups of published records'},ensure_ascii=False),'utf-8')
    (out/'fixture.json').write_text(json.dumps({'day':business_day,'sku_day':sku_day or business_day,'clock':'2026-11-25T09:00:00+08:00',
        'weeks':str(weeks.resolve()), 'roster_reference':str(reference.resolve()),'this_is_reference_scope':True,
        'links':{role:{'path':str(path.resolve()),'sha256':sha256(path)} for role,path in {**refs,'weeks':weeks}.items()},
        'db_sha256':{str(p.relative_to(root)):sha256(p) for p in root.rglob('*.sqlite3')}},ensure_ascii=False,indent=2),'utf-8')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    for name in ('out','source','reference','blacklist','must-sell','owners','weeks'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--day',required=True)
    args=parser.parse_args()
    prepare(args.out.resolve(),args.source,args.reference,args.day,args.blacklist,args.must_sell,args.owners,args.weeks)
