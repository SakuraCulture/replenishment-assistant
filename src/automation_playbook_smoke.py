"""Replay business recipes over the real stdio MCP path; no LLM-success claims."""
import copy
import json
from unittest.mock import patch
from PySide6.QtCore import QMimeData
from PySide6.QtWidgets import QApplication
from automation_playbooks import PLAYBOOKS, render
from automation_playbook_ui import show_playbooks


def exercise(app, win, client, out, wait, require):
    from parameter_workspace_smoke import fixture
    import batch_service
    import automation_jobs
    sample = fixture(); originals = sample['normalized']; sample['normalized'] = []
    for data, metadata in originals:
        for index in range(125):
            row, meta = copy.deepcopy(data), copy.deepcopy(metadata)
            row.update(E=str(1001 + index), D='合成测试商品' + str(index + 1))
            if index == 1: row['S'] = '禁用'
            meta['export_row'] = index + 2
            sample['normalized'].append((row, meta))
    win.reset_job(); win.loaded_ok(sample)
    for code in ('A', 'B'): win.store_widgets[code]['mode'].setCurrentIndex(1)
    win.concurrency.setValue(1)
    g = win.automation
    clipboard_backup = QMimeData()
    clipboard_data = QApplication.clipboard().mimeData()
    if clipboard_data:
        for fmt in clipboard_data.formats(): clipboard_backup.setData(fmt, clipboard_data.data(fmt))
    report = {'passed': False, 'transport': 'actual stdio MCP to Qt workbench', 'data': 'synthetic A/B, 125 SKUs each including a disabled item; no production data',
              'natural_language_model_runs': 0, 'cases': [], 'ui': [], 'transcript': []}
    def call(name, args=None, error=False):
        value = client.call(name, args, error)
        report['transcript'].append({'tool': name, 'arguments': args or {}, 'result': value})
        return value
    def finish(tid):
        wait(lambda: g.worker is None, 60)
        return call('task_status', {'task_id': tid})
    def prompt(case, **values):
        text = render(case, call('workbench_context'), values)
        (out / ('prompt-' + case + '.txt')).write_text(text, 'utf-8')
        return text
    def calculate(key, stores, changes=None):
        args = dict(context_token=call('workbench_context')['context_token'], operation_key=key, mode='trial', stores=stores)
        if changes: args['parameter_changes'] = changes
        p = call('prepare_calculation', args); require(finish(p['task_id'])['state'] == 'prepared', 'Preparation failed')
        task = call('run_calculation', {'preview_id': p['task_id']}); require(finish(task['task_id'])['state'] == 'completed', 'Calculation failed')
        prepared = g.prepared[p['task_id']]
        for code in stores:
            expected = batch_service.calculate_store(win.loaded, code, prepared['jobs'][code], win.profile)
            actual = g.require_result(task['task_id'])['results'][code]
            require(actual['rows'] == expected['rows'] and actual['summary'] == expected['summary'], 'Recipe changes original business calculation')
        return task['task_id']
    def record(case, result): report['cases'].append({'case': case, 'passed': True, **result})
    before = copy.deepcopy(win.snapshot())
    try:
        prompt('check', stores=['B', 'A'])
        for kind in ('files', 'catalogs'): call('find_resources', {'kind': kind, 'limit': 100})
        params = call('effective_parameters', {'stores': ['B', 'A']})
        require([r['store'] for r in params['stores']] == ['B', 'A'], 'Parameters reordered')
        record('check', {'stores': ['B', 'A'], 'unchanged': win.snapshot() == before})
        prompt('calculate', stores=['B', 'A']); baseline = calculate('recipe-standard', ['B', 'A'])
        summary = call('calculation_results', {'task_id': baseline})
        require([r['store'] for r in summary['stores']] == ['B', 'A'], 'Store order differs')
        require(win.snapshot() == before, 'Trial mutated current workspace')
        pages = []; offset = 0
        while offset is not None:
            page = call('calculation_results', {'task_id': baseline, 'store': 'A', 'offset': offset, 'limit': 100})
            pages.extend(page['items']); offset = page['next_offset']
        require(len(pages) == 125 and [r['sku'] for r in pages] == [str(1001 + i) for i in range(125)], 'Pagination omitted or reordered products')
        record('calculate', {'summary': summary, 'exact_business_differences': 0, 'paged_products': len(pages)})
        prompt('review', task_id=baseline)
        for code in ('B', 'A'):
            issues = call('calculation_results', {'task_id': baseline, 'store': code, 'filter': 'issues', 'limit': 100})
            require(any(x['sku'] == '1002' for x in issues['items']), 'Disabled product absent from review')
        record('review', {'order': ['B', 'A'], 'read_only': True})
        prompt('explain', task_id=baseline, stores=['A'], warehouse='山东仓', sku='1001')
        explanation = call('explain_decision', {'task_id': baseline, 'store': 'A', 'warehouse': '山东仓', 'sku': '1001'})
        require(explanation['calculation'], 'Missing calculation evidence'); record('explain', {'explanation': explanation})
        prompt('compare', stores=['A'], warehouse='山东仓', days_a=5, days_b=7)
        comparisons = []
        for days in (5, 7):
            tid = calculate('recipe-days-' + str(days), ['A'], [{'store': 'A', 'path': ['warehouses', '山东仓', 0], 'value': days}])
            items = []; offset = 0
            while offset is not None:
                page = call('calculation_results', {'task_id': tid, 'store': 'A', 'limit': 100, 'offset': offset})
                items.extend(page['items']); offset = page['next_offset']
            comparisons.append({'days': days, 'task_id': tid, 'result': {'items': items, 'stores': page['stores']}})
        require(comparisons[0]['result']['items'] != comparisons[1]['result']['items'], 'Parameter trial did not actually affect result')
        require(win.snapshot() == before, 'Comparison changed current or other store')
        record('compare', {'trials': comparisons, 'exact_business_differences': 0, 'other_store_unchanged': True})
        dialog = show_playbooks(win); app.processEvents()
        require(not dialog.isModal() and QApplication.activeModalWidget() is None, 'Recipes block MCP with modal window')
        dialog.cases.setCurrentRow(0); dialog.copy_prompt()
        require('开工检查' in QApplication.clipboard().text(), 'Copy failed')
        require('context_token' not in dialog.preview.toPlainText() and 'context_token' in QApplication.clipboard().text(), 'Business preview leaks protocol or copy omits binding')
        dialog.grab().save(str(out / 'AI常用业务.png'))
        call('effective_parameters', {'stores': ['A']})
        dialog.cases.setCurrentRow(2)
        require(not dialog.copy_button.isEnabled(), 'Comparison silently accepted two stores or an unspecified warehouse')
        dialog.scope.setCurrentIndex(1); dialog.codes.setText('A')
        dialog.warehouse.setCurrentIndex(dialog.warehouse.findData('山东仓'))
        require(dialog.copy_button.isEnabled(), dialog.hint.text())
        dialog.copy_prompt(); require('"stores": [\n    "A"' in QApplication.clipboard().text(), 'Single-store scope not copied')
        dialog.warehouse.setCurrentIndex(dialog.warehouse.findData('河北仓'))
        require(not dialog.copy_button.isEnabled(), 'Non-supplying warehouse allowed')
        dialog.cases.setCurrentRow(0); dialog.scope.setCurrentIndex(0); dialog.copy_prompt()
        saved_clipboard = QApplication.clipboard().text()
        win.store_widgets['A']['amount'].setText('123')
        dialog.copy_prompt(); require(QApplication.clipboard().text() == saved_clipboard and not dialog.copy_button.isEnabled(), 'Old context copied')
        win.store_widgets['A']['amount'].setText(''); dialog.reload()
        report['ui'].extend(['Modeless window permits real MCP requests', 'Copy binds exact state', 'Business preview hides protocol fields', 'Comparison requires one exact store and its supplying warehouse', 'Manual changes reject stale copy'])
        # Explicit cancellation and retry use the same real stdio path as the recipes.
        p = call('prepare_calculation', dict(context_token=g.token(), operation_key='recipe-stop', mode='trial', stores=['B', 'A']))
        finish(p['task_id']); t = call('run_calculation', {'preview_id': p['task_id']})
        prompt('stop', task_id=t['task_id']); call('stop_remaining', {'task_id': t['task_id']})
        stopped = finish(t['task_id']); require(any(s['state'] == '已停止' for s in stopped['stores']), 'No queued store stopped')
        record('stop', {'states': stopped['stores']})
        prompt('retry', task_id=t['task_id'])
        retry_codes = [s['code'] for s in stopped['stores'] if s['state'] in ('已停止', '计算失败')]
        r = call('retry_failed', {'task_id': t['task_id'], 'stores': retry_codes, 'operation_key': 'recipe-retry'})
        require(finish(r['task_id'])['state'] == 'completed', 'Retry failed')
        require(call('task_status', {'task_id': t['task_id']})['state'] == 'completed', 'Retry did not join root')
        record('retry', {'retried': retry_codes, 'root_order': [s['store'] for s in call('calculation_results', {'task_id': t['task_id']})['stores']]})
        # Exercise a genuine failure state (injected worker failure), then real model retry.
        p = call('prepare_calculation', dict(context_token=g.token(), operation_key='recipe-failure', mode='trial', stores=['B', 'A']))
        finish(p['task_id'])
        def failed(prepared, codes, notify, cancel):
            for code in codes: notify(code, '计算失败', None, '合成瞬时失败，用于验证原批次重试')
        with patch.object(automation_jobs, 'calculate', failed):
            ft = call('run_calculation', {'preview_id': p['task_id']}); finish(ft['task_id'])
        retry = call('retry_failed', {'task_id': ft['task_id'], 'stores': ['B', 'A'], 'operation_key': 'recipe-failure-retry'})
        require(finish(retry['task_id'])['state'] == 'completed', 'Failed-store retry did not recover')
        # Adopt only an explicitly chosen finished trial; no formal save/sign.
        tid = ft['task_id']; prompt('adopt', task_id=tid, reviewed=True)
        dialog.reload(); dialog.cases.setCurrentRow(8); dialog.task.setCurrentIndex(dialog.task.findData(tid))
        require(not dialog.copy_button.isEnabled(), 'Adopt lacks explicit confirmation')
        dialog.reviewed.setChecked(True); require(dialog.copy_button.isEnabled(), dialog.hint.text())
        dialog.copy_prompt(True); require(not dialog.isVisible(), 'Copy-and-return did not close')
        defaults = copy.deepcopy(win.work_repository.stores())
        old_jobs = win.work_repository.list_jobs(); old_orders = win.orders.headers()
        adopted = call('adopt_trial', {'task_id': tid, 'context_token': g.token()})
        require(adopted['adopted'] and list(win.results) == ['B', 'A'], 'Adoption wrong')
        require(defaults == win.work_repository.stores() and old_jobs == win.work_repository.list_jobs() and old_orders == win.orders.headers(), 'Formal state changed')
        record('adopt', {'adopted': True, 'saved_defaults_drafts_orders': False})
        # Historical order is created by QA, not the AI interface.
        previous = next((h['revision'] for h in old_orders if h['store'] == 'A' and h['day'] == win.job_day), None)
        expected_job = next((j['revision'] for j in old_jobs if j['day'] == win.job_day), None)
        orders = win.orders.confirm(win.snapshot(), ['A'], expected_job, {'A': previous})
        from archive_orders import number
        order_id = number(orders[0]['number'], '山东仓')
        prompt('history', order_id=order_id, warehouse='山东仓', sku='1001')
        hist = call('explain_decision', {'order_id': order_id, 'warehouse': '山东仓', 'sku': '1001'})
        explained = finish(hist['task_id'])['explanation']
        require(explained['order']['revision'] == orders[0]['revision'], 'Wrong history revision')
        record('history', {'order_id': order_id, 'revision': explained['order']['revision']})
        require({x['case'] for x in report['cases']} == {x[0] for x in PLAYBOOKS}, 'Case coverage incomplete')
        report['passed'] = True
    finally:
        QApplication.clipboard().setMimeData(clipboard_backup)
        (out / 'playbook-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    return report
