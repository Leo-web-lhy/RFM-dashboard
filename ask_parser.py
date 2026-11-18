# -*- coding: utf-8 -*-
"""⑦ 中文问答查数 · 规则版 ChatBI（完全离线，不调用任何外部 API / LLM）

设计原则
--------
1. **零新依赖**：只用 re + pandas，与主程序共用同一套 DataFrame（不重写口径）。
2. **不 import Flask**：本模块可单独 import 做单元测试（见 test_ask_parser.py）。
3. **口径与页面一一对应**（本项目存在同名不同义的两套客群，必须各归其位）：
   - 销售额 / 购买客户数 / 笔单价：RFM 客群口径（RFM_Segment，即全局筛选器「客群」），
     对应页① / 总览；
   - 复购率：客户档案营销层级口径（CustomerTier），对应页② KPI；
   - 唤醒率、沉睡客户数：CustomerTier 口径，对应页③ KPI；
   - ROI：活动表 ΣRevenue ÷ ΣCost，对应页④ KPI。
   凡是可能被误读的地方，答案里同时给出另一套口径的数字并在 note 说明。
4. **解析不了不报错**：返回 ok=False + 引导话术 + 示例问句，接口恒 200。
5. **解析与聚合分离**（⑨ 本地 LLM 复用本模块）：槽位可由 parse() 规则产生，
   也可由 llm_slots.py 经 schema 校验后传入 preslots；两条路径走**同一套聚合**
   （answer_scalar / rank / campaign / roi …），LLM 永远碰不到数字。

对外暴露：
    parse(question, vocab=None) -> slots
    build_vocab(df, rfm, camp) -> dict
    answer(question, df, rfm, camp, cust_map=None, preslots=None) -> dict
"""
from __future__ import annotations

import re

import pandas as pd

WAN = 10000.0

# ---------------------------------------------------------------- 词表
SEG_ORDER = ["高价值客户", "大众客户", "潜力客户", "沉睡客户"]
SEG_ALIASES = {
    "高价值客户": ["高价值客户", "高价值客群", "高价值群", "高价值", "大客户", "vip客户", "vip"],
    "大众客户": ["大众客户", "大众客群", "普通客户", "一般客户", "大众"],
    "潜力客户": ["潜力客户", "潜力客群", "潜力"],
    "沉睡客户": ["沉睡客户", "沉睡客群", "流失客户", "沉默客户", "不活跃客户", "沉睡"],
}
CAT_ALIASES = {
    "家居生活": ["家居生活", "家居"],
    "服饰鞋包": ["服饰鞋包", "服饰", "鞋包", "服装"],
    "数码电子": ["数码电子", "数码", "电子"],
    "食品生鲜": ["食品生鲜", "食品", "生鲜"],
    "美妆护肤": ["美妆护肤", "美妆", "护肤", "化妆品"],
}
STRAT_ALIASES = {
    "大水漫灌": ["大水漫灌", "漫灌", "广撒网"],
    "精准滴灌": ["精准滴灌", "滴灌", "精准营销"],
}
GENDER_ALIASES = {"男": ["男性", "男生", "男客户"], "女": ["女性", "女生", "女客户"]}

# 指标识别：顺序即优先级（"复购率"必须先于"客户数"，"销售额"先于"销售"）
METRIC_RULES = [
    ("repurchase",   ("复购率", "复购比例", "回购率", "重复购买率")),
    ("awaken_rate",  ("唤醒率", "唤醒比例")),
    ("aov",          ("笔单价", "客单价", "平均单价", "单均", "每单平均", "平均每单")),
    ("roi",          ("roi", "投产比", "投入产出比", "投入产出")),
    ("orders",       ("订单数", "订单量", "交易笔数", "笔数", "单量", "多少笔", "多少单", "几笔")),
    ("sales",        ("销售额", "gmv", "销售金额", "销售总额", "销售业绩", "营收", "营业额",
                      "卖了多少钱", "卖了多少", "销售", "收入")),
    ("customers",    ("客户数", "多少人", "多少个人", "人数", "买家数", "购买人数",
                      "多少个客户", "客户人数", "客户数量")),
]
METRIC_LABEL = {
    "sales": "销售额", "customers": "购买客户数", "orders": "交易笔数",
    "aov": "笔单价", "repurchase": "复购率", "roi": "营销 ROI", "awaken_rate": "唤醒率",
}
METRIC_UNIT = {
    "sales": "万元", "customers": "人", "orders": "笔",
    "aov": "元", "repurchase": "%", "roi": "", "awaken_rate": "%",
}

_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# 槽位模板：所有槽位键的唯一来源（规则解析与 LLM 解析共用，避免两边字段漂移）
SLOT_TEMPLATE = {"year": None, "month": None, "seg": None, "category": None, "strat": None,
                 "gender": None, "metric": None, "dim": None, "topn": None,
                 "order": "desc", "latest": False}

