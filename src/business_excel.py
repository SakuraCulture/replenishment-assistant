"""Small shared business workbook writer; no Qt or calculation side effects."""
import os,tempfile
from pathlib import Path
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from runtime_support import checkpoint
from export_progress import RowProgress

def write(path,sheets,cancel=None,progress=lambda _:None):
    path=Path(path)
    if path.exists():raise FileExistsError('目标文件已存在，不覆盖：'+str(path))
    fd,tmp=tempfile.mkstemp(suffix='.xlsx',prefix='.business-',dir=path.parent);os.close(fd)
    book=Workbook(write_only=True)
    try:
        for title,headers,rows in sheets:
            ws=book.create_sheet(title[:31]);ws.freeze_panes='A2';ws.append(headers)
            for i,row in enumerate(rows,1):
                checkpoint(cancel);cells=[]
                for value in row:
                    cell=WriteOnlyCell(ws,value)
                    if isinstance(value,str):cell.data_type='s'
                    cells.append(cell)
                ws.append(cells)
                if i%256==0:progress(f'{title}：已写{i}行')
        progress('保存Excel…');book.save(tmp);checkpoint(cancel);os.link(tmp,path)
    finally:
        book.close();Path(tmp).unlink(missing_ok=True)
    return str(path)
