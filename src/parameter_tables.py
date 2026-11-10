"""Classified V2 parameter tables. Legacy files retain their original blank contract."""
import copy
import os
import tempfile
from pathlib import Path
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment
import parameter_schema as schema
import store_parameter_import as legacy
from app_defaults import WAREHOUSES

MARKER='参数模板信息'
FORMAT='补货助手分类参数/2'
FORMAT_SCOPED='补货助手分类参数/3'
SHEETS={
 '分仓周期':['门店编码','门店名称','属仓','备货天数','到货天数'],
 '门店通用参数':['门店编码','门店名称']+[n for k,(n,_,_) in schema.HELP.items() if k not in legacy.FIXED],
 '分类参数':['门店编码','门店名称','分类组']+list(legacy.WEIGHTS.values())+[schema.HELP[k][0] for k in schema.PROFILE_KEYS],
 '分类映射':['门店编码','门店名称','商品分类','目标分类组'],
 '补货策略':['门店编码','门店名称','启用一箱例外']+list(legacy.policy.LABELS.values()),
 '供货关系':['门店编码','门店名称','主仓','从仓'],
 '门店预算':['门店编码','门店名称','预算方式','预算金额'],
}
KINDS={'warehouse_cycles':['分仓周期'], 'classified_parameters':['分仓周期','分类参数'],
       'common_parameters':['门店通用参数'],
       'store_policy':['补货策略'],'store_routes':['供货关系'],'store_budget':['门店预算']}
KINDS['complete_parameters']=list(SHEETS)

def numeric(value, key):
    if isinstance(value,str) and value.endswith('%'):
        if key not in ('probability','w1','w2'):raise ValueError('此参数不是百分比')
        return legacy.number(value[:-1])/100
    return legacy.number(value)

def parse_book(wb,path,cancel,scoped=False):
    standard=schema.standard(); rows=[];seen={}
    for ws in wb:
        if ws.title not in SHEETS:continue
        ws.reset_dimensions(); header=None
        for rn,cells in enumerate(ws.iter_rows(),1):
            legacy.checkpoint(cancel)
            if header is None:
                if rn>20:break
                names=[str(c.value or '').strip() for c in cells]
                if '门店编码' not in names:continue
                if len([h for h in names if h])!=len(set(h for h in names if h)):raise ValueError(f'{ws.title}：重复表头')
                unknown=set(names)-set(SHEETS[ws.title])-{''}
                if unknown:raise ValueError(f'{ws.title}：不支持的字段 {"、".join(sorted(unknown))}')
                header=names;continue
            if all(c.value in (None,'') for c in cells):continue
            source=f'{Path(path).name} / {ws.title} / 第{rn}行'
            try:
                if any(c.value not in (None,'') and (i>=len(header) or not header[i]) for i,c in enumerate(cells)):raise ValueError('数据列缺少表头')
                if any(c.data_type in ('f','e') for c in cells):raise ValueError('不能使用公式或Excel错误值，请粘贴为值')
                raw={h:cells[i].value if i<len(cells) else None for i,h in enumerate(header) if h}
                code=legacy.code_value(cells[header.index('门店编码')]);values={};reset=[]
                def required(name):
                    v=str(raw.get(name) or '').strip()
                    if not v:raise ValueError(name+'必填')
                    return v
                group=required('属仓') if ws.title=='分仓周期' else required('分类组') if ws.title=='分类参数' else required('商品分类') if ws.title=='分类映射' else ''
                if ws.title=='分仓周期' and group not in WAREHOUSES:raise ValueError('属仓须为山东仓或河北仓')
                for h,v in raw.items():
                    if h in ('门店编码','门店名称','属仓','分类组','商品分类'):continue
                    blank=v is None or isinstance(v,str) and not v.strip()
                    if scoped and blank:continue
                    if scoped and v=='系统默认':blank=True
                    if scoped and ws.title=='分类映射':raise ValueError('分类映射请从全局分类映射入口维护，不再按门店导入')
                    if ws.title=='分仓周期':
                        idx=('备货天数','到货天数').index(h);f=('warehouses',group,idx);v=standard['warehouses'][group][idx] if blank else legacy.number(v)
                    elif ws.title=='分类参数':
                        k=next((k for k,n in legacy.WEIGHTS.items() if n==h),None)
                        if k:
                            f=('weight_profiles',group,k)
                            if blank and group not in standard['weight_profiles']:raise ValueError('此分类没有系统默认权重，请填写完整权重')
                            v=standard['weight_profiles'][group][k] if blank else numeric(v,k)
                        else:
                            k=next(k for k in schema.PROFILE_KEYS if schema.HELP[k][0]==h);f=('profile_parameters',group,k);v='沿用门店' if blank else numeric(v,k)
                    elif ws.title=='分类映射':f=('category_profiles',group);v=standard['category_profiles'].get(group,'清空') if blank else str(v).strip()
                    else:
                        f=legacy.field(h)
                        if f[0]=='parameters':v=standard['parameters'][f[1]] if blank else numeric(v,f[1])
                        elif blank:continue  # Non-parameter templates explicitly keep existing blank semantics.
                    values[f]=v
                    if blank:reset.append(h)
                key=(code,ws.title,group)
                if key in seen:
                    if seen[key]['values']!=values:raise ValueError('同一业务键参数冲突，首次在'+seen[key]['source'])
                    seen[key]['duplicates'].append(source);continue
                row={'code':code,'name':str(raw.get('门店名称') or '').strip(),'values':values,'source':source,'reset_fields':reset,'duplicates':[]}
                seen[key]=row;rows.append(row)
            except (ValueError,StopIteration,IndexError) as exc:raise ValueError(source+'：'+str(exc)) from exc
    if not rows:raise ValueError(Path(path).name+'：没有填写可导入的门店记录')
    return rows