RANK_WORDS = ("最高", "最多", "最好", "最强", "排名", "排行", "top", "第几")
LOW_WORDS = ("最低", "最少", "最差", "最弱")


def _clean(q):
    """去空白/全角空格"""
    return re.sub(r"[\s\u3000]+", " ", (q or "").strip())


def _find_alias(text, alias_map, vocab_values=None):
    """在文本中匹配别名（长别名优先），vocab_values 兜住数据里新增的取值"""
    best, best_len = None, 0
    for canon, aliases in alias_map.items():
        for a in aliases:
            if a and a in text and len(a) > best_len:
                best, best_len = canon, len(a)
    if best is None and vocab_values:
        for v in vocab_values:
            if v and v in text and len(v) > best_len:
                best, best_len = v, len(v)
    return best


def build_vocab(df, rfm, camp):
    """数据里真实存在的取值集合（校验槽位用：LLM 编出来的客群/品类一律拦下）"""
    return {
        "segments": sorted(rfm["RFM_Segment"].dropna().unique().tolist()),
        "categories": sorted(df["Category"].dropna().unique().tolist()),
        "strategies": sorted(camp["StrategyGroup"].dropna().unique().tolist()),
    }


def _has_rank(text):
    return any(k in text for k in RANK_WORDS) or any(k in text for k in LOW_WORDS)


def _finalize_slots(s, text, rank=None):
    """槽位收尾：TopN / 指标默认值的补全规则。

    规则解析与 LLM 解析共用这一处，保证「同一个槽位组合」无论来自哪条路径，
    落到聚合时的语义完全一致。
    """
    if rank is None:
        rank = _has_rank(text)
    if s.get("dim") and s.get("order") == "desc" and s["dim"] != "customer":
        s["topn"] = s.get("topn") or (5 if rank and ("排名" in text or "排行" in text) else 1)
    if s.get("dim") == "customer":
        s["topn"] = s.get("topn") or 5

    # ---- 维度存在但没给指标：默认问销售额（"哪个品类卖得最好"）
    if s.get("metric") is None and s.get("dim") in ("category", "segment", "customer"):
        s["metric"] = "sales"
    if s.get("metric") is None and s.get("dim") == "campaign":
        s["metric"] = "roi"

    s["_text"] = text
    return s


def parse(question, vocab=None):
    """把问句解析成槽位。纯函数，不碰数据（vocab 只用于兜底别名）。"""
    text = _clean(question)
    low = text.lower()
    vocab = vocab or {}
    s = dict(SLOT_TEMPLATE)

    # ---- 时间：2025年3月 / 2025-03 / 2025/3 / 2025年 / 3月 / 最新月
    m = re.search(r"(20\d{2})\s*[年\-/\.]\s*(\d{1,2})\s*月?", text)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        s["year"] = y
        if 1 <= mo <= 12:
            s["month"] = mo
    else:
        my = re.search(r"(20\d{2})\s*年?", text)
        if my:
            s["year"] = int(my.group(1))
        mm = re.search(r"(?<!\d)(\d{1,2})\s*月", text)
        if mm and 1 <= int(mm.group(1)) <= 12:
            s["month"] = int(mm.group(1))

    if any(k in text for k in ("最新", "最近一个月", "上月", "本月", "当月")):
        s["latest"] = True
        s["year"] = None
        s["month"] = None

    # ---- 维度实体
    s["seg"] = _find_alias(low, SEG_ALIASES, vocab.get("segments"))
    s["category"] = _find_alias(low, CAT_ALIASES, vocab.get("categories"))
    s["strat"] = _find_alias(low, STRAT_ALIASES, vocab.get("strategies"))
    s["gender"] = _find_alias(low, GENDER_ALIASES)

    # ---- 指标
    for name, words in METRIC_RULES:
        if any(w in low for w in words):
            s["metric"] = name
            break

    # ---- 问法维度（谁排第一 / Top N）
    rank = any(k in text for k in ("最高", "最多", "最好", "最强", "排名", "排行", "top", "第几"))
    if any(k in text for k in ("最低", "最少", "最差", "最弱")):
        s["order"] = "asc"
        rank = True

    if "品类" in text or "类目" in text:
        s["dim"] = "category"
    elif "营销活动" in text or "活动" in text or "投放" in text:
        s["dim"] = "campaign"
    elif "客群" in text and (rank or "贡献" in text or "构成" in text or "分布" in text):
        s["dim"] = "segment"
    elif rank and "客户" in text and not s["seg"]:
        s["dim"] = "customer"
    elif "客户" in text and any(k in text for k in ("消费最多", "买得最多", "客单价最高")):
        s["dim"] = "customer"

    # ---- Top N
    tn = re.search(r"(?:top|前)\s*(\d{1,2})", low)
    if tn:
        s["topn"] = max(1, min(int(tn.group(1)), 20))
    else:
        cn = re.search(r"前([一二两三四五六七八九十])", text)
        if cn:
            s["topn"] = _CN_NUM.get(cn.group(1), 1)

    return _finalize_slots(s, text, rank)


