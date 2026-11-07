"""Reset is exercised only in unique temporary directories."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import application_reset as reset


class ResetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'app-data';self.root.mkdir()
        for name in ('settings/parameters.json','settings/aoxiang/intent.json','analysis/raw.xlsx','app.log'):
            p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(('原资料-'+name).encode())
        with closing(sqlite3.connect(self.root/'settings/workbench.sqlite3')) as db:
            db.execute('CREATE TABLE t(v TEXT)');db.execute("INSERT INTO t VALUES('业务记录')");db.commit()
        self.probe=patch.object(reset,'_check_other_processes');self.probe.start();self.addCleanup(self.probe.stop)

    def content(self,folder):return {p.relative_to(folder).as_posix():p.read_bytes() for p in folder.rglob('*') if p.is_file()}

    def test_complete_backup_includes_settings_platform_identity_and_analysis(self):
        before=self.content(self.root);value=reset.plan(self.root);result=reset.perform(value)
        self.assertEqual(result['state'],'COMPLETED');self.assertEqual(self.content(Path(value['backup'])),before)
        self.assertEqual(set(self.content(self.root)),{'初始化完成.json'})
        self.assertIsNone(reset.recover(self.root));self.assertFalse(reset._paths(self.root)[0].exists())

    def test_multiple_instances_allowed_but_reset_exclusive(self):
        a=reset.InstanceLease(self.root);b=reset.InstanceLease(self.root)
        try:
            with self.assertRaisesRegex(ValueError,'另一个'):reset.perform(reset.plan(self.root))
            a.close()
            with self.assertRaisesRegex(ValueError,'另一个'):reset.perform(reset.plan(self.root))
        finally:a.close();b.close()
        reset.perform(reset.plan(self.root))

    def test_old_instance_blocks_before_backup(self):
        value=reset.plan(self.root)
        with patch.object(reset,'_check_other_processes',side_effect=ValueError('其他窗口')):
            with self.assertRaisesRegex(ValueError,'其他窗口'):reset.perform(value)
        self.assertTrue((self.root/'app.log').exists());self.assertFalse(Path(value['backup']).exists())

    def test_busy_writer_keeps_original(self):
        db=sqlite3.connect(self.root/'settings/workbench.sqlite3');db.execute('BEGIN IMMEDIATE')
        value=reset.plan(self.root)
        try:
            with self.assertRaisesRegex(ValueError,'占用'):reset.perform(value)
        finally:db.close()
        self.assertFalse(Path(value['backup']).exists());self.assertTrue((self.root/'app.log').exists())

    def test_failed_commit_restores_original(self):
        before=self.content(self.root);value=reset.plan(self.root);write=reset._write
        def fail(path,data):
            if path.name=='初始化完成.json':raise OSError('模拟磁盘写入失败')
            return write(path,data)
        with patch.object(reset,'_write',fail), self.assertRaises(OSError):reset.perform(value)
        self.assertEqual(self.content(self.root),before);self.assertFalse(Path(value['backup']).exists())

    def test_interrupted_after_rename_restores_before_open(self):
        value=reset.plan(self.root);before=self.content(self.root)
        reset._write(reset._paths(self.root)[0],value);self.root.rename(value['backup'])
        self.assertEqual(reset.recover(self.root),'ROLLED_BACK');self.assertEqual(self.content(self.root),before)

    def test_interrupted_before_rename_leaves_original(self):
        value=reset.plan(self.root);before=self.content(self.root)
        reset._write(reset._paths(self.root)[0],value)
        self.assertEqual(reset.recover(self.root),'UNCHANGED');self.assertEqual(self.content(self.root),before)

    def test_interrupted_after_commit_recognizes_receipt(self):
        value=reset.plan(self.root);reset.perform(value);reset._write(reset._paths(self.root)[0],value)
        self.assertEqual(reset.recover(self.root),'COMPLETED');self.assertTrue(Path(value['backup']).is_dir())

    def test_ambiguous_new_content_never_overwritten(self):
        value=reset.plan(self.root);reset._write(reset._paths(self.root)[0],value);self.root.rename(value['backup'])
        self.root.mkdir();(self.root/'new.txt').write_text('新数据')
        with self.assertRaisesRegex(ValueError,'均已保留'):reset.recover(self.root)
        self.assertEqual((self.root/'new.txt').read_text(),'新数据');self.assertTrue((Path(value['backup'])/'app.log').exists())

    def test_reject_unsafe_paths_and_changed_destination(self):
        for p in (Path.home(),Path(self.root.anchor),Path(__file__).resolve().parents[1]):
            with self.assertRaises(ValueError):reset.plan(p)
        value=reset.plan(self.root);value['backup']=str(self.root/'nested')
        with self.assertRaises(ValueError):reset.perform(value)
        value=reset.plan(self.root);Path(value['backup']).mkdir()
        with self.assertRaises(ValueError):reset.perform(value)

    def test_repeated_reset_keeps_each_previous_backup(self):
        first=reset.perform(reset.plan(self.root));(self.root/'new.txt').write_text('第二轮')
        second=reset.perform(reset.plan(self.root))
        self.assertNotEqual(first['backup'],second['backup']);self.assertTrue((Path(first['backup'])/'app.log').exists())
        self.assertEqual((Path(second['backup'])/'new.txt').read_text(),'第二轮')


if __name__=='__main__':unittest.main()
