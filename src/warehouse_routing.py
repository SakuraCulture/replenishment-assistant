"""Pure selection shared by replenishment and analysis; no I/O or defaults."""


def select_warehouse(primary, secondary, primary_state, primary_match, secondary_match):
    target = secondary if secondary and primary_state in {'禁用', '无货', '无该商品'} else primary
    retain_primary = (target != primary and primary_match == '商品编码精确匹配'
                      and secondary_match == '资料未匹配')
    return (primary if retain_primary else target), retain_primary