# ---------------------------------------------------------------- 口径辅助
def _scope(df, rfm, s):
    """RFM 客群口径的客户池 + 交易窗口（与 /api/dashboard 的 pool/dff 同构）"""
    pool = rfm
    if s.get("gender"):
        pool = pool[pool["Gender"] == s["gender"]]
    if s.get("seg"):
        pool = pool[pool["RFM_Segment"] == s["seg"]]
    keys = set(pool["CustomerKey"])
    d = df[df["CustomerKey"].isin(keys)]
    if s.get("year") is not None:
        d = d[d["Year"] == s["year"]]
    if s.get("month") is not None:
        d = d[d["Month"] == s["month"]]
    if s.get("category"):
        d = d[d["Category"] == s["category"]]
    return d, pool


def _tier_pool(rfm, s):
    """客户档案营销层级口径的客户池（复购率/唤醒率用，对应页②页③ KPI）"""
    p = rfm
    if s.get("gender"):
        p = p[p["Gender"] == s["gender"]]
    if s.get("seg"):
        p = p[p["CustomerTier"] == s["seg"]]
    return p


def _scope_text(s):
    parts = []
    if s.get("year") and s.get("month"):
        parts.append(f"{s['year']}年{s['month']}月")
    elif s.get("year"):
        parts.append(f"{s['year']}年")
    elif s.get("month"):
        parts.append(f"各年{s['month']}月")
    if s.get("latest"):
        parts.append("最新月")
    if s.get("gender"):
        parts.append(s["gender"] + "性")
    if s.get("seg"):
        parts.append(s["seg"])
    if s.get("category"):
        parts.append(s["category"])
    if s.get("strat"):
        parts.append(s["strat"] + "策略")
    return "".join(parts)


def _wan(v):
    return f"{v / WAN:,.1f}"


def _understood(s):
    """把识别到的槽位变成人话，前端展示「我按这些条件理解」"""
    out = []
    if s.get("year"):
        out.append(f"年份 {s['year']}")
    if s.get("month"):
        out.append(f"月份 {s['month']}月")
    if s.get("latest"):
        out.append("时间 最新月")
    if s.get("seg"):
        out.append(f"客群 {s['seg']}")
    if s.get("category"):
        out.append(f"品类 {s['category']}")
    if s.get("strat"):
        out.append(f"策略组 {s['strat']}")
    if s.get("gender"):
        out.append(f"性别 {s['gender']}")
    if s.get("metric"):
        out.append(f"指标 {METRIC_LABEL.get(s['metric'], s['metric'])}")
    return out


def _examples(df, camp=None):
    years = sorted(int(y) for y in df["Year"].unique()) if "Year" in df else []
    y = years[-1] if years else 2025
    return [
        f"{y}年3月销售额",
        f"{y}年高价值客户复购率",
        "沉睡客户有多少人",
        f"{y}年 ROI 最高的营销活动",
        "销售额 Top 3 品类",
    ]


def _public_slots(s):
    keys = ("year", "month", "seg", "category", "strat", "gender", "metric", "dim", "topn", "latest")
    return {k: s.get(k) for k in keys}


def _slot_str(v):
    return "—" if v is None else str(v)


def _guide(reason, df, camp=None, slots=None):
    return {
        "ok": False,
        "question": (slots or {}).get("_text", ""),
        "answer": reason,
        "value": None,
        "unit": "",
        "metric": None,
        "detail": None,
        "examples": _examples(df, camp),
        "suggest": _examples(df, camp)[:3],
        "slots": _public_slots(slots or {}),
        "understood": [],
        "note": "本问答为离线规则解析：能识别年份/月份/客群/品类/性别/策略组 + 7 类指标。"
                "没听懂时不猜，直接给示例问句。",
    }


def _fmt(metric, v):
    if v is None:
        return "—", METRIC_UNIT.get(metric, "")
    if metric == "sales":
        return _wan(v), "万元"
    if metric in ("customers", "orders"):
        return f"{int(v):,}", METRIC_UNIT[metric]
    if metric == "aov":
        return f"{v:,.0f}", "元"
    if metric in ("repurchase", "awaken_rate"):
        return f"{v:.1f}", "%"
    if metric == "roi":
        return f"{v:.2f}", ""
    return str(v), ""


