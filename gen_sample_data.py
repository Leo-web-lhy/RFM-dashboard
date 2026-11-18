# -*- coding: utf-8 -*-
"""
RFM 大屏 - 脱敏示例数据生成器
==============================
用途：本项目源自真实电商项目，原始业务数据因保密协议不可公开。
本脚本按「相同表结构、相同统计口径」生成全随机模拟数据，用于：
1. 让 GitHub 上的仓库克隆后即可直接运行（python gen_sample_data.py）；
2. 求职展示时证明数据处理链路真实可用（脚本本身也是工程能力的一部分）。

生成规则
--------
- 固定随机种子（seed=42），任何人重新生成的数据完全一致，可复现；
- 客户 800 人 / 交易约 6500 笔 / 2023-01-01 ~ 2025-12-31；
- RFM 分层口径与真实项目一致：R=距基准日(2025-12-31)天数，
  F=有效订单数，M=累计净额；分位打分 1-5；
  分层：高价值客户 / 潜力客户 / 大众客户 / 沉睡客户(R>180天)。
"""
import os
import random
from datetime import date, timedelta

random.seed(42)
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(OUT, exist_ok=True)

START = date(2023, 1, 1)
END = date(2025, 12, 31)
BASE = END  # R_days 口径基准日
N_CUST = 800
N_TXN = 6500

CATEGORIES = {
    "家居生活": 150, "数码电子": 600, "美妆个护": 120,
    "食品饮料": 60, "服饰鞋包": 200, "户外运动": 300,
}
GENDERS = ["男", "女", "未知"]
AGES = ["18-25", "26-35", "36-45", "45+"]
CITIES = ["一线城市", "二线城市", "三线及以下"]
SEG_GUESS = ["大众客户", "潜力客户", "高价值客户"]  # 新客默认分层


def w(path, header, rows):
    with open(os.path.join(OUT, path), "w", encoding="utf-8-sig", newline="") as f:
        f.write(",".join(header) + "\n")
        for r in rows:
            f.write(",".join(str(x) for x in r) + "\n")
    print(f"  data/{path}  {len(rows)} 行")


def rdate(d0=START, d1=END):
    return d0 + timedelta(days=random.randint(0, (d1 - d0).days))


# ---------------- Dim_Time ----------------
dates = []
d = START
while d <= END:
    dates.append(d)
    d += timedelta(days=1)
w("Dim_Time.csv",
  ["TimeKey", "Date", "Year", "Quarter", "Month", "IsWeekend"],
  [(int(x.strftime("%Y%m%d")), x.isoformat(), x.year, f"Q{(x.month - 1) // 3 + 1}",
    x.month, x.weekday() >= 5) for x in dates])
TIME_KEYS = [int(x.strftime("%Y%m%d")) for x in dates]

# ---------------- Dim_Product ----------------
products = []
pk = 0
for cat, price_mid in CATEGORIES.items():
    for i in range(1, 9):
        pk += 1
        price = round(price_mid * random.uniform(0.4, 1.8), 2)
        products.append((pk, f"{cat}_商品{i:02d}", cat, price,
                         round(price * random.uniform(0.45, 0.75), 2)))
w("Dim_Product.csv",
  ["ProductKey", "ProductName", "Category", "UnitPrice", "CostPrice"], products)

# ---------------- Dim_Customer ----------------
customers = []
for k in range(1, N_CUST + 1):
    customers.append({
        "key": k, "id": f"CUST{k:06d}",
        "gender": random.choices(GENDERS, [47, 47, 6])[0],
        "age": random.choice(AGES),
        "reg": rdate(),
        "tier": "大众客户",
        "city": random.choices(CITIES, [28, 38, 34])[0],
    })
w("Dim_Customer.csv",
  ["CustomerKey", "CustomerID", "Gender", "AgeGroup", "RegisterDate",
   "CustomerTier", "CityLevel"],
  [(c["key"], c["id"], c["gender"], c["age"], c["reg"].isoformat(),
    c["tier"], c["city"]) for c in customers])

# ---------------- Fact_Transaction ----------------
# 每位客户 1~20 笔订单，购物频率呈长尾分布；近一年更活跃
txns = []
for i in range(N_TXN):
    c = random.choice(customers)
    p = random.choice(products)
    # 交易日期偏 recent-heavy，保证近月有数据（预测/沙盘可用）
    if random.random() < 0.45:
        td = rdate(date(2025, 1, 1), END)
    else:
        td = rdate()
    qty = random.choices([1, 2, 3, 4, 5], [38, 27, 18, 10, 7])[0]
    unit = round(p[3] * random.uniform(0.85, 1.15), 2)
    disc = random.choice([1.0, 1.0, 0.95, 0.9, 0.88, 0.8])
    total = round(qty * unit * disc, 2)
    txns.append({
        "id": f"TXN{i + 1:08d}", "tk": int(td.strftime("%Y%m%d")),
        "ck": c["key"], "pk": p[0], "qty": qty, "unit": unit,
        "total": total, "od": td.isoformat(), "disc": round(disc, 4),
    })
