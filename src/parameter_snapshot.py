"""Full current parameter export plus preset/list evidence, without source writes."""
from pathlib import Path
import os,tempfile,uuid,shutil
from runtime_support import checkpoint

def export(work,destination,progress=lambda _:None,cancel=None):
    from parameter_tables import write
    from priority_list import Repository
    from parameter_schema import HELP
    from xlsx_exports import stream_book
    import openpyxl
    from export_files import publish_new
    target=Path(destination).resolve()
    if target.exists() or target.suffix.lower()!='.xlsx':raise ValueError('请选择尚不存在的.xlsx文件')
    target.parent.mkdir(parents=True,exist_ok=True);records=work.stores();presets=work.presets(include_archived=True);profile=Repository(work.path.parent).profile()
    with tempfile.TemporaryDirectory(prefix='parameters-',dir=target.parent) as temp:
        file=Path(temp)/'parameters.xlsx';write(file,'complete_parameters',records,progress,cancel,scoped=True)
        book=openpyxl.load_workbook(file)
        try:
            ws=book.create_sheet('预案版本与完整参数');ws.append(['预案编号','版本','预案名称','已归档','参数分组','参数名','值'])
            def visit(value,path=()):
                if isinstance(value,dict):
                    for key,v in value.items():yield from visit(v,path+(str(key),))
                elif isinstance(value,(list,tuple)):
                    for index,v in enumerate(value,1):yield from visit(v,path+(str(index),))
                else:yield ('／'.join(path[:-1]),HELP[path[-1]][0] if path[-1] in HELP else path[-1],value)
            for preset in presets:
                checkpoint(cancel)
                for group,name,value in visit(preset['config']):ws.append([preset['id'],preset['version'],preset['name'],bool(preset.get('archived')),group,name,value])
            ws=book.create_sheet('当前必卖清单');ws.append(['商品编码','商品名称','分类','清单名称','清单版本'])
            for item in profile['items']:checkpoint(cancel);ws.append([item['sku'],item.get('name',''),item.get('category',''),profile['name'],profile['revision']])
            ws=book['填写说明'];ws.append(['导出范围',f'全部已保存默认 {len(records)} 店；预案版本 {len(presets)} 份。未保存的本次界面修改不在此表中。'])
            ws.append(['辅助页','预案版本和当前必卖清单为只读核对依据；完整迁移请使用订单迁移包，不把本表当作完整订单备份。'])
            for sheet in book:
                sheet.freeze_panes='C2'
                if sheet.sheet_state!='visible':continue
                sheet.auto_filter.ref=sheet.dimensions
                for row in sheet:
                    for cell in row:
                        if isinstance(cell.value,str):cell.data_type='s'
            book.save(file);checkpoint(cancel)
            published=target.with_name('.parameters-'+uuid.uuid4().hex+'.xlsx')
            try:shutil.copyfile(file,published);checkpoint(cancel);publish_new(published,target)
            finally:published.unlink(missing_ok=True)
        finally:book.close()
    return str(target)
