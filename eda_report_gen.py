# -*- coding: utf-8 -*-
"""
EDA 报告生成模块（RFM版本5.0 大屏「EDA报告生成」页）
按勾选的客群/品类/年份筛选数据子集，按勾选的章节生成 PDF 报告。
图表风格复用 RFM版本2.0 eda_report.py（A4 横向 + 深蓝标题栏 + 中文字体）。
"""
import os
import time
import textwrap

import numpy as np
# matplotlib 延迟导入（见 _init_mpl）：未安装 matplotlib 时不影响大屏启动，
# 仅当页⑤真正生成 PDF 报告时才需要。

plt = None
PdfPages = None
COLORS = []

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EDA_DIR = os.path.join(BASE_DIR, "eda_reports")

def _init_mpl():
    """延迟导入 matplotlib 并完成一次性全局配置（Agg 后端 + 中文字体 + 配色）。

    未安装 matplotlib 时仅页⑤「生成 EDA 报告」会报错，大屏其余功能不受影响。
    """
    global plt, PdfPages, COLORS
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    # 中文字体探测（Windows 优先微软雅黑）
    for font in ["Microsoft YaHei", "SimHei", "PingFang SC", "Noto Sans CJK SC"]:
        try:
            from matplotlib.font_manager import findfont, FontProperties
            path = findfont(FontProperties(family=font), fallback_to_default=False)
            if path:
                plt.rcParams["font.family"] = font
                break
        except Exception:
            continue
    plt.rcParams["axes.unicode_minus"] = False

    COLORS = ["#4C72B0", "#DD8452", "#55A868", "#C44E52", "#8172B3",
              "#937860", "#DA8BC3", "#8C8C8C"]
    plt.rcParams["axes.prop_cycle"] = plt.cycler(color=COLORS)

SECTION_KEYS = ["quality", "trend", "product", "customer", "rfm", "marketing", "findings"]
SECTION_NAMES = {
    "quality": "数据质量检查",
    "trend": "销售趋势分析",
    "product": "产品维度分析",
    "customer": "客户画像分析",
    "rfm": "RFM分布与唤醒",
    "marketing": "营销活动ROI",
    "findings": "关键发现与建议",
}


def _fmt_wan(x):
    return f"{x / 10000:,.1f}"


def _filter_subset(df, rfm, camp, options):
    """按勾选范围筛选数据子集，返回 (df_f, rfm_f, camp_f, scope_desc)"""
    segs = [str(s) for s in (options.get("segments") or [])]
    cats = [str(c) for c in (options.get("categories") or [])]
    years = [int(y) for y in (options.get("years") or [])]

    df_f, rfm_f = df, rfm
    if segs:
        rfm_f = rfm[rfm["RFM_Segment"].isin(segs)]
        keys = set(rfm_f["CustomerKey"])
        df_f = df[df["CustomerKey"].isin(keys)]
    if cats:
        df_f = df_f[df_f["Category"].isin(cats)]
    if years:
        df_f = df_f[df_f["Year"].isin(years)]

    camp_f = camp
    if years:
        camp_f = camp_f.copy()
        if "Year" not in camp_f.columns:
            camp_f["Year"] = pd_datetime_year(camp_f)
        camp_f = camp_f[camp_f["Year"].isin(years)]

    if len(df_f) == 0:
        raise ValueError("勾选条件下无交易数据，请调整勾选范围后重试")

    scope = {
        "segments": segs or ["全部"],
        "categories": cats or ["全部"],
        "years": years or ["全部"],
    }
    return df_f, rfm_f, camp_f, scope


def pd_datetime_year(camp):
    import pandas as pd
    return pd.to_datetime(camp["StartDate"]).dt.year


def _scope_text(scope):
    return ("客群: " + "、".join(scope["segments"]) +
            "   |   品类: " + "、".join(scope["categories"]) +
            "   |   年份: " + "、".join(str(y) for y in scope["years"]))