w("Fact_Transaction.csv",
  ["TransactionID", "TimeKey", "CustomerKey", "ProductKey", "Quantity",
   "UnitPrice", "TotalAmount", "IsAwakenedOrder", "OrderDate", "DiscountRate"],
  [(t["id"], t["tk"], t["ck"], t["pk"], t["qty"], t["unit"], t["total"],
    False, t["od"], t["disc"]) for t in txns])

# ---------------- Fact_Marketing_Campaign ----------------
strategies = [
    ("广告投放(大水漫灌)", 0.55), ("广告投放(精准投放)", 1.60),
    ("会员运营", 2.10), ("促销活动", 1.25), ("私域运营", 1.90),
]
camps = []
for i, (name, roi_mid) in enumerate(strategies, 1):
    sd = rdate(START, END - timedelta(days=60))
    cost = round(random.uniform(3, 12) * 10000, 2)
    roi = round(max(0.3, random.gauss(roi_mid, 0.25)), 2)
    camps.append({
        "key": i, "name": f"{sd.strftime('%Y%m')}_{name}_活动",
        "st": name, "stk": int(sd.strftime("%Y%m%d")),
        "cost": cost, "rev": round(cost * roi, 2), "roi": roi,
        "seg": random.choice(SEG_GUESS), "sd": sd.isoformat(),
        "grp": name, "stage": "早期(2023-2024)" if sd.year <= 2024 else "成熟期(2025)",
    })
w("Fact_Marketing_Campaign.csv",
  ["CampaignKey", "CampaignName", "StrategyType", "StartTimeKey", "Cost",
   "Revenue", "ROI", "TargetSegment", "StartDate", "StrategyGroup", "Stage"],
  [(c["key"], c["name"], c["st"], c["stk"], c["cost"], c["rev"], c["roi"],
    c["seg"], c["sd"], c["grp"], c["stage"]) for c in camps])

# ---------------- Agg_Customer_RFM（与真实项目同口径预计算） ----------------
by_cust = {}
for t in txns:
    by_cust.setdefault(t["ck"], []).append(t)

def score5(v, arr, reverse=False):
    """五分位打分 1-5；reverse=True 表示值越小得分越高（R 天数）"""
    s = sorted(arr, reverse=reverse)
    q = max(1, min(5, (s.index(v) * 5) // len(s) + 1)) if len(s) else 1
    return q

agg = []
for c in customers:
    ts = by_cust.get(c["key"], [])
    if not ts:
        continue
    m = round(sum(t["total"] for t in ts), 2)
    last = max(date.fromisoformat(t["od"]) for t in ts)
    r_days = (BASE - last).days
    f = len(ts)
    # 沉睡客户的历史唤醒订单：距上次成交超过 180 天后再次成交的笔数
    ods = sorted(date.fromisoformat(t["od"]) for t in ts)
    awakened = sum(1 for a, b in zip(ods, ods[1:])
                   if (b - a).days > 180)
    is_aw = awakened > 0
    agg.append({
        "ck": c["key"], "tier": c["tier"], "reg": c["reg"].isoformat(),
        "g": c["gender"], "a": c["age"], "city": c["city"],
        "F": f, "M": m, "last": last.isoformat(), "aw": awakened,
        "r_days": r_days, "is_aw": is_aw,
    })

fs = [x["F"] for x in agg]
ms = [x["M"] for x in agg]
rs = [x["r_days"] for x in agg]
for x in agg:
    x["F_s"] = score5(x["F"], fs)
    x["M_s"] = score5(x["M"], ms)
    x["R_s"] = score5(x["r_days"], rs, reverse=True)
    x["score"] = x["F_s"] + x["M_s"] + x["R_s"]
    if x["r_days"] > 180:
        x["seg"] = "沉睡客户"
    elif x["F_s"] >= 4 and x["M_s"] >= 4:
        x["seg"] = "高价值客户"
    elif x["F_s"] >= 3 or x["M_s"] >= 3:
        x["seg"] = "潜力客户"
    else:
        x["seg"] = "大众客户"

w("Agg_Customer_RFM.csv",
  ["CustomerKey", "CustomerTier", "RegisterDate", "Gender", "AgeGroup",
   "CityLevel", "F", "M", "LastOrderDate", "AwakenedOrders", "R_days",
   "IsAwakened", "F_score", "M_score", "R_score", "RFM_Score", "RFM_Segment"],
  [(x["ck"], x["tier"], x["reg"], x["g"], x["a"], x["city"], x["F"], x["M"],
    x["last"], x["aw"], x["r_days"], x["is_aw"], x["F_s"], x["M_s"],
    x["R_s"], x["score"], x["seg"]) for x in agg])

print("\n完成：6 张脱敏示例 CSV 已生成到 data/ 目录，可直接启动大屏。")
