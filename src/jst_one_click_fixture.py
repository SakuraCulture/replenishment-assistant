"""Isolated platform replies for one-click tests and frozen GUI acceptance."""
from base64 import b64decode
from decimal import Decimal
from hashlib import sha256

import openpyxl

import catalog_sync_validation
from catalog_sync_client import ComponentError
import warehouse_catalog as catalogs


WAREHOUSES = ('河北仓', '山东仓')
SKUS = dict(zip(WAREHOUSES, ('0001', '0002')))


class Component:
    """Test transport for the real sync/adoption and order/payment services."""

    def __init__(self, folder):
        self.folder = folder
        self.running = False
        self.calls = []
        self.batches = {}
        self.orders = {}
        self.permits = {}
        self.stock = dict(zip(WAREHOUSES, (17, 12)))
        self.no_stock_receipt = set()
        self.missing_preview = set()
        self.drop_import_response = False
        self.after_sync = lambda: None
        self.accounts = [
            {'id': wh, 'warehouse': wh, 'expectedCompany': wh+'测试企业', 'expectedCoid': coid,
             'expectedUid': uid, 'mappingVerified': True, 'bindingVersion': 7, 'enabled': True}
            for wh, coid, uid in zip(WAREHOUSES, ('1', '2'), ('3', '4'))
        ]

    def acquire(self):
        if self.running:
            raise AssertionError('already in use')
        return self

    def start(self):
        self.running = True

    def close(self, *, timeout):
        self.running = False
        return True

    def request(self, action, *, spec=None, batch_id=None, **args):
        if not self.running:
            raise AssertionError('missing session')
        if action.startswith('order.'):
            self.calls.append((action, spec['warehouse'] if spec else args['warehouse'],
                               spec['online'] if spec else args['online']))
            return self.order(action, spec, args)
        if action == 'sync.start':
            self.calls.append((action, tuple(i['warehouse'] for i in args['items'])))
            batch_id = args['operation_id']
            selected, = args['items']
            wh = selected['warehouse']
            account = next(a for a in self.accounts if a['warehouse'] == wh)
            self.batches[batch_id] = {
                'id': batch_id, 'purpose': args['purpose'], 'items': [
                    {'id': 'item', 'stage': 'FAILED' if wh in self.no_stock_receipt else 'WAIT_HOST',
                     'observationRequest': 'observe', 'account': account.copy(), **selected}]}
            return {'batch_id': batch_id}
        if action == 'sync.status':
            batch = self.batches.get(batch_id)
            active = bool(batch and batch['items'][0]['stage'] == 'WAIT_HOST')
            return {'batches': [batch] if batch else [], 'running': active, 'active': active, 'accounts': self.accounts}
        batch = self.batches[batch_id]
        item, = batch['items']
        if action == 'sync.provide_observation':
            wh = item['warehouse']
            dest = self.folder/'downloads'/batch_id/item['id']
            dest.mkdir(parents=True)
            source = dest/'库存.xlsx'
            book = openpyxl.Workbook()
            book.active.append(catalogs.REQUIRED+['商品标签', '商品名称'])
            book.active.append([SKUS[wh], '测试', 2, 1, '启用', self.stock[wh], '', '合成商品'])
            book.save(source)
            book.close()
            sha = catalog_sync_validation.digest(source)
            receipt = catalog_sync_validation.validate({
                'schema': 1, 'warehouse': wh, 'file': str(source), 'sha256': sha,
                'expected_rows': 1, 'candidate': str(dest/'candidate.json')})
            item.update(stage='VALIDATED', observation=args['observation'], receipt=receipt,
                        native={'sha256': sha, 'nativeTaskRef': 'fixture', 'scope': {
                            'coid': item['account']['expectedCoid'], 'uid': item['account']['expectedUid']}})
        elif action == 'sync.acknowledge_adoption':
            item.update(stage='APPLIED', adoption=args['receipt'])
            self.after_sync()
        elif action == 'sync.record_adoption_error':
            item['adoption_error'] = args['message']
        else:
            raise AssertionError(action)
        return {}

    def order(self, action, spec, args):
        if action == 'order.import':
            online = args['online']
            if online in self.orders:
                raise AssertionError('duplicate import')
            self.orders[online] = {**self.permits.pop(args['permit']), 'internal': str(17001+len(self.orders))}
            if self.drop_import_response:
                raise ComponentError('RESPONSE_UNKNOWN', '合成断线')
            return {}
        wh, online = spec['warehouse'], spec['online']
        if action in ('order.preview', 'order.stage_import'):
            if online in self.orders:
                return {'exists': True}
            if sha256(b64decode(spec['file'])).hexdigest() != spec['sha']:
                raise AssertionError('wrong workbook')
            if wh in self.missing_preview:
                raise ComponentError('PREVIEW_CHANGED', '合成平台预览未通过')
            account = next(a for a in self.accounts if a['warehouse'] == wh)
            binding = {'warehouse': wh, 'shop': spec['shop'], 'company': account['expectedCompany'],
                       'coid': account['expectedCoid'], 'uid': account['expectedUid'],
                       'version': account['bindingVersion'], 'shop_id': '22' if wh == '河北仓' else '44'}
            rows = [dict(sku=sku, qty=qty, price=price, amount=str(Decimal(qty)*Decimal(price)))
                    for sku, qty, price in spec['lines']]
            amount = str(sum(Decimal(row['amount']) for row in rows))
            facts = dict(binding=binding, contact_ok=True, buyer_check='verified', online=online,
                         native_status='已付款待审核', status='awaiting', amount=amount,
                         items=rows, detail_count=len(rows), currency='CNY', freight='0')
            if action == 'order.stage_import':
                self.permits[online] = facts
            return {**facts, 'lines': spec['lines'], 'permit': online}
        if action in ('order.lookup', 'order.payment_detail'):
            return {**self.orders[online], 'found': True} if online in self.orders else {'found': False}
        raise AssertionError(action)
