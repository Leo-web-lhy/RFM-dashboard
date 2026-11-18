# -*- coding: utf-8 -*-
"""⑦ 中文问答解析器单元测试（不依赖 Flask / 不联网）

运行：python test_ask_parser.py -v
覆盖：
  A. parse() 槽位抽取：年份/月份/客群/品类/性别/策略组/指标/维度/TopN
  B. answer() 数值与页面口径一致：用独立 pandas 复算（不复用 ask_parser 内部函数）
  C. 兜底：乱码、空串、无数据年份 → 不抛异常、返回引导
  D. 全部返回值结构完整（前端渲染所需字段齐全）
"""
import os
import unittest

import pandas as pd

import ask_parser as ap

BASE = os.path.dirname(os.path.abspath(__file__))


def _load():
    """按 load_data 的口径独立重建事实宽表（测试里重写一遍，避免与被测代码同源出错）"""
    tx = pd.read_csv(os.path.join(BASE, "Fact_Transaction.csv"))
    cust = pd.read_csv(os.path.join(BASE, "Dim_Customer.csv"))
    prod = pd.read_csv(os.path.join(BASE, "Dim_Product.csv"))
    tm = pd.read_csv(os.path.join(BASE, "Dim_Time.csv"))
    rfm = pd.read_csv(os.path.join(BASE, "Agg_Customer_RFM.csv"))
    camp = pd.read_csv(os.path.join(BASE, "Fact_Marketing_Campaign.csv"))

    df = tx.merge(cust.drop(columns=["CustomerID"]), on="CustomerKey", how="left")
    df = df.merge(prod.drop(columns=["UnitPrice"]), on="ProductKey", how="left")
    df = df.merge(tm.drop(columns=["Date"]), on="TimeKey", how="left")
    df = df.merge(rfm[["CustomerKey", "R_days", "F", "M", "RFM_Score", "RFM_Segment", "IsAwakened"]],
                  on="CustomerKey", how="left")
    df["OrderDate"] = pd.to_datetime(df["OrderDate"])
    df["YearMonth"] = df["OrderDate"].dt.to_period("M").astype(str)
    df["Year"] = df["OrderDate"].dt.year
    df["Month"] = df["OrderDate"].dt.month
    camp["Year"] = pd.to_datetime(camp["StartDate"]).dt.year
    cust_map = dict(zip(cust["CustomerKey"], cust["CustomerID"]))
    return df, rfm, camp, cust_map


DF, RFM, CAMP, CMAP = _load()


def ask(q):
    return ap.answer(q, DF, RFM, CAMP, CMAP)


class TestParse(unittest.TestCase):
    """A. 槽位抽取"""

    def check(self, q, **expect):
        s = ap.parse(q)
        for k, v in expect.items():
            self.assertEqual(s[k], v, f"{q!r} 的 {k} 期望 {v!r}，实际 {s[k]!r}")

    def test_year_month(self):
        self.check("2025年3月销售额", year=2025, month=3, metric="sales")
        self.check("2025-12 的 GMV", year=2025, month=12, metric="sales")
        self.check("2025/3 销售额", year=2025, month=3)
        self.check("2024年购买客户数", year=2024, metric="customers")
        self.check("3月销售额", month=3, year=None)
        self.check("销售额", month=None, year=None)

    def test_entity(self):
        self.check("2025年3月高价值客户销售额", seg="高价值客户")
        self.check("沉睡客户有多少人", seg="沉睡客户", metric="customers")
        self.check("服饰鞋包销售额", category="服饰鞋包")
        self.check("美妆护肤 2025 年销售额", category="美妆护肤", year=2025)
        self.check("女性客户销售额", gender="女")
        self.check("2025年精准滴灌 ROI", strat="精准滴灌", metric="roi")

    def test_metric_priority(self):
        # 「复购率」必须先于「客户数」，「销售额」先于「销售」
        self.check("2025年3月复购率", metric="repurchase")
        self.check("2025年3月销售额", metric="sales")
        self.check("2025年3月销售", metric="sales")
        self.check("2025年笔单价", metric="aov")
        self.check("沉睡客户唤醒率", metric="awaken_rate")
        self.check("2025年交易笔数", metric="orders")

    def test_dim_and_topn(self):
        self.check("销售额 Top 3 品类", dim="category", topn=3)
        self.check("哪个客群贡献最多", dim="segment")
        self.check("2024年 ROI 最高的营销活动", dim="campaign", metric="roi")
        self.check("消费最高的客户 Top 3", dim="customer", topn=3)
        self.check("品类销售排名", dim="category", topn=5)
        self.check("前三品类销售额", dim="category", topn=3)

    def test_order(self):
        self.check("ROI 最低的营销活动", order="asc", dim="campaign")
        self.check("销售额最低的品类", order="asc", dim="category")