# ---------------------------------------------------------------- 主入口
def answer(question, df, rfm, camp, cust_map=None, preslots=None):
    """主入口：解析 + 聚合 + 组装答案。任何异常都不外抛，返回引导话术。

    preslots：⑨ 本地 LLM 经 schema 校验后的槽位（见 llm_slots.py）。
    传了就跳过规则 parse()，但**聚合链路完全相同**——LLM 只决定「查什么」，
    不参与「算出多少」。
    """
    try:
        return _answer(question, df, rfm, camp, cust_map, preslots)
    except Exception as e:     # 兜底：问答是辅助功能，绝不让它 500
        return {
            "ok": False,
            "answer": "这个问题我暂时算不出来（内部聚合异常），换一种问法试试，或放宽筛选条件。",
            "value": None, "unit": "", "metric": None, "detail": None,
            "examples": _examples(df, camp), "suggest": _examples(df, camp)[:3],
            "slots": {}, "understood": [],
            "note": f"调试信息：{type(e).__name__}",
        }


def _answer(question, df, rfm, camp, cust_map, preslots=None):
    vocab = build_vocab(df, rfm, camp)
    if preslots:
        # 防御性拷贝 + 只取模板内的键：调用方塞进来的任何额外字段都进不了聚合
        s = dict(SLOT_TEMPLATE)
        for k in SLOT_TEMPLATE:
            if k in preslots and preslots[k] is not None:
                s[k] = preslots[k]
        s = _finalize_slots(s, _clean(question))
    else:
        s = parse(question, vocab)
    qtext = s["_text"]

    if not qtext:
        return _guide("请先输入一个问题，例如「2025年3月销售额」。", df, camp, s)
    if s["metric"] is None and s["dim"] is None:
        return _guide("没看懂你想查哪个指标。我支持：销售额、购买客户数、交易笔数、"
                      "笔单价、复购率、ROI、唤醒率，以及「X 最高的品类 / 营销活动 / 客户」。",
                      df, camp, s)

    # 最新月：解析成数据里最后一个有交易的月份
    if s["latest"]:
        last = df["YearMonth"].max()
        s["year"], s["month"] = int(str(last)[:4]), int(str(last)[5:7])

    # ---------- 分维度问句 ----------
    if s["dim"] == "campaign":
        return _answer_campaign(s, camp)
    if s["dim"] in ("category", "segment", "customer"):
        r = _answer_rank(s, df, rfm, cust_map)
        if r is not None:
            return r

    # ---------- 标量问句 ----------
    metric = s["metric"]
    scope = _scope_text(s)

    if metric == "roi":
        return _answer_roi(s, camp, scope)
    if metric == "awaken_rate":
        return _answer_awaken(s, df, rfm, scope)
    if metric == "repurchase":
        return _answer_repurchase(s, df, rfm, scope)
    return _answer_scalar(s, df, rfm, scope)


def _answer_scalar(s, df, rfm, scope):
    """销售额 / 购买客户数 / 交易笔数 / 笔单价"""
    d, pool = _scope(df, rfm, s)
    metric = s["metric"]
    label = METRIC_LABEL[metric]

    # 「沉睡客户有多少人」这类没有时间窗口的人数问句 → 客户档案口径（页③ KPI 同源）
    no_window = s.get("year") is None and s.get("month") is None and not s.get("category")
    if metric == "customers" and s.get("seg") and no_window:
        tier_n = int(len(_tier_pool(rfm, s)))
        rfm_n = int(len(pool))
        val = tier_n
        detail = {"type": "table",
                  "columns": ["口径", "人数", "说明"],
                  "rows": [["客户档案营销层级（CustomerTier）", f"{tier_n:,}", "页③ KPI「沉睡客户数」同源"],
                           ["RFM 客群（RFM_Segment）", f"{rfm_n:,}", "全局筛选器「客群」同源"]]}
        same = tier_n == rfm_n
        sent = (f"{scope}共 {tier_n:,} 人（客户档案营销层级口径，与页③ KPI 一致）。"
                + ("" if same else f" 按 RFM 客群口径（筛选器「客群」）为 {rfm_n:,} 人。"))
        return {
            "ok": True, "question": s["_text"], "answer": sent,
            "value": f"{tier_n:,}", "unit": "人", "metric": metric, "metric_label": label,
            "detail": detail, "examples": _examples(df), "suggest": _suggest(s, df),
            "slots": _public_slots(s), "understood": _understood(s),
            "note": "两套客群同名不同义：CustomerTier 是客户档案里的营销层级（页②③ 用），"
                    "RFM_Segment 是按 R/F/M 打分重新分的客群（页① 与筛选器用）。",
            "follow": _follow(s),
        }

    if len(d) == 0:
        return {
            "ok": True, "empty": True, "question": s["_text"],
            "answer": f"{scope}没有交易记录，无法计算{label}。",
            "value": None, "unit": METRIC_UNIT.get(metric, ""), "metric": metric,
            "detail": None, "examples": _examples(df),
            "suggest": _suggest(s, df), "slots": _public_slots(s),
            "understood": _understood(s),
            "note": "该组合在事实表中没有成交记录（例如沉睡客户在时间窗口内通常无交易）。",
        }

    if metric == "sales":
        v = float(d["TotalAmount"].sum())
        orders = int(len(d))
        val_txt = _wan(v)
        sent = (f"{scope}{label}为 {val_txt} 万元"
                f"（{orders:,} 笔，笔单价 {v / max(orders, 1):,.0f} 元，"
                f"{int(d['CustomerKey'].nunique()):,} 名客户）。")
        extra = {"orders": orders}
    elif metric == "orders":
        v = int(len(d))
        val_txt = f"{v:,}"
        sent = f"{scope}{label}为 {val_txt} 笔（{int(d['CustomerKey'].nunique()):,} 名客户）。"
        extra = {}
    elif metric == "aov":
        v = float(d["TotalAmount"].mean())
        val_txt = f"{v:,.0f}"
        sent = f"{scope}{label}为 {val_txt} 元（{len(d):,} 笔交易）。"
        extra = {}
    else:   # customers（有时间窗口）
        v = int(d["CustomerKey"].nunique())
        val_txt = f"{v:,}"
        sent = f"{scope}{label}为 {val_txt} 人。"
        extra = {}

    return {
        "ok": True, "question": s["_text"], "answer": sent,
        "value": val_txt, "unit": METRIC_UNIT[metric], "metric": metric, "metric_label": label,
        "detail": _detail_scalar(d, s, metric, extra),
        "examples": _examples(df), "suggest": _suggest(s, df),
        "slots": _public_slots(s), "understood": _understood(s),
        "note": "口径：销售额=ΣTotalAmount；购买客户数=窗口内去重客户数；"
                "笔单价=ΣTotalAmount÷笔数。与页① KPI 同源，数字可逐笔复算。",
        "follow": _follow(s),
    }


