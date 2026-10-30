"""2026-10-22 已确认补货模型。纯 Python 计算，不依赖 Excel/WPS。

计算字段沿用参考主表列号；AS 是最终补货量。此模块不做文件匹配，
不把未知输入变成 0，不把中心仓库存当成门店库存。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal, ROUND_HALF_UP
from math import exp, log, sqrt, floor, ceil, lgamma, isfinite
from statistics import NormalDist, stdev
from typing import Any

VERSION = "2026-10-22-8+5-v1"
PENDING = "待复核"
CONFLICTS = {"销量窗口异常", "缺货销量冲突"}
GROWTH_STATES = {"持续上升", "平稳后上升", "新近动销（试补候选）", "低位回升",
                 "回升至早期水平", "回升超过早期水平", "增长后高位持平"}
CALCULATED = "W X Y Z AA AE AK AL AM AN AO AP AQ AR AS AW AX AY AZ BA BB BC BD BE BF BG BH BI BJ BK BL BM BN BO BP BQ BR BS BT BU BV BW BX".split()


def number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and isfinite(x)


def text(x: Any) -> bool:
    return isinstance(x, str)


def blank(x: Any) -> bool:
    return x is None or x == ""


def xround(x: float | Decimal, digits: int = 0) -> float:
    """Excel ROUND: ties away from zero; never Python's bankers' round.

    Excel's 15-significant-digit arithmetic also treats 3.4999999999999996
    as the exact half boundary 3.5. Keep this explicit for carton boundaries.
    """
    d = x if isinstance(x, Decimal) else Decimal(format(float(x), ".15g"))
    return float(d.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP))


def fmt(x: Any, digits: int = 10) -> str:
    """Match workbook TEXT(…, '0.##########'), including integer trailing dot."""
    if not number(x):
        return str(x or "")
    s = f"{xround(x, digits):.{digits}f}".rstrip("0")
    return s if "." in s else s + "."


def poisson_cdf(k: int, mean: float) -> float:
    """Poisson CDF by convergent probability recurrences (no normal shortcut).

    Start near k in log space, avoiding exp(-mean) underflow for large demand.
    The calculation uses the same 10-point quantile search as the workbook.
    """
    if mean < 0 or not isfinite(mean):
        raise ValueError("无效泊松均值")
    if k < 0:
        return 0.0
    if mean == 0:
        return 1.0
    k = int(k)
    if k < mean:
        term = exp(-mean + k * log(mean) - lgamma(k + 1))
        total, j = term, k
        while j > 0:
            term *= j / mean
            total += term
            j -= 1
            if term <= total * 2e-16:
                break
        return min(1.0, max(0.0, total))
    j = k + 1
    term = exp(-mean + j * log(mean) - lgamma(j + 1))
    total = term
    for _ in range(1_000_000):
        j += 1
        term *= mean / j
        total += term
        if term <= total * 2e-16 or term == 0:
            return min(1.0, max(0.0, 1.0 - total))
    raise ArithmeticError("泊松分布计算未收敛")


@dataclass(frozen=True)
class Rules:
    depth: float = 8
    lead: float = 5
    cap_days: float = 21
    probability: float = .9
    single_sample_multiple: float = 1.5
    strict_monthly: float = 3
    exit_monthly: float = 10
    monthly_extra_money: float = 6
    weak_extra_money: float = 30
    historical_reference_days: float = 13
    extreme_days: float = 30
    minimum_reference_units: float = 1
    growth_reference_days: float = 4
    moq_days: float = 12
    moq_price: float = 6

    def valid(self) -> bool:
        vals = asdict(self)
        return (all(number(v) for v in vals.values())
                and min(self.depth, self.lead, self.cap_days) >= 0
                and self.depth + self.lead > 0 and .5 <= self.probability <= .99
                and self.single_sample_multiple > 0 and self.strict_monthly >= 0
                and self.exit_monthly > self.strict_monthly
                and min(self.monthly_extra_money, self.weak_extra_money) >= 0
                and self.historical_reference_days > 0 and self.extreme_days == 30
                and self.minimum_reference_units > 0 and self.growth_reference_days > 0)


def turnover(h: Any, i: Any, j: Any, k: Any, l: Any) -> Any:
    """Original P: ROUND(MAX(K+0.7L,0)/(I/7 if I>0 else J/30),0)."""
    if not all(number(x) for x in (i, j, k, l)):
        return PENDING
    if i == 0 and j == 0:
        return "无动销"
    sales, days = (i, 7) if i > 0 else (j, 30)
    if sales == 0:
        return "周转分母异常"
    covered = max(Decimal(str(k)) + Decimal("0.7") * Decimal(str(l)), Decimal(0))
    return xround(covered * days / Decimal(str(sales)))


def sales_state(d: dict) -> str:
    if d["AW"] != "可计算":
        return d["AW"]
    if d["BB"] in CONFLICTS:
        return d["BB"] + "，按原回退计算"
    if d["AX"] <= 0:
        return "单段历史样本，近期方向未知" if d["BB"] == "单段样本" else "近期无有货样本，方向未知"
    w, z, a, e = d["W"], d["Z"], d["AA"], 1e-10
    if d["J"] == 0:
        return "30天无动销"
    if w > 0 and z == a == 0:
        return "新近动销（试补候选）"
    if w == z == 0:
        return "近14天无动销"
    if w > z + e:
        if z >= a - e:
            return "持续上升" if z > a + e else "平稳后上升"
        return "低位回升" if w < a - e else "回升至早期水平" if abs(w-a) <= e else "回升超过早期水平"
    if w < z - e:
        if z <= a + e:
            return "持续下降" if z < a - e else "平稳后下降"
        return "高位回落" if w > a + e else "冲高后回落至早期或更低水平"
    return "增长后高位持平" if w > a + e else "下降后低位持平" if w < a - e else "三段日均一致"


def diagnostics(d: dict) -> dict:
    if d["AW"] != "可计算":
        v = d["AW"]
    elif d["BB"] in CONFLICTS:
        v = d["BB"]
    elif min(d[c] for c in ("AX", "AY", "AZ")) <= 0:
        v = "有效阶段不足"
    elif sum(d[c] for c in ("W", "Z", "AA")) == 0:
        v = "无动销"
    else:
        values = [d[c] for c in ("W", "Z", "AA")]
        avg = sum(values) / 3
        return {"CV": stdev(values)/avg, "诊断销售状态": d["BL"],
                "三段日均极差率": (max(values)-min(values))/avg}
    return {"CV": v, "诊断销售状态": d["BL"], "三段日均极差率": v}


def calculate(inputs: dict, rules: Rules | None = None) -> dict:
    """Calculate one normalized SKU. Inputs use Excel column keys A:V/AB:AJ.

    AB/AC/AD are supplied constants; AE is automatically 1-AC-AD. Row AG/AH,
    AI/AJ overrides support reproducible workbook comparisons. No external I/O.
    """
    p = rules or Rules()
    d = dict(inputs)
    for c in "H I J K L N O P S V AB AC AD".split():
        d.setdefault(c, None)
    d.setdefault("AG", p.depth); d.setdefault("AH", p.lead)
    d.setdefault("AI", p.moq_days); d.setdefault("AJ", p.moq_price)
    d.setdefault("AF", 1.28)  # retained legacy parameter; does not drive safety stock
    h, i, j, k, l, n, o, v = [d[c] for c in "H I J K L N O V".split()]
    nvalid = blank(n) or number(n)
    n0 = max(n or 0, 0) if nvalid else None
    # A stockout lasting beyond the full sales window cannot coexist with monthly
    # sales. Discard only that input for forecasting; retain raw N for auditing.
    discarded_stockout = number(n) and n > 30 and number(j) and j > 0
    if discarded_stockout:
        n0 = 0
        d["缺货天数处理"] = "原缺货天数>30且月销>0，缺货天数无效；有效值按0，销量沿用原预测公式"
    s14 = xround(h * 14) if number(h) else None
    d["14天累计销量（重组）"] = s14
    d["AE"] = xround(1-d["AC"]-d["AD"], 10) if number(d["AC"]) and number(d["AD"]) else PENDING
    if not nvalid or not all(number(x) for x in (h, i, j)):
        bb = "输入缺失"
    elif min(h, i, j) < 0 or i > s14 or s14 > j:
        bb = "销量窗口异常"
    elif n0 >= 7 and i != 0 or n0 >= 14 and s14 != 0 or n0 >= 30 and j != 0:
        bb = "缺货销量冲突"
    elif n0 >= 30:
        bb = "极端缺货"
    elif n0 >= 14:
        bb = "单段样本"
    else:
        bb = "正常"
    d["BB"] = bb
    d["BA"] = (0 if bb in CONFLICTS else n0) if nvalid else PENDING
    if number(d["BA"]):
        days = d["BA"]
        d["AX"] = max(7-days, 0)
        d["AY"] = max(14-days, 0)-max(7-days, 0)
        d["AZ"] = max(30-days, 0)-max(14-days, 0)
    else:
        d.update(AX=PENDING, AY=PENDING, AZ=PENDING)
    params = [d[c] for c in "AB AC AD AE AG AH AI AJ".split()]
    if not p.valid() or not all(number(x) for x in params):
        aw = "参数异常"
    elif (d["AB"] < 0 or min(d["AC"],d["AD"],d["AE"]) < 0
          or sum(d[c] for c in ("AC","AD","AE")) <= 0
          or min(d["AG"],d["AH"]) < 0 or d["AG"]+d["AH"] <= 0):
        aw = "参数异常"
    elif not nvalid or not all(number(x) for x in (h,i,j,k,l)):
        aw = "输入缺失"
    else:
        aw = "极端缺货自动拉回" if d["BA"] >= 30 else "可计算"
    d["AW"] = aw
    for c in "W X Y Z AA AK AL AM AN AO AP AQ BE BF BG BH BN".split():
        d[c] = PENDING
    d["BC"] = (j/30 if j >= 0 else s14/14 if h >= 0 else max(i,0)/7) if aw == "可计算" else PENDING
    if aw == "可计算":
        ax, ay, az = d["AX"], d["AY"], d["AZ"]
        if bb == "销量窗口异常":
            d["W"] = d["Z"] = d["AA"] = d["AN"] = d["BC"]
        else:
            d["AA"] = (j-s14)/az
            d["Z"] = (s14-i)/ay if ay > 0 else d["AA"]
            d["W"] = i/ax if ax > 0 else d["Z"]
            if bb == "单段样本":
                d["AN"] = (j-s14)/max(az,(d["AG"]+d["AH"])/p.single_sample_multiple)
            else:
                denom = d["AC"]*ax/7 + d["AD"]*ay/7 + d["AE"]*az/16
                if denom <= 0:
                    d.update(AW="参数异常", BL="参数异常", AS=PENDING, BM="有效阶段权重为0；参数异常")
                    return finish_unavailable(d)
                d["AN"] = (d["AC"]*i/7+d["AD"]*(s14-i)/7+d["AE"]*(j-s14)/16)/denom
        w, z, a = d["W"], d["Z"], d["AA"]
        d["X"] = (w*7+z*7)/14
        d["Y"] = (w*7+z*7+a*16)/30
        d["AK"] = stdev([w,z,a] if ax > 0 else [z,a])
        d["BL"] = sales_state(d)
        if bb in CONFLICTS or ax <= 0 or d["AN"] == 0:
            d["AM"] = 1
        else:
            trend = min(max(xround(exp(d["AB"]*(log(max(w,1e-10))-log(max(d["Y"],1e-10)))),4),.3),2)
            supported = w*min(2,xround((w/z)**d["AB"],4)) if d["BL"] in {"持续上升","平稳后上升"} else w
            d["AM"] = 1+ax/7*(max(0,min(trend,supported/d["AN"]))-1)
        d["AO"] = d["AN"]*(d["AG"]+d["AH"])
        d["AP"] = d["AO"]*d["AM"]
        demand = d["AP"]
        normal_q = NormalDist().inv_cdf(p.probability)
        d["BE"] = max(0,floor(demand+normal_q*sqrt(demand)+(normal_q**2-1)/6)-3)
        d["BF"] = 0 if demand == 0 else d["BE"] + sum(poisson_cdf(d["BE"]+t,demand)<p.probability for t in range(10))
        q = d["BF"]
        d["BG"] = "通过" if demand == 0 or (poisson_cdf(q,demand)>=p.probability and (q==0 or poisson_cdf(q-1,demand)<p.probability)) else "分位计算待复核"
        d["AL"] = max(q-demand,0) if d["BG"] == "通过" else PENDING
        d["AQ"] = max(demand+d["AL"]-k-l,0) if d["BG"] == "通过" else PENDING
        historical = d["BC"] if bb == "销量窗口异常" else j/(30-d["BA"])
        corrected = ((j-s14)/max(az,p.historical_reference_days/p.single_sample_multiple)
                     if bb == "单段样本" else d["AN"]*d["AM"])
        d["BH"] = min(corrected,historical)
        if d["BL"] in GROWTH_STATES:
            d["BN"] = d["BH"]+min(1,ax/p.growth_reference_days)*max(0,min(d["AN"]*d["AM"],w,s14/(ax+ay))-d["BH"])
        else:
            d["BN"] = d["BH"]
    else:
        d["BL"] = sales_state(d)
    d["BD"] = 1 if o == "/" else o if number(o) and o > 0 else "待核箱规"
    step = d["BD"]
    if "P" not in inputs:
        d["P"] = turnover(h,i,j,k,l)
    if all(number(x) for x in (i,j,k,l)):
        d.setdefault("Q",("周销暂停" if k>0 and j>0 else "新品" if l>0 else "不动销") if d["P"] == -1 else "")
        if i == 0:
            health = "零销量"
        else:
            weeks = (k+.7*l)/i
            health = next((label for cut,label in [(8,"严重积压"),(6,"积压"),(4,"偏高"),(8.5/7,"正常"),(8.5/7*.5,"偏低"),(8.5/7*.3,"极低")] if weeks>cut),"危险")
        d.setdefault("R",health)
    d["BI"] = int((number(d["P"]) and number(d["AI"]) and d["P"]<=d["AI"] and d["P"]!=-1)
                  or (number(j) and j<=5 and number(v) and number(d["AJ"]) and v<d["AJ"]))
    d["BO"] = ""
    if aw == "极端缺货自动拉回":
        d["BO"] = step if text(step) else step if k<=0 and max(l,0)<min(p.minimum_reference_units,step) else 0
        d["AQ"] = d["BO"]
    aq = d["AQ"]
    d["BJ"] = aq if text(aq) else step if text(step) else 0 if aq==0 else ceil(xround(aq/step,10))*step
    if text(d["BJ"]):
        d["BK"] = d["BJ"]
    elif aw == "极端缺货自动拉回":
        d["BK"] = d["BJ"]
    else:
        position_limit = (k+l+d["BJ"] if p.cap_days == 0 else
                          max(p.cap_days*d["BH"],min(p.cap_days*d["BN"],k+l+d["BJ"])))
        d["库存额度目标量"] = position_limit
        d["BK"] = max(0,floor(xround((position_limit-k-l)/step,10)))*step
    d["AR"] = d["BJ"] if text(d["BJ"]) else min(d["BJ"],d["BK"])
    if text(d["AR"]):
        d["BP"] = d["AR"]
    elif aw == "极端缺货自动拉回":
        d["BP"] = d["AR"]
    else:
        ordinary = d["AR"] if d["BI"]==1 else 0
        sales_floor = k<=0 and max(h,i,j)>0 and (l<=0 or aq>0)
        zero_restore = j==0 and number(n) and 0<n<p.extreme_days and k<=0 and max(l,0)<min(p.minimum_reference_units,step)
        d["BP"] = max(ordinary,step) if sales_floor or zero_restore else ordinary
    if text(d["BP"]):
        d["BQ"] = d["BP"]
    elif aw == "极端缺货自动拉回":
        d["BQ"] = d["BO"]
    else:
        d["BQ"] = step if (d["BP"]>0 and k<=0 and (max(h,i,j)>0 or j==0 and number(n) and 0<n<p.extreme_days)
                              and max(l,0)<min(p.minimum_reference_units,step)) else 0
    if text(d["BP"]):
        d["BR"] = d["BP"]
    elif aw == "极端缺货自动拉回":
        d["BR"] = d["BQ"]
    elif max(j,0)>=p.exit_monthly:
        d["BR"] = d["BP"]
    else:
        d["BR"] = max(d["AR"] if d["BI"]==1 else 0,d["BQ"])
    d["BS"] = PENDING
    if aw in {"可计算","极端缺货自动拉回"}:
        d["BS"] = 0 if bb in CONFLICTS or min(i,s14-i,j-s14)<0 else max(0,max(j,0)-max(i,s14-i,j-s14))
    price_valid = number(v) and v>=0
    if text(d["BR"]):
        d["BT"] = d["BU"] = d["AS"] = d["BR"]
    else:
        d["BT"] = max(step*v,min(max(j,0)*v+p.monthly_extra_money,d["BS"]*v+p.weak_extra_money)) if price_valid else "待核采购价"
        if d["BR"]==0:
            d["BU"] = d["AS"] = 0
        elif aw == "极端缺货自动拉回":
            d["BU"] = d["AS"] = d["BQ"]
        elif j>=p.exit_monthly:
            d["BU"] = d["AS"] = d["BR"]
        elif not price_valid:
            d["BU"] = "待核采购价"
            d["AS"] = d["BQ"] if d["BQ"]>0 else "待核采购价（额外量）"
        elif v==0:
            d["BU"] = d["AS"] = d["BR"]
        else:
            stock = max(k,0)+max(l,0)
            release = max(0,min(1,(max(j,0)-p.strict_monthly)/(p.exit_monthly-p.strict_monthly)))
            full = (stock+d["BR"])*v
            budget = min(full,(1-release)*d["BT"]+release*full)
            d["资金释放比例"] = release
            d["实际库存金额上限"] = budget
            d["BU"] = step*max(0,floor(xround((budget/v-stock)/step,10)))
            d["AS"] = max(d["BQ"],min(d["BR"],d["BU"]))
    d["BX"] = "待核" if text(d["BR"]) else max(d["BR"]-d["BQ"],0) if aw=="可计算" and j<p.exit_monthly and not price_valid else 0
    noncentral = d["S"]=="/" and v=="/"
    q = d["AS"]
    d["BV"] = (("数量待定（非中心仓资金规则）" if noncentral else "数量待核") if text(q) else
               0 if q==0 else "未计金额（非中心仓）" if noncentral else "待核采购价" if not price_valid else q*v)
    d["BW"] = focus(d,p)
    d["BM"] = explanation(d,p)
    d.update(diagnostics(d))
    return d


def finish_unavailable(d: dict) -> dict:
    for c in CALCULATED:
        d.setdefault(c,PENDING)
    d.update(diagnostics(d))
    return d


def focus(d: dict, p: Rules) -> str:
    q, br, bq, step, v, j, n = [d[c] for c in "AS BR BQ BD V J N".split()]
    noncentral = d["S"]=="/" and v=="/"
    out = []
    if d.get("缺货天数处理"):
        out.append(d["缺货天数处理"])
    if j==0 and number(n) and 0<n<p.extreme_days and number(bq) and bq>0:
        out.append("零月销缺货恢复")
    if text(q):
        out.append("非中心仓资金规则待定（最终数量未定）" if noncentral else q)
    if number(q) and q>0 and (not number(v) or v<0) and not noncentral:
        out.append("补货金额待核")
    if number(d["BX"]) and d["BX"]>0:
        out.append("非中心仓资金规则待定（额外量）" if noncentral else "缺价额外量待核")
    if all(number(x) for x in (q,bq,step,v)) and q>0 and bq>0 and step*v>p.weak_extra_money:
        out.append("最低出库成本超过"+fmt(p.weak_extra_money,2)+"元")
    if number(q) and number(j) and 1<=j<=3 and q>j:
        out.append("补货超过历史月销")
    if number(q) and number(br) and q<br:
        out.append("资金缩减额外量")
    if d["BB"] in CONFLICTS:
        out.append(d["BB"])
    if number(q) and q>0 and d["S"] in {"禁用","备用"}:
        out.append("中心仓"+d["S"])
    if number(v) and v==0:
        out.append("零价格口径待核")
    return "；".join(out)+("；" if out else "")


def explanation(d: dict, p: Rules) -> str:
    q, br, bq, step, v, j, n, k, l = [d[c] for c in "AS BR BQ BD V J N K L".split()]
    noncentral = d["S"]=="/" and v=="/"
    prefix = "非中心仓商品（正常）；" if noncentral else ""
    if d["AW"] not in {"可计算","极端缺货自动拉回"}:
        return prefix+d["AW"]
    out = d["BL"]+"；"
    if j==0 and number(n) and 0<n<p.extreme_days and number(bq) and bq>0:
        out += "零月销缺货按最低有货恢复，不上调销量预测；"
    if text(q):
        return prefix+out+("非中心仓资金规则待定（最终数量未定）" if noncentral else q)
    if d["AW"]=="极端缺货自动拉回":
        out += "恢复一个出库单位；首铺价档数量不作为永久持有下限" if q>0 else "已有最低覆盖，不重复恢复"
    elif br==0:
        out += "在途已有最低覆盖，常规候选为0" if k<=0 and max(l,0)>=min(p.minimum_reference_units,step) else "常规候选为0"
    elif j>=p.exit_monthly:
        out += "月销达到"+fmt(p.exit_monthly,2)+"，新增资金层退出"
    elif not number(v) or v<0:
        if bq>0:
            out += "最低恢复量先执行；非中心仓资金规则待定" if noncentral else "最低恢复量先执行，额外量及金额待核"
        else:
            out += "非中心仓资金规则待定（额外量）" if noncentral else "额外数量待核采购价"
    elif v==0:
        out += "零价格沿用候选，价格口径待核"
    else:
        out += "低基数资金约束缩减额外数量" if q<br else "保留资金约束候选数量"
    out += "；实际补货"+fmt(q)
    if number(bq) and bq>0:
        out += "；最低恢复量"+fmt(bq)
    if number(d["AQ"]) and d["AW"]=="可计算":
        out += "；净需求"+fmt(d["AQ"])
    if number(d["BX"]) and d["BX"]>0:
        out += "；另有"+fmt(d["BX"])+("个额外候选待定（非中心仓资金规则）" if noncentral else "个额外候选待核采购价")
    if q>0 and d["BI"]==0:
        out += "；最低恢复豁免原资格门槛"
    if number(d["BU"]) and q>d["BU"]:
        out += "；最低有货优先于资金上限"
    if d["BB"]=="单段样本":
        out += "；单段预测限制为样本销量的"+fmt(p.single_sample_multiple,2)+"倍，安全库存及整箱另计"
    if k<0 or l<0:
        out += "；净需求沿用原负库存，资金口径负库存按0"
    if number(n) and n<0:
        out += "；负缺货天数按0"
    if d["BW"]:
        out += "；"+d["BW"]
    return prefix+out
