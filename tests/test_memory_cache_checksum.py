"""Same derived-cache bytes and full integrity checks with bounded hash buffers."""
import datetime,hashlib,json,sqlite3,tempfile,unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
import history_read as history
import work_repository as wr


class MemoryCacheChecksumTests(unittest.TestCase):
    def test_exact_utf8_concatenation_at_chunk_boundaries(self):
        for values in [[],[''],['','中文','😀'],['a'*65535+'中😀','末'*65537],['空\n白', '\\u1234', '0', 'false']]:
            with self.subTest(lengths=list(map(len,values))):
                self.assertEqual(history.projection_checksum(values),hashlib.sha256(''.join(values).encode()).hexdigest())

    def test_hash_update_never_receives_the_whole_large_value(self):
        original=hashlib.sha256;lengths=[]
        class Hash:
            def __init__(self):self.hash=original()
            def update(self,value):lengths.append(len(value));self.hash.update(value)
            def hexdigest(self):return self.hash.hexdigest()
        values=['😀'*200000,'中'*140000,'x'*70000]
        with patch.object(history.hashlib,'sha256',Hash):actual=history.projection_checksum(values)
        self.assertEqual(actual,original(''.join(values).encode()).hexdigest())
        self.assertGreater(len(lengths),3);self.assertLessEqual(max(lengths),65536*4)

    def test_put_keeps_existing_schema_strings_and_checksum(self):
        with tempfile.TemporaryDirectory() as folder:
            index=history.Index(Path(folder)/'cache.sqlite3')
            record={'b':False,'n':None,'a':0.0,'d':datetime.date(2026,11,13)}
            view={'search':['0001 中文','😀'],'rows':[['0001',0,False,None,0.0]],'head':{'x':1}}
            index.put('key',record,view)
            expected=[wr.dumps(record),json.dumps(view['search'],ensure_ascii=False),json.dumps(view,ensure_ascii=False)]
            with closing(index.connect()) as db:
                row=db.execute('SELECT summary,search,projection,checksum FROM views').fetchone()
                columns=[r[1] for r in db.execute('PRAGMA table_info(views)')]
            self.assertEqual(row,(*expected,hashlib.sha256(''.join(expected).encode()).hexdigest()))
            self.assertEqual(columns,['k','summary','search','projection','checksum'])
            self.assertEqual(index.get('key',True),{'record':wr.loads(expected[0]),'search':view['search'],'projection':view})

    def test_old_cache_is_readable_and_unrequested_projection_still_checked(self):
        with tempfile.TemporaryDirectory() as folder:
            index=history.Index(Path(folder)/'cache.sqlite3');values=[wr.dumps({'n':None,'false':False}),json.dumps(['首','次']),json.dumps({'rows':[[1]]})]
            with closing(index.connect()) as db,db:db.execute('INSERT INTO views VALUES(?,?,?,?,?)',('old',*values,hashlib.sha256(''.join(values).encode()).hexdigest()))
            self.assertEqual(index.get('old',False),{'record':wr.loads(values[0]),'search':['首','次'],'projection':None})
            with closing(index.connect()) as db,db:db.execute('UPDATE views SET projection=? WHERE k=?',('changed','old'))
            self.assertIsNone(index.get('old',False));self.assertIsNone(index.get('old',True))

    def test_malformed_json_behavior_is_not_relaxed(self):
        with tempfile.TemporaryDirectory() as folder:
            index=history.Index(Path(folder)/'cache.sqlite3')
            for position in range(3):
                values=['{}','[]','{}'];values[position]='{invalid'
                with closing(index.connect()) as db,db:db.execute('INSERT OR REPLACE INTO views VALUES(?,?,?,?,?)',('bad',*values,hashlib.sha256(''.join(values).encode()).hexdigest()))
                self.assertIsNone(index.get('bad',True))
                if position<2:self.assertIsNone(index.get('bad',False))
                else:self.assertEqual(index.get('bad',False),{'record':{},'search':[],'projection':None})


if __name__=='__main__':unittest.main()