class TestAnswerNumbers(unittest.TestCase):
    """B. 数值与独立复算一致"""

    def test_sales_month_seg(self):
        r = ask("2025年3月高价值客户销售额")
        self.assertTrue(r["ok"])
        d = DF[(DF["Year"] == 2025) & (DF["Month"] == 3) & (DF["RFM_Segment"] == "高价值客户")]
        self.assertAlmostEqual(float(d["TotalAmount"].sum()) / 1e4,
                               float(r["value"].replace(",", "")), places=1)
        self.assertEqual(r["unit"], "万元")

    def test_sales_month_all(self):
        r = ask("2025年12月销售额")
        d = DF[(DF["Year"] == 2025) & (DF["Month"] == 12)]
        self.assertAlmostEqual(float(d["TotalAmount"].sum()) / 1e4,
                               float(r["value"].replace(",", "")), places=1)

    def test_customers_year(self):
        r = ask("2025年购买客户数")
        n = DF[DF["Year"] == 2025]["CustomerKey"].nunique()
        self.assertEqual(int(r["value"].replace(",", "")), int(n))

    def test_aov_year(self):
        r = ask("2025年笔单价")
        v = DF[DF["Year"] == 2025]["TotalAmount"].mean()
        self.assertEqual(int(r["value"].replace(",", "")), round(v))

    def test_orders(self):
        r = ask("2024年交易笔数")
        self.assertEqual(int(r["value"].replace(",", "")), int((DF["Year"] == 2024).sum()))

    def test_repurchase_tier(self):
        """高价值客户复购率 = 客户档案口径 F>=2 占比（页② 71.8%）"""
        r = ask("高价值客户复购率")
        g = RFM[RFM["CustomerTier"] == "高价值客户"]
        expect = (g["F"] >= 2).mean() * 100
        self.assertAlmostEqual(float(r["value"]), round(expect, 1), places=1)
        self.assertEqual(r["value"], "71.8")

    def test_awaken_rate(self):
        """唤醒率 = 162/774 = 20.9%（页③ KPI）"""
        r = ask("沉睡客户唤醒率")
        sleep = RFM[RFM["CustomerTier"] == "沉睡客户"]
        expect = sleep["IsAwakened"].sum() / len(sleep) * 100
        self.assertAlmostEqual(float(r["value"]), round(expect, 1), places=1)
        self.assertEqual(r["value"], "20.9")

    def test_sleep_count(self):
        """沉睡客户人数默认客户档案口径 774（页③），并附 RFM 客群口径 2000"""
        r = ask("沉睡客户有多少人")
        self.assertEqual(int(r["value"].replace(",", "")),
                         int((RFM["CustomerTier"] == "沉睡客户").sum()))
        self.assertIn("2,000", r["answer"])

    def test_campaign_best(self):
        r = ask("2024年 ROI 最高的营销活动")
        c24 = CAMP[CAMP["Year"] == 2024].assign(roi=lambda x: x["Revenue"] / x["Cost"])
        best = c24.sort_values("roi", ascending=False).iloc[0]
        self.assertAlmostEqual(float(r["value"]), round(float(best["roi"]), 2), places=2)
        self.assertIn(best["CampaignName"], r["answer"])

    def test_seg_rank(self):
        r = ask("哪个客群贡献最多")
        g = DF.groupby("RFM_Segment")["TotalAmount"].sum().sort_values(ascending=False)
        self.assertAlmostEqual(float(r["value"].replace(",", "")), g.iloc[0] / 1e4, places=1)
        self.assertIn(g.index[0], r["answer"])

    def test_category_top3(self):
        r = ask("销售额 Top 3 品类")
        g = DF.groupby("Category")["TotalAmount"].sum().sort_values(ascending=False)
        self.assertEqual(len(r["detail"]["rows"]), 3)
        self.assertEqual(r["detail"]["rows"][0][1], g.index[0])
        self.assertAlmostEqual(float(r["detail"]["rows"][0][2].replace(",", "")),
                               g.iloc[0] / 1e4, places=1)

    def test_customer_top(self):
        r = ask("消费最高的客户 Top 3")
        g = DF.groupby("CustomerKey")["TotalAmount"].sum().sort_values(ascending=False)
        self.assertEqual(len(r["detail"]["rows"]), 3)
        self.assertEqual(r["detail"]["rows"][0][1], CMAP[g.index[0]])

    def test_roi_strategy(self):
        r = ask("2025年精准滴灌 ROI")
        cf = CAMP[(CAMP["Year"] == 2025) & (CAMP["StrategyGroup"] == "精准滴灌")]
        self.assertAlmostEqual(float(r["value"]),
                               round(cf["Revenue"].sum() / cf["Cost"].sum(), 2), places=2)

    def test_month_only(self):
        r = ask("各年12月销售额")
        d = DF[DF["Month"] == 12]
        self.assertAlmostEqual(float(r["value"].replace(",", "")),
                               float(d["TotalAmount"].sum()) / 1e4, places=1)


