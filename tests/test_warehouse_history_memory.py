"""Only directory metadata stays live; restore still reads the exact full version."""
import copy,gc,json,os,tempfile,threading,unittest,weakref
from pathlib import Path
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import warehouse_catalog as catalogs
from runtime_support import Cancelled


def catalog(warehouse='山东仓',version=0,count=3):
    items={f'{i:08d}':{'商品编码':f'{i:08d}','商品名称':'合成商品😀'+str(i),'分类':'合成类目',
          '基本售价':1.25,'出库箱规':1,'商品状态':'启用','可用数':i+version,'商品标签':''} for i in range(count)}
    return {'schema':1,'warehouse':warehouse,'imported_at':f'2026-11-{version+1:02d}T09:00:00',
            'source_path':'synthetic.xlsx','source_sha256':'a'*64,'items':items,'revision':catalogs.revision(items),
            'extra':{'must_keep':[None,False,0,0.0,'首','次']}}


def seed(folder,versions=4,count=3):
    repository=catalogs.Repository(folder)
    for wh in catalogs.WAREHOUSES:
        target=Path(folder)/'warehouse_catalogs/history'/wh;target.mkdir(parents=True)
        for version in range(versions):
            path=target/f'{version}.json';path.write_text(json.dumps(catalog(wh,version,count),ensure_ascii=False),'utf-8')
            os.utime(path,ns=(1000000000+version,1000000000+version))
    return repository


class WarehouseHistoryMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name);self.repo=seed(self.folder)

    def test_same_order_metadata_and_complete_selected_values(self):
        for wh in catalogs.WAREHOUSES:
            old=self.repo.history(wh);entries=self.repo.history_entries(wh)
            self.assertEqual([e['metadata'] for e in entries],[{'imported_at':c['imported_at'],'revision':c['revision'],'count':len(c['items'])} for c in old])
            self.assertTrue(all('items' not in e and 'items' not in e['metadata'] for e in entries))
            before={p:p.read_bytes() for p in (self.folder/'warehouse_catalogs/history'/wh).glob('*.json')}
            for entry,expected in zip(entries,old):
                self.assertEqual(json.dumps(self.repo.read_history_entry(entry),ensure_ascii=False),json.dumps(expected,ensure_ascii=False))
            self.assertEqual({p:p.read_bytes() for p in before},before)

    def test_previous_catalog_is_not_held_while_loading_next(self):
        validate=catalogs.validate;refs=[];alive=[]
        class Tracked(dict):pass
        def checked(value):
            gc.collect();alive.append(sum(ref() is not None for ref in refs))
            obj=Tracked(validate(value));refs.append(weakref.ref(obj));return obj
        with patch.object(catalogs,'validate',side_effect=checked):entries=self.repo.history_entries('山东仓')
        gc.collect();self.assertEqual(alive,[0,0,0,0]);self.assertTrue(all(r() is None for r in refs))
        self.assertEqual(len(entries),4)

    def test_empty_unknown_and_cancel_keep_existing_validation(self):
        self.assertEqual(catalogs.Repository(self.folder/'empty').history_entries('山东仓'),[])
        for method in (self.repo.history,self.repo.history_entries):
            with self.assertRaisesRegex(ValueError,'未知仓库'):method('other')
        event=threading.Event();event.set()
        with self.assertRaises(Cancelled):self.repo.history_entries('山东仓',event)
        entry=self.repo.history_entries('山东仓')[0]
        with self.assertRaises(Cancelled):self.repo.read_history_entry(entry,event)

    def test_windows_unsigned_file_identity_roundtrips_qt_without_overflow(self):
        from types import SimpleNamespace
        value=catalogs._history_identity(SimpleNamespace(st_dev=2**64-1,st_ino=2**127-1,st_size=3,st_mtime_ns=1760000000000000000,st_ctime_ns=1760000000000000001))
        self.assertTrue(all(isinstance(v,str) for v in value))
        self.assertEqual(int(value[0]),2**64-1);self.assertEqual(int(value[1]),2**127-1)

    def test_corrupt_unselected_history_still_rejects_the_directory(self):
        bad=self.folder/'warehouse_catalogs/history/山东仓/bad.json';bad.write_text('{broken','utf-8')
        for method in (self.repo.history,self.repo.history_entries):
            with self.assertRaises(json.JSONDecodeError):method('山东仓')

    def test_replaced_deleted_and_metadata_changed_do_not_choose_another(self):
        for mode in ('replace','delete','metadata'):
            with self.subTest(mode=mode):
                entry=self.repo.history_entries('山东仓')[0];path=Path(entry['file']);raw=path.read_bytes()
                try:
                    if mode=='replace':path.write_text(json.dumps(catalog('山东仓',8)),'utf-8')
                    elif mode=='delete':path.unlink()
                    else:entry=copy.deepcopy(entry);entry['metadata']['revision']='another'
                    with self.assertRaisesRegex(ValueError,'变化|移除'):self.repo.read_history_entry(entry)
                finally:path.write_bytes(raw)

    def test_changed_digest_is_rejected_even_with_current_file_identity(self):
        entry=self.repo.history_entries('山东仓')[0];entry['sha256']='0'*64
        with self.assertRaisesRegex(ValueError,'变化'):self.repo.read_history_entry(entry)

    def test_foreign_folder_is_not_restored(self):
        entry=self.repo.history_entries('山东仓')[0];outside=self.folder/'foreign.json';outside.write_bytes(Path(entry['file']).read_bytes());entry['file']=str(outside)
        with self.assertRaisesRegex(ValueError,'位置不符'):self.repo.read_history_entry(entry)

    def test_file_change_during_validation_is_rejected(self):
        entry=self.repo.history_entries('山东仓')[0];path=Path(entry['file']);validate=catalogs.validate
        def changed(value):
            result=validate(value);path.write_bytes(path.read_bytes()+b' ');return result
        with patch.object(catalogs,'validate',side_effect=changed),self.assertRaisesRegex(ValueError,'读取期间变化'):
            self.repo.read_history_entry(entry)


class WarehouseHistoryUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])

    def test_real_restore_dialog_keeps_only_metadata_and_reads_chosen_version(self):
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication,QComboBox,QWidget
        from warehouse_ui import WarehouseUi
        with tempfile.TemporaryDirectory() as folder:
            repo=seed(folder)
            class Host(WarehouseUi,QWidget):
                def __init__(self):QWidget.__init__(self);self.worker=None;self.readonly=False;self.catalog_repository=repo;self.restored=None;self.error=None
                def catalog_imported(self,value):self.restored=value
                def failed(self,value):self.error=value
            host=Host();chosen=[]
            def select():
                dialog=QApplication.activeModalWidget();combo=dialog.findChild(QComboBox)
                for i in range(combo.count()):self.assertNotIn('items',combo.itemData(i))
                combo.setCurrentIndex(2);chosen.append(combo.currentData());dialog.accept()
            QTimer.singleShot(0,select);host.restore_catalog()
            self.assertEqual(host.restored,repo.read_history_entry(chosen[0]));self.assertIsNone(host.error);host.close()


if __name__=='__main__':unittest.main()