def read(paths,progress=lambda _:None,cancel=None):
    allrows=[];sources={};formats=[]
    for path in dict.fromkeys(str(Path(p).resolve()) for p in paths):
        legacy.checkpoint(cancel);before=legacy.digest(path);progress('读取参数：'+Path(path).name)
        wb=load_workbook(path,read_only=True,data_only=False)
        try:
            marker=wb[MARKER]['A1'].value if MARKER in wb.sheetnames else None
            if marker is not None and marker not in (FORMAT,FORMAT_SCOPED):raise ValueError('参数模板版本无法识别，请使用本版模板')
            if marker in (FORMAT,FORMAT_SCOPED):
                rows=parse_book(wb,path,cancel,marker==FORMAT_SCOPED);formats.append('分组参数V3：空白保持，填写系统默认可恢复' if marker==FORMAT_SCOPED else '分类参数V2：可选参数空白恢复系统默认')
            else:
                old=legacy.read_legacy([path],progress,cancel,_book=wb,_before=before)
                rows=old['rows'];formats.append('旧模板：空白保持原值')
        finally:wb.close()
        if marker in (FORMAT,FORMAT_SCOPED) and before!=legacy.digest(path):raise ValueError('读取期间文件变化，请保存后重试：'+Path(path).name)
        allrows+=rows;sources[path]=before
    if all(f.startswith('旧模板') for f in formats):
        # Keep legacy duplicate rejection across files, including identical rows.
        codes=[r['code'] for r in allrows]
        if len(codes)!=len(set(codes)):raise ValueError('门店编码重复：旧模板每店只能一行')
    merged={}
    for row in allrows:
        if row['code'] not in merged:merged[row['code']]=copy.deepcopy(row);continue
        old=merged[row['code']]
        if old['name'] and row['name'] and old['name']!=row['name']:raise ValueError('同码不同门店名称：'+row['source'])
        if any(k in old['values'] and old['values'][k]!=v for k,v in row['values'].items()):raise ValueError('多表或多文件参数冲突：'+row['source']+'；首次在'+old['source'])
        old['values'].update(row['values']);old['name']=old['name'] or row['name'];old['source']+='；'+row['source']
        old.setdefault('reset_fields',[]).extend(row.get('reset_fields',[]))
    return {'rows':list(merged.values()),'sources':sources,'formats':list(dict.fromkeys(formats)),**({'scoped':True} if any(f.startswith('分组参数V3') for f in formats) else {})}

