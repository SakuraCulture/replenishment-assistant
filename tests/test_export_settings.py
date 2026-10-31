"""Portable preference persistence checks; no Qt or business input needed."""
import json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import export_settings as settings

class ExportSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.config=self.root/'settings'/settings.FILENAME;self.target=self.root/'门店 导出';self.target.mkdir()
    def tearDown(self):self.tmp.cleanup()
    def test_first_read_has_no_write(self):
        self.assertEqual(settings.load(self.config),'');self.assertFalse(self.config.parent.exists())
    def test_save_reload_unicode_and_clear(self):
        settings.save(self.config,str(self.target));self.assertEqual(settings.load(self.config),str(self.target.resolve()));self.assertEqual(json.loads(self.config.read_text(encoding='utf-8')),{'schema':1,'default_export_directory':str(self.target.resolve())});self.assertEqual(list(self.target.iterdir()),[])
        settings.save(self.config,'');self.assertEqual(settings.load(self.config),'');self.assertTrue(self.target.exists())
    def test_invalid_directory_keeps_previous(self):
        settings.save(self.config,str(self.target));before=self.config.read_bytes();file=self.root/'file';file.write_text('original')
        for path in ('relative',str(self.root/'missing'),str(file)):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):settings.save(self.config,path)
                self.assertEqual(self.config.read_bytes(),before)
    def test_unwritable_directory_rejected(self):
        settings.save(self.config,str(self.target));before=self.config.read_bytes()
        with patch('export_settings.tempfile.TemporaryFile',side_effect=PermissionError('denied')):
            with self.assertRaisesRegex(ValueError,'不可写'):settings.save(self.config,str(self.target))
        self.assertEqual(self.config.read_bytes(),before)
    def test_replace_failure_keeps_config_and_cleans_temp(self):
        settings.save(self.config,str(self.target));before=self.config.read_bytes()
        with patch('export_settings.os.replace',side_effect=PermissionError('locked')):
            with self.assertRaises(PermissionError):settings.save(self.config,'')
        self.assertEqual(self.config.read_bytes(),before);self.assertEqual(list(self.config.parent.iterdir()),[self.config])
    def test_corrupt_and_unknown_schema_not_rewritten(self):
        self.config.parent.mkdir()
        for value in ('not json','[]','{"schema":2,"default_export_directory":""}','{"schema":true,"default_export_directory":""}','{"schema":1,"default_export_directory":"relative"}'):
            self.config.write_text(value,encoding='utf-8');before=self.config.read_bytes()
            with self.assertRaises(ValueError):settings.load(self.config)
            self.assertEqual(self.config.read_bytes(),before)
    def test_removed_folder_does_not_destroy_preference(self):
        settings.save(self.config,str(self.target));self.target.rmdir();saved=settings.load(self.config)
        with self.assertRaisesRegex(ValueError,'不存在'):settings.validate(saved)
        self.assertEqual(saved,settings.load(self.config))
    def test_other_config_files_untouched(self):
        self.config.parent.mkdir();other=self.config.parent/'workbench.json';other.write_bytes(b'{"preserved":true}')
        settings.save(self.config,str(self.target));self.assertEqual(other.read_bytes(),b'{"preserved":true}')

if __name__=='__main__':unittest.main()
