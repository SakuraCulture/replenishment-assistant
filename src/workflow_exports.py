"""Business-first clean reading layer, reusing the streaming Excel writer."""
from collections import OrderedDict
from openpyxl.chart import LineChart,Reference
from funnel_monitor_export import Sheet,NUMBER,PERCENT
from analytics_identifiers import HEADERS as IDS,cells
import workflow_analysis as wf
from runtime_support import checkpoint

def append(book,report,rows,progress=lambda _:None,cancel=None,*,custom=False):
    options=report.get('_workflow_options',{}) if custom else {}
    rows=wf.selected(rows,options.get('filters'));sheets=[]
    overview=Sheet(book,'漏斗总览',[25,24,24,24,24,24],cancel)
    overview.row(['门店补货全流程经营漏斗'],'title',merge=True,height=36)
    overview.note(f"业务日期 {report['query']['from']} 至 {report['query']['to']}；截至 {report['as_of']}。历史截至只回放订单及单据版本；平台单号为本机当前关联。")
    overview.note('数量、商品款数和签发价货值分开阅读。比率按同一可比明细计算；未导入、缺价、分母0留空。库存实际变化只看独立观测日报。')
    overview.row(['环节','数量（库存单位）','商品款数（去重）','货值（签发价）','数量有效行','待核行'],'header')
    ms={kind:wf.summary(rows,kind) for kind in wf.METRICS}
    for s in wf.STAGES:
        overview.row([wf.NAMES[s],ms['quantity']['stages'][s],ms['skus']['stages'][s],ms['value']['stages'][s],
                      sum(r['scope']=='纳入' and r['valid'].get(s,False) for r in rows),sum(r['scope']=='纳入' and not r['valid'].get(s,False) for r in rows)])
    overview.band('原五段比率与新增分段比率')
    overview.row(['比率','数量比率','款数比率','货值比率','货值可比订单','货值可比商品'],'header')
    for i,name in enumerate(wf.RATIOS):
        overview.row([name,*[ms[k]['pairs'][i]['rate'] for k in ('quantity','skus','value')],ms['value']['pairs'][i]['orders'],ms['value']['pairs'][i]['skus']],formats={1:PERCENT,2:PERCENT,3:PERCENT})
    overview.note('不同库存单位合计仅作执行数量统计；需同单位比较时，在趋势页按库存单位筛选后导出本视角。原五段比率不改为相邻七段连乘。')
    sheets.append('漏斗总览')
    groups=OrderedDict()
    key=options.get('dimension')
    for r in rows:
        group=(r['day'],r['store'],r['store_name'],r['warehouse'],r['group'],str(r.get(key) or '待核') if key else '')
        groups.setdefault(group,[]).append(r)
    kinds=[options.get('metric','value')] if custom else ['quantity','skus','value']
    for kind in kinds:
        name={'quantity':'经营主表','skus':'商品款数','value':'签发价货值'}[kind]
        sheet=Sheet(book,name,[16,24,36,16,16,26]+[18]*7+[20]*4+[18,30,48,38,38],cancel)
        headers=['业务日期','门店编码','门店名称','供货仓','必采属性','自定义切面',*[wf.NAMES[s] for s in wf.STAGES],
                 '回传／签发','运营／回传','付款／回传','收货／建议','本组订单数','资料提示',*IDS]
        sheet.row(headers,'header',height=45);sheet.ws.freeze_panes='G2';sheet.ws.print_title_rows='1:1'
        for group,rs in groups.items():
            checkpoint(cancel);m=wf.summary(rs,kind)
            sheet.row([*group,*[m['stages'][s] for s in wf.STAGES],*[m['pairs'][i]['rate'] for i in (1,5,2,4)],m['orders'],
                f"未纳入{sum(r['scope']!='纳入' for r in rs)}行；未确认不算0",*cells(rs)],formats={i:PERCENT for i in range(13,17)},height=34)
        sheet.ws.auto_filter.ref=f'A1:V{sheet.count}';sheets.append(name)
    grain_names={'day':'日','week':'周','month':'月'}
    grains=[options.get('grain','day')] if custom else list(grain_names)
    dimension=options.get('dimension');grouped={}
    for r in rows:grouped.setdefault(str(r.get(dimension) or '待核') if dimension else '全部',[]).append(r)
    if not grouped:grouped['全部']=[]
    for grain in grains:
        name='漏斗率'+grain_names[grain]+'趋势';sheet=Sheet(book,name,[16,16,24,22,24,24,24,18,18,20,20,20,26],cancel)
        sheet.row(['周期起','周期止','切面分组','指标','比率','分子','分母','比率值','变化(pp)','可比订单','可比商品','可比明细','周期说明'],'header',height=38)
        for group,rs in sorted(grouped.items()):
            for kind in kinds:
                for p in wf.trends(rs,report['query']['from'],report['query']['to'],grain,kind,cancel=cancel):
                    for i,label in enumerate(wf.RATIOS):
                        ratio=p['pairs'][i];sheet.row([p['period'],p['end'],group,wf.METRICS[kind],label,ratio['after'],ratio['before'],ratio['rate'],p['delta_pp'][i],ratio['orders'],ratio['skus'],ratio['rows'],'非完整周期' if p['partial'] else '完整周期'],formats={7:PERCENT})
        sheet.ws.freeze_panes='F2';sheet.ws.auto_filter.ref=f'A1:M{sheet.count}';sheets.append(name)
    name='原五段比率看板';chart_sheet=Sheet(book,name,[18]+[24]*5+[20,20],cancel)
    chart_sheet.row(['周期',*wf.RATIOS[:5],'货值可比订单（收货／建议）','周期说明'],'header',height=42)
    for p in wf.trends(rows,report['query']['from'],report['query']['to'],options.get('grain','day'),'value',cancel=cancel):
        chart_sheet.row([p['period'],*[v['rate'] for v in p['pairs'][:5]],p['pairs'][4]['orders'],'非完整周期' if p['partial'] else '完整周期'],formats={i:PERCENT for i in range(1,6)})
    if chart_sheet.count>1:
        chart=LineChart();chart.title='原五段货值漏斗率随时间变化';chart.y_axis.title='漏斗率';chart.y_axis.numFmt='0%';chart.x_axis.title='业务日期周期'
        chart.add_data(Reference(chart_sheet.ws,min_col=2,max_col=6,min_row=1,max_row=chart_sheet.count),titles_from_data=True)
        chart.set_categories(Reference(chart_sheet.ws,min_col=1,min_row=2,max_row=chart_sheet.count));chart.display_blanks='gap';chart.width=30;chart.height=13;chart_sheet.ws.add_chart(chart,'A'+str(chart_sheet.count+3))
    sheets.append(name)
    name='动作日完成量';actions=Sheet(book,name,[18,24,24],cancel);actions.row(['实际保存日期','环节','有效订单数'],'header')
    for r in wf.action_trends(rows):actions.row([r['day'],wf.NAMES[r['stage']],r['orders']])
    actions.note('动作日期为当前有效版本的保存时间；与业务日期漏斗分开统计。未单独留存模型计算保存时间，不用签发时间代替；旧修订不计作新订单。');sheets.append(name)
    stock=report.get('linked_stockout');name='缺货率观测趋势';sheet=Sheet(book,name,[18,18,28,20,20,20,20,32],cancel)
    sheet.row(['观测对象','观测日期','范围','缺货数','统计基数','缺货率','指标待核','说明'],'header')
    if stock:
        for title,key in [('门店','store'),('大仓','warehouse')]:
            for row in stock[key]:
                m=row.get('total',row);sheet.row([title,row['day'],row.get('warehouse','已选门店'),m['zero'],m['base'],m['rate'],m.get('bad',m.get('pending',0)),'当前发布日报'],formats={5:PERCENT})
        sheet.note(stock['basis']);sheet.note('；'.join(stock['notes']))
    else:sheet.note('尚未点击“联看留存缺货率趋势”；未自动读取分析库，不以订单推算实际库存或缺货率。')
    sheets.append(name);progress('全流程与漏斗率趋势已生成');return sheets