def write(path,kind='warehouse_cycles',records=None,progress=lambda _:None,cancel=None,*,scoped=False):
    """Application template generator; same Excel library as existing exports."""
    target=Path(path)
    if target.exists():raise FileExistsError('目标已存在，请另存为新文件')
    wb=Workbook();wb.remove(wb.active)
    titles=['分仓周期','门店通用参数','分类参数','分类映射'] if kind=='classified_parameters' and not scoped else KINDS[kind]
    for title in titles:
        ws=wb.create_sheet(title);headers=SHEETS[title];ws.append(headers);ws.freeze_panes='C2'
        for code,record in (records or {}).items():
            legacy.checkpoint(cancel);c=record['preset']['config'];ui=record['ui'];common={'门店编码':code,'门店名称':record.get('name','')}
            if title=='分仓周期':rows=[{**common,'属仓':w,'备货天数':v[0],'到货天数':v[1]} for w,v in c['warehouses'].items()]
            elif title=='门店通用参数':rows=[{**common,**{schema.HELP[k][0]:v for k,v in c['parameters'].items() if k not in legacy.FIXED}}]
            elif title=='分类参数':rows=[{**common,'分类组':g,**{n:weights[k] for k,n in legacy.WEIGHTS.items()},**{schema.HELP[k][0]:c.get('profile_parameters',{}).get(g,{}).get(k,'系统默认' if scoped else None) for k in schema.PROFILE_KEYS}} for g,weights in c['weight_profiles'].items()]
            elif title=='分类映射':rows=[{**common,'商品分类':k,'目标分类组':v} for k,v in c['category_profiles'].items()]
            elif title=='补货策略':rows=[{**common,'启用一箱例外':'是' if c['exception'] else '否',**{legacy.policy.LABELS[k]:'开启' if v else '关闭' for k,v in legacy.policy.values(c).items()}}]
            elif title=='供货关系':rows=[{**common,'主仓':ui.get('primary') or '未指定','从仓':ui.get('secondary') or '无从仓'}]
            else:rows=[{**common,'预算方式':('未指定','不限预算','限定预算')[ui.get('mode',0)],'预算金额':float(ui['amount']) if ui.get('mode')==2 else None}]
            for row in rows:ws.append([row.get(h) for h in headers])
        for cell in ws[1]:
            cell.font=Font(bold=True,color='FFFFFF');cell.fill=PatternFill('solid',fgColor='126C63');cell.alignment=Alignment(wrap_text=True);ws.column_dimensions[cell.column_letter].width=24
        ws.row_dimensions[1].height=45
        for row in ws:
            for cell in row:
                if isinstance(cell.value,str):cell.data_type='s'
        for rn in range(2,max(ws.max_row+1,102)):ws.cell(rn,1).number_format='@'
        from parameter_template_help import decorate
        decorate(ws,headers,scoped=scoped,groups=schema.standard()['weight_profiles'])
    info=wb.create_sheet('填写说明')
    for pair in [('事项','说明'),('必填','门店编码；分仓周期另填属仓，分类参数另填分类组，分类映射另填商品分类。其他字段选填。'),('空白参数','已有列的空白恢复系统默认；分类专用参数空白沿用门店通用值；缺列、缺行不修改。0是实际值。'),('其他设置','供货关系、预算、策略空白保持；这些设置使用独立模板，不随周期模板改变。'),('编码','按文本填写原始编码，不能使用公式；列顺序可调整。'),('概率和权重','可填90%或0.9。分类权重两仓共用；属仓只维护周期，不启用供货。'),('确认范围','核对差异后才应用；本次作业和长期默认分别选择。')]:info.append(pair)
    info.column_dimensions['A'].width=20;info.column_dimensions['B'].width=100
    if scoped:
        info.cell(3,2,'空白、缺列、缺行保持原值；填写“系统默认”明确恢复。分类专用参数填“系统默认”表示沿用门店通用值；0是实际数值。')
        info.append(['模板范围','完整门店参数仅分仓周期和分类参数；门店通用参数另表维护；分类映射全局集中维护。'])
        if kind=='complete_parameters':info.cell(info.max_row,2,'本文件包含全部门店默认：周期、通用参数、分类参数、分类映射、补货策略、供货关系与预算；辅助清单页用于查阅，不自动写回。')
    marker=wb.create_sheet(MARKER);marker.append([FORMAT_SCOPED if scoped else FORMAT]);marker.sheet_state='hidden'
    from parameter_template_help import instructions
    instructions(info)
    fd,tmp=tempfile.mkstemp(dir=target.parent,suffix='.xlsx');os.close(fd)
    try:wb.save(tmp);legacy.checkpoint(cancel);os.link(tmp,target)
    finally:wb.close();Path(tmp).unlink(missing_ok=True)
    return str(target)
