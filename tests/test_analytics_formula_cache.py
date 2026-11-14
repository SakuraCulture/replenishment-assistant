import hashlib
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

import openpyxl
from openpyxl.cell import WriteOnlyCell

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from analytics_formula_cache import FormulaCache
from runtime_support import Cancelled


class FormulaValues(unittest.TestCase):
    def test_packaged_et_xmlfile_empty_cells_do_not_hide_following_formula(self):
        script = '''from pathlib import Path
import sys,tempfile
sys.path.insert(0,str(Path.cwd()/'src'))
import openpyxl
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import PatternFill
from analytics_formula_cache import FormulaCache
assert not openpyxl.LXML
with tempfile.TemporaryDirectory() as folder,FormulaCache(folder) as cache:
 b=openpyxl.Workbook(write_only=True);s=b.create_sheet('表')
 blank=WriteOnlyCell(s,None);blank.fill=PatternFill('solid',fgColor='FFFFFF')
 s.append([blank,'=1+1',blank,'=IF(1=0,0,"n.a.")'])
 cache.add('表','B1',2);cache.add('表','D1','n.a.')
 p=Path(folder)/'et.xlsx';b.save(p);cache.apply(p)
 r=openpyxl.load_workbook(p,read_only=True,data_only=True)
 assert r.active['B1'].value==2 and r.active['D1'].value=='n.a.'
 r.close()
'''
        result=subprocess.run([sys.executable,'-X','utf8','-c',script],cwd=Path(__file__).resolve().parents[1],
                              env={**os.environ,'OPENPYXL_LXML':'False','PYTHONUTF8':'1'},capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_self_closing_cells_are_not_matched_as_nonempty_cells(self):
        from analytics_formula_cache import CELL
        xml=b'<row r="1"><c r="A1" s="2" /><c r="B1"><f>1+1</f><v /></c><c r="C1"/></row>'
        self.assertEqual([m[1] for m in CELL.finditer(xml)],[b'B1'])

    def test_numeric_and_unavailable_cache_formulas_names_and_other_zip_members_preserved(self):
        with tempfile.TemporaryDirectory() as folder, FormulaCache(folder) as cache:
            p=Path(folder)/'book.xlsx'; b=openpyxl.Workbook(write_only=True)
            for title in ('中文&表','Second'):
                s=b.create_sheet(title); s.freeze_panes='C2'
                s.append([2,4,'=A1/B1','=IF(A1=0,"n.a.",A1)'])
                cache.add(title,'C1',.5); cache.add(title,'D1','n.a.')
                literal=WriteOnlyCell(s,'=原商品名称'); literal.data_type='s'; s.append([literal])
            b.save(p)
            with zipfile.ZipFile(p) as z: before={n:z.read(n) for n in z.namelist()}
            cache.apply(p)
            with zipfile.ZipFile(p) as z:
                self.assertIsNone(z.testzip())
                for name,data in before.items():
                    if not name.startswith('xl/worksheets/'): self.assertEqual(z.read(name),data)
            for data_only in (False,True):
                r=openpyxl.load_workbook(p,read_only=True,data_only=data_only)
                try:
                    for s in r:
                        self.assertEqual(s['C1'].value,.5 if data_only else '=A1/B1')
                        self.assertEqual(s['D1'].value,'n.a.' if data_only else '=IF(A1=0,"n.a.",A1)')
                        self.assertEqual(s['A2'].value,'=原商品名称')
                finally:r.close()

    def test_missing_cell_aborts_without_replacing_original(self):
        with tempfile.TemporaryDirectory() as folder, FormulaCache(folder) as cache:
            p=Path(folder)/'book.xlsx';b=openpyxl.Workbook(write_only=True);b.create_sheet('表').append([1]);b.save(p)
            before=p.read_bytes();cache.add('表','C1',3)
            with self.assertRaisesRegex(ValueError,'missing'):cache.apply(p)
            self.assertEqual(p.read_bytes(),before)

    def test_cancel_leaves_original_file_and_cleans_spool(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'book.xlsx';b=openpyxl.Workbook(write_only=True);b.create_sheet('表').append(['=1+1']);b.save(p)
            before=p.read_bytes();event=threading.Event()
            with FormulaCache(folder,cancel=event) as cache:
                cache.add('表','A1',2);event.set()
                with self.assertRaises(Cancelled):cache.apply(p)
            self.assertEqual(p.read_bytes(),before)
            self.assertEqual([v.name for v in Path(folder).iterdir()],['book.xlsx'])

    def test_invalid_or_out_of_order_cache_rejected(self):
        with tempfile.TemporaryDirectory() as folder, FormulaCache(folder) as cache:
            for coordinate,value in [('A0',1),('A1',None),('A1',float('nan'))]:
                with self.assertRaises(ValueError):cache.add('表',coordinate,value)
            cache.add('表','B2',0)
            with self.assertRaisesRegex(ValueError,'row order'):cache.add('表','A1',0)


if __name__=='__main__':unittest.main()
