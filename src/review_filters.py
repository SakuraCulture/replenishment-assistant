"""Presentation-only predicates shared by final-list, detail and new selection."""
import result_semantics as sem
import new_products

GROUPS=['全部工作商品','动销补货','本月新品','往期新品','其他工作商品','新品归期待核']
KINDS={'manual':'人工改量','new':'新品补足','plan':'计划补足','promotion':'活动补足'}

def search_text(d,extra=''):
    return ' '.join(str(d.get(k) or '') for k in ('D','E','F')).lower()+' '+str(d.get('_meta',{}).get('product_tags') or '').lower()+' '+extra.lower()

def matches(text,needle):return all(word in text for word in needle.lower().split())

def group(d,focused=(),known=True):
    if d.get('_source')!='资料匹配' or not sem.work_eligible(d):return '其他工作商品'
    if d.get('_preissue',{}).get('current_month'):return '本月新品'
    if new_products.is_new(d):return ('本月新品' if d['E'] in focused else '往期新品') if known else '新品归期待核'
    return '动销补货' if sem.orderable(d) else '其他工作商品'

def budget_state(d):
    if not d.get('_budget_active'):return '未启用预算'
    if d.get('_budget_gap',0)>0:return '部分安排' if sem.numeric(sem.order_quantity(d)) and sem.order_quantity(d)>0 else '预算暂缓'
    return '无预算缺口'

def accepts(d,values,focused=(),known=True):
    g=values.get('group','全部工作商品')
    if g!='全部工作商品' and group(d,focused,known)!=g:return False
    q=sem.order_quantity(d);v=values.get('quantity','全部数量')
    if v=='正数量' and not (sem.numeric(q) and q>0):return False
    if v=='零数量' and q!=0:return False
    if v=='数量待核' and sem.numeric(q):return False
    v=values.get('adjustment','全部来源')
    if v!='全部来源' and KINDS.get(d.get('_preissue',{}).get('kind'),'自动安排')!=v:return False
    v=values.get('budget','全部预算状态')
    if v!='全部预算状态' and budget_state(d)!=v:return False
    if values.get('must')=='仅必卖' and not d.get('_must_sell'):return False
    return True

def adjust_counts(out,d,values,focused,known,delta):
    keys=('group','quantity','adjustment','budget','must')
    defaults={'group':'全部工作商品','quantity':'全部数量','adjustment':'全部来源','budget':'全部预算状态','must':'全部优先级'}
    q=sem.order_quantity(d);actual={'group':group(d,focused,known),'quantity':'数量待核' if not sem.numeric(q) else '正数量' if q>0 else '零数量' if q==0 else '',
        'adjustment':KINDS.get(d.get('_preissue',{}).get('kind'),'自动安排'),'budget':budget_state(d),'must':'仅必卖' if d.get('_must_sell') else ''}
    failed=[k for k in keys if values.get(k,defaults[k]) not in (defaults[k],actual[k])]
    for k in keys:
        if any(x!=k for x in failed):continue
        out[k]['all']+=delta;out[k][actual[k]]=out[k].get(actual[k],0)+delta

def facet_counts(rows,values,focused=(),known=True):
    out={k:{'all':0} for k in ('group','quantity','adjustment','budget','must')}
    for d in rows:adjust_counts(out,d,values,focused,known,1)
    return out

class FacetCounter:
    def __init__(self,values,focused,known):
        self.values=values;self.focused=focused;self.known=known;self.rows={};self.counts=facet_counts([],values)
    def update(self,key,row,allowed):
        old=self.rows.pop(key,None)
        if old is not None:adjust_counts(self.counts,old,self.values,self.focused,self.known,-1)
        if allowed:self.rows[key]=row;adjust_counts(self.counts,row,self.values,self.focused,self.known,1)