class TestFallback(unittest.TestCase):
    """C. 兜底：不抛异常、给引导"""

    def test_garbage(self):
        for q in ["asdkjfhaskjdf", "今天天气怎么样", "帮我写一首诗", "???"]:
            r = ask(q)
            self.assertFalse(r["ok"], q)
            self.assertTrue(r["answer"])
            self.assertEqual(len(r["suggest"]), 3)

    def test_empty(self):
        r = ask("")
        self.assertFalse(r["ok"])
        self.assertIn("请先输入", r["answer"])

    def test_no_data_year(self):
        r = ask("2030年1月销售额")
        self.assertTrue(r["ok"])
        self.assertIsNone(r["value"])
        self.assertTrue(r.get("empty"))

    def test_long_input(self):
        r = ask("销售额" * 200)
        self.assertIn("ok", r)


class TestShape(unittest.TestCase):
    """D. 返回结构满足前端渲染"""

    QS = ["2025年3月高价值客户销售额", "2025年购买客户数", "2025年3月复购率",
          "沉睡客户唤醒率", "销售额 Top 3 品类", "哪个客群贡献最多",
          "2024年 ROI 最高的营销活动", "消费最高的客户 Top 3", "2025年笔单价"]

    def test_keys(self):
        for q in self.QS:
            r = ask(q)
            for k in ("ok", "answer", "value", "unit", "metric", "detail", "suggest", "slots", "note"):
                self.assertIn(k, r, f"{q} 缺字段 {k}")
            self.assertIsInstance(r["suggest"], list)
            self.assertLessEqual(len(r["suggest"]), 3)
            self.assertIsInstance(r["slots"], dict)

    def test_detail_table(self):
        for q in self.QS:
            r = ask(q)
            if r.get("detail"):
                t = r["detail"]
                self.assertEqual(t["type"], "table")
                self.assertGreaterEqual(len(t["columns"]), 2)
                for row in t["rows"]:
                    self.assertEqual(len(row), len(t["columns"]), f"{q} 行列数不齐")


if __name__ == "__main__":
    unittest.main(verbosity=2)
