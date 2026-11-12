"""One streaming workbook, with separate store/warehouse report namespaces."""
from pathlib import Path
import uuid
from xlsx_exports import stream_book
from export_files import publish_new
from stockout_common import resource_check
from runtime_support import checkpoint


class PrefixBook:
    def __init__(self,book,prefix):self.book,self.prefix=book,prefix
    def __getattr__(self,key):return getattr(self.book,key)
    def create_sheet(self,title=None,**kwargs):
        name=self.prefix+str(title or kwargs.pop('title','表'));base=name[:31];name=base;n=1
        while name.casefold() in {x.casefold() for x in self.book.sheetnames}:
            n+=1;suffix='_'+str(n);name=base[:31-len(suffix)]+suffix
        return self.book.create_sheet(title=name,**kwargs)


def export(root,args,cancel,progress):
    mode=args['mode']
    if mode not in ('store','warehouse','both'):raise ValueError('导出范围无效')
    target=Path(args['destination']).resolve()
    if target.suffix.lower()!='.xlsx' or target.exists():raise ValueError('请选择尚不存在的.xlsx文件')
    target.parent.mkdir(parents=True,exist_ok=True);resource_check(target.parent)
    temp=target.with_name('.stockout-hub-'+uuid.uuid4().hex+'.xlsx')
    try:
        with stream_book() as book:
            if mode!='warehouse':
                from stockout_repository import Repository
                from stockout_exports import _write
                from stockout_metrics import validate
                from stockout_views import combinations
                repo=Repository(root)
                if not repo.path.exists():raise ValueError('尚无门店分析资料；可单独选择大仓导出')
                combos=[] if args.get('include_detail') and args['store_combinations']==[] else combinations(args['store_combinations'])
                _write(PrefixBook(book,'门店_'),repo,validate(args['store_query']),args.get('include_detail',False),combos,cancel,progress)
            if mode!='store':
                from warehouse_stockout import Repository
                from warehouse_stockout_exports import write_report
                repo=Repository(root)
                if not repo.path.exists():raise ValueError('尚无大仓分析资料；可单独选择门店导出')
                write_report(PrefixBook(book,'大仓_'),repo,args['warehouse_query'],args['warehouse_combinations'],include_detail=args.get('include_detail',False),cancel=cancel,progress=progress)
            checkpoint(cancel);progress('正在保存统一缺货率报表');book.save(temp);checkpoint(cancel);publish_new(temp,target)
    finally:temp.unlink(missing_ok=True)
    return str(target)
