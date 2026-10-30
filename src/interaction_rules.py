"""Presentation-scope checks only; quantities, priorities and snapshots are untouched."""


def validate_return_scope(plan,head):
    if head is None:return
    for p in plan['plans']:
        h=p['head']
        if h['oid']!=head['oid'] or h['revision']!=head['revision'] or any(item.get('warehouse')!=head['warehouse'] for item in p['payload']['incoming'].values()):
            raise ValueError('文件包含其它单据、仓库或修订。请到历史页“批量导入加盟商回传”；本入口仅接受当前仓单。')


def configuration_changes(configs,edited):
    from parameter_view import flatten,display_path
    target=dict(flatten(edited));rows=[]
    for store,old in configs.items():
        values=dict(flatten(old))
        for key in dict.fromkeys([*values,*target]):
            if values.get(key)!=target.get(key) or (key in values)!=(key in target):rows.append([store,display_path(key),values.get(key,'未设置'),target.get(key,'未设置')])
    return rows
