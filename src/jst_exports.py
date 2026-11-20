"""Exact-generation exports with the local warehouse order number; no allocation."""
import hashlib
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from decimal import Decimal
from pathlib import Path
import openpyxl
from openpyxl.styles import Font, PatternFill
from openpyxl.cell import WriteOnlyCell
import jst_order_store as orders
from jst_order_guards import require_generation, require_generation_basis, upstream_stamp
from work_repository import Conflict, read_connection
from runtime_support import checkpoint, Cancelled
from sqlite_write import begin
from export_progress import RowProgress


def filename(p):
    h=p['head'];name=f"{h['day']}_{h.get('name',h['store'])}_{h['warehouse']}_{h['number']}_聚水潭订单_{p['position']:03d}.xlsx"
    return re.sub(r'[<>:"/\\|?*]', '_', name)


def _preflight(path, gids, historical, cancel):
    proofs = {}
    with closing(read_connection(path)) as db:
        for gid in gids:
            checkpoint(cancel)
            with db:
                db.execute('BEGIN')
                p, digest = orders.read_generation(db, gid)
                require_generation(db, p, digest, historical=historical)
                stamp = None
                if not historical:
                    head = p['head']
                    products = p.get('signed_products', p['basis']['products'])
                    require_generation_basis(db,p)
                    stamp = upstream_stamp(db, head, products)
                proofs[gid] = digest, stamp
    return proofs


def _target_identity(path):
    try:
        s = path.stat()
    except FileNotFoundError:
        return None
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


def _publish(db, p, proof, tmp, target, expected_target, historical, cancel):
    digest, stamp = proof
    require_generation(db, p, digest, historical=historical)
    if not historical:
        products = p.get('signed_products', p['basis']['products'])
        if upstream_stamp(db, p['head'], products) != stamp:
            raise Conflict('门店回传或运营签发依据已更新，请重新分配后再导出：'+p['head']['number'])
    if _target_identity(target) != expected_target:
        raise Conflict('同名导出文件已变化，请刷新后重试：'+p['head']['number'])
    checkpoint(cancel)
    os.replace(tmp, target)


def export_checked(path,gids,folder,progress=lambda _:None,cancel=None):
    """Generation panel and automatic per-order export share the same guard."""
    if not gids:
        raise ValueError('没有可导出的已保存结果')
    receipt = export(path, gids, folder, progress, cancel)
    done = {f['gid'] for f in receipt['files']}
    receipt.update(remaining=[g for g in gids if g not in done], total=len(gids))
    return receipt


def export(path, gids, folder, progress=lambda _:None, cancel=None,*,historical=False):
    """Stage Excel outside SQLite's write lock and publish only current facts."""
    proofs = _preflight(path, gids, historical, cancel)
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    result={'files':[], 'failed':[], 'cancelled':False,'warnings':[]}
    for i,gid in enumerate(gids):
        tmp=None
        number=gid
        try:
            checkpoint(cancel)
            with closing(read_connection(path)) as db:
                p,digest=orders.read_generation(db,gid)
            number=p['head']['number']
            if digest!=proofs[gid][0]:
                raise Conflict('生成记录已变化，请刷新后再导出')
            target=folder/filename(p)
            if historical:target=target.with_stem(target.stem+'_旧版本_'+p['gid'][:12])
            expected_target=_target_identity(target)
            fd,tmp=tempfile.mkstemp(suffix='.xlsx',prefix='.jst-',dir=folder);os.close(fd)
            book=openpyxl.Workbook(write_only=True);sheet=book.create_sheet('订单明细')
            sheet.column_dimensions['A'].width=28;sheet.column_dimensions['B'].width=16;sheet.column_dimensions['C'].width=16
            sheet.freeze_panes='A2'
            sheet.column_dimensions['D'].width=46
            title=[]
            for value in ['商品编码','单价','数量','订单号']:
                cell=WriteOnlyCell(sheet,value);cell.font=Font(bold=True,color='FFFFFF');cell.fill=PatternFill('solid',fgColor='176B65');title.append(cell)
            sheet.append(title)
            for r in p['rows']:
                checkpoint(cancel)
                if Decimal(r['quantity'])<=0:continue
                sku=WriteOnlyCell(sheet,r['sku']);sku.data_type='s';sku.number_format='@'
                price=WriteOnlyCell(sheet,Decimal(r['price']));price.number_format='0.00########'
                qty=WriteOnlyCell(sheet,Decimal(r['quantity']));qty.number_format='0.########'
                number_cell=WriteOnlyCell(sheet,number);number_cell.data_type='s';number_cell.number_format='@'
                sheet.append([sku,price,qty,number_cell])
            book.save(tmp);book.close();checkpoint(cancel)
            with open(tmp,'rb') as stream:sha=hashlib.file_digest(stream,'sha256').hexdigest()
            try:
                with closing(sqlite3.connect(path,timeout=.1)) as db,db:
                    begin(db,cancel)
                    _publish(db,p,proofs[gid],tmp,target,expected_target,historical,cancel)
                    tmp=None
                    result['files'].append({'gid':gid,'path':str(target),'sha256':sha})
                    orders.event(db,p['head'],'导出聚水潭订单',{'gid':gid,'path':str(target),'sha256':sha,**({'historical':True} if historical else {})})
            except sqlite3.Error as exc:
                if tmp is not None:raise
                import logging
                message=f'文件已完成，但导出日志未保存：{target}；{exc}'
                logging.exception(message);result['warnings'].append(message)
            progress(RowProgress(f'已导出 {i+1}/{len(gids)}：'+p['head']['number'],i+1,len(gids)))
        except Cancelled:
            result['cancelled']=True;break
        except Exception as exc:
            message=str(exc)
            result['failed'].append({'gid':gid,'error':message if number in message else number+'：'+message})
        finally:
            if tmp:Path(tmp).unlink(missing_ok=True)
    return result
