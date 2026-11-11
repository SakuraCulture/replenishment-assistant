import tempfile,unittest
from pathlib import Path
import openpyxl
import work_repository as wr
import parameter_schema as schema
import parameter_tables
from parameter_snapshot import export
import store_parameter_import as spi

class ParameterSnapshotTests(unittest.TestCase):
    def test_all_business_tables_presets_and_list_without_source_write(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);work=wr.Repository(root/'settings');record=spi.base();record['name']='测试门店';record['ui'].update(primary='山东仓',secondary='河北仓',mode=2,amount=5000)
            record['preset']['config']['parameters']['minimum_reference_units']=2;work.save_stores({'0001':record});work.save_preset('保底2',record['preset']['config'])
            before=work.path.read_bytes();target=root/'parameters.xlsx';export(work,target)
            b=openpyxl.load_workbook(target,data_only=True)
            try:
                for title in parameter_tables.COMPLETE_EDITABLE:self.assertIn(title,b.sheetnames)
                self.assertIn(parameter_tables.MAPPING_REFERENCE,b.sheetnames)
                self.assertNotIn('分类映射',b.sheetnames)
                self.assertIn('预案版本与完整参数',b.sheetnames);self.assertIn('当前必卖清单',b.sheetnames)
                rows=list(b['供货关系'].values);self.assertEqual(rows[1],('0001','测试门店','山东仓','河北仓'))
                self.assertEqual(b['门店预算']['D2'].value,5000)
                self.assertEqual(b[parameter_tables.MARKER]['A1'].value,parameter_tables.FORMAT_SCOPED)
            finally:b.close()
            self.assertEqual(work.path.read_bytes(),before)
            data=spi.read([target]);self.assertTrue(data['notices'])
            self.assertEqual(spi.plan(data,work.stores())['errors'],[])
            with self.assertRaises(ValueError):export(work,target)


if __name__=='__main__':unittest.main()