def _answer_repurchase(s, df, rfm, scope):
    """复购率：客户档案营销层级口径（页② KPI 同源）；有时间窗口时按窗口内下单次数判定"""
    cohort = _tier_pool(rfm, s)
    win = s.get("year") is not None or s.get("month") is not None or s.get("category")
    if win:
        keys = set(cohort["CustomerKey"])
        w = df[df["CustomerKey"].isin(keys)]
        if s.get("year") is not None:
            w = w[w["Year"] == s["year"]]
        if s.get("month") is not None:
            w = w[w["Month"] == s["month"]]
        if s.get("category"):
            w = w[w["Category"] == s["category"]]
        per = w.groupby("CustomerKey")["TransactionID"].count()
        n = int(len(per))
        rep = int((per >= 2).sum())
    else:
        n = int(len(cohort))
        rep = int((cohort["F"] >= 2).sum())
    if n == 0:
        return _guide(f"{scope}没有可统计的客户，无法计算复购率。", df, None, s)
    rate = rep / n * 100
    val_txt = f"{rate:.1f}"
    sent = (f"{scope}复购率为 {val_txt}%（{n:,} 名客户中 {rep:,} 人"
            f"{'窗口内' if win else ''}下单≥2 次）。")
    note = ("口径：复购率 = 下单≥2 次客户 ÷ 客户数，客户按客户档案营销层级（CustomerTier）划分，"
            "与页② KPI 同源。")
    if s.get("seg"):
        seg_n = int(len(_scope(df, rfm, s)[1]))
        note += (f" 注：本次按客户档案口径的「{s['seg']}」{n:,} 人计算；"
                 f"若按 RFM 客群口径（{seg_n:,} 人，筛选器「客群」）则该分群本身按频次划分，"
                 f"复购率会失真（近 100%），故不作为答案。")
    return {
        "ok": True, "question": s["_text"], "answer": sent,
        "value": val_txt, "unit": "%", "metric": "repurchase", "metric_label": "复购率",
        "detail": {"type": "table", "columns": ["项目", "数值"],
                   "rows": [["客户数（客户档案口径）", f"{n:,}"],
                            ["其中下单≥2 次", f"{rep:,}"],
                            ["复购率", f"{rate:.1f}%"]]},
        "examples": _examples(df), "suggest": _suggest(s, df),
        "slots": _public_slots(s), "understood": _understood(s),
        "note": note, "follow": _follow(s),
    }


