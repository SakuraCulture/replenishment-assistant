import tempfile,unittest
from pathlib import Path
from application_processes import classify,wrapper_ids,source_identity

class ProcessTests(unittest.TestCase):
    def test_only_real_entrypoints_not_text_inside_python_code(self):
        self.assertEqual(classify(['python.exe','-c','text="D:/project/app.py"'],'python.exe'),{'application':False,'main':False})
        self.assertTrue(classify(['python.exe','D:/project/app.py'],'python.exe')['main'])
        self.assertFalse(classify(['python.exe','D:/project/app.py','--storage-copy'],'python.exe')['main'])
        self.assertTrue(classify(['补货助手.exe'],'补货助手.exe')['main'])
        self.assertFalse(classify(['补货助手.exe','--multiprocessing-fork'],'补货助手.exe')['main'])
        self.assertFalse(classify(['python.exe','tests.py','--input','D:/project/app.py'],'python.exe')['application'])
        self.assertTrue(classify(['python.exe','-m','app'],'python.exe')['main'])
        self.assertFalse(classify(['python.exe','-m','unittest'],'python.exe')['application'])
    def test_own_launcher_not_a_second_gui_but_independent_and_real_parent_are(self):
        rows=[{'pid':1,'parent':9,'argv':['venv/python.exe','app.py']},{'pid':2,'parent':1,'argv':['base/python.exe','app.py']},{'pid':3,'parent':9,'argv':['python.exe','app.py']}]
        self.assertEqual(wrapper_ids(rows,2,{}),{1})
        self.assertEqual(wrapper_ids(rows,2,{1:100}),set())
        rows[0]['argv']=['python.exe','different.py'];self.assertEqual(wrapper_ids(rows,2,{}),set())
    def test_another_projects_app_py_is_not_this_application(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);script=root/'app.py';script.write_text('')
            self.assertFalse(source_identity(['python.exe',str(script)]))
            (root/'version.json').write_text('{"app":"补货助手"}',encoding='utf-8')
            self.assertTrue(source_identity(['python.exe',str(script)]))
            self.assertFalse(source_identity(['python.exe','-c',str(script)]))
