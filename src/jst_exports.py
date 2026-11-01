"""Exact-generation, three-column exports. Never allocate on export."""
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
import jst_orders as orders
from runtime_support import checkpoint, Cancelled
from export_progress import RowProgress


def filename(p):
    h=p['head'];name=f"{h['day']}_{h.get('name',h['store'])}_{h['warehouse']}_{h['number']}_聚水潭订单_{p['position']:03d}.xlsx"
    return re.sub(r'[<>:"/\\|?*]', '_', name)


def export(path, gids, folder, progress=lambda _:None, cancel=None):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    result={'files':[], 'failed':[], 'cancelled':False}
    for i,gid in enumerate(gids):
        tmp=None
        try:
            checkpoint(cancel);p=orders.get(path,gid,cancel=cancel);target=folder/filename(p)
            fd,tmp=tempfile.mkstemp(suffix='.xlsx',prefix='.jst-',dir=folder);os.close(fd)
            book=openpyxl.Workbook(write_only=True);sheet=book.create_sheet('订单明细')
            sheet.column_dimensions['A'].width=28;sheet.column_dimensions['B'].width=16;sheet.column_dimensions['C'].width=16
            sheet.freeze_panes='A2'
            title=[]
            for value in ['商品编码','单价','数量']:
                cell=WriteOnlyCell(sheet,value);cell.font=Font(bold=True,color='FFFFFF');cell.fill=PatternFill('solid',fgColor='176B65');title.append(cell)
            sheet.append(title)
            for r in p['rows']:
                checkpoint(cancel)
                if Decimal(r['quantity'])<=0:continue
                sku=WriteOnlyCell(sheet,r['sku']);sku.data_type='s';sku.number_format='@'
                price=WriteOnlyCell(sheet,Decimal(r['price']));price.number_format='0.00########'
                qty=WriteOnlyCell(sheet,Decimal(r['quantity']));qty.number_format='0.########'
                sheet.append([sku,price,qty])
            book.save(tmp);book.close();checkpoint(cancel)
            with open(tmp,'rb') as stream:sha=hashlib.file_digest(stream,'sha256').hexdigest()
            os.replace(tmp,target);tmp=None
            with closing(sqlite3.connect(path,timeout=.1)) as db,db:
                # File is already written: record that fact even if cancellation just arrived.
                orders.lc.begin(db,None);orders.event(db,p['head'],'导出聚水潭订单',{'gid':gid,'path':str(target),'sha256':sha})
            result['files'].append({'gid':gid,'path':str(target),'sha256':sha})
            progress(RowProgress(f'已导出 {i+1}/{len(gids)}：'+p['head']['number'],i+1,len(gids)))
        except Cancelled:
            result['cancelled']=True;break
        except Exception as exc:result['failed'].append({'gid':gid,'error':str(exc)})
        finally:
            if tmp:Path(tmp).unlink(missing_ok=True)
    return result
