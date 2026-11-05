"""Input guidance only: preserve headers, row positions and parser semantics."""
from openpyxl.comments import Comment
from openpyxl.styles import Font
from openpyxl.worksheet.datavalidation import DataValidation


def decorate(ws, headers, *, scoped, groups):
    required={'门店编码'} | ({'属仓'} if ws.title=='分仓周期' else {'分类组'} if ws.title=='分类参数' else set())
    options={'属仓':['山东仓','河北仓'],'主仓':['山东仓','河北仓','未指定'],
             '从仓':['山东仓','河北仓','无从仓'],'预算方式':['不限预算','限定预算','未指定'],
             '启用一箱例外':['是','否'],'极端缺货自动恢复':['开启','关闭'],'新品铺设':['开启','关闭'],
             '分类组':list(groups)}
    blank='空白不修改；填写系统默认可恢复参数。' if scoped else '本模板数值参数空白恢复默认；预算、仓和开关空白不修改。'
    hints={'门店编码':'必填。按文本填写精确编码，保留前导零。',
           '门店名称':'选填；填写时须与已有编码对应名称一致。',
           '属仓':'必填。下拉选择；只维护此仓周期，不改变供货关系。',
           '分类组':'必填。可下拉选择，也可填写门店方案已有分类组。',
           '预算方式':'选填。不限预算/限定预算/未指定；空白保持。只填金额会自动选择限定预算。',
           '预算金额':'条件必填：限定预算需有效金额；留空保留已有金额，无已有金额会报错。0元是不安排。不限预算或未指定时勿填金额。',
           '主仓':'选填。空白不修改；未指定可清除。',
           '从仓':'选填。空白不修改；无从仓明确清除。'}
    for i,h in enumerate(headers,1):
        cell=ws.cell(1,i);hint=hints.get(h,('选填。空白不修改；从下拉选择。' if h in options else '选填。'+blank+' 0为实际数值。'))
        cell.comment=Comment(hint,'补货助手')
        if h in required:cell.font=Font(bold=True,color='FFF1A8')
        col=cell.column_letter
        choices=options.get(h)
        dv=DataValidation(type='list' if choices else 'custom',formula1='"'+','.join(choices)+'"' if choices else 'TRUE',allow_blank=True)
        dv.showInputMessage=True;dv.promptTitle='必填' if h in required else '选填 / 填写提示';dv.prompt=hint[:255]
        dv.showDropDown=False
        dv.showErrorMessage=bool(choices);dv.errorTitle='请核对填写选项';dv.error='请选择下拉选项。空白不会当作0；导入时仍将校验。'
        dv.errorStyle='warning' if h=='分类组' else 'stop'
        if h=='分类组':dv.error='自定义分类须已存在于门店参数方案中，导入时会核对。'
        ws.add_data_validation(dv);dv.add(f'{col}2:{col}{max(10001,ws.max_row)}')


def instructions(info):
    for row in [('填写提示','黄色字表头为必填；点击数据单元格可看提示，选项列可下拉。空白不代表0。'),
                ('预算示例','限定预算 + 3000：每店3000元；只填3000也可。不限预算：金额留空。0元：明确不安排。'),
                ('限定预算留空','有已保存金额则保留；没有有效金额会提示门店和来源行，不自动补0。'),
                ('推荐操作','统一开关、预算或供货仓可直接在软件“批量设置”完成；各店不同数值再使用Excel。')]:info.append(row)
