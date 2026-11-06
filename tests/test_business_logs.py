import copy,hashlib,sqlite3,sys,tempfile,unittest,zlib
from contextlib import closing
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import business_log_views as view
import work_repository as wr
import normal_orders as no


class BusinessLogTests(unittest.TestCase):
    def test_names_bound_to_exact_revision_not_current_store_catalog(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'qa.sqlite3'
            with closing(sqlite3.connect(path)) as db,db:
                db.executescript('CREATE TABLE normal_orders(oid,day,store,current); CREATE TABLE normal_revisions(oid,revision,payload);')
                db.execute('INSERT INTO normal_orders VALUES(?,?,?,?)',('o','2026-11-02','001',2))
                for revision,name in [(1,'当时店名'),(2,'后来店名')]:
                    db.execute('INSERT INTO normal_revisions VALUES(?,?,?)',('o',revision,no.encode({'result':{'store_name':name}},db)))
            before=hashlib.sha256(path.read_bytes()).hexdigest()
            records=[{'oid':'o','revision':1,'store':'001'},{'day':'2026-11-02','store':'001'},{'oid':'o','revision':1,'store':'001'}]
            original=copy.deepcopy(records);view.attach_names(path,records)
            self.assertEqual([r['display_name'] for r in records],['当时店名','后来店名','当时店名'])
            self.assertEqual([{k:v for k,v in r.items() if k!='display_name'} for r in records],original)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),before)
    def test_unknown_ids_and_paths_kept_in_technical_details(self):
        detail={'summary':{'quantity':0,'amount':None},'gid':'generation-id','path':'C:/a.xlsx','future_internal':{'keep':''}}
        before=copy.deepcopy(detail);business=view.detail_rows(detail);technical=view.detail_rows(detail,True)
        self.assertEqual(len(business),2);self.assertEqual(business[0][1],'0');self.assertEqual(business[1][1],'未提供')
        self.assertTrue(any('future_internal / keep' in r[3] and r[1]=='空白' for r in technical))
        self.assertTrue(any(r[1]=='generation-id' for r in technical));self.assertEqual(detail,before)
    def test_summary_is_bounded_full_values_remain_available(self):
        detail={'reason':'原因'*300,'quantity':123,'amount':456}
        self.assertLessEqual(len(view.summary(detail)),111)
        self.assertEqual(view.detail_rows(detail)[0][1],'原因'*300)


if __name__=='__main__':unittest.main()