def _answer_awaken(s, df, rfm, scope):
    """唤醒率：沉睡客群专属指标，客户档案营销层级口径（页③ KPI 同源）"""
    pool = rfm
    if s.get("gender"):
        pool = pool[pool["Gender"] == s["gender"]]
    sleep = pool[pool["CustomerTier"] == "沉睡客户"]
    n = int(len(sleep))
    if n == 0:
        return _guide("当前筛选下没有沉睡客户，无法计算唤醒率。", df, None, s)
    awake = int(sleep["IsAwakened"].sum())
    rate = awake / n * 100
    val_txt = f"{rate:.1f}"

    rows = [["客户档案口径（页③ KPI）", f"{n:,}", f"{awake:,}", f"{rate:.1f}%"]]
    seg_n = int((rfm["RFM_Segment"] == "沉睡客户").sum())
    seg_awake = int(rfm[(rfm["RFM_Segment"] == "沉睡客户")]["IsAwakened"].sum())
    if seg_n and seg_n != n:
        rows.append(["RFM 客群口径（筛选器）", f"{seg_n:,}", f"{seg_awake:,}",
                     f"{seg_awake / seg_n * 100:.1f}%"])

    sent = (f"沉睡客户唤醒率为 {val_txt}%（沉睡客户 {n:,} 人，其中已唤醒 {awake:,} 人）。")
    if len(rows) > 1:
        sent += (f" 注：按 RFM 客群口径则为 {seg_awake / seg_n * 100:.1f}%"
                 f"（{seg_n:,} 人中唤醒 {seg_awake:,} 人），页③ KPI 用前者。")
    return {
        "ok": True, "question": s["_text"], "answer": sent,
        "value": val_txt, "unit": "%", "metric": "awaken_rate", "metric_label": "唤醒率",
        "detail": {"type": "table",
                   "columns": ["口径", "沉睡客户数", "已唤醒", "唤醒率"], "rows": rows},
        "examples": _examples(df), "suggest": _suggest(s, df),
        "slots": _public_slots(s), "understood": _understood(s),
        "note": "口径：唤醒率 = IsAwakened 客户数 ÷ 沉睡客户数；沉睡客户按客户档案营销层级"
                "（CustomerTier）判定，与页③ KPI 同源。RFM 客群口径另列一行供对照。",
        "follow": _follow(s),
    }


def _detail_scalar(d, s, metric, extra):
    """标量问句的补充明细：按最相关维度拆一层，全部真实聚合"""
    if len(d) == 0:
        return None
    cols, rows = None, []
    if metric in ("sales", "orders"):
        if not s.get("category"):
            g = d.groupby("Category")["TotalAmount"].agg(["sum", "count"]).sort_values("sum", ascending=False)
            total = float(g["sum"].sum()) or 1.0
            cols = ["品类", "销售额(万)", "占比", "订单数"]
            rows = [[str(i), _wan(r["sum"]), f"{r['sum'] / total * 100:.1f}%", f"{int(r['count']):,}"]
                    for i, r in g.iterrows()]
        else:
            g = d.groupby("YearMonth")["TotalAmount"].agg(["sum", "count"]).sort_index()
            total = float(g["sum"].sum()) or 1.0
            cols = ["月份", "销售额(万)", "占比", "订单数"]
            rows = [[str(i), _wan(r["sum"]), f"{r['sum'] / total * 100:.1f}%", f"{int(r['count']):,}"]
                    for i, r in g.iterrows()]
    elif metric == "customers":
        g = d.groupby("RFM_Segment")["CustomerKey"].nunique().sort_values(ascending=False)
        n = int(g.sum()) or 1
        cols = ["客群", "购买客户数", "占比"]
        rows = [[str(i), f"{int(v):,}", f"{v / n * 100:.1f}%"] for i, v in g.items()]
    elif metric == "aov":
        g = d.groupby("Category")["TotalAmount"].agg(["mean", "count"]).sort_values("mean", ascending=False)
        cols = ["品类", "笔单价(元)", "订单数"]
        rows = [[str(i), f"{r['mean']:,.0f}", f"{int(r['count']):,}"] for i, r in g.iterrows()]
    if not cols:
        return None
    return {"type": "table", "columns": cols, "rows": rows[:8]}


