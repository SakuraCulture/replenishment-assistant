"""Actual frozen windows and Node host, local-only queries; no browser connection."""
import json
from aoxiang_setup_ui import SetupDialog
from aoxiang_fetch_ui import FetchDialog
from aoxiang_workflow_ui import ReceiptSelection
from aoxiang_purchase_ui import PurchaseDialog


def exercise_shared(win, output, wait, active, checks, business=False):
    factories = [('setup', lambda: SetupDialog(win, win.repository.folder)),
                 ('fetch', lambda: FetchDialog(win, win.repository.folder, win.job_day)),
                 ('receipt', lambda: ReceiptSelection(win, win))]
    if business:
        head = json.loads((output/'business-fixture.json').read_text(encoding='utf-8'))['head']
        factories.append(('purchase', lambda: PurchaseDialog(win, win, head)))
    factories.append(('fetch-again', lambda: FetchDialog(win, win.repository.folder, win.job_day)))
    pids = []
    for name, create in factories:
        dialog = create();active['dialog'] = dialog;dialog.show()
        wait(lambda: dialog.worker is None)
        data = []
        def inspect(_):
            dialog.ensure_client()
            return dialog.client.request('session.status')
        dialog.run(inspect, data.append);wait(lambda: dialog.worker is None)
        if len(data) != 1 or data[0]['connected'] or data[0]['worker_pid'] is not None:
            raise ValueError('Local shared smoke unexpectedly connected to Edge')
        pids.append(data[0]['host_pid'])
        dialog.grab().save(str(output/('shared-'+name+'.png')))
        dialog.close();wait(lambda: dialog._can_close and dialog.worker is None)
        if not win.aoxiang_workflows.session.client.running or win.aoxiang_workflows.gate.yielding:
            raise ValueError('Normal window close did not retain transport and release operation ownership')
        dialog.deleteLater();active['dialog'] = None
    if len(set(pids)) != 1:
        raise ValueError('Cross-window host restarted')
    (output/'shared-session.json').write_text(json.dumps(dict(host_pids=pids,windows=[n for n,_ in factories],
        local_only=True,live_edge=False,retained_until_application_close=True),ensure_ascii=False,indent=2),encoding='utf-8')
    checks.append('真实窗口跨模块复用同一Node进程，正常关闭只释放操作权，重开取数不重启组件；不连接Edge')
