"""Host checks and original file handling for one AX purchase task."""
from pathlib import Path

from aoxiang_directory import Directory
from aoxiang_purchase_tasks import validate_for_upload
from platform_import_files import author, read_original, write_original


def basis(dialog, *, binding=False):
    return validate_for_upload(dialog.host.work_repository.path, dialog.package,
                               dialog.client.request('configuration'), Directory(dialog.folder).suppliers(),
                               for_binding=binding, for_manual=True)


def handoff(dialog, purchase, operator, reason):
    values = author(operator, reason)
    return dialog.client.request('purchase.handoff', purchase_id=purchase['id'], sequence=purchase['sequence'],
                                 host_facts=basis(dialog), timeout=150, **values)


def export_source(client, purchase_id, usage, **values):
    source = client.request('purchase.export_source', purchase_id=purchase_id, usage=usage, **values)
    return source, read_original(source['file'], source['sha256'])


def export_original(dialog, purchase_id, usage, target):
    facts = {'host_facts':basis(dialog)} if usage=='manual' else {}
    source, raw = export_source(dialog.client, purchase_id, usage, **facts)
    receipt = write_original(raw, source['sha256'], target, '.xlsx')
    return dialog.client.request('purchase.exported', purchase_id=purchase_id, usage=usage,
                                 output=receipt['path'], export_sha256=receipt['sha256'], **facts)


def failure_rows(client, purchase_id):
    from io import BytesIO

    from openpyxl import load_workbook

    _, raw = export_source(client, purchase_id, 'failure')
    workbook = load_workbook(BytesIO(raw), read_only=True, data_only=True)
    rows = []
    try:
        for sheet in workbook:
            fields = [str(v or '') for v in next(sheet.values, ())]
            if not fields:
                continue
            for index, cells in enumerate(sheet.iter_rows(min_row=2, values_only=True), 2):
                if any(v is not None for v in cells):
                    detail = ' | '.join(f'{key} {value}' for key,value in zip(fields,cells)
                                        if key and value is not None)
                    rows.append([sheet.title, index, detail])
    finally:
        workbook.close()
    return rows
