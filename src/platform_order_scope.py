"""Order type scope for the shared platform operations."""
from contextlib import closing

import work_repository as wr


LABELS = {'normal_model':'补货单','material':'物料单'}


def groups(path, values, head=lambda value:value):
    """Use local order identities as authority. Preserve selected order within each type."""
    selected = {}
    with closing(wr.read_connection(path)) as db:
        for item in values:
            oid = head(item)['oid']
            row = db.execute('SELECT kind FROM normal_orders WHERE oid=?', (oid,)).fetchone()
            key = 'material' if row and row[0]=='material' else 'normal_model'
            selected.setdefault(key, []).append(item)
    return selected


def require_single(path, values, head=lambda value:value):
    if len(values)>1 and len(groups(path, values, head))>1:
        raise ValueError('补货单和物料单请分别确认并提交原平台批次')


def choose(parent, path, values, head=lambda value:value):
    """The all-types view still asks the operator which group to act on."""
    if len(values)<2:
        return values
    choices = groups(path, values, head)
    if len(choices)==1:
        return values
    from PySide6.QtWidgets import QInputDialog

    titles = {f'{LABELS[key]}（{len(items)} 张）':items for key,items in choices.items()}
    title, ok = QInputDialog.getItem(parent,'按订单类型确认批次','本次处理',list(titles),0,False)
    return titles[title] if ok else []
