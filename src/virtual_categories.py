"""Exact native virtual-category tokens. They are not product tags or fuzzy words."""
RETIRED = frozenset(['标杆淘汰'] + ['标杆淘汰（'+x+'）' for x in (
    '箱规废弃','厂家停产','夏季','冬季','拉回下单','样品/赠品','质量问题','节庆商品','召回退供','河北仓专属')])
