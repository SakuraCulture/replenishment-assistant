"""Generate two-store synthetic acceptance DB; refuses an existing output folder."""
import argparse,sqlite3,shutil,sys
from contextlib import closing
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);a=p.parse_args();out=a.out.resolve();out.mkdir(parents=True,exist_ok=False)
    sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
    import test_jst as f,warehouse_catalog as wc
    test=f.Service();test.setUp()
    try:
        test.second_store();wc.Repository(test.path.parent).save(f.catalog(stock=100))
        settings=out/'settings';settings.mkdir()
        with closing(sqlite3.connect(test.path)) as source,closing(sqlite3.connect(settings/'workbench.sqlite3')) as target:source.backup(target)
        shutil.copytree(test.path.parent/'warehouse_catalogs',settings/'warehouse_catalogs')
    finally:test.tearDown()
    print(out)
if __name__=='__main__':main()