# =========================================================
# 各章节页面
# =========================================================
def page_cover(pdf, df_f, scope):
    """封面/概览：勾选范围 + 核心 KPI"""
    fig = plt.figure(figsize=(11.69, 8.27))
    fig.text(0.5, 0.80, "EDA 探索性分析报告", fontsize=30, fontweight="bold",
             ha="center", color="#1F3864")
    fig.text(0.5, 0.72, "RFM 客户分析项目 · 勾选数据子集分析", fontsize=16,
             ha="center", color="#2E5395")
    fig.text(0.5, 0.665, f"生成日期: {time.strftime('%Y-%m-%d %H:%M:%S')}",
             fontsize=10, ha="center", color="#666666")
    fig.text(0.5, 0.62, "数据范围: " + _scope_text(scope), fontsize=10.5,
             ha="center", color="#666666")

    total_rev = df_f["TotalAmount"].sum()
    # 整体毛利率=成交口径 (实付-成本)/实付（与报告书 §4.3.1 一致，约为 43.0%）；
    # 无成本信息的行（上传未含成本列）跳过；全部无成本时显示 "—" 而非 0%
    mask = df_f["CostPrice"].notna()
    if mask.any():
        gross = ((df_f["TotalAmount"][mask] - df_f["Quantity"][mask] * df_f["CostPrice"][mask]).sum()
                 / df_f["TotalAmount"][mask].sum() * 100)
    else:
        gross = float("nan")
    stats = [
        ("交易笔数", f"{len(df_f):,}"),
        ("客户数", f"{df_f['CustomerKey'].nunique():,}"),
        ("商品数", f"{df_f['ProductKey'].nunique()}"),
        ("总销售额(万元)", f"{total_rev/10000:,.0f}"),
        ("笔单价(元)", f"{df_f['TotalAmount'].mean():,.0f}"),
        ("整体毛利率", f"{gross:.1f}%" if np.isfinite(gross) else "—"),
    ]
    for i, (label, val) in enumerate(stats):
        x = 0.12 + (i % 3) * 0.28
        y = 0.44 - (i // 3) * 0.16
        fig.text(x + 0.09, y + 0.045, val, fontsize=20, fontweight="bold",
                 ha="center", color="#1F3864")
        fig.text(x + 0.09, y - 0.015, label, fontsize=10.5, ha="center", color="#666666")
    fig.text(0.5, 0.08, "本报告基于大屏勾选的数据子集生成，各章节对应勾选的分析内容。",
             fontsize=9.5, ha="center", color="#888888")
    pdf.savefig(fig)
    plt.close(fig)


def page_quality(pdf, df_f, scope):
    """数据质量检查：子集规模 / 空值 / 金额复算 / 主键唯一"""
    fig = plt.figure(figsize=(11.69, 8.27))
    fig.text(0.03, 0.955, "1. 数据质量检查", fontsize=17, fontweight="bold", color="#1F3864")
    fig.text(0.03, 0.918, "针对勾选数据子集的基础质量校验", fontsize=10, color="#666666")
    fig.add_artist(plt.Line2D([0.03, 0.97], [0.935, 0.935], color="#1F3864", lw=1.2,
                              transform=fig.transFigure))

    checks = []
    checks.append(("子集规模",
                   f"交易 {len(df_f):,} 行 / 客户 {df_f['CustomerKey'].nunique():,} 人 / "
                   f"商品 {df_f['ProductKey'].nunique()} 个 / 时间 "
                   f"{df_f['OrderDate'].min():%Y-%m-%d} ~ {df_f['OrderDate'].max():%Y-%m-%d}",
                   True))
    nulls = df_f.isna().sum()
    # 成本缺失是有意的业务状态（上传未含成本列 → 毛利率显示「—」），不计入空值异常
    nulls = nulls.drop(labels=["CostPrice", "GrossMargin"], errors="ignore")
    null_cols = nulls[nulls > 0]
    checks.append(("空值检查",
                   "无空值列" if null_cols.empty else f"存在空值: {null_cols.to_dict()}",
                   null_cols.empty))
    diff = (df_f["TotalAmount"] - df_f["UnitPrice"] * df_f["Quantity"] * df_f["DiscountRate"]).abs()
    # 上传行（TransactionID 前缀 UP）的单价为 4 位小数复算值，容差放宽为 max(1, 金额×1e-6)
    is_up = df_f["TransactionID"].astype(str).str.startswith("UP")
    tol = np.where(is_up, np.maximum(1.0, df_f["TotalAmount"].abs() * 1e-6), 1.0)
    bad = int((diff > tol).sum())
    checks.append(("金额复算", f"金额=单价×数量×折扣，偏差>容差的行数: {bad}", bad == 0))
    dup = int(df_f["TransactionID"].duplicated().sum())
    checks.append(("交易主键唯一", f"TransactionID 重复行数: {dup}", dup == 0))
    # 年份筛选时客户级 M/F 累计口径与子集聚合口径不同，跳过一致性校验
    if scope["years"] == ["全部"] and scope["segments"] == ["全部"]:
        agg_m = df_f.groupby("CustomerKey")["TotalAmount"].sum().round(2)
        rfm_m = df_f.drop_duplicates("CustomerKey").set_index("CustomerKey")["M"].round(2)
        cmp = pd_concat(agg_m, rfm_m)
        mm = int((cmp["agg"] - cmp["rfm"]).abs().gt(1.0).sum())
        checks.append(("RFM 口径校验", f"M值(客户累计消费)与交易聚合不一致: {mm} / {len(cmp)} 个客户", mm == 0))
    else:
        checks.append(("RFM 口径校验", "已勾选客群/年份子集，客户级累计口径与子集聚合不同，跳过该校验", True))

    y = 0.84
    for name, body, ok in checks:
        fig.text(0.06, y, ("[通过] " if ok else "[异常] ") + name, fontsize=13,
                 fontweight="bold", color="#2E7D32" if ok else "#C44E52")
        wrapped = "\n".join(textwrap.wrap(body, 78))
        fig.text(0.08, y - 0.035, wrapped, fontsize=10.5, color="#333333", linespacing=1.5)
        y -= 0.075 + 0.024 * (wrapped.count("\n"))
    pdf.savefig(fig)
    plt.close(fig)


def pd_concat(agg_m, rfm_m):
    import pandas as pd
    return pd.concat([agg_m.rename("agg"), rfm_m.rename("rfm")], axis=1).dropna()


def page_trend(pdf, df_f):
    """销售趋势：月度销售额/订单量 + 年度对比"""
    fig = plt.figure(figsize=(11.69, 8.27))
    fig.text(0.03, 0.955, "2. 销售趋势分析", fontsize=17, fontweight="bold", color="#1F3864")
    fig.text(0.03, 0.918, "月度销售额与订单量走势", fontsize=10, color="#666666")
    fig.add_artist(plt.Line2D([0.03, 0.97], [0.935, 0.935], color="#1F3864", lw=1.2,
                              transform=fig.transFigure))

    ax1 = fig.add_axes([0.06, 0.44, 0.88, 0.42])
    monthly = df_f.groupby("YearMonth").agg(Sales=("TotalAmount", "sum"),
                                            Orders=("TransactionID", "count"))
    x = range(len(monthly))
    ax1.plot(list(x), monthly["Sales"] / 10000, marker="o", ms=3.5, lw=1.6,
             color="#4C72B0", label="月度销售额(万元)")
    ax1.fill_between(list(x), monthly["Sales"] / 10000, alpha=0.15, color="#4C72B0")
    step = max(1, len(monthly) // 10)
    ax1.set_xticks(list(x)[::step])
    ax1.set_xticklabels(monthly.index[::step], rotation=45, fontsize=8)
    ax1.set_ylabel("销售额（万元）")
    ax1.legend(loc="upper left", fontsize=9)
    ax1.grid(alpha=0.3)

    ax2 = fig.add_axes([0.06, 0.10, 0.88, 0.22])
    yearly = df_f.groupby("Year").agg(Sales=("TotalAmount", "sum"), AOV=("TotalAmount", "mean"))
    ax2.bar(yearly.index.astype(str), yearly["Sales"] / 10000, color="#55A868", alpha=0.85, width=0.5)
    for i, (_, row) in enumerate(yearly.iterrows()):
        ax2.text(i, row["Sales"] / 10000 * 1.02, f"{row['Sales']/10000:,.0f}万", ha="center", fontsize=9)
        ax2.text(i, row["Sales"] / 10000 * 0.5, f"笔单价 {row['AOV']:,.0f}元",
                 ha="center", va="center", fontsize=8.5, color="white")
    ax2.set_ylabel("销售额（万元）")
    ax2.grid(alpha=0.3, axis="y")
    ax2.set_title("年度对比：销售额 / 笔单价", fontsize=10, loc="left", pad=6)
    pdf.savefig(fig)
    plt.close(fig)


def page_product(pdf, df_f):
    """产品维度：品类销售结构、占比与毛利率"""
    fig = plt.figure(figsize=(11.69, 8.27))
    fig.text(0.03, 0.955, "3. 产品维度分析", fontsize=17, fontweight="bold", color="#1F3864")
    fig.text(0.03, 0.918, "品类销售结构、毛利率与价格分布", fontsize=10, color="#666666")
    fig.add_artist(plt.Line2D([0.03, 0.97], [0.935, 0.935], color="#1F3864", lw=1.2,
                              transform=fig.transFigure))

    ax1 = fig.add_axes([0.06, 0.40, 0.40, 0.44])
    cat = df_f.groupby("Category").agg(Sales=("TotalAmount", "sum"),
                                       Margin=("GrossMargin", "mean")).sort_values("Sales")
    ax1.barh(cat.index, cat["Sales"] / 10000,
             color=[COLORS[i % 8] for i in range(len(cat))], alpha=0.85)
    for i, (_, row) in enumerate(cat.iterrows()):
        ax1.text(row["Sales"] / 10000 * 1.01, i, f"{row['Sales']/10000:,.0f}万", va="center", fontsize=8.5)
    ax1.set_xlabel("销售额（万元）")
    ax1.set_title("各品类销售额", fontsize=11, loc="left")
    ax1.grid(alpha=0.3, axis="x")

    ax2 = fig.add_axes([0.58, 0.40, 0.36, 0.44])
    share = cat["Sales"] / cat["Sales"].sum() * 100
    ax2.pie(share, labels=share.index, autopct="%.1f%%", startangle=90,
            colors=[COLORS[i % 8] for i in range(len(share))], textprops={"fontsize": 8.5},
            wedgeprops={"edgecolor": "white", "lw": 1})
    ax2.set_title("品类销售占比", fontsize=11, loc="left")

    ax3 = fig.add_axes([0.08, 0.10, 0.85, 0.22])
    margin_by_cat = df_f.groupby("Category")["GrossMargin"].mean().dropna() * 100
    if margin_by_cat.empty:
        ax3.text(0.5, 0.5, "无成本信息（数据未含成本列），毛利率不可计算",
                 ha="center", va="center", fontsize=12, color="#888888",
                 transform=ax3.transAxes)
        ax3.set_xticks([])
        ax3.set_yticks([])
    else:
        bars = ax3.bar(margin_by_cat.index, margin_by_cat.values,
                       color=["#DD8452" if v < margin_by_cat.mean() else "#4C72B0"
                              for v in margin_by_cat.values], alpha=0.85)
        for b, v in zip(bars, margin_by_cat.values):
            ax3.text(b.get_x() + b.get_width() / 2, v + 0.3, f"{v:.1f}%", ha="center", fontsize=9)
        ax3.axhline(margin_by_cat.mean(), ls="--", color="gray", lw=1,
                    label=f"平均毛利率 {margin_by_cat.mean():.1f}%")
        ax3.legend(fontsize=8.5)
    ax3.set_ylabel("平均毛利率(%)")
    ax3.set_title("各品类平均毛利率（橙色低于均值，需关注定价/成本）", fontsize=10, loc="left")
    pdf.savefig(fig)
    plt.close(fig)


def page_customer(pdf, df_f, rfm_f):
    """客户画像：人口属性分布 + RFM 分层结构"""
    fig = plt.figure(figsize=(11.69, 8.27))
    fig.text(0.03, 0.955, "4. 客户画像分析", fontsize=17, fontweight="bold", color="#1F3864")
    fig.text(0.03, 0.918, "人口属性分布与 RFM 分层结构", fontsize=10, color="#666666")
    fig.add_artist(plt.Line2D([0.03, 0.97], [0.935, 0.935], color="#1F3864", lw=1.2,
                              transform=fig.transFigure))

    grids = [
        ("CustomerTier", [0.05, 0.50, 0.27, 0.32]),
        ("Gender", [0.38, 0.50, 0.27, 0.32]),
        ("AgeGroup", [0.71, 0.50, 0.24, 0.32]),
    ]
    for col, pos in grids:
        ax = fig.add_axes(pos)
        vc = rfm_f[col].value_counts()
        ax.pie(vc, labels=vc.index, autopct="%.0f%%", startangle=90, radius=0.9,
               colors=[COLORS[i % 8] for i in range(len(vc))],
               textprops={"fontsize": 8}, wedgeprops={"edgecolor": "white", "lw": 1})
        ax.set_title(f"{col} 分布（客户数）", fontsize=10, loc="center")

    ax4 = fig.add_axes([0.08, 0.10, 0.38, 0.30])
    seg = rfm_f["RFM_Segment"].value_counts()
    ax4.barh(seg.index[::-1], seg.values[::-1],
             color=[COLORS[i % 8] for i in range(len(seg))], alpha=0.85)
    for i, v in enumerate(seg.values[::-1]):
        ax4.text(v + max(seg.values) * 0.01, i, f"{v:,}", va="center", fontsize=8.5)
    ax4.set_title("RFM 客户分群人数", fontsize=10, loc="left")
    ax4.set_xlabel("客户数")
    ax4.grid(alpha=0.3, axis="x")

    ax5 = fig.add_axes([0.55, 0.10, 0.40, 0.30])
    seg_sales = df_f.groupby("RFM_Segment")["TotalAmount"].sum().sort_values(ascending=False) / 10000
    ax5.barh(seg_sales.index, seg_sales.values, color="#55A868", alpha=0.85)
    for i, v in enumerate(seg_sales.values):
        ax5.text(v * 1.01, i, f"{v:,.0f}万", va="center", fontsize=8.5)
    ax5.set_title("RFM 分群贡献销售额", fontsize=10, loc="left")
    ax5.set_xlabel("销售额（万元）")
    ax5.grid(alpha=0.3, axis="x")
    pdf.savefig(fig)
    plt.close(fig)


def page_rfm(pdf, rfm_f):
    """RFM 评分分布与唤醒分析"""
    fig = plt.figure(figsize=(11.69, 8.27))
    fig.text(0.03, 0.955, "5. RFM 评分分布与唤醒分析", fontsize=17, fontweight="bold", color="#1F3864")
    fig.text(0.03, 0.918, "R/F/M 单维评分 与 唤醒客户表现", fontsize=10, color="#666666")
    fig.add_artist(plt.Line2D([0.03, 0.97], [0.935, 0.935], color="#1F3864", lw=1.2,
                              transform=fig.transFigure))

    ax1 = fig.add_axes([0.06, 0.42, 0.27, 0.42])
    for col, label in [("R_score", "R(最近购买)"), ("F_score", "F(消费频次)"), ("M_score", "M(消费金额)")]:
        vc = rfm_f[col].value_counts().sort_index()
        ax1.plot(vc.index, vc.values / vc.sum() * 100, marker="o", ms=4, label=label)
    ax1.set_xlabel("评分(1-5)")
    ax1.set_ylabel("客户占比(%)")
    ax1.set_title("R/F/M 单维评分分布", fontsize=10, loc="left")
    ax1.legend(fontsize=8)
    ax1.grid(alpha=0.3)

    ax2 = fig.add_axes([0.40, 0.42, 0.27, 0.42])
    ax2.hist(rfm_f["RFM_Score"], bins=range(3, 17), color="#8172B3", alpha=0.85, edgecolor="white")
    ax2.set_xlabel("RFM 总分(3-15)")
    ax2.set_ylabel("客户数")
    ax2.set_title("RFM 总分分布", fontsize=10, loc="left")
    ax2.grid(alpha=0.3, axis="y")

    ax3 = fig.add_axes([0.74, 0.42, 0.22, 0.42])
    ax3.hist(rfm_f.loc[rfm_f["R_days"] <= 548, "R_days"], bins=40, color="#DD8452", alpha=0.85)
    ax3.set_xlabel("R_days(天)")
    ax3.set_title("未沉睡客户 R 值分布", fontsize=10, loc="left")
    ax3.grid(alpha=0.3, axis="y")

    awaken_cust = rfm_f[rfm_f["IsAwakened"]]
    normal_cust = rfm_f[~rfm_f["IsAwakened"]]
    ax4 = fig.add_axes([0.08, 0.10, 0.85, 0.24])
    if len(awaken_cust) == 0 or len(normal_cust) == 0:
        ax4.text(0.5, 0.5, "勾选子集中唤醒/未唤醒客户不齐全，跳过对比",
                 ha="center", va="center", fontsize=12, color="#888888")
        ax4.set_xticks([])
        ax4.set_yticks([])
    else:
        metrics = {
            "人均消费(元)": [awaken_cust["M"].mean(), normal_cust["M"].mean()],
            "人均订单数(F)": [awaken_cust["F"].mean(), normal_cust["F"].mean()],
            "平均R值(天)": [awaken_cust["R_days"].mean(), normal_cust["R_days"].mean()],
            "平均RFM总分": [awaken_cust["RFM_Score"].mean(), normal_cust["RFM_Score"].mean()],
        }
        x = range(len(metrics))
        v1 = [v[0] for v in metrics.values()]
        v2 = [v[1] for v in metrics.values()]
        # 统一按每个指标两组中的较大值归一：柱高比 = 真实数值比，两组可直接对比（0/0 兜底为 1）
        scale = [max(a, b) or 1 for a, b in zip(v1, v2)]
        ax4.bar([i - 0.18 for i in x], [a / s for a, s in zip(v1, scale)], 0.32,
                color="#C44E52", alpha=0.85, label="唤醒客户")
        ax4.bar([i + 0.18 for i in x], [b / s for b, s in zip(v2, scale)], 0.32,
                color="#8C8C8C", alpha=0.7, label="未唤醒客户")
        for xi, (a, b, s) in enumerate(zip(v1, v2, scale)):
            ax4.text(xi - 0.18, a / s + 0.02, f"{a:,.1f}", ha="center", fontsize=8)
            ax4.text(xi + 0.18, b / s + 0.02, f"{b:,.1f}", ha="center", fontsize=8)
        ax4.set_xticks(list(x))
        ax4.set_xticklabels(list(metrics.keys()), fontsize=9)
        ax4.set_yticks([])
        ax4.legend(fontsize=9)
        ax4.set_title(f"唤醒 vs 未唤醒客户对比（唤醒 {len(awaken_cust)} 人 / "
                      f"{len(awaken_cust)/len(rfm_f)*100:.1f}%；柱高按各指标两组最大值统一归一化，两组可直接对比，标注为真实值）",
                      fontsize=10, loc="left")
    pdf.savefig(fig)
    plt.close(fig)


def page_marketing(pdf, camp_f):
    """营销活动 ROI 分析"""
    fig = plt.figure(figsize=(11.69, 8.27))
    fig.text(0.03, 0.955, "6. 营销活动 ROI 分析", fontsize=17, fontweight="bold", color="#1F3864")
    fig.text(0.03, 0.918, "按策略类型 / 阶段 / 目标客群的活动投入产出（营销表仅按年份筛选）",
             fontsize=10, color="#666666")
    fig.add_artist(plt.Line2D([0.03, 0.97], [0.935, 0.935], color="#1F3864", lw=1.2,
                              transform=fig.transFigure))

    ax1 = fig.add_axes([0.06, 0.44, 0.42, 0.40])
    g = camp_f.groupby("StrategyGroup").agg(Cost=("Cost", "sum"), Rev=("Revenue", "sum"))
    g["ROI"] = g["Rev"] / g["Cost"]
    bars = ax1.bar(g.index, g["ROI"],
                   color=["#C44E52" if v < 1 else "#55A868" for v in g["ROI"]], alpha=0.85)
    for b, v in zip(bars, g["ROI"]):
        ax1.text(b.get_x() + b.get_width() / 2, v + 0.02, f"{v:.2f}", ha="center", fontsize=9)
    ax1.axhline(1.0, ls="--", color="gray", lw=1, label="盈亏线 ROI=1")
    ax1.set_ylabel("ROI")
    ax1.legend(fontsize=8.5)
    ax1.set_title("各策略组 ROI（绿色=盈利，红色=亏损）", fontsize=10, loc="left")
    ax1.grid(alpha=0.3, axis="y")

    ax2 = fig.add_axes([0.56, 0.44, 0.39, 0.40])
    st = camp_f.groupby("Stage").agg(Cost=("Cost", "sum"), Rev=("Revenue", "sum"))
    st["ROI"] = st["Rev"] / st["Cost"]
    x = range(len(st))
    ax2.bar([i - 0.2 for i in x], st["Cost"] / 10000, 0.38, label="成本(万)", color="#4C72B0", alpha=0.85)
    ax2.bar([i + 0.2 for i in x], st["Rev"] / 10000, 0.38, label="收入(万)", color="#DD8452", alpha=0.85)
    for i, (c, r, roi) in enumerate(zip(st["Cost"], st["Rev"], st["ROI"])):
        ax2.text(i, max(c, r) / 10000 * 1.04, f"ROI {roi:.2f}", ha="center", fontsize=8.5)
    ax2.set_xticks(list(x))
    ax2.set_xticklabels(st.index, fontsize=8.5)
    ax2.set_ylabel("金额（万元）")
    ax2.legend(fontsize=8.5)
    ax2.set_title("各阶段成本 vs 收入", fontsize=10, loc="left")
    ax2.grid(alpha=0.3, axis="y")

    ax3 = fig.add_axes([0.08, 0.10, 0.85, 0.25])
    tg = camp_f.groupby("TargetSegment").agg(Cost=("Cost", "sum"), Rev=("Revenue", "sum"),
                                             N=("CampaignKey", "count"))
    tg["ROI"] = tg["Rev"] / tg["Cost"]
    x = range(len(tg))
    bars = ax3.bar(list(x), tg["ROI"], width=0.5,
                   color=["#55A868" if v >= 1 else "#C44E52" for v in tg["ROI"]], alpha=0.85)
    for i, (b, row) in enumerate(zip(bars, tg.itertuples())):
        ax3.text(b.get_x() + b.get_width() / 2, row.ROI + 0.02,
                 f"{row.ROI:.2f}\n({int(row.N)}场/成本{row.Cost/10000:,.0f}万)", ha="center", fontsize=8)
    ax3.axhline(1.0, ls="--", color="gray", lw=1)
    ax3.set_xticks(list(x))
    ax3.set_xticklabels(tg.index, fontsize=9)
    ax3.set_ylim(0, max(tg["ROI"]) * 1.35)
    ax3.set_ylabel("ROI")
    ax3.set_title("目标客群 ROI 对比 —— 精准投放 vs 大水漫灌效果差异", fontsize=10, loc="left")
    ax3.grid(alpha=0.3, axis="y")
    pdf.savefig(fig)
    plt.close(fig)


def page_findings(pdf, df_f, rfm_f, camp_f, sections):
    """关键发现与建议（根据勾选章节动态生成）"""
    fig = plt.figure(figsize=(11.69, 8.27))
    fig.text(0.03, 0.955, "7. 关键发现与建议", fontsize=17, fontweight="bold", color="#1F3864")
    fig.text(0.03, 0.918, "基于勾选数据子集与勾选章节的自动摘要", fontsize=10, color="#666666")
    fig.add_artist(plt.Line2D([0.03, 0.97], [0.935, 0.935], color="#1F3864", lw=1.2,
                              transform=fig.transFigure))

    findings = []
    if "quality" in sections:
        dup = int(df_f["TransactionID"].duplicated().sum())
        findings.append(("数据质量", f"勾选子集共 {len(df_f):,} 行交易、"
                                     f"{df_f['CustomerKey'].nunique():,} 位客户，主键重复 {dup} 行，"
                                     "数据可用于探索性分析。" if dup == 0 else
                         f"勾选子集存在 {dup} 行重复交易主键，建议先核对数据。"))
    if "trend" in sections:
        yearly = df_f.groupby("Year")["TotalAmount"].sum()
        findings.append(("销售趋势", f"子集总销售额 {df_f['TotalAmount'].sum()/10000:,.0f} 万元；"
                                     f"{yearly.idxmax()} 年销售额最高（{yearly.max()/10000:,.0f} 万元）。"))
    if "product" in sections:
        cat = df_f.groupby("Category").agg(Sales=("TotalAmount", "sum"),
                                           Margin=("GrossMargin", "mean"))
        best_cat = cat["Sales"].idxmax()
        if cat["Margin"].notna().any():
            cat_m = cat.dropna(subset=["Margin"])
            low_margin = cat_m["Margin"].idxmin()
            findings.append(("产品结构", f"{best_cat} 为勾选范围内销售第一大品类；"
                                         f"{low_margin} 毛利率最低（{cat_m['Margin'].min()*100:.1f}%），"
                                         "建议复核定价与成本。"))
        else:
            findings.append(("产品结构", f"{best_cat} 为勾选范围内销售第一大品类；"
                                         "数据未含成本列，毛利率不可计算（显示为 —）。"))
    if "customer" in sections or "rfm" in sections:
        seg_sales = df_f.groupby("RFM_Segment")["TotalAmount"].sum()
        best_seg = seg_sales.idxmax()
        sleep_cnt = int((rfm_f["CustomerTier"] == "沉睡客户").sum())
        awaken_cnt = int(rfm_f["IsAwakened"].sum())
        findings.append(("客户价值", f"RFM 分群中 {best_seg} 贡献销售额最高"
                                     f"（{seg_sales.max()/10000:,.0f} 万元）；勾选范围含沉睡客户 {sleep_cnt:,} 人，"
                                     f"其中已唤醒 {awaken_cnt:,} 人，是唤醒运营的核心目标池。"))
    if "marketing" in sections and len(camp_f):
        g = camp_f.groupby("StrategyGroup").agg(Cost=("Cost", "sum"), Rev=("Revenue", "sum"))
        g["ROI"] = g["Rev"] / g["Cost"]
        st = camp_f.groupby("Stage").agg(Cost=("Cost", "sum"), Rev=("Revenue", "sum"))
        st["ROI"] = st["Rev"] / st["Cost"]
        findings.append(("营销ROI", f"{st['ROI'].idxmax()} 阶段整体 ROI 最优（{st['ROI'].max():.2f}）；"
                                   f"策略组间 ROI 差异明显（{g['ROI'].min():.2f} ~ {g['ROI'].max():.2f}），"
                                   "建议预算向高 ROI 精准客群策略倾斜。"))

    if not findings:
        findings.append(("提示", "未勾选任何分析章节，请在「EDA报告生成」页勾选分析内容后重新生成。"))

    y = 0.84
    for title, body in findings:
        fig.text(0.06, y, "■ " + title, fontsize=12, fontweight="bold", color="#1F3864")
        wrapped = "\n".join(textwrap.wrap(body, 62))
        fig.text(0.075, y - 0.032, wrapped, fontsize=10, color="#333333", linespacing=1.5)
        y -= 0.115 + 0.023 * (wrapped.count("\n"))
    pdf.savefig(fig)
    plt.close(fig)


def _empty_page(pdf, title, note):
    """无数据章节的占位页"""
    fig = plt.figure(figsize=(11.69, 8.27))
    fig.text(0.03, 0.955, title, fontsize=17, fontweight="bold", color="#1F3864")
    fig.add_artist(plt.Line2D([0.03, 0.97], [0.935, 0.935], color="#1F3864", lw=1.2,
                              transform=fig.transFigure))
    fig.text(0.5, 0.5, note, ha="center", fontsize=14, color="#888888")
    pdf.savefig(fig)
    plt.close(fig)


# =========================================================
# 主入口：按勾选生成 PDF
# =========================================================
def generate_eda_report(df, rfm, camp, options):
    """按勾选范围与章节生成 EDA PDF 报告，返回 (pdf_path, meta)"""
    _init_mpl()  # 延迟导入 matplotlib：未安装时在此处才报错
    df_f, rfm_f, camp_f, scope = _filter_subset(df, rfm, camp, options)

    sections = [s for s in (options.get("sections") or SECTION_KEYS)
                if s in SECTION_KEYS]
    if not sections:
        sections = list(SECTION_KEYS)

    os.makedirs(EDA_DIR, exist_ok=True)
    fname = "EDA_" + time.strftime("%Y%m%d_%H%M%S") + ".pdf"
    pdf_path = os.path.join(EDA_DIR, fname)
    if os.path.exists(pdf_path):  # 同秒冲突，追加序号
        i = 2
        while os.path.exists(pdf_path):
            pdf_path = os.path.join(EDA_DIR, fname.replace(".pdf", f"_{i}.pdf"))
            i += 1

    with PdfPages(pdf_path) as pdf:
        page_cover(pdf, df_f, scope)
        if "quality" in sections:
            page_quality(pdf, df_f, scope)
        if "trend" in sections:
            page_trend(pdf, df_f)
        if "product" in sections:
            page_product(pdf, df_f)
        if "customer" in sections:
            page_customer(pdf, df_f, rfm_f)
        if "rfm" in sections:
            page_rfm(pdf, rfm_f)
        if "marketing" in sections:
            if len(camp_f):
                page_marketing(pdf, camp_f)
            else:
                _empty_page(pdf, "6. 营销活动 ROI 分析",
                            "勾选的年份范围内无营销活动数据，本页跳过。")
        if "findings" in sections:
            page_findings(pdf, df_f, rfm_f, camp_f, sections)

    meta = {
        "rows": int(len(df_f)),
        "customers": int(df_f["CustomerKey"].nunique()),
        "total_sales": round(float(df_f["TotalAmount"].sum()), 2),
        "scope": _scope_text(scope),
        "sections": [SECTION_NAMES[s] for s in sections],
    }
    return pdf_path, meta


def list_reports():
    """返回 eda_reports 目录下已生成报告列表（按时间倒序）"""
    if not os.path.isdir(EDA_DIR):
        return []
    items = []
    for name in os.listdir(EDA_DIR):
        if not name.lower().endswith(".pdf"):
            continue
        p = os.path.join(EDA_DIR, name)
        st = os.stat(p)
        items.append({"name": name, "size": st.st_size, "mtime": st.st_mtime})
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items
