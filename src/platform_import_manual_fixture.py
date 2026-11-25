"""Controlled original-order replies shared by the manual import acceptance tests."""
from decimal import Decimal

from jst_one_click_fixture import Component


class JstManual(Component):
    def binding(self, spec):
        wh = spec['warehouse']
        account = next(a for a in self.accounts if a['warehouse']==wh)
        return {'warehouse':wh,'shop':spec['shop'],'company':account['expectedCompany'],
                'coid':account['expectedCoid'],'uid':account['expectedUid'],'version':account['bindingVersion'],
                'shop_id':'22' if wh=='河北仓' else '44'}

    def accepted(self, task, internal, *, changes=False):
        p=task['payload']
        binding=self.binding({'warehouse':p['head']['warehouse'],'shop':p['shop']['name']})
        goods=[{'sku':sku,'qty':qty,'price':price,'amount':str(Decimal(qty)*Decimal(price))}
               for sku,qty,price in p['lines']]
        if changes:
            goods[0]['qty']=str(Decimal(goods[0]['qty'])-1)
            goods[0]['amount']=str(Decimal(goods[0]['qty'])*Decimal(goods[0]['price']))
            goods.append({'sku':'@00000001','qty':'2','price':'3','amount':'6'})
        self.orders[task['online']]={'binding':binding,'contact_ok':True,'buyer_check':'verified',
            'online':task['online'],'internal':internal,'native_status':'已付款待审核','status':'awaiting',
            'amount':str(sum(Decimal(row['amount']) for row in goods)),'items':goods,
            'detail_count':len(goods),'currency':'CNY','freight':'0'}

    def order(self, action, spec, args):
        if action=='order.probe':
            return {'binding':self.binding(spec)}
        if action=='order.lookup' and spec['online'] not in self.orders:
            return {'found':False,'binding':self.binding(spec),'online':spec['online'],'exact_absence':True}
        return super().order(action,spec,args)