# ---------------------------------------------------------------- 分维度
def _answer_rank(s, df, rfm, cust_map):
    d, pool = _scope(df, rfm, s)
    if len(d) == 0:
        return None
    scope = _scope_text(s)
    n = s.get("topn") or 5
    asc = s.get("order") == "asc"

    if s["dim"] == "category":
        g = d.groupby("Category")["TotalAmount"].sum().sort_values(ascending=asc)
        total = float(g.sum()) or 1.0
        top = g.head(n)
        rows = [[str(i + 1), str(k), _wan(v), f"{v / total * 100:.1f}%"]
                for i, (k, v) in enumerate(top.items())]
        best, best_v = top.index[0], float(top.iloc[0])
        word = "最低" if asc else "最高"
        sent = (f"{scope}销售额{word}的品类是「{best}」，{_wan(best_v)} 万元，"
                f"占该口径 {best_v / total * 100:.1f}%。")
        if n > 1:
            sent += f" 前 {len(top)} 名见下表。"
        return {
            "ok": True, "question": s["_text"], "answer": sent,
            "value": _wan(best_v), "unit": "万元", "metric": "sales", "metric_label": "销售额",
            "detail": {"type": "table", "columns": ["排名", "品类", "销售额(万)", "占比"], "rows": rows},
            "examples": _examples(df), "suggest": _suggest(s, df),
            "slots": _public_slots(s), "understood": _understood(s),
            "note": "口径：按品类汇总 ΣTotalAmount（与页②「各品类销售金额排名」同口径）。",
            "follow": {"link": {"category": best}},
        }

    if s["dim"] == "segment":
        g = d.groupby("RFM_Segment")["TotalAmount"].sum().sort_values(ascending=asc)
        total = float(d["TotalAmount"].sum()) or 1.0
        rows = [[str(i + 1), str(k), _wan(v), f"{v / total * 100:.1f}%"]
                for i, (k, v) in enumerate(g.items())]
        best, best_v = g.index[0], float(g.iloc[0])
        word = "最低" if asc else "最高"
        return {
            "ok": True, "question": s["_text"],
            "answer": f"{scope}贡献销售额{word}的是「{best}」，{_wan(best_v)} 万元，占 {best_v / total * 100:.1f}%。",
            "value": _wan(best_v), "unit": "万元", "metric": "sales", "metric_label": "销售额",
            "detail": {"type": "table", "columns": ["排名", "客群", "销售额(万)", "占比"], "rows": rows},
            "examples": _examples(df), "suggest": _suggest(s, df),
            "slots": _public_slots(s), "understood": _understood(s),
            "note": "口径：客群 = RFM_Segment（与全局筛选器「客群」一致）；销售额 = ΣTotalAmount。",
            "follow": {"filters": {"tier": best}},
        }

    # customer：消费最高的客户
    g = d.groupby("CustomerKey").agg(sales=("TotalAmount", "sum"),
                                     orders=("TransactionID", "count"),
                                     last=("YearMonth", "max")).sort_values("sales", ascending=asc)
    g = g.head(n)
    seg_map = dict(zip(rfm["CustomerKey"], rfm["RFM_Segment"]))
    rows = []
    for i, (k, r) in enumerate(g.iterrows()):
        cid = str(cust_map.get(k, f"CUST-{int(k)}")) if cust_map else f"#{int(k)}"
        rows.append([str(i + 1), cid, str(seg_map.get(k, "")), _wan(r["sales"]),
                     f"{int(r['orders'])}", str(r["last"])])
    best = g.index[0]
    best_id = str(cust_map.get(best, f"#{int(best)}")) if cust_map else f"#{int(best)}"
    word = "最低" if asc else "最高"
    return {
        "ok": True, "question": s["_text"],
        "answer": f"{scope}消费{word}的客户是 {best_id}，{_wan(float(g.iloc[0]['sales']))} 万元"
                  f"（{int(g.iloc[0]['orders'])} 笔，最近购买 {g.iloc[0]['last']}）。",
        "value": _wan(float(g.iloc[0]["sales"])), "unit": "万元", "metric": "sales",
        "metric_label": "客户销售额",
        "detail": {"type": "table",
                   "columns": ["排名", "客户ID", "客群", "销售额(万)", "笔数", "最近购买"], "rows": rows},
        "examples": _examples(df), "suggest": _suggest(s, df),
        "slots": _public_slots(s), "understood": _understood(s),
        "note": "客户 ID 取自 Dim_Customer；销售额为所选时间窗口内的 ΣTotalAmount。",
        "follow": {},
    }


def _camp_rows(cf):
    g = cf.groupby("StrategyGroup").agg(cost=("Cost", "sum"), rev=("Revenue", "sum")).reset_index()
    return [[str(r["StrategyGroup"]), _wan(r["cost"]), _wan(r["rev"]),
             f"{r['rev'] / r['cost']:.2f}" if r["cost"] else "-"] for _, r in g.iterrows()]


def _answer_campaign(s, camp):
    cf = camp if s.get("year") is None else camp[camp["Year"] == s["year"]]
    if s.get("strat"):
        cf = cf[cf["StrategyGroup"] == s["strat"]]
    if len(cf) == 0:
        return {
            "ok": True, "empty": True, "question": s["_text"],
            "answer": f"{_scope_text(s)}没有营销活动记录。", "value": None, "unit": "",
            "metric": "roi", "detail": None, "examples": _examples(camp, camp),
            "suggest": ["2025年 ROI 最高的营销活动", "各策略组 ROI 对比"],
            "slots": _public_slots(s), "understood": _understood(s),
            "note": "活动表按 StartDate 的年份归属。",
        }
    asc = s.get("order") == "asc"
    cf = cf.assign(_roi=cf["Revenue"] / cf["Cost"].replace(0, pd.NA))
    cf = cf.dropna(subset=["_roi"]).sort_values("_roi", ascending=asc)
    if len(cf) == 0:
        return _guide("没有可计算 ROI 的活动（成本为 0）。", camp, camp, s)
    r = cf.iloc[0]
    n = s.get("topn") or 5
    rows = [[str(i + 1), str(x["CampaignName"]), str(x["StrategyGroup"]),
             _wan(x["Cost"]), _wan(x["Revenue"]), f"{x['_roi']:.2f}"]
            for i, (_, x) in enumerate(cf.head(n).iterrows())]
    word = "最低" if asc else "最高"
    scope = _scope_text(s) or "全时段"
    sent = (f"{scope} ROI {word}的营销活动是「{r['CampaignName']}」，ROI {r['_roi']:.2f}"
            f"（投入 {_wan(r['Cost'])} 万，产出 {_wan(r['Revenue'])} 万，"
            f"策略组 {r['StrategyGroup']}）。")
    return {
        "ok": True, "question": s["_text"], "answer": sent,
        "value": f"{r['_roi']:.2f}", "unit": "", "metric": "roi", "metric_label": "营销 ROI",
        "detail": {"type": "table",
                   "columns": ["排名", "营销活动", "策略组", "投入(万)", "产出(万)", "ROI"], "rows": rows},
        "examples": _examples(camp, camp), "suggest": _suggest(s, camp),
        "slots": _public_slots(s), "understood": _understood(s),
        "note": "口径：单活动 ROI = 该活动 Revenue ÷ Cost（活动表自带字段）；页④ 的整体 ROI 是 "
                "ΣRevenue÷ΣCost 的加权口径，两者层级不同。",
        "follow": _follow(s),
    }


def _answer_roi(s, camp, scope):
    cf = camp if s.get("year") is None else camp[camp["Year"] == s["year"]]
    if s.get("strat"):
        cf = cf[cf["StrategyGroup"] == s["strat"]]
    if len(cf) == 0 or cf["Cost"].sum() == 0:
        return {
            "ok": True, "empty": True, "question": s["_text"],
            "answer": f"{scope}没有可用的营销投入记录。", "value": None, "unit": "",
            "metric": "roi", "detail": None, "examples": _examples(camp, camp),
            "suggest": ["2025年 ROI 最高的营销活动", "2025年整体 ROI"],
            "slots": _public_slots(s), "understood": _understood(s),
            "note": "活动表按 StartDate 的年份归属。",
        }
    roi = float(cf["Revenue"].sum() / cf["Cost"].sum())
    return {
        "ok": True, "question": s["_text"],
        "answer": (f"{scope}整体营销 ROI 为 {roi:.2f}（投入 {_wan(cf['Cost'].sum())} 万，"
                   f"产出 {_wan(cf['Revenue'].sum())} 万，{len(cf)} 个活动）。"),
        "value": f"{roi:.2f}", "unit": "", "metric": "roi", "metric_label": "营销 ROI",
        "detail": {"type": "table", "columns": ["策略组", "投入(万)", "产出(万)", "ROI"],
                   "rows": _camp_rows(cf)},
        "examples": _examples(camp, camp), "suggest": _suggest(s, camp),
        "slots": _public_slots(s), "understood": _understood(s),
        "note": "口径：ROI = ΣRevenue ÷ ΣCost（与页④ KPI 一致）；成本仅含活动投放费。",
        "follow": _follow(s),
    }


# ---------------------------------------------------------------- 建议追问
def _suggest(s, df):
    if s.get("dim") == "campaign":
        return ["各策略组 ROI 对比", "2025年营销投入与产出", "精准滴灌 ROI"]
    out = []
    if s.get("seg"):
        out.append(f"{s['seg']}销售额最高的品类")
        out.append(f"{s['seg']}复购率")
    if s.get("year") and s.get("month"):
        out.append(f"{s['year']}年整体销售额")
    elif s.get("year"):
        out.append(f"{s['year']}年各月销售额")
        out.append(f"{s['year']}年 ROI 最高的营销活动")
    if not out:
        out = ["各客群销售额排名", "销售额 Top 3 品类", "沉睡客户唤醒率"]
    out.append("沉睡客户有多少人")
    seen, res = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            res.append(x)
    return res[:3]


def _follow(s):
    f, l = {}, {}
    if s.get("seg"):
        f["tier"] = s["seg"]
    if s.get("year"):
        f["year"] = str(s["year"])
    if s.get("month"):
        f["month"] = str(s["month"])
    if s.get("gender"):
        f["gender"] = s["gender"]
    if s.get("category"):
        l["category"] = s["category"]
    return {"filters": f, "link": l}
