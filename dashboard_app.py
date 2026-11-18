# -*- coding: utf-8 -*-
"""
RFM 客户分析可视化大屏 - Flask 后端（四页版 + 筛选器）
参考 DS04_PowerBI建模操作手册 第5/6步：
  页1 RFM客群分层总览 | 页2 高价值客户分析 | 页3 沉睡客户唤醒 | 页4 营销产出分析
启动: python dashboard_app.py  然后访问 http://127.0.0.1:5000
"""

import os
import io
import csv
import json
import re
import shutil
import threading
import pandas as pd
import numpy as np
from datetime import datetime
from urllib.parse import quote
from flask import Flask, render_template, jsonify, request, send_from_directory, Response
import ask_parser   # ⑦ 中文问答：规则解析与聚合独立成模块，主文件只挂路由
import llm_slots    # ⑨ 本地 LLM 意图解析（默认关闭；开启时也只解析意图，不出数）
import login_auth   # 登录鉴权：飞书 / 钉钉 / 微信 扫码（默认演示模式，见 login_config.json）
# 注意：eda_report_gen 延迟导入（见各 EDA 路由内部），
# 避免未安装 matplotlib 时影响大屏启动。

# 数据清洗引擎（三色分流 + SQLite 日志，见 clean_engine.py）：
# 容错加载——rules_config.json 损坏/缺失时清洗接口不可用，但不影响大屏主体。
try:
    import clean_engine as _clean_mod
    _CLEAN_ENGINE = _clean_mod.load_engine()
except Exception as _e:
    print(f"[clean] 清洗引擎加载失败（/api/clean/* 接口将不可用）: {_e}")
    _clean_mod = None
    _CLEAN_ENGINE = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def PA(f):
    """路径解析：优先项目根目录，其次 data/ 示例数据目录（GitHub 公开版）"""
    p = os.path.join(BASE_DIR, f)
    if not os.path.exists(p):
        alt = os.path.join(BASE_DIR, "data", f)
        if os.path.exists(alt):
            return alt
    return p
UPLOAD_DIR = PA("uploads")
BATCH_DIR = PA("uploads/batches")
PENDING_DIR = PA("uploads/pending")  # 上传预览暂存（确认后才并入批次）

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
app.config["TEMPLATES_AUTO_RELOAD"] = True  # 开发期模板改动即时生效
app.json.ensure_ascii = False
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # 上传上限 64MB

# 登录鉴权（登录版新增）：全局 before_request 拦截，未登录访问页面跳 /login，
# 未登录访问 /api/* 返回 401；/login、/api/login/*、/static 不受影响。
login_auth.init_login(app)

_CACHE = {}
# 缓存读写锁：Flask 默认多线程处理请求，上传后的 invalidate_cache 与 load_data
# 若不互斥，可能读到「半合并」状态（df 已换新、rfm 仍是旧），或重算结果
# 覆盖掉刚上传的新批次。所有缓存操作（读/写/清空）均持此锁。
_CACHE_LOCK = threading.Lock()
CUSTOM_METRIC_NOTE = (
    "口径：自定义数据按追加方式合并入星型模型（不影响原 6 张 CSV 文件），"
    "新客户默认归入「大众客户」客群；金额单位万元，保留2位小数；"
    "清空上传批次即可恢复基线数据。"
)

# 上传数据列名别名（中文/英文），用于自动映射到星型模型字段
COL_ALIASES = {
    "amount": ["totalamount", "amount", "sales", "金额", "销售额", "成交额", "销售金额", "交易金额"],
    "qty": ["quantity", "qty", "数量", "件数", "销量"],
    "cost": ["costprice", "cost", "成本", "成本价", "成本单价", "单位成本", "进货价"],
    "date": ["orderdate", "date", "日期", "订单日期", "交易日期", "时间"],
    "customer": ["customerkey", "customerid", "客户id", "客户编号", "客户号", "客户"],
    "product": ["productname", "商品", "商品名", "产品", "产品名", "品名"],
    "category": ["category", "品类", "类目", "分类"],
}
UPLOAD_DEADLINE = "2025-12-31"  # R_days 口径基准日（上传新数据时基准日自动顺延至数据最大日期，本常量仅兜底）


def load_data():
    """读取并合并数据（进程内缓存；含自定义上传扩展，无上传时行为与基线一致）

    线程安全：整个函数持 _CACHE_LOCK。三键缓存必须原子读写，否则并发请求
    可能拿到 df/rfm/camp 来自两次不同合并的「半合并」数据；同时避免重算
    期间 invalidate 插入，导致上传后的新批次被过期重算结果覆盖。
    """
    with _CACHE_LOCK:
        if "df" in _CACHE:
            return _CACHE["df"], _CACHE["rfm"], _CACHE["camp"]

        tx = pd.read_csv(PA("Fact_Transaction.csv"))
        cust = pd.read_csv(PA("Dim_Customer.csv"))
        prod = pd.read_csv(PA("Dim_Product.csv"))
        time_ = pd.read_csv(PA("Dim_Time.csv"))
        rfm = pd.read_csv(PA("Agg_Customer_RFM.csv"))
        camp = pd.read_csv(PA("Fact_Marketing_Campaign.csv"))

        ext = load_extension()
        if ext is not None and len(ext) > 0:
            # R_days 口径基准日随数据最大日期顺延（上传 2026 数据时不再被 clip 全压成 0）
            deadline = pd.to_datetime(UPLOAD_DEADLINE)
            ext_max = pd.to_datetime(ext["OrderDate"]).max()
            if pd.notna(ext_max) and ext_max > deadline:
                deadline = ext_max
            cust, prod, time_, rfm = derive_dims(ext, cust, prod, time_, rfm, deadline)
            # 维度列已由 merge 提供，去掉 ext 自带的 ProductName/Category/CostPrice 避免列名冲突
            drop_cols = [c for c in ("ProductName", "Category", "CostPrice") if c in ext.columns]
            tx = pd.concat([tx, ext.drop(columns=drop_cols)], ignore_index=True)

        df = tx.merge(cust.drop(columns=["CustomerID"]), on="CustomerKey", how="left")
        df = df.merge(prod.drop(columns=["UnitPrice"]), on="ProductKey", how="left")
        df = df.merge(time_.drop(columns=["Date"]), on="TimeKey", how="left")
        # 毛利率按实际成交口径：(实付 - 成本)/实付，与报告书 §4.3.1 一致（价格表口径 45.9% 因未计折扣而高估，仅作参考）；
        # 无成本信息的行（上传新商品未含成本列）跳过 → NaN，展示「—」而不是按成本=单价算出 0%
        df["GrossMargin"] = np.where(
            df["TotalAmount"] > 0,
            (df["TotalAmount"] - df["Quantity"] * df["CostPrice"]) / df["TotalAmount"],
            np.nan)
        df = df.merge(
            rfm[["CustomerKey", "R_days", "F_score", "M_score", "R_score",
                 "RFM_Score", "RFM_Segment", "IsAwakened", "F", "M"]],
            on="CustomerKey", how="left",
        )
        df["OrderDate"] = pd.to_datetime(df["OrderDate"])
        df["YearMonth"] = df["OrderDate"].dt.to_period("M").astype(str)
        df["Year"] = df["OrderDate"].dt.year
        df["Month"] = df["OrderDate"].dt.month
        camp["Year"] = pd.to_datetime(camp["StartDate"]).dt.year

        _CACHE["df"], _CACHE["rfm"], _CACHE["camp"] = df, rfm, camp
        return df, rfm, camp


def invalidate_cache():
    """上传/删除批次后清缓存，下一次 load_data 重新合并（与 load_data 互斥）"""
    with _CACHE_LOCK:
        _CACHE.clear()


def f2(x):
    try:
        return round(float(x), 2)
    except (TypeError, ValueError):
        return 0.0


class BadArg(ValueError):
    """URL 参数非法（类型/范围），由 errorhandler 统一返回 400 JSON 中文提示"""


def _int_arg(name, default=None, lo=None, hi=None):
    """URL 整数参数校验：缺省/空 → default；非整数或越界 → BadArg（400 而非 500 堆栈）"""
    v = request.args.get(name, "").strip()
    if not v:
        return default
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise BadArg(f"参数「{name}」必须是整数，收到「{v}」")
    if lo is not None and n < lo:
        raise BadArg(f"参数「{name}」不能小于 {lo}")
    if hi is not None and n > hi:
        raise BadArg(f"参数「{name}」不能大于 {hi}")
    return n


def _float_arg(name, default=0.0, lo=None, hi=None):
    """URL 浮点参数校验（滑块用）：缺省/空 → default；非数值/NaN/越界 → BadArg（400）"""
    v = request.args.get(name, "").strip()
    if not v:
        return default
    try:
        n = float(v)
    except (TypeError, ValueError):
        raise BadArg(f"参数「{name}」必须是数值，收到「{v}」")
    if n != n or n in (float("inf"), float("-inf")):
        raise BadArg(f"参数「{name}」必须是有限数值，收到「{v}」")
    if lo is not None and n < lo:
        raise BadArg(f"参数「{name}」不能小于 {lo}")
    if hi is not None and n > hi:
        raise BadArg(f"参数「{name}」不能大于 {hi}")
    return n


# 批次/暂存 ID 白名单：服务端只生成「数字（清洗批次带 C 前缀）」格式的 ID。
# 这些 ID 会被拼进文件路径做删除/写入/读取，必须先过白名单——否则
# 「../../Fact_Transaction」这类输入可以把删除重定向到批次目录之外（路径穿越）。
_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9]{8,32}$")


def _safe_batch_id(v):
    """校验 batch_id / pending_id：合法原样返回，非法返回空串（调用方按不存在处理）。"""
    s = str(v or "").strip()
    return s if _BATCH_ID_RE.match(s) else ""


@app.errorhandler(BadArg)
def handle_bad_arg(e):
    """非法 URL 参数 → 400 JSON 中文提示（不外泄堆栈）"""
    return jsonify({"error": str(e)}), 400


def full_months(series, lo=None, hi=None):
    """补齐缺失月份：默认按传入序列 index 的 min~max 生成完整月序列；
    lo/hi 可显式指定（如全局数据范围），保证各趋势轴一致延伸到最新数据月；
    空序列退回基线范围兜底。"""
    if lo is None or hi is None:
        if len(series) == 0:
            lo, hi = "2023-01", "2025-12"
        else:
            if lo is None:
                lo = str(min(series.index))
            if hi is None:
                hi = str(max(series.index))
    idx = pd.period_range(lo, hi, freq="M").astype(str)
    return series.reindex(idx, fill_value=0)


def pie_list(series):
    return [{"name": str(k), "value": int(v)} for k, v in series.value_counts().items()]


# ---------------- 自定义数据上传：解析/映射/扩展行 ----------------
def ensure_batch_dir():
    os.makedirs(BATCH_DIR, exist_ok=True)


def load_extension():
    """读取全部批次扩展行（Fact_Transaction 同 schema + ProductName/Category），无批次返回 None"""
    ensure_batch_dir()
    files = [f for f in os.listdir(BATCH_DIR)
             if f.endswith(".csv") and not f.endswith(".orig.csv") and not f.endswith(".tmp.csv")]
    if not files:
        return None
    parts = [pd.read_csv(os.path.join(BATCH_DIR, f)) for f in sorted(files)]
    ext = pd.concat(parts, ignore_index=True)
    return ext


def _batch_meta_list():
    """批次元信息列表（按文件名排序）"""
    ensure_batch_dir()
    out = []
    for f in sorted(os.listdir(BATCH_DIR)):
        if not f.endswith(".csv") or f.endswith(".orig.csv") or f.endswith(".tmp.csv"):
            continue
        bid = f[:-4]
        meta_path = os.path.join(BATCH_DIR, bid + ".json")
        meta = {}
        if os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as fp:
                    meta = json.load(fp)
            except Exception:
                meta = {}
        try:
            rows = int(pd.read_csv(os.path.join(BATCH_DIR, f), usecols=[0]).shape[0])
        except Exception:
            rows = 0
        out.append({"id": bid, "name": meta.get("name", f), "rows": rows,
                    "time": meta.get("time", "")})
    return out


def derive_dims(ext, cust, prod, time_, rfm, deadline=None):
    """由扩展事实行确定性推导新增维度行（客户/商品/日期/RFM），返回扩展后的四张维度表"""
    if deadline is None:
        deadline = pd.to_datetime(UPLOAD_DEADLINE)

    # 新客户 -> Dim_Customer + Agg_Customer_RFM
    g = ext.groupby("CustomerKey").agg(
        first=("OrderDate", "min"), last=("OrderDate", "max"),
        F=("TransactionID", "count"), M=("TotalAmount", "sum")).reset_index()
    new_cust = g[~g["CustomerKey"].isin(set(cust["CustomerKey"]))]
    if len(new_cust):
        rows_c = pd.DataFrame({
            "CustomerKey": new_cust["CustomerKey"].astype(int),
            "CustomerID": ["CUST-UP" + str(int(k)) for k in new_cust["CustomerKey"]],
            "Gender": "未知", "AgeGroup": "未知",
            "RegisterDate": new_cust["first"].astype(str),
            "CustomerTier": "大众客户", "CityLevel": "三线及以下",
        })
        cust = pd.concat([cust, rows_c], ignore_index=True)
        rows_r = pd.DataFrame({
            "CustomerKey": new_cust["CustomerKey"].astype(int),
            "CustomerTier": "大众客户",
            "RegisterDate": new_cust["first"].astype(str),
            "Gender": "未知", "AgeGroup": "未知", "CityLevel": "三线及以下",
            "F": new_cust["F"].astype(int), "M": new_cust["M"].round(2),
            "LastOrderDate": new_cust["last"].astype(str),
            "AwakenedOrders": 0, "IsAwakened": False,
            "R_days": (deadline - pd.to_datetime(new_cust["last"])).dt.days.clip(lower=0),
            "F_score": 1, "M_score": 1, "R_score": 1, "RFM_Score": 3,
            "RFM_Segment": "大众客户",
        })
        rfm = pd.concat([rfm, rows_r], ignore_index=True)

    # 新商品 -> Dim_Product（成本仅在上传表含成本列时取上传值，否则 NaN → 毛利率计算跳过）
    agg = dict(ProductName=("ProductName", "first"), Category=("Category", "first"),
               UnitPrice=("UnitPrice", "mean"))
    if "CostPrice" in ext.columns:
        agg["CostPrice"] = ("CostPrice", "first")
    gp = ext.groupby("ProductKey").agg(**agg).reset_index()
    new_prod = gp[~gp["ProductKey"].isin(set(prod["ProductKey"]))]
    if len(new_prod):
        rows_p = pd.DataFrame({
            "ProductKey": new_prod["ProductKey"].astype(int),
            "ProductName": new_prod["ProductName"],
            "Category": new_prod["Category"].fillna("其他"),
            "UnitPrice": new_prod["UnitPrice"].round(4),
            # 成本未知（上传未含成本列）置 NaN：毛利率显示「—」，而不是按成本=单价算出 0%
            "CostPrice": new_prod["CostPrice"].round(4) if "CostPrice" in new_prod.columns else np.nan,
        })
        prod = pd.concat([prod, rows_p], ignore_index=True)

    # 新日期 -> Dim_Time
    new_tk = ext[~ext["TimeKey"].isin(set(time_["TimeKey"]))]["TimeKey"].unique()
    if len(new_tk):
        dt = pd.to_datetime([str(int(k)) for k in new_tk], format="%Y%m%d", errors="coerce")
        rows_t = pd.DataFrame({
            "TimeKey": [int(k) for k in new_tk],
            "Date": dt.strftime("%Y-%m-%d"),
            "Year": dt.year, "Quarter": dt.quarter,
            "Month": dt.month, "IsWeekend": dt.dayofweek >= 5,
        }).dropna(subset=["Date"])
        time_ = pd.concat([time_, rows_t], ignore_index=True)

    return cust, prod, time_, rfm


def _map_columns(cols):
    """列名 -> 字段映射报告（忽略大小写/空白，按别名精确匹配）"""
    norm = {c: str(c).strip().lower() for c in cols}
    mapping, used = {}, set()
    for field, aliases in COL_ALIASES.items():
        for c, n in norm.items():
            if n in aliases and field not in mapping:
                mapping[field] = c
                used.add(c)
    return mapping


_XLSX_MAGIC = b"PK\x03\x04"                          # xlsx = ZIP 容器
_XLS_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"     # 老式 xls = OLE2 容器


def _binary_kind(data: bytes):
    """按文件头（魔数）识别真实类型，不信任扩展名"""
    if data[:4] == _XLSX_MAGIC:
        return "xlsx"
    if data[:8] == _XLS_MAGIC:
        return "xls"
    return None


def _decode_text(data: bytes):
    """按常见编码依次尝试解码；全部失败才容错降级，并返回警告"""
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(enc), None
        except (UnicodeDecodeError, ValueError):
            continue
    return data.decode("utf-8-sig", errors="replace"), "文件编码不是 UTF-8/GBK，已容错读取，个别字符可能显示异常"


def parse_tabular(data: bytes, filename=""):
    """解析 CSV/Excel/粘贴文本 -> DataFrame。
    按文件内容（魔数）识别真实格式，不信任扩展名——防止 .xlsx 被改名 .csv 后
    被当文本解析，抛出 'new-line character seen in unquoted field' 之类的天书报错。"""
    name = (filename or "").lower()
    kind = _binary_kind(data)

    if kind in ("xlsx", "xls"):
        # 扩展名与内容不符时给出可读提示，便于用户发现"改名"操作
        note = ""
        if kind == "xlsx" and name.endswith(".csv"):
            note = f"（注意：'{filename}' 实际是 Excel 文件，扩展名却被改成了 .csv，已按 Excel 正常解析）"
        elif kind == "xls" and name.endswith(".csv"):
            note = f"（注意：'{filename}' 实际是 .xls 文件，扩展名却被改成了 .csv，已按 Excel 正常解析）"
        try:
            df = pd.read_excel(io.BytesIO(data))
        except Exception as e:
            raise ValueError(
                f"文件是 Excel 格式但无法读取（可能已损坏、加密或为空）：{e}。"
                f"请用 Excel 重新另存后再上传。{note}")
        print(f"[upload] {filename or '粘贴文本'}: 按内容识别为 {kind}{note}")
        return df

    # ---- 文本类（CSV / TSV / 粘贴文本）----
    text, warn = _decode_text(data)
    if warn:
        print(f"[upload] {filename or '粘贴文本'}: {warn}")

    # 容错解码后若仍是大量乱码/控制字符，基本可断定是二进制文件被伪装成表格
    sample = text[:4096]
    bad_ratio = sum(1 for ch in sample if ord(ch) == 0xFFFD or (ord(ch) < 9 and ch not in "\t\n\r")) / max(len(sample), 1)
    if bad_ratio > 0.05:
        raise ValueError(
            f"'{filename or '粘贴文本'}' 不是有效的表格文件（内容是二进制数据），无法解析。"
            f"支持：.csv / .xlsx / .xls / 直接粘贴表格文本。")

    if not text.strip():
        raise ValueError(f"'{filename or '粘贴文本'}' 内容为空，没有可解析的数据行。")

    if text.count("\t") >= text.count(","):
        sep = "\t"
    elif text.count(";") > text.count(","):
        sep = ";"
    else:
        sep = ","
    try:
        return pd.read_csv(io.StringIO(text), sep=sep, engine="python")
    except (pd.errors.ParserError, csv.Error) as e:
        raise ValueError(
            f"CSV 内容存在无法修复的格式问题：{e}。"
            f"常见原因：① 某个字段内部有换行但没用英文双引号包裹；"
            f"② 文件被 Excel/WPS 保存成了非常规格式。"
            f"建议用 Excel 打开 → 另存为「CSV UTF-8」后重新上传。")


def _int_or_none(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def build_extension(raw: pd.DataFrame, allow_negative=False):
    """原始上传表 -> 扩展事实行（Fact_Transaction 同 schema + ProductName/Category）
    返回 (ext_df, mapping 报告)；金额列缺失且无数值列时报 ValueError。
    allow_negative=True 时保留负金额（清洗流程的退款行：M 净额口径下与支付行对冲），
    旧上传流程默认 clip 到 0，行为不变。"""
    if raw is None or raw.empty:
        raise ValueError("上传内容为空，未解析到任何数据行")
    raw = raw.dropna(how="all")
    mapping = _map_columns(raw.columns)

    # 金额列：别名优先，否则第一个数值列兜底
    numeric_cols = [c for c in raw.columns if pd.api.types.is_numeric_dtype(raw[c])]
    amt_col = mapping.get("amount")
    if amt_col is None:
        amt_col = numeric_cols[0] if numeric_cols else None
    if amt_col is None:
        raise ValueError("未找到金额/数值列：请确认表格至少包含一列数值（如 金额/销售额/数量）")
    mapping["amount"] = amt_col

    qty_col = mapping.get("qty")
    cost_col = mapping.get("cost")   # 可选：命中别名才取成本，否则毛利率显示「—」
    date_col = mapping.get("date")
    cust_col = mapping.get("customer")
    prod_col = mapping.get("product")
    cat_col = mapping.get("category")

    amount = pd.to_numeric(raw[amt_col], errors="coerce")
    dropped_rows = int(amount.isna().sum())
    raw = raw[amount.notna()].copy()
    if len(raw) == 0:
        raise ValueError("金额列无有效数值，无法导入")
    amount = pd.to_numeric(raw[amt_col], errors="coerce")
    if not allow_negative:
        amount = amount.clip(lower=0)
    qty = pd.to_numeric(raw[qty_col], errors="coerce").clip(lower=1).fillna(1).astype(int) if qty_col else pd.Series(1, index=raw.index)

    # 日期：别名列解析，失败/缺失时默认 2025-12-31
    if date_col:
        dt = pd.to_datetime(raw[date_col], errors="coerce")
    else:
        dt = pd.Series(pd.NaT, index=raw.index)
    date_default = int(dt.isna().sum())
    dt = dt.fillna(pd.to_datetime(UPLOAD_DEADLINE))

    # 客户 Key：整数且已存在则复用，否则按唯一值分配新 Key
    base_cust = pd.read_csv(PA("Dim_Customer.csv"))
    ext_old = load_extension()
    known_keys = set(base_cust["CustomerKey"])
    if ext_old is not None and len(ext_old):
        known_keys |= set(ext_old["CustomerKey"])
    next_key = max(known_keys) + 1 if known_keys else 1
    cust_keys, cust_pool = [], {}
    if cust_col:
        for v in raw[cust_col]:
            k = _int_or_none(v)
            if k is not None and k in known_keys:
                cust_keys.append(k)
            else:
                key = cust_pool.setdefault(str(v), next_key + len(cust_pool))
                cust_keys.append(key)
    else:
        single = next_key
        cust_keys = [single] * len(raw)
        cust_pool = {"(无客户列)统一上传客户": single}
    new_cust_cnt = len(cust_pool)

    # 商品 Key：同名复用现有，否则分配新 Key
    base_prod = pd.read_csv(PA("Dim_Product.csv"))
    name2key = dict(zip(base_prod["ProductName"], base_prod["ProductKey"]))
    if ext_old is not None and len(ext_old):
        for n, k in zip(ext_old["ProductName"], ext_old["ProductKey"]):
            name2key.setdefault(str(n), int(k))
    next_pk = max(base_prod["ProductKey"].max(),
                   int(ext_old["ProductKey"].max()) if ext_old is not None and len(ext_old) else 0) + 1
    prod_keys, prod_pool = [], {}
    if prod_col:
        for v in raw[prod_col]:
            name = str(v)
            if name in name2key:
                prod_keys.append(int(name2key[name]))
            else:
                pk = prod_pool.setdefault(name, next_pk + len(prod_pool))
                prod_keys.append(pk)
    else:
        p_single = next_pk
        prod_keys = [p_single] * len(raw)
        prod_pool = {"(无商品列)上传商品": p_single}
    new_prod_cnt = len(prod_pool)

    # 品类/商品名缺省
    cat = raw[cat_col].fillna("其他").astype(str) if cat_col else pd.Series("其他", index=raw.index)
    pname = raw[prod_col].astype(str) if prod_col else pd.Series("(无商品列)上传商品", index=raw.index)

    batch_id = datetime.now().strftime("%Y%m%d%H%M%S%f")
    # 成本列：无别名列时置 NaN（毛利率计算跳过，显示「—」而不是 0%）
    cost_val = (pd.to_numeric(raw[cost_col], errors="coerce") if cost_col
                else pd.Series(np.nan, index=raw.index))
    ext = pd.DataFrame({
        "TransactionID": [f"UP{batch_id[-8:]}-{i + 1:05d}" for i in range(len(raw))],
        "TimeKey": (dt.dt.year * 10000 + dt.dt.month * 100 + dt.dt.day).astype(int),
        "CustomerKey": cust_keys,
        "ProductKey": prod_keys,
        "Quantity": qty.astype(int),
        # 单价保留 4 位小数：2 位会让复算「单价×数量×折扣 ≠ 实付」，EDA 质检误报
        "UnitPrice": (amount / qty).round(4),
        "TotalAmount": amount.round(2),
        "IsAwakenedOrder": False,
        "OrderDate": dt.dt.strftime("%Y-%m-%d"),
        "DiscountRate": 1.0,
        "CostPrice": cost_val.round(4),
        "ProductName": pname.values,
        "Category": cat.values,
    })
    report = {
        "columns_total": int(len(raw.columns)),
        "amount_col": amt_col,
        "qty_col": qty_col or "（无，默认 1）",
        "date_col": date_col or "（无，默认 2025-12-31）",
        "customer_col": cust_col or "（无，归入单个上传客户）",
        "product_col": prod_col or "（无，归入单个上传商品）",
        "category_col": cat_col or "（无，默认「其他」）",
        "cost_col": cost_col or "（无，毛利率不可计算 → 显示「—」）",
        "new_customers": new_cust_cnt,
        "new_products": new_prod_cnt,
        "date_default_cnt": date_default,
        "dropped_rows": dropped_rows,
    }
    return ext, report, batch_id


def _fmt_cell(v):
    """单元格展示值：NaN/空 -> 空串，浮点整数去掉小数尾巴"""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else str(v)
    return str(v)


def classify_dirty(orig: pd.DataFrame, mapping: dict):
    """逐格分类上传原始行的脏数据：bad=无效/已丢弃(红)，warn=已修正/兜底(橙)
    mapping 兼容内部键名(amount/qty/...)与报表键名(amount_col/qty_col/...)"""
    cols = set(orig.columns)

    def _col(*keys):
        for k in keys:
            v = mapping.get(k)
            if v and v in cols:
                return v
        return None

    amt = _col("amount", "amount_col")
    qty = _col("qty", "qty_col")
    dt = _col("date", "date_col")
    cust = _col("customer", "customer_col")
    prod = _col("product", "product_col")
    cat = _col("category", "category_col")
    rows_out, seen = [], set()
    for _, r in orig.iterrows():
        cells = {c: _fmt_cell(r.get(c)) for c in orig.columns}
        flags, status, row_msg = {}, "ok", ""
        if amt is not None:
            num = pd.to_numeric(r.get(amt), errors="coerce")
            if pd.isna(num):
                status = "dropped"
                flags[amt] = {"level": "bad", "msg": "金额无效，整行未计入"}
                row_msg = "金额无效，整行已丢弃"
            elif num < 0:
                flags[amt] = {"level": "warn", "msg": "金额为负，按 0 计入"}
                if status == "ok":
                    status = "fixed"
        if status != "dropped" and dt is not None:
            v = str(r.get(dt)).strip()
            if v and pd.isna(pd.to_datetime(v, errors="coerce")):
                flags[dt] = {"level": "warn", "msg": "日期格式异常，按 2025-12-31 计入"}
                if status == "ok":
                    status = "fixed"
        if qty is not None:
            v = str(r.get(qty)).strip()
            if v:
                q = pd.to_numeric(v, errors="coerce")
                if pd.isna(q):
                    flags[qty] = {"level": "warn", "msg": "数量非数值，按 1 计入"}
                    if status == "ok":
                        status = "fixed"
                elif q < 0:
                    flags[qty] = {"level": "warn", "msg": "数量为负，按 1 计入"}
                    if status == "ok":
                        status = "fixed"
        if cat is not None and not str(r.get(cat)).strip():
            flags[cat] = {"level": "warn", "msg": "品类缺失，按「其他」"}
            if status == "ok":
                status = "fixed"
        if prod is not None and not str(r.get(prod)).strip():
            flags[prod] = {"level": "warn", "msg": "商品缺失，按「上传商品」"}
            if status == "ok":
                status = "fixed"
        if cust is not None:
            s = str(r.get(cust))
            if s != s.strip():
                flags[cust] = {"level": "warn", "msg": "客户ID首尾含空格"}
                if status == "ok":
                    status = "fixed"
        if all(cells.get(c) == str(c) for c in orig.columns):
            status = "dropped"
            row_msg = "疑似重复表头行，已丢弃"
            for c in list(flags):
                if flags[c]["level"] != "bad":
                    del flags[c]
        key = tuple(cells.values())
        if status == "ok" and key in seen:
            status = "fixed"
            row_msg = "与前面行内容完全重复"
        seen.add(key)
        rows_out.append({"cells": cells, "flags": flags, "row_status": status, "row_msg": row_msg})
    return rows_out


# ---------------- 页面路由 ----------------
@app.route("/")
def index():
    return render_template("dashboard.html")


# 筛选器固定展示顺序
GENDER_ORDER = ["男", "女", "未知"]
CITY_ORDER = ["一线城市", "二线城市", "三线及以下"]


def ordered_values(series, order):
    """按固定顺序返回列取值，未列入顺序的值按默认排序追加到末尾"""
    vals = set(series.dropna().unique().tolist())
    head = [v for v in order if v in vals]
    return head + sorted(vals - set(order))


@app.route("/api/filters")
def filters():
    df, rfm, camp = load_data()
    return jsonify({
        "tier": sorted(rfm["RFM_Segment"].dropna().unique().tolist()),
        "gender": ordered_values(rfm["Gender"], GENDER_ORDER),
        "age": sorted(rfm["AgeGroup"].dropna().unique().tolist()),
        "city": ordered_values(rfm["CityLevel"], CITY_ORDER),
        "year": sorted(df["Year"].unique().tolist()),
        "month": list(range(1, 13)),
    })


# ---------------- 数据大屏主接口 ----------------
@app.route("/api/dashboard")
def dashboard():
    df, rfm, camp = load_data()

    # 全局筛选器参数
    tier = request.args.get("tier", "").strip()
    gender = request.args.get("gender", "").strip()
    age = request.args.get("age", "").strip()
    city = request.args.get("city", "").strip()
    year = _int_arg("year")     # int 或 None；非法值由 errorhandler 转 400 中文提示
    month = _int_arg("month")

    # 联动下钻参数（由图表点击产生）
    seg = request.args.get("seg", "").strip()
    category = request.args.get("category", "").strip()
    strat = request.args.get("strat", "").strip()

    # 客户池筛选（性别/年龄/城市作用于客户，层级参数见下方说明）
    pool = rfm
    if gender:
        pool = pool[pool["Gender"] == gender]
    if age:
        pool = pool[pool["AgeGroup"] == age]
    if city:
        pool = pool[pool["CityLevel"] == city]
    if seg:
        pool = pool[pool["RFM_Segment"] == seg]

    # tier 筛选（RFM 客群口径）：与图表联动 seg 同字段，作用于所有页面客户池
    pool1 = pool if not tier else pool[pool["RFM_Segment"] == tier]

    keys1 = set(pool1["CustomerKey"])
    dff = df[df["CustomerKey"].isin(keys1)]
    if year is not None:
        dff = dff[dff["Year"] == year]
    if month is not None:
        dff = dff[dff["Month"] == month]
    if category:
        dff = dff[dff["Category"] == category]

    campf = camp if year is None else camp[camp["Year"] == year]
    if strat:
        campf = campf[campf["StrategyGroup"] == strat]
    # 策略对比图的数据源：year 过滤但不含 strat 裁剪（保证多柱对比，P0-05）
    camp_y = camp if year is None else camp[camp["Year"] == year]

    # 数据期间：按实际数据 min~max 生成（上传 2026 数据后自动延伸）
    data_lo = df["OrderDate"].min().strftime("%Y-%m-%d")
    data_hi = df["OrderDate"].max().strftime("%Y-%m-%d")

    resp = {
        "data_range": f"{data_lo} ~ {data_hi}",
        "filters": {"tier": tier, "gender": gender, "age": age, "city": city,
                    "year": str(year) if year is not None else "",
                    "month": str(month) if month is not None else ""},
        "links": {"seg": seg, "category": category, "strat": strat},
        "metric_notes": PAGE_NOTES,   # 主四页底部口径说明（p1~p4）
        "metric_meta": KPI_META,      # ⑧ 每个 KPI 的结构化口径追溯（公式/来源/步骤/注释）
        "anomalies": detect_anomalies(df),   # 异常月检测（全局口径，主图 markPoint 用）
        "p0": page_overview(df, rfm, pool, tier, year, month),
        "p1": page_rfm_overview(dff, pool1),
        "p2": page_high_value(df, rfm, pool1, category, year, month),
        "p3": page_awaken(df, rfm, pool1, category, year, month),
        "p4": page_marketing(campf, camp_y, camp, strat),
    }
    return jsonify(resp)


# ---------------- 月度异常检测与归因（对标 Power BI「解释下降」） ----------------
def detect_anomalies(df, window=6, th=2.0):
    """滚动窗口 z-score 异常月检测（纯 GMV 水平口径，手写实现无外部依赖）。

    对每月 GMV（TotalAmount 按月求和），取其前 window 个月（不含当月）的均值与
    样本标准差计算 z；窗口不足 window 个月或窗口标准差为 0 时不判定；
    |z|>th 判为异常月。返回 [{ym, z, gmv, prev_gmv, pct}]。"""
    s = df.groupby("YearMonth")["TotalAmount"].sum().sort_index()
    vals = s.values.astype(float)
    out = []
    for i in range(len(s)):
        if i < window:
            continue
        win = vals[i - window:i]
        sd = win.std(ddof=1)
        if sd <= 0:
            continue
        z = float((vals[i] - win.mean()) / sd)
        if abs(z) > th:
            prev = float(vals[i - 1])
            pct = (vals[i] - prev) / prev * 100.0 if prev else None
            out.append({"ym": str(s.index[i]),
                        "z": round(z, 2),
                        "gmv": round(float(vals[i]), 2),
                        "prev_gmv": round(prev, 2),
                        "pct": round(pct, 2) if pct is not None else None})
    return out


@app.route("/api/anomaly")
def anomaly():
    """异常月归因：?ym=YYYY-MM。z 口径与 detect_anomalies 一致（前 6 个月滚动窗口，
    |z|>2）。非异常月/无数据月/窗口不足月返回 400 与中文提示；异常月返回当月 GMV、
    环比、z 分值，及各 RFM_Segment 客群/各 Category 品类较上月的销售额变化量与
    变化率（按绝对变化降序，数字全部真实聚合）。"""
    ym = request.args.get("ym", "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}", ym):
        raise BadArg(f"参数「ym」必须是 YYYY-MM 格式，收到「{ym}」")
    df, rfm, camp = load_data()

    s = df.groupby("YearMonth")["TotalAmount"].sum().sort_index()
    if ym not in s.index:
        return jsonify({"error": f"{ym} 不在数据范围内，无归因数据"}), 400
    i = list(s.index).index(ym)
    if i < 6:
        return jsonify({"error": f"{ym} 历史窗口不足 6 个月，不参与异常判定"}), 400

    hits = {a["ym"]: a for a in detect_anomalies(df)}
    hit = hits.get(ym)
    if hit is None:
        win = s.values[i - 6:i]
        sd = win.std(ddof=1)
        z = float((s.values[i] - win.mean()) / sd) if sd > 0 else 0.0
        return jsonify({"error": f"{ym} 非异常月（|z|={abs(z):.2f} ≤ 2），无可归因的显著变化"}), 400

    gmv = float(s.values[i])
    m_prev = _shift_month(ym, -1)
    prev_gmv = float(s.get(m_prev, 0.0))
    pct = (gmv - prev_gmv) / prev_gmv * 100.0 if prev_gmv else None

    def drivers(dim, fill):
        """该维度各分组的当月 vs 上月销售额变化（|delta| 降序）"""
        cur = df[df["YearMonth"] == ym].assign(_d=df[dim].fillna(fill)).groupby("_d")["TotalAmount"].sum()
        prev = df[df["YearMonth"] == m_prev].assign(_d=df[dim].fillna(fill)).groupby("_d")["TotalAmount"].sum()
        rows = []
        for name in cur.index.union(prev.index):
            cv, pv = float(cur.get(name, 0.0)), float(prev.get(name, 0.0))
            rows.append({"name": str(name),
                         "cur": round(cv, 2), "prev": round(pv, 2),
                         "delta": round(cv - pv, 2),
                         "pct": round((cv - pv) / pv * 100.0, 2) if pv else None})
        rows.sort(key=lambda r: -abs(r["delta"]))
        return rows

    return jsonify({"ym": ym, "z": round(float(hit["z"]), 2),
                    "gmv": round(gmv, 2), "prev_gmv": round(prev_gmv, 2),
                    "pct": round(pct, 2) if pct is not None else None,
                    "seg_drivers": drivers("RFM_Segment", "未分层"),
                    "cat_drivers": drivers("Category", "未分类")})


# ---------------- 自动洞察卡（对标 Tableau Pulse） ----------------
def _shift_month(ym, k):
    """'YYYY-MM' 平移 k 个月（-1=上月，-12=去年同月）"""
    y, m = int(ym[:4]), int(ym[5:7])
    t = y * 12 + (m - 1) + k
    return f"{t // 12:04d}-{t % 12 + 1:02d}"


def build_insights(df, keys, category, year, month):
    """自动洞察：GMV 环比/同比 + 客群与品类变化贡献分解 + 笔单价环比（3-5 条）。

    口径与页1 KPI 一致：销售金额=TotalAmount 求和（万元展示）、笔单价=TotalAmount 均值；
    year/month 作为目标月定位参数（缺省取筛选范围内最近完整月，环比取其上一自然月，
    同比取去年同月）；所有数字均从事实表真实聚合，无数据可算时返回空数组。"""
    dff = df[df["CustomerKey"].isin(keys)]
    if category:
        dff = dff[dff["Category"] == category]
    if len(dff) == 0:
        return []

    avail = sorted(set(dff["YearMonth"].unique()))
    if year is not None and month is not None:
        m_cur = f"{year:04d}-{month:02d}"
    elif year is not None:
        cands = [ym for ym in avail if ym.startswith(f"{year:04d}-")]
        m_cur = cands[-1] if cands else ""
    elif month is not None:
        cands = [ym for ym in avail if ym.endswith(f"-{month:02d}")]
        m_cur = cands[-1] if cands else ""
    else:
        m_cur = avail[-1]
    if m_cur not in avail:
        return []
    g_cur = float(dff.loc[dff["YearMonth"] == m_cur, "TotalAmount"].sum())
    if g_cur <= 0:
        return []

    def ym_sum(ym):
        return float(dff.loc[dff["YearMonth"] == ym, "TotalAmount"].sum())

    def pct(cur, prev):
        return (cur - prev) / prev * 100.0 if prev else None

    def pct_type(p):
        return "good" if p >= 1 else ("bad" if p <= -1 else "warn")

    def fmt_wan(v):
        return f"{v / 10000:,.1f}"

    def fmt_pct(p):
        return f"{'+' if p >= 0 else ''}{p:.1f}%"

    out = []
    m_prev = _shift_month(m_cur, -1)
    m_yoy = _shift_month(m_cur, -12)
    has_prev = dff["YearMonth"].eq(m_prev).any()
    has_yoy = dff["YearMonth"].eq(m_yoy).any()
    m_link = {"key": "month", "value": str(int(m_cur[5:7]))}

    # ① GMV 环比（最近完整月 vs 上一月）
    if has_prev:
        g_prev = ym_sum(m_prev)
        if g_prev > 0:
            p = pct(g_cur, g_prev)
            out.append({
                "type": pct_type(p),
                "text": f"GMV 环比：{m_cur} 销售 {fmt_wan(g_cur)} 万，"
                        f"较上月 {m_prev}（{fmt_wan(g_prev)} 万）"
                        f"{'增长' if p >= 0 else '下降'} {abs(p):.1f}%",
                "delta": fmt_pct(p),
                "link": m_link,
            })

    # ② 同比（最近完整月 vs 去年同月）
    if has_yoy:
        g_yoy = ym_sum(m_yoy)
        if g_yoy > 0:
            p = pct(g_cur, g_yoy)
            out.append({
                "type": pct_type(p),
                "text": f"GMV 同比：{m_cur} 销售 {fmt_wan(g_cur)} 万，"
                        f"较去年同期 {m_yoy}（{fmt_wan(g_yoy)} 万）"
                        f"{'增长' if p >= 0 else '下降'} {abs(p):.1f}%",
                "delta": fmt_pct(p),
                "link": m_link,
            })

    # ③ 变化贡献分解：按客群 / 品类，各取一条「变化最大贡献者」
    if has_prev and g_prev > 0:
        total_p = pct(g_cur, g_prev)
        for dim, fill, link_key, dim_name in (
                ("RFM_Segment", "未分层", "seg", "客群"),
                ("Category", "未分类", "category", "品类")):
            g = dff.assign(_d=dff[dim].fillna(fill))
            cur_s = g[g["YearMonth"] == m_cur].groupby("_d")["TotalAmount"].sum()
            prev_s = g[g["YearMonth"] == m_prev].groupby("_d")["TotalAmount"].sum()
            idx = cur_s.index.union(prev_s.index)
            delta_s = cur_s.reindex(idx, fill_value=0) - prev_s.reindex(idx, fill_value=0)
            if len(delta_s) == 0:
                continue
            if total_p >= 0:
                name, contrib = delta_s.idxmax(), delta_s.max()
            else:
                name, contrib = delta_s.idxmin(), delta_s.min()
            if not contrib:
                continue
            share = abs(contrib) / abs(delta_s).sum() * 100.0
            out.append({
                "type": pct_type(total_p),
                "text": f"{dim_name}贡献：{m_cur} 环比变化主要来自「{name}」"
                        f"（较上月 {'+' if contrib >= 0 else ''}{contrib / 10000:.1f} 万，"
                        f"占{dim_name}总变化 {share:.0f}%）",
                "delta": f"{'+' if contrib >= 0 else ''}{contrib / 10000:.1f}万",
                "link": {"key": link_key, "value": str(name)},
            })

    # ④ 笔单价环比（口径=页1 KPI「笔单价(元)」）
    if has_prev:
        aov_cur = float(dff.loc[dff["YearMonth"] == m_cur, "TotalAmount"].mean())
        aov_prev = float(dff.loc[dff["YearMonth"] == m_prev, "TotalAmount"].mean())
        if aov_prev > 0:
            p = pct(aov_cur, aov_prev)
            out.append({
                "type": pct_type(p),
                "text": f"笔单价环比：{m_cur} 笔单价 {aov_cur:,.0f} 元，"
                        f"较上月（{aov_prev:,.0f} 元）{'提升' if p >= 0 else '回落'} {abs(p):.1f}%",
                "delta": fmt_pct(p),
                "link": m_link,
            })

    return out[:5]


@app.route("/api/insights")
def insights():
    """自动洞察卡接口：接收与 /api/dashboard 相同的筛选参数
    （tier/gender/age/city/year/month/strat，参数解析复用 _int_arg 与 load_data()），
    返回 3-5 条结构化洞察 {type: good/bad/warn, text, delta}；无数据时返回空数组。"""
    df, rfm, camp = load_data()
    tier = request.args.get("tier", "").strip()
    gender = request.args.get("gender", "").strip()
    age = request.args.get("age", "").strip()
    city = request.args.get("city", "").strip()
    year = _int_arg("year")
    month = _int_arg("month")
    seg = request.args.get("seg", "").strip()
    category = request.args.get("category", "").strip()
    strat = request.args.get("strat", "").strip()  # 策略组仅作用于营销表，交易洞察不受影响（与 /api/dashboard 一致）

    pool = rfm
    if gender:
        pool = pool[pool["Gender"] == gender]
    if age:
        pool = pool[pool["AgeGroup"] == age]
    if city:
        pool = pool[pool["CityLevel"] == city]
    if seg:
        pool = pool[pool["RFM_Segment"] == seg]
    pool1 = pool if not tier else pool[pool["RFM_Segment"] == tier]
    keys = set(pool1["CustomerKey"])
    return jsonify(build_insights(df, keys, category, year, month))


# ---------------- ④ GMV 预测（对数空间阻尼 Holt，手写实现无外部依赖） ----------------
FC_METHOD = "对数空间阻尼 Holt 线性趋势（无季节项）"
FC_CONF = 0.80
FC_Z = 1.2816          # 80% 双侧正态分位（1.2816）

FC_NOTE = (
    "方法：对月度 GMV 取自然对数后做阻尼趋势 Holt 指数平滑"
    "（L_t=α·z_t+(1-α)(L_{t-1}+φ·T_{t-1})，T_t=β(L_t-L_{t-1})+(1-β)φ·T_{t-1}），"
    "h 步点预测 = exp(L_T+T_T·Σφ^j)；α/β/φ 由网格搜索最小化一步预测残差平方和确定。"
    "区间：80% 置信带，半宽 = 1.2816×残差标准差×√h（对数空间，再指数还原，故不倒挂、不为负）。"
    "未使用季节项的原因：现有 36 个月只含 2 个完整年周期，且序列处于强增长通道，"
    "季节因子与趋势高度共线（实测对数季节振幅达 ±1.0，相当于 2.7 倍），"
    "会系统性压低 1 月、抬高 12 月；实测 1 步滚动回测 MAPE 由 17.0% 恶化到 51.5%，故不采用。"
)


def _holt_log(z, h=3, alpha=0.65, beta=0.10, phi=0.98):
    """对数空间阻尼趋势 Holt：状态 L(水平)/T(趋势)，无季节项。

    递推（z = ln 月度GMV）：
        ŷ_t = L_{t-1} + φ·T_{t-1}
        L_t = α·z_t + (1-α)(L_{t-1} + φ·T_{t-1})
        T_t = β(L_t - L_{t-1}) + (1-β)·φ·T_{t-1}
    h 步点预测（对数）：L_T + T_T·Σ_{j=1..h}φ^j。
    返回 fc_log（对数点预测）、sd（一步残差标准差，对数单位）、sse。"""
    L = float(z[0])
    T = float(z[1] - z[0]) if len(z) > 1 else 0.0
    resid = []
    for t in range(1, len(z)):
        pred = L + phi * T
        resid.append(float(z[t] - pred))
        L_new = alpha * float(z[t]) + (1 - alpha) * (L + phi * T)
        T = beta * (L_new - L) + (1 - beta) * phi * T
        L = L_new
    fc_log = [float(L + T * sum(phi ** j for j in range(1, k + 1))) for k in range(1, h + 1)]
    sd = float(np.std(resid, ddof=2)) if len(resid) > 2 else 0.20
    return {"fc_log": fc_log, "sd": sd, "sse": float(np.sum(np.square(resid))),
            "L": L, "T": T}


def _holt_grid(z):
    """网格搜索 α/β/φ：目标=一步预测残差平方和最小（残差在对数空间，等价于最小化相对误差）"""
    best = None
    for a in (0.15, 0.25, 0.35, 0.5, 0.65, 0.8, 0.95):
        for b in (0.01, 0.03, 0.06, 0.1, 0.2):
            for phi in (0.75, 0.85, 0.92, 0.98):
                r = _holt_log(z, alpha=a, beta=b, phi=phi)
                if best is None or r["sse"] < best[1]["sse"]:
                    best = ({"alpha": a, "beta": b, "phi": phi}, r)
    return best


def _fc_backtest(z, y, months, cfg, start=6, hold=3):
    """回测两块：① 1 步滚动（逐月用历史重估再预测下一个月）② 末 hold 个月留出。
    返回 MAPE 与留出明细，供页面直接展示精度，不写死任何误差数字。"""
    one = []
    for i in range(start, len(z)):
        r = _holt_log(z[:i], h=1, **cfg)
        one.append(abs(float(np.exp(r["fc_log"][0])) - y[i]) / y[i] * 100.0)
    detail, hold_mape = None, None
    if len(z) - hold >= 6:
        r = _holt_log(z[:-hold], h=hold, **cfg)
        preds = [float(np.exp(v)) for v in r["fc_log"]]
        detail = []
        for i, pv in enumerate(preds):
            act = float(y[-hold + i])
            detail.append({"ym": months[-hold + i], "pred": _wan(pv), "actual": _wan(act),
                           "ape": round(abs(pv - act) / act * 100.0, 2) if act else None})
        hold_mape = round(float(np.mean([abs(pv - float(y[-hold + i])) / float(y[-hold + i]) * 100.0
                                         for i, pv in enumerate(preds)])), 2)
    return {"one_step": {"n": len(one), "mape": round(float(np.mean(one)), 2) if one else None},
            "holdout": {"h": hold, "mape": hold_mape, "detail": detail}}


@app.route("/api/forecast")
def forecast():
    """下月（及未来 h 个月）GMV 预测：?h=3（1-6）+ 全局筛选参数，口径与总览主图完全一致。

    history 给出参与拟合的月度实际值（供前端衔接虚线），forecast 给出 point/lo/hi（万元）。
    历史月不足 12 个时返回 available=false 与中文原因，前端显示提示而非空图。"""
    h = _int_arg("h", 3, 1, 6)
    df, rfm, camp = load_data()

    tier = request.args.get("tier", "").strip()
    gender = request.args.get("gender", "").strip()
    age = request.args.get("age", "").strip()
    city = request.args.get("city", "").strip()
    seg = request.args.get("seg", "").strip()
    category = request.args.get("category", "").strip()
    year = _int_arg("year")
    month = _int_arg("month")

    pool = rfm
    if gender:
        pool = pool[pool["Gender"] == gender]
    if age:
        pool = pool[pool["AgeGroup"] == age]
    if city:
        pool = pool[pool["CityLevel"] == city]
    if seg:
        pool = pool[pool["RFM_Segment"] == seg]
    if tier:
        pool = pool[pool["RFM_Segment"] == tier]
    if len(pool) == 0:
        return jsonify({"available": False, "reason": "当前筛选条件下没有客户，无法预测。"})

    dff = df[df["CustomerKey"].isin(set(pool["CustomerKey"]))]
    if year is not None:
        dff = dff[dff["Year"] == year]
    if month is not None:
        dff = dff[dff["Month"] == month]
    if category:
        dff = dff[dff["Category"] == category]

    monthly = dff.groupby("YearMonth")["TotalAmount"].sum().sort_index()
    months = [str(i) for i in monthly.index]
    y = [float(v) for v in monthly.values]
    if len(y) < 12:
        return jsonify({
            "available": False,
            "reason": (f"当前口径只有 {len(y)} 个有交易的月份，"
                       f"至少需要 12 个月才能拟合趋势（可放宽年份/月份/品类筛选）。"),
            "n_months": len(y)})

    # 口径对齐闸门：预测基线月必须覆盖全局最新数据月，否则主图预测段会从 0 起跳、
    # 语义上也变成「给一个已停止交易的口径外推」，如实返回不适用
    global_last = str(df["YearMonth"].max())
    if months[-1] < global_last:
        return jsonify({
            "available": False,
            "reason": (f"该口径最后交易月为 {months[-1]}，未覆盖最新数据月 {global_last}"
                       f"（最近一个月无交易），趋势外推不适用。"),
            "n_months": len(y)})

    z = np.log(np.maximum(np.array(y, dtype=float), 1.0))
    cfg, fit = _holt_grid(z)
    full = _holt_log(z, h=h, **cfg)

    # 质量闸门：一步残差 σ 过大（>0.47，约 ±60%）说明该口径波动无法被趋势解释，
    # 预测区间会宽到失去意义，此时如实返回不可用而不是给一个「看起来有数」的区间
    if full["sd"] > 0.47:
        return jsonify({
            "available": False,
            "reason": (f"该口径月度波动过大（拟合残差 σ={full['sd']:.2f}，"
                       f"对应一步区间约 ±{(np.exp(1.2816 * full['sd']) - 1) * 100:.0f}%），"
                       f"趋势无法解释其波动，预测不可靠。建议放宽筛选或改用全量口径。"),
            "n_months": len(y), "params": {"sigma_log": round(full["sd"], 4)}})

    base = pd.Period(months[-1], freq="M")
    rows = []
    for k, v in enumerate(full["fc_log"], 1):
        w = FC_Z * full["sd"] * float(np.sqrt(k))
        rows.append({"ym": str(base + k),
                     "point": _wan(float(np.exp(v))),
                     "lo": _wan(float(np.exp(v - w))),
                     "hi": _wan(float(np.exp(v + w))),
                     "width_pct": round((float(np.exp(w)) - 1) * 100.0, 1)})

    hist = [{"ym": months[i], "gmv": _wan(y[i])} for i in range(max(0, len(y) - 12), len(y))]
    return jsonify({
        "available": True,
        "method": FC_METHOD,
        "method_note": FC_NOTE,
        "confidence": FC_CONF,
        "base_ym": months[-1],
        "n_months": len(y),
        "params": {"alpha": cfg["alpha"], "beta": cfg["beta"], "phi": cfg["phi"],
                   "sigma_log": round(full["sd"], 4),
                   "level_log": round(full["L"], 4), "trend_log": round(full["T"], 5)},
        "history": hist,
        "forecast": rows,
        "backtest": _fc_backtest(z, y, months, cfg),
        "warn": ("样本仅 %d 个月（<24），趋势估计稳定性有限。" % len(y)) if len(y) < 24 else None,
    })


# ---------------- What-if 营销模拟器（决策沙盘，⑤） ----------------
SIM_NOTE = (
    "口径：基线=最近完整月 GMV（与页1「销售金额」同口径）；"
    "三档杠杆按「百分点 × 客群总人数」线性外推，人均基数取最近完整月该客群实际成交客户人均销售额"
    "（唤醒档取历史唤醒订单人均，挽回档按潜力客群人均）；"
    "投入=让利成本（增量GMV×(1-近12月毛利率)）+触达成本（触达人数×人均营销成本）；增量ROI=增量GMV÷投入。"
    "注意：本数据集营销成本仅含活动投放费用、不含人力与固定成本，ROI 绝对值偏高，只用于杠杆间横向比较。"
)


def _wan(x):
    """元 → 万元（2 位小数），统一页面金额单位"""
    return round(float(x) / 10000.0, 2)


def _seg_last_month(df_m, seg):
    """最近完整月某客群：(总人数由调用方给) 当月购买人数 / 当月销售额 / 当月人均"""
    sub = df_m[df_m["RFM_Segment"] == seg]
    buyers = int(sub["CustomerKey"].nunique())
    sales = float(sub["TotalAmount"].sum())
    return buyers, sales, (sales / buyers if buyers else 0.0)


@app.route("/api/simulate")
def simulate():
    """What-if 营销模拟器：三个杠杆 → 下月增量 GMV / 投入 / 增量 ROI。

    参数：rebuy_pp（高价值复购率 +pp，0-20）、awaken_pp（沉睡唤醒率 +pp，0-20）、
    recover_n（挽回沉睡客户人数，0-沉睡总数）；可选 tier/gender/age/city 沿用当前客户池。
    三个参数全为 0 时输出必须严格等于基线（inc_gmv=0、total_gmv=baseline）。"""
    rebuy_pp = _float_arg("rebuy_pp", 0.0, 0, 20)
    awaken_pp = _float_arg("awaken_pp", 0.0, 0, 20)
    recover_n = _int_arg("recover_n", 0, lo=0)
    tier = request.args.get("tier", "").strip()
    gender = request.args.get("gender", "").strip()
    age = request.args.get("age", "").strip()
    city = request.args.get("city", "").strip()

    df, rfm, camp = load_data()
    pool = rfm
    if gender:
        pool = pool[pool["Gender"] == gender]
    if age:
        pool = pool[pool["AgeGroup"] == age]
    if city:
        pool = pool[pool["CityLevel"] == city]
    if tier:
        pool = pool[pool["RFM_Segment"] == tier]
    if len(pool) == 0:
        return jsonify({"error": "当前筛选条件下没有客户，无法模拟，请放宽筛选条件"}), 400

    keys = set(pool["CustomerKey"])
    dfp = df[df["CustomerKey"].isin(keys)]
    if len(dfp) == 0:
        return jsonify({"error": "当前筛选条件下没有交易记录，无法模拟"}), 400

    # 基线：客户池内的最近完整月（不随 year/month 筛选变动，否则基线会变成空窗口）
    ym = str(dfp["YearMonth"].max())
    m = dfp[dfp["YearMonth"] == ym]
    base_gmv = float(m["TotalAmount"].sum())
    base_orders = int(len(m))
    base_cust = int(m["CustomerKey"].nunique())
    if base_gmv <= 0 or base_cust == 0:
        return jsonify({"error": f"{ym} 无成交记录，无法建立基线"}), 400

    # —— 客群基数（全部来自真实分布） ——
    hv_n = int((pool["RFM_Segment"] == "高价值客户").sum())
    sl_pool = pool[pool["RFM_Segment"] == "沉睡客户"]
    sl_n = int(len(sl_pool))
    sl_awakened = int(sl_pool["IsAwakened"].sum())
    if hv_n == 0 and sl_n == 0:
        return jsonify({"error": "当前客户池既无高价值客户也无沉睡客户，两个杠杆无法生效"}), 400

    hv_buy, hv_sales, hv_per = _seg_last_month(m, "高价值客户")
    pot_buy, pot_sales, pot_per = _seg_last_month(m, "潜力客户")

    # 唤醒档人均：历史唤醒订单的真实人均（跨月，因沉睡客户当月必无成交）
    aw = dfp[dfp["IsAwakenedOrder"] == True]                      # noqa: E712 (pandas 逐行布尔)
    aw_cust = int(aw["CustomerKey"].nunique())
    aw_per = float(aw["TotalAmount"].sum()) / aw_cust if aw_cust else 0.0

    # 兜底链：唤醒人均 → 潜力人均 → 全客群当月人均（均为真实值，不设拍脑袋常数）
    aw_fb = pot_fb = False
    if aw_per <= 0:
        aw_per = pot_per
        aw_fb = True
    if aw_per <= 0:
        aw_per = base_gmv / base_cust
        aw_fb = True
    if pot_per <= 0:
        pot_per = base_gmv / base_cust
        pot_fb = True

    # —— 三档杠杆增量 ——
    hv_cap = max(0, hv_n - hv_buy)                     # 复购率提升空间（未购买的高价值客户）
    rebuy_persons = min(int(round(hv_n * rebuy_pp / 100.0)), hv_cap)
    sl_cap = max(0, sl_n - sl_awakened)                # 未唤醒的沉睡客户
    awaken_persons = min(int(round(sl_n * awaken_pp / 100.0)), sl_cap)
    remain_sleep = max(0, sl_n - sl_awakened - awaken_persons)
    recover_used = min(int(recover_n), remain_sleep)

    inc_rebuy = rebuy_persons * hv_per
    inc_awaken = awaken_persons * aw_per
    inc_recover = recover_used * pot_per
    inc = inc_rebuy + inc_awaken + inc_recover

    # —— 投入与 ROI（让利成本 + 触达成本，两个基数都取自真实数据） ——
    lo12 = (pd.Period(ym, freq="M") - 11).strftime("%Y-%m")
    span = dfp[dfp["YearMonth"] >= lo12]
    span_rev = float(span["TotalAmount"].sum())
    span_cost = float((span["Quantity"] * span["CostPrice"]).sum())
    gm = (span_rev - span_cost) / span_rev if span_rev > 0 else 0.0
    gm = min(max(gm, 0.0), 1.0)
    active12 = int(span["CustomerKey"].nunique())
    camp12 = camp[pd.to_datetime(camp["StartDate"]) >= pd.Timestamp(lo12 + "-01")]
    cost12 = float(camp12["Cost"].sum())
    per_cap_cost = (cost12 / active12) if active12 > 0 else 0.0

    reach = rebuy_persons + awaken_persons + recover_used
    discount_cost = inc * (1.0 - gm)
    reach_cost = reach * per_cap_cost
    total_cost = discount_cost + reach_cost
    roi = (inc / total_cost) if total_cost > 0 else None
    lift = (inc / base_gmv * 100.0) if base_gmv > 0 else 0.0

    hv_rate = (hv_buy / hv_n * 100.0) if hv_n else 0.0
    sl_rate = (sl_awakened / sl_n * 100.0) if sl_n else 0.0

    assumptions = [
        f"高价值客群 {hv_n:,} 人；最近完整月（{ym}）购买 {hv_buy:,} 人（复购率 {hv_rate:.1f}%），"
        f"当月人均消费 {hv_per:,.0f} 元——复购档人均基数。",
        f"沉睡客群 {sl_n:,} 人，其中已唤醒 {sl_awakened:,} 人（{sl_rate:.2f}%）；历史唤醒订单 "
        f"{int(len(aw)):,} 笔 / {aw_cust:,} 人，唤醒后人均 {aw_per:,.0f} 元——唤醒档人均基数。",
        f"潜力客群最近完整月购买 {pot_buy:,} 人，人均 {pot_per:,.0f} 元——挽回档人均基数"
        f"（{ym} 潜力客群销售额 {_wan(pot_sales):,.2f} 万元）。",
        f"近 12 个月毛利率 {gm * 100:.1f}% =（销售额 {_wan(span_rev):,.2f} 万 − 成本 "
        f"{_wan(span_cost):,.2f} 万）÷ 销售额，按实际成交价计算；让利成本 = 增量GMV ×（1 − 毛利率）。",
        f"人均营销成本 {per_cap_cost:,.2f} 元/人 = 近 12 个月活动投放成本 {cost12:,.0f} 元 ÷ "
        f"近 12 个月活跃客户 {active12:,} 人（{ym} 往前 12 个月）。",
        "增量GMV = 新增复购人数×高价值人均 + 新增唤醒人数×唤醒后人均 + 挽回人数×潜力人均；"
        "触达人数 = 三者之和；增量ROI = 增量GMV ÷（让利成本 + 触达成本）。",
    ]
    if recover_n > remain_sleep:
        assumptions.append(
            f"提示：挽回人数已按剩余沉睡池截断——请求 {int(recover_n):,} 人，"
            f"扣除已唤醒与本次新增唤醒后仅剩 {remain_sleep:,} 人可挽回。")
    if rebuy_pp > 0 and int(round(hv_n * rebuy_pp / 100.0)) > hv_cap:
        assumptions.append(f"提示：复购档新增人数已封顶 {hv_cap:,} 人（高价值客群未购买人数上限）。")
    if aw_fb:
        assumptions.append("提示：当前客户池无历史唤醒订单样本，唤醒档人均回退为可用真实人均（非估算常数）。")
    if pot_fb:
        assumptions.append("提示：当前客户池最近完整月无潜力客群成交样本，挽回档人均回退为当前客户池当月人均。")

    return jsonify({
        "ym": ym,
        "baseline": {"gmv": _wan(base_gmv), "orders": base_orders, "cust": base_cust,
                     "aov": round(base_gmv / base_orders, 2) if base_orders else 0.0},
        "params": {"rebuy_pp": round(rebuy_pp, 2), "awaken_pp": round(awaken_pp, 2),
                   "recover_n": int(recover_n)},
        "limits": {"rebuy_pp": 20, "awaken_pp": 20, "recover_n": sl_n,
                   "rebuy_persons_cap": hv_cap, "awaken_persons_cap": sl_cap},
        "current": {"hv_rebuy_rate": round(hv_rate, 2), "sl_awaken_rate": round(sl_rate, 2),
                    "hv_n": hv_n, "sl_n": sl_n},
        "levers": [
            {"key": "rebuy", "label": "高价值复购", "persons": rebuy_persons,
             "per_capita": round(hv_per, 2), "inc": _wan(inc_rebuy)},
            {"key": "awaken", "label": "沉睡唤醒", "persons": awaken_persons,
             "per_capita": round(aw_per, 2), "inc": _wan(inc_awaken)},
            {"key": "recover", "label": "人工挽回", "persons": recover_used,
             "per_capita": round(pot_per, 2), "inc": _wan(inc_recover)},
        ],
        "inc_gmv": _wan(inc),
        "total_gmv": _wan(base_gmv + inc),
        "lift_pct": round(lift, 2),
        "cost": {"total": _wan(total_cost), "discount": _wan(discount_cost),
                 "reach": _wan(reach_cost), "per_cap_cost": round(per_cap_cost, 2),
                 "persons": reach, "gross_margin": round(gm * 100, 2)},
        "roi": round(roi, 2) if roi is not None else None,
        "assumptions": assumptions,
        "note": SIM_NOTE,
        "filters": {"tier": tier, "gender": gender, "age": age, "city": city},
    })


# ---------------- 中文问答查数（⑦ 规则版 ChatBI，离线） ----------------
@app.route("/api/ask")
def ask():
    """自然语言查数：解析与聚合全部在 ask_parser.py，本处只挂路由。

    任何输入（空串、乱码、无关内容）都返回 200 + 引导话术，不抛 500。

    ⑨ 开关关闭（默认）时走下面这一行——与 ⑦ 的响应逐字节一致，不多一个字段；
    开启时交给 llm_slots：先让本地 LLM 解析意图，校验通过后**仍然回到
    ask_parser 聚合**，拿不到槽位则自动回退规则解析并标注「（规则解析）」。
    """
    q = (request.args.get("q") or "").strip()[:120]      # 截断超长输入
    df, rfm, camp = load_data()
    cust_map = pd.read_csv(PA("Dim_Customer.csv")).set_index("CustomerKey")["CustomerID"]
    if not llm_slots.is_enabled():
        return jsonify(ask_parser.answer(q, df, rfm, camp, cust_map.to_dict()))
    return jsonify(llm_slots.ask(q, df, rfm, camp, cust_map.to_dict()))


# ---------------- ⑨ 本地 LLM 开关（默认关闭，仅回环地址） ----------------
@app.route("/api/llm/status")
def llm_status():
    """当前解析模式 / 本地 Ollama 是否可用 / 最近一次耗时与回退情况"""
    return jsonify(llm_slots.status())


@app.route("/api/llm/toggle", methods=["POST"])
def llm_toggle():
    """切换规则解析 ↔ 本地 LLM 解析（内存态，重启回到默认关闭）。

    三重防护：只允许本机调用 + 必须带自定义请求头（跨站请求会触发预检而被拦截）
    + 环境变量锁定时拒绝改动。避免局域网或恶意网页偷偷把大屏改成走 LLM。
    """
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify({"error": "只允许本机操作"}), 403
    if request.headers.get("X-RFM-Action") != "toggle-llm":
        return jsonify({"error": "缺少请求标记，已拒绝"}), 403
    if llm_slots.env_locked():
        st = llm_slots.status()
        st["error"] = "开关已被启动环境变量 RFM_ENABLE_LLM 锁定"
        return jsonify(st), 409
    on = (request.args.get("on") or "").strip().lower()
    if on not in ("0", "1", "true", "false"):
        return jsonify({"error": "参数 on 只能是 1 或 0"}), 400
    llm_slots.set_enabled(on in ("1", "true"))
    return jsonify(llm_slots.status())


# ---------------- 下钻明细接口 ----------------
@app.route("/api/drill")
def drill():
    """下钻明细：type=seg 返回客群 Top 客户明细；type=month 返回单月销售明细（支持联动参数）"""
    df, rfm, camp = load_data()
    dtype = request.args.get("type", "seg").strip()
    seg = request.args.get("seg", "").strip()
    category = request.args.get("category", "").strip()
    year = _int_arg("year")
    month = _int_arg("month")
    # 先校验 top（非整数/≤0 → 400），避免非法值在 seg 分支之后才抛
    topn = min(_int_arg("top", 10, lo=1), 50)   # >50 仍截断到 50

    if dtype == "month":
        ym = request.args.get("ym", "").strip()
        if not ym:
            return jsonify({"error": "缺少 ym 参数"}), 400
        return jsonify(month_drill(df, rfm, ym, seg, category))

    if not seg:
        return jsonify({"error": "缺少 seg 参数"}), 400

    g = rfm[rfm["RFM_Segment"] == seg]
    if len(g) == 0:
        return jsonify({"error": "客群不存在"}), 404

    top = g.nlargest(topn, "M")[
        ["CustomerKey", "F", "M", "R_days", "RFM_Score", "IsAwakened"]]
    customers = [{"key": int(r["CustomerKey"]), "f": int(r["F"]), "m": f2(r["M"]),
                  "r": int(r["R_days"]), "score": int(r["RFM_Score"]),
                  "awaken": bool(r["IsAwakened"])}
                 for _, r in top.iterrows()]

    # 该客群贡献销售（交易聚合，避免事实表与汇总表混用；品类联动时按品类口径）
    # year/month 作用于交易窗口，保证抽屉数字与页面 KPI 同口径（P0-04）
    txs = df[df["CustomerKey"].isin(set(g["CustomerKey"]))]
    if year is not None:
        txs = txs[txs["Year"] == year]
    if month is not None:
        txs = txs[txs["Month"] == month]
    if category:
        txs = txs[txs["Category"] == category]
    sales = float(txs["TotalAmount"].sum())
    avg_tx_m = float(txs["TotalAmount"].mean()) if len(txs) else 0.0
    scope = (f"{year}年{month}月" if year is not None and month is not None
             else f"{year}年" if year is not None else "全时段")
    summary = {
        "seg": seg,
        "scope": scope,
        "cnt": int(len(g)),
        "avg_m": f2(avg_tx_m),   # 筛选窗口内笔均交易金额（与页面 KPI 笔单价同口径）
        "avg_f": f2(g["F"].mean()),
        "avg_r": f2(g["R_days"].mean()),
        "awaken_cnt": int(g["IsAwakened"].sum()),
        "total_sales": f2(sales / 10000),
        "tier_dist": {str(k): int(v) for k, v in g["CustomerTier"].value_counts().items()},
    }
    return jsonify({"type": "seg", "summary": summary, "customers": customers})


def month_drill(df, rfm, ym, seg="", category=""):
    """月度下钻：单月销售摘要 + 品类构成 + Top 商品（可叠加客群/品类联动筛选）"""
    dff = df[df["YearMonth"] == ym]
    if seg:
        keys = set(rfm[rfm["RFM_Segment"] == seg]["CustomerKey"])
        dff = dff[dff["CustomerKey"].isin(keys)]
    if category:
        dff = dff[dff["Category"] == category]
    if len(dff) == 0:
        return {"type": "month", "ym": ym, "empty": True}

    sales = float(dff["TotalAmount"].sum())
    summary = {
        "ym": ym,
        "total_sales": f2(sales / 10000),
        "order_cnt": int(len(dff)),
        "cust_cnt": int(dff["CustomerKey"].nunique()),
        "aov": f2(dff["TotalAmount"].mean()),
        "awake_cnt": int(dff["IsAwakenedOrder"].sum()),
    }
    cat = dff.groupby("Category")["TotalAmount"].sum().sort_values(ascending=False)
    top_category = [{"name": str(k), "sales": f2(v / 10000),
                     "pct": f2(v / sales * 100)} for k, v in cat.items()]
    prod = dff.groupby("ProductName").agg(
        sales=("TotalAmount", "sum"), category=("Category", "first"))
    prod = prod.sort_values("sales", ascending=False).head(10).reset_index()
    top_product = [{"name": r["ProductName"], "category": r["category"],
                    "sales": f2(r["sales"] / 10000)} for _, r in prod.iterrows()]
    return {"type": "month", "summary": summary,
            "top_category": top_category, "top_product": top_product}


# ---------------- 二级下钻：客群深挖 / 月度深挖 ----------------
def _prev_ym(ym):
    """YYYY-MM 的上一个月字符串"""
    try:
        p = pd.Period(ym, freq="M") - 1
        return str(p)
    except Exception:
        return None


# 主四页底部指标口径说明（12px 灰字，由 /api/dashboard 一并返回；与二级深挖页 note 口径一致）
PAGE_NOTES = {
    "p1": "购买客户数/交易笔数/销售金额/笔单价按筛选窗口内交易统计；"
          "客群结构占比扇区人数之和=购买客户数；F/M均值与RFM散点取窗口内活跃客户；"
          "金额单位万元，保留2位小数。",
    "p2": "对比组固定为高价值 vs 非高价值（营销层级 CustomerTier 分层，与页①RFM客群同名不同义）；"
          "复购率=下单≥2次客户占比（客户级）；提升幅度=(高价值复购率-非高价值复购率)/非高价值复购率；"
          "趋势与品类排名为筛选期内交易；金额单位万元，保留2位小数。",
    "p3": "本页固定分析沉睡客户（营销层级 CustomerTier 分层，与页①RFM客群同名不同义）；"
          "唤醒率=唤醒客户数/沉睡客户数（客户级，IsAwakened=True）；"
          "唤醒订单=IsAwakenedOrder=True 的交易；趋势与对比图为筛选期内交易。",
    "p4": "ROI=收入/成本；KPI 按 year+strat 筛选统计；策略对比图按 year 过滤、不含 strat 裁剪（仅高亮）；"
          "年度ROI 用全量营销数据、不受 year 裁剪；成本/收入单位万元，保留2位小数。",
}


# ---------------- ⑧ 口径追溯：KPI 结构化元数据（公式与 page_* 代码逐条对应） ----------------
# 每个 KPI 卡右上角 ⓘ 打开的就是这里的内容；note 与 PAGE_NOTES 同一措辞，不得互相矛盾。
KPI_META = {
    "p1_customers": {
        "page": "页① RFM客群分层总览",
        "label": "购买客户数",
        "formula": "购买客户数 = COUNT(DISTINCT CustomerKey)｜窗口内有交易的客户",
        "source": "Fact_Transaction.CustomerKey；客户属性取 Dim_Customer(Gender/AgeGroup/CityLevel) 与 Agg_Customer_RFM(RFM_Segment)",
        "calc_path": "① 按性别/年龄/城市/客群筛客户池 → ② 取该池客户在筛选窗口（年/月/品类）内的交易行 → ③ 对 CustomerKey 去重计数",
        "note": "只统计筛选窗口内有交易的客户，不是全部注册客户；与页①饼图扇区人数之和一致。",
    },
    "p1_orders": {
        "page": "页① RFM客群分层总览",
        "label": "交易笔数",
        "formula": "交易笔数 = COUNT(*)｜窗口内交易行数（一行 = 一笔）",
        "source": "Fact_Transaction（按筛选窗口过滤后的行数）",
        "calc_path": "① 客户池筛选 → ② 按 Year/Month/Category 过滤交易 → ③ 统计剩余行数",
        "note": "笔数按交易明细行计，不做订单号合并；与「购买客户数」比值即人均购买频次。",
    },
    "p1_sales": {
        "page": "页① RFM客群分层总览",
        "label": "销售金额(万)",
        "formula": "销售金额 = Σ TotalAmount ÷ 10000（万元，保留 2 位小数）",
        "source": "Fact_Transaction.TotalAmount（实付金额，已含折扣）",
        "calc_path": "① 客户池筛选 → ② 按 Year/Month/Category 过滤 → ③ 对 TotalAmount 求和 → ④ ÷10000 转万元",
        "note": "为实付口径（含折扣），非吊牌价；金额单位万元，保留 2 位小数。",
    },
    "p1_aov": {
        "page": "页① RFM客群分层总览",
        "label": "笔单价(元)",
        "formula": "笔单价 = Σ TotalAmount ÷ COUNT(*)（元，取整展示）",
        "source": "Fact_Transaction.TotalAmount 与交易行数",
        "calc_path": "① 取窗口内交易 → ② 总销售额 ÷ 交易笔数（等价于对 TotalAmount 取均值）→ ③ 四舍五入到元",
        "note": "笔单价 = 销售额 ÷ 交易笔数，不是「客户平均消费」；后者见客群深挖页的人均 M。",
    },
    "p2_rep_hv": {
        "page": "页② 高价值客户分析",
        "label": "高价值客户复购率",
        "formula": "复购率 = COUNT(F ≥ 2) ÷ COUNT(*) × 100%｜客户池 = CustomerTier「高价值客户」",
        "source": "Dim_Customer.CustomerTier（营销层级）+ Agg_Customer_RFM.F（累计订单数）",
        "calc_path": "① 取 CustomerTier=高价值客户 的客户 → ② 判断各自 F 是否 ≥2 → ③ 满足数 ÷ 该池客户数",
        "note": "客户级口径：F 为全时段累计订单数，不受年/月筛选影响；与页①RFM客群同名不同义（此处是营销层级 CustomerTier）。",
    },
    "p2_rep_other": {
        "page": "页② 高价值客户分析",
        "label": "非高价值客户复购率",
        "formula": "复购率 = COUNT(F ≥ 2) ÷ COUNT(*) × 100%｜客户池 = CustomerTier ≠「高价值客户」",
        "source": "Dim_Customer.CustomerTier + Agg_Customer_RFM.F",
        "calc_path": "① 取 CustomerTier≠高价值客户 的全部客户 → ② 判断 F≥2 → ③ 满足数 ÷ 该池客户数",
        "note": "作为高价值组的对照组，用于计算提升幅度；同为客户级全时段口径。",
    },
    "p2_rep_lift": {
        "page": "页② 高价值客户分析",
        "label": "复购率提升幅度",
        "formula": "提升幅度 = (高价值复购率 − 非高价值复购率) ÷ 非高价值复购率 × 100%",
        "source": "由本页前两个 KPI 直接推导，无独立数据源",
        "calc_path": "① 取两组复购率（百分比）→ ② 求差 → ③ 除以非高价值组 → ④ 保留 1 位小数并带正负号",
        "note": "相对提升（不是百分点差）；百分比基数为非高价值组。",
    },
    "p3_sleep_cnt": {
        "page": "页③ 沉睡客户唤醒",
        "label": "沉睡客户数",
        "formula": "沉睡客户数 = COUNT(*)｜客户池 = CustomerTier「沉睡客户」",
        "source": "Dim_Customer.CustomerTier（营销层级分层）",
        "calc_path": "① 在客户池中筛 CustomerTier=沉睡客户 → ② 统计客户行数（一客户一行）",
        "note": "客户级静态口径，随性别/年龄/城市筛选变化，不随年/月筛选变化；与页①RFM客群同名不同义。",
    },
    "p3_awake_cnt": {
        "page": "页③ 沉睡客户唤醒",
        "label": "唤醒客户数",
        "formula": "唤醒客户数 = Σ IsAwakened（True 计 1）",
        "source": "Agg_Customer_RFM.IsAwakened（布尔标记）",
        "calc_path": "① 取沉睡客户池 → ② 对 IsAwakened 求和 → ③ 得到被唤醒的客户人数",
        "note": "IsAwakened=True 表示该客户已被成功唤醒；唤醒订单笔数另按 Fact_Transaction.IsAwakenedOrder 统计。",
    },
    "p3_awake_rate": {
        "page": "页③ 沉睡客户唤醒",
        "label": "唤醒率",
        "formula": "唤醒率 = 唤醒客户数 ÷ 沉睡客户数 × 100%",
        "source": "Agg_Customer_RFM.IsAwakened + Dim_Customer.CustomerTier",
        "calc_path": "① 取两数（本页前两个 KPI）→ ② 相除 → ③ ×100 保留 1 位小数",
        "note": "客户级口径，分母为当前沉睡客户池；筛选客户池后分母随之变化。",
    },
    "p4_roi_all": {
        "page": "页④ 营销产出分析",
        "label": "整体ROI",
        "formula": "ROI = Σ Revenue ÷ Σ Cost（倍）",
        "source": "Fact_Marketing_Campaign.Revenue / Cost（按 Year 与 StrategyGroup 筛选）",
        "calc_path": "① 按年份（及策略组联动）过滤活动 → ② 分别对 Revenue、Cost 求和 → ③ 相除，保留 2 位小数",
        "note": "本数据集成本仅含活动投放费用，不含人力与固定成本，ROI 绝对值偏高，宜作策略间横向比较。",
    },
    "p4_roi_early": {
        "page": "页④ 营销产出分析",
        "label": "早期营销ROI",
        "formula": "ROI(早期) = Σ Revenue ÷ Σ Cost｜Stage 以「早期」开头的活动",
        "source": "Fact_Marketing_Campaign.Stage / Revenue / Cost",
        "calc_path": "① 在已筛选活动中取 Stage 前缀为「早期」… → ② 收入求和 ÷ 成本求和，保留 2 位小数；无数据展示「-」",
        "note": "早期/近期按 Stage 字段前缀划分，与年份筛选叠加生效。",
    },
    "p4_roi_recent": {
        "page": "页④ 营销产出分析",
        "label": "近期营销ROI",
        "formula": "ROI(近期) = Σ Revenue ÷ Σ Cost｜Stage 以「近期」开头的活动",
        "source": "Fact_Marketing_Campaign.Stage / Revenue / Cost",
        "calc_path": "① 取 Stage 前缀为「近期」… → ② 收入求和 ÷ 成本求和，保留 2 位小数；无数据展示「-」",
        "note": "与早期 ROI 同一成本口径，二者可比。",
    },
    "p4_roi_lift": {
        "page": "页④ 营销产出分析",
        "label": "ROI提升倍数",
        "formula": "提升倍数 = ROI(近期) ÷ ROI(早期)（倍）",
        "source": "由本页早期/近期 ROI 推导，无独立数据源",
        "calc_path": "① 取两个 ROI → ② 相除 → ③ 保留 2 位小数并加「x」；任一为 0 时展示「-」",
        "note": "倍数口径（>1 表示近期效率更高），不是百分比增幅。",
    },
}


SEG_METRIC_NOTE = (    "口径：复购率=窗口内有交易且下单≥2次客户/窗口内有交易客户；"
    "累计消费/人均M·F·平均R取该客群全量客户；月度趋势为全时段；"
    "金额单位万元，保留2位小数。"
)
MONTH_METRIC_NOTE = (
    "口径：笔单价=当月总销售额/订单数；唤醒占比=唤醒订单数/订单数；"
    "复购构成按当月订单次数划分；环比为较上一自然月，上一月无数据以「—」表示；"
    "金额单位万元，保留2位小数。"
)


@app.route("/api/segment_profile")
def segment_profile():
    """二级客群深挖：KPI + 画像 + 全时段趋势 + 品类偏好 + Top20 客户（叠加 year/month/category 联动）"""
    df, rfm, camp = load_data()
    seg = request.args.get("seg", "").strip()
    year = _int_arg("year")
    month = _int_arg("month")
    category = request.args.get("category", "").strip()

    note = {"type": "seg", "seg": seg, "metric_note": SEG_METRIC_NOTE}
    pool = rfm[rfm["RFM_Segment"] == seg]
    if not seg or len(pool) == 0:
        return jsonify({**note, "empty": True})

    keys = set(pool["CustomerKey"])
    # 窗口交易（用于复购率、累计消费、品类偏好；year/month/category 叠加过滤）
    wdf = df[df["CustomerKey"].isin(keys)]
    if year is not None:
        wdf = wdf[wdf["Year"] == year]
    if month is not None:
        wdf = wdf[wdf["Month"] == month]
    if category:
        wdf = wdf[wdf["Category"] == category]

    # 复购率：窗口内有交易且窗口内下单≥2次客户 / 窗口内有交易客户
    per_cust_orders = wdf.groupby("CustomerKey")["TransactionID"].count()
    active = int(len(per_cust_orders))
    repeat = int((per_cust_orders >= 2).sum())
    rep_rate = f2(repeat / active * 100) if active else 0.0

    total_sales = float(wdf["TotalAmount"].sum())
    kpi = [
        {"label": "客户数", "value": f"{len(pool):,}"},
        {"label": "累计消费(万)", "value": f"{total_sales/10000:,.1f}"},
        {"label": "人均M(元)", "value": f"{pool['M'].mean():,.0f}"},
        {"label": "人均F(次)", "value": f2(pool['F'].mean())},
        {"label": "平均R(天)", "value": f2(pool['R_days'].mean())},
        {"label": "复购率", "value": f"{rep_rate:.1f}%"},
    ]

    profile = {
        "gender": pie_list(pool["Gender"]),
        "age": pie_list(pool["AgeGroup"]),
        "city": pie_list(pool["CityLevel"]),
    }

    # 全时段月度销售（补齐到全局数据范围，前端裁前导 0；上传新月份后自动延伸）
    tm = df[df["CustomerKey"].isin(keys)]
    if category:
        tm = tm[tm["Category"] == category]
    monthly = full_months(tm.groupby("YearMonth")["TotalAmount"].sum(),
                          df["YearMonth"].min(), df["YearMonth"].max())
    trend = [{"ym": str(i), "sales": f2(monthly.loc[i] / 10000)} for i in monthly.index]

    # 品类偏好 Top3（占该客群窗口总销售百分比）
    cat = wdf.groupby("Category")["TotalAmount"].sum().sort_values(ascending=False).head(3)
    denom = total_sales if total_sales else 1.0
    category_pref = [{"name": str(k), "sales": f2(v / 10000),
                      "pct": f2(v / denom * 100)} for k, v in cat.items()]

    # Top20 客户（默认按 M 降序，前端可切换 F/最近购买）
    top = pool.nlargest(20, "M")
    top_customers = [{
        "key": int(r["CustomerKey"]), "f": int(r["F"]), "m": f2(r["M"]),
        "r": int(r["R_days"]), "score": int(r["RFM_Score"]),
        "gender": str(r["Gender"]), "city": str(r["CityLevel"]),
        "last_month": str(r["LastOrderDate"])[:7], "awaken": bool(r["IsAwakened"]),
    } for _, r in top.iterrows()]

    if active == 0 and total_sales == 0:
        return jsonify({**note, "empty": True})

    return jsonify({**note, "kpi": kpi, "profile": profile, "trend": trend,
                    "category_pref": category_pref, "top_customers": top_customers})


@app.route("/api/month_profile")
def month_profile():
    """二级月度深挖：KPI + 环比 + 复购构成 + 品类全量 + Top10 商品/客户（叠加 seg/category 联动）"""
    df, rfm, camp = load_data()
    ym = request.args.get("ym", "").strip()
    seg = request.args.get("seg", "").strip()
    category = request.args.get("category", "").strip()

    note = {"type": "month", "ym": ym, "metric_note": MONTH_METRIC_NOTE}
    if not ym:
        return jsonify({**note, "error": "缺少 ym 参数"}), 400

    def month_slice(target_ym):
        d = df[df["YearMonth"] == target_ym]
        if seg:
            keys = set(rfm[rfm["RFM_Segment"] == seg]["CustomerKey"])
            d = d[d["CustomerKey"].isin(keys)]
        if category:
            d = d[d["Category"] == category]
        return d

    dff = month_slice(ym)
    if len(dff) == 0:
        return jsonify({**note, "empty": True})

    sales = float(dff["TotalAmount"].sum())
    order_cnt = int(len(dff))
    cust_cnt = int(dff["CustomerKey"].nunique())
    aov = f2(dff["TotalAmount"].mean())
    awake_cnt = int(dff["IsAwakenedOrder"].sum())
    awake_pct = f2(awake_cnt / order_cnt * 100) if order_cnt else 0.0
    kpi = [
        {"label": "销售金额(万)", "value": f"{sales/10000:,.1f}"},
        {"label": "订单数", "value": f"{order_cnt:,}"},
        {"label": "购买客户数", "value": f"{cust_cnt:,}"},
        {"label": "笔单价(元)", "value": f"{aov:,.0f}"},
        {"label": "唤醒订单数", "value": f"{awake_cnt:,}"},
        {"label": "唤醒占比", "value": f"{awake_pct:.1f}%"},
    ]

    # 环比上一月（上一月无数据返回 None -> 前端显示「—」）
    prev = month_slice(_prev_ym(ym)) if _prev_ym(ym) else df.iloc[0:0]
    prev_sales = float(prev["TotalAmount"].sum()) if len(prev) else 0.0
    prev_orders = int(len(prev))
    mom_sales = f2((sales - prev_sales) / prev_sales * 100) if prev_sales else None
    mom_orders = f2((order_cnt - prev_orders) / prev_orders * 100) if prev_orders else None
    mom = {"sales": mom_sales, "orders": mom_orders, "prev_ym": _prev_ym(ym)}

    # 复购构成：当月下单 1 次 vs ≥2 次客户
    per_cust = dff.groupby("CustomerKey")["TransactionID"].count()
    repurchase = {"once": int((per_cust == 1).sum()), "multi": int((per_cust >= 2).sum())}

    # 品类全量环形
    cat = dff.groupby("Category")["TotalAmount"].sum().sort_values(ascending=False)
    category_pie = [{"name": str(k), "value": f2(v / 10000)} for k, v in cat.items()]

    # Top10 商品
    prod = dff.groupby("ProductName").agg(
        sales=("TotalAmount", "sum"), qty=("Quantity", "sum"),
        category=("Category", "first"))
    prod = prod.sort_values("sales", ascending=False).head(10).reset_index()
    top_product = [{"name": r["ProductName"], "category": r["category"],
                    "qty": int(r["qty"]), "sales": f2(r["sales"] / 10000),
                    "pct": f2(r["sales"] / sales * 100)} for _, r in prod.iterrows()]

    # Top10 消费客户
    custg = dff.groupby("CustomerKey").agg(
        amount=("TotalAmount", "sum"), order_cnt=("TransactionID", "count"))
    seg_map = rfm.set_index("CustomerKey")["RFM_Segment"]
    custg = custg.sort_values("amount", ascending=False).head(10).reset_index()
    top_customers = [{"key": int(r["CustomerKey"]),
                      "seg": str(seg_map.get(r["CustomerKey"], "未分层")),
                      "order_cnt": int(r["order_cnt"]),
                      "amount": f2(r["amount"] / 10000)} for _, r in custg.iterrows()]

    return jsonify({**note, "kpi": kpi, "mom": mom, "repurchase": repurchase,
                    "category_pie": category_pie, "top_product": top_product,
                    "top_customers": top_customers})


# ---------------- 经营月报自动生成（模板叙事，无 LLM） ----------------
MONTHLY_REPORT_NOTE = (
    "口径：GMV=当月成交销售额（与页面「销售金额」同口径，单位万元保留1位小数）；"
    "环比=较上一自然月；购买客户数=当月有交易的独立客户；笔单价=当月销售额/订单数；"
    "客群/品类贡献按当月销售额汇总；营销ROI=当月活动收入/成本；"
    "唤醒订单=IsAwakenedOrder=True 的交易，唤醒客户=IsAwakened=True 且当月有交易的客户。"
)


def build_monthly_report(dff, camp, ym, prev=None):
    """将当月真实聚合数据填入固定模板，生成 150-200 字中文月报正文。
    数字与页面同口径：GMV=TotalAmount 求和（万）、笔单价=TotalAmount 均值、
    客群/品类贡献按销售额降序取第一；营销活动按 StartDate 所在月归集；
    prev 为上一月交易切片（用于环比，可为空）。返回 (text, figures)。"""
    y, m = int(ym[:4]), int(ym[5:7])
    gmv = float(dff["TotalAmount"].sum())
    order_cnt = int(len(dff))
    cust_cnt = int(dff["CustomerKey"].nunique())
    aov = float(dff["TotalAmount"].mean()) if order_cnt else 0.0

    # 环比上一自然月（上一月无数据如实说明，不硬凑）
    prev_gmv = float(prev["TotalAmount"].sum()) if prev is not None and len(prev) else 0.0
    prev_orders = int(len(prev)) if prev is not None else 0
    mom = f2((gmv - prev_gmv) / prev_gmv * 100.0) if prev_gmv else None
    order_mom = f2((order_cnt - prev_orders) / prev_orders * 100.0) if prev_orders else None

    # 客群贡献最大（销售额口径，与页1客群结构一致）
    seg = dff.groupby("RFM_Segment")["TotalAmount"].sum().sort_values(ascending=False)
    top_seg = str(seg.index[0])
    top_seg_sales = float(seg.iloc[0])
    top_seg_pct = top_seg_sales / gmv * 100.0 if gmv else 0.0

    # 品类贡献最大
    cat = dff.groupby("Category")["TotalAmount"].sum().sort_values(ascending=False)
    top_cat = str(cat.index[0])
    top_cat_sales = float(cat.iloc[0])
    top_cat_pct = top_cat_sales / gmv * 100.0 if gmv else 0.0

    # 当月营销活动（StartDate 所在月；无活动则如实说明，不硬凑）
    campm = camp[camp["StartMonth"] == ym]
    camp_cnt = int(len(campm))
    camp_cost = float(campm["Cost"].sum())
    camp_rev = float(campm["Revenue"].sum())
    roi = camp_rev / camp_cost if camp_cost else None

    # 沉睡唤醒（当月唤醒订单 + 唤醒客户；均按当月交易统计）
    awake_orders = int(dff["IsAwakenedOrder"].sum())
    awake_cust = int(dff.groupby("CustomerKey")["IsAwakened"].max().sum())
    awake_pct = awake_orders / order_cnt * 100.0 if order_cnt else 0.0

    figures = {
        "gmv": f2(gmv / 10000), "prev_gmv": f2(prev_gmv / 10000) if prev_gmv else None,
        "mom": mom, "order_cnt": order_cnt, "order_mom": order_mom,
        "cust_cnt": cust_cnt, "aov": f2(aov),
        "top_seg": top_seg, "top_seg_sales": f2(top_seg_sales / 10000),
        "top_seg_pct": f2(top_seg_pct),
        "top_cat": top_cat, "top_cat_sales": f2(top_cat_sales / 10000),
        "top_cat_pct": f2(top_cat_pct),
        "camp_cnt": camp_cnt, "camp_cost": f2(camp_cost / 10000),
        "camp_rev": f2(camp_rev / 10000), "roi": f2(roi) if roi is not None else None,
        "awake_orders": awake_orders, "awake_cust": awake_cust,
        "awake_pct": f2(awake_pct),
    }

    def _pct(x):
        return ("+" if x >= 0 else "") + f"{x:,.1f}%"

    mom_txt = _pct(mom) if mom is not None else "上月无数据"
    omom_txt = _pct(order_mom) if order_mom is not None else "上月无数据"
    if camp_cnt:
        roi_txt = f"{figures['roi']:.2f}" if figures["roi"] is not None else "—"
        camp_txt = (f"投放{camp_cnt}场活动，投入{figures['camp_cost']:,.1f}万元、"
                    f"产出{figures['camp_rev']:,.1f}万元（ROI {roi_txt}）。")
    else:
        camp_txt = "当月无营销活动投放。"
    if awake_orders or awake_cust:
        awake_txt = f"唤醒{awake_orders}笔订单、{awake_cust}名客户。"
    else:
        awake_txt = "当月无沉睡客户唤醒。"

    text = (
        f"{y}年{m}月经营概况：GMV{figures['gmv']:,.1f}万元（环比{mom_txt}），"
        f"购买客户{cust_cnt:,}人、成交{order_cnt:,}笔、笔单价{aov:,.0f}元。"
        f"{top_seg}客群贡献最大（{figures['top_seg_sales']:,.1f}万元，占{figures['top_seg_pct']:,.1f}%）；"
        f"{top_cat}为第一大品类（{figures['top_cat_sales']:,.1f}万元，占{figures['top_cat_pct']:,.1f}%）。"
        f"营销方面，{camp_txt}唤醒方面，{awake_txt}"
        f"订单环比{omom_txt}，以上均为当月真实交易聚合。"
    )
    return text, figures


@app.route("/api/monthly_report")
def monthly_report():
    """经营月报：?ym=YYYY-MM。固定模板 + 当月真实聚合，末尾附口径说明；无数据月返回空态"""
    df, rfm, camp = load_data()
    ym = request.args.get("ym", "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}", ym):
        return jsonify({"ym": ym, "error": "ym 参数格式应为 YYYY-MM"}), 400

    dff = df[df["YearMonth"] == ym]
    if len(dff) == 0:
        return jsonify({"ym": ym, "empty": True, "reason": "该月无交易数据",
                        "metric_note": MONTHLY_REPORT_NOTE})

    prev_ym = _prev_ym(ym)
    prev = df[df["YearMonth"] == prev_ym] if prev_ym else df.iloc[0:0]
    if "StartMonth" not in camp.columns:
        camp = camp.copy()
        camp["StartMonth"] = pd.to_datetime(camp["StartDate"]).dt.to_period("M").astype(str)

    text, figures = build_monthly_report(dff, camp, ym, prev=prev)
    return jsonify({"ym": ym, "text": text, "metric_note": MONTHLY_REPORT_NOTE,
                    "figures": figures})


# ---------------- 客群名单导出（分析 → 行动闭环） ----------------
# 建议动作按 RFM 客群映射；未收录客群统一给「促销转化」兜底
SEG_ACTIONS = {
    "高价值客户": "会员深耕：专属权益 + 新品优先体验 + 定期回访",
    "潜力客户": "复购激励：组合券 + 第二件优惠 + 复购提醒",
    "沉睡客户": "唤醒礼包：大额召回券 + 爆款推荐 + 限时唤醒活动",
    "大众客户": "促销转化：满减促销 + 新客礼包 + 品类试用",
}
EXPORT_ROW_CAP = 5000   # 名单导出上限（超过返回 400 提示缩小范围）
EXPORT_NOTE = (
    "口径：名单为当前筛选条件下的客户池（设置了年份/月份/品类时，仅含窗口内有交易的客户，"
    "与页1客群结构人数一致）；R/F/M/RFM评分取 Agg_Customer_RFM 汇总表，"
    "累计消费/最近购买按事实表全时段聚合；推荐品类=该客群销售额 Top1 品类（全时段）；"
    "上限 5000 行。"
)


def _seg_top_category(df):
    """各客群销售额 Top1 品类（全时段真实聚合）-> {客群: 品类}"""
    g = df.groupby(["RFM_Segment", "Category"])["TotalAmount"].sum().reset_index()
    g = g.sort_values("TotalAmount", ascending=False).drop_duplicates("RFM_Segment")
    return dict(zip(g["RFM_Segment"], g["Category"]))


@app.route("/api/export/segment")
def export_segment():
    """客群名单导出 CSV：接收与 /api/dashboard 相同的筛选参数（tier/gender/age/city/
    seg/category/year/month），客户池口径与页面客群结构一致；UTF-8 BOM 防 Excel 乱码。
    行数超 EXPORT_ROW_CAP 返回 400 提示缩小范围。"""
    df, rfm, camp = load_data()
    tier = request.args.get("tier", "").strip()
    gender = request.args.get("gender", "").strip()
    age = request.args.get("age", "").strip()
    city = request.args.get("city", "").strip()
    seg = request.args.get("seg", "").strip()
    category = request.args.get("category", "").strip()
    year = _int_arg("year")
    month = _int_arg("month")

    # 客户池筛选（与 /api/dashboard 完全一致的口径）
    pool = rfm
    if gender:
        pool = pool[pool["Gender"] == gender]
    if age:
        pool = pool[pool["AgeGroup"] == age]
    if city:
        pool = pool[pool["CityLevel"] == city]
    if seg:
        pool = pool[pool["RFM_Segment"] == seg]
    pool1 = pool if not tier else pool[pool["RFM_Segment"] == tier]

    # 窗口口径：设置年份/月份/品类时，名单收敛为窗口内活跃客户（与页面显示人数一致）
    if year is not None or month is not None or category:
        keys_pool = set(pool1["CustomerKey"])
        wdf = df[df["CustomerKey"].isin(keys_pool)]
        if year is not None:
            wdf = wdf[wdf["Year"] == year]
        if month is not None:
            wdf = wdf[wdf["Month"] == month]
        if category:
            wdf = wdf[wdf["Category"] == category]
        pool1 = pool1[pool1["CustomerKey"].isin(set(wdf["CustomerKey"].dropna().unique()))]

    if len(pool1) == 0:
        return jsonify({"error": "当前筛选条件下没有客户可导出，请放宽筛选条件",
                        "metric_note": EXPORT_NOTE}), 400
    if len(pool1) > EXPORT_ROW_CAP:
        return jsonify({"error": f"当前筛选共 {len(pool1):,} 名客户，超过单次导出上限 "
                        f"{EXPORT_ROW_CAP:,} 行，请按客群/性别/城市等缩小范围后再导出",
                        "metric_note": EXPORT_NOTE}), 400

    # 客户ID：优先 Dim_Customer 注册 ID；上传衍生的新客户回退 CUST-{key}
    cust_map = pd.read_csv(PA("Dim_Customer.csv")).set_index("CustomerKey")["CustomerID"]
    # 累计消费/最近购买月份：事实表全时段聚合
    agg = df.groupby("CustomerKey").agg(total=("TotalAmount", "sum"),
                                        last_ym=("YearMonth", "max"))
    top_cat = _seg_top_category(df)

    rows = []
    for _, r in pool1.iterrows():
        k = r["CustomerKey"]
        seg_name = str(r["RFM_Segment"])
        rows.append([
            str(cust_map.get(k, f"CUST-{int(k)}")),
            seg_name,
            int(r["R_days"]),
            int(r["F"]),
            f2(r["M"]),
            int(r["RFM_Score"]),
            f2(agg["total"].get(k, 0.0)),
            str(agg["last_ym"].get(k, "")),
            str(top_cat.get(seg_name, "")),
            SEG_ACTIONS.get(seg_name, "促销转化：满减促销 + 品类试用"),
        ])

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["客户ID", "客群", "R天(近度)", "F次(频次)", "M金额(元)", "RFM评分",
                "累计消费(元)", "最近购买月份", "推荐品类", "建议动作"])
    w.writerows(rows)

    scope = seg or tier or "全量"
    fname = f"客群名单_{scope}_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    return Response(
        buf.getvalue().encode("utf-8-sig"),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f"attachment; filename=\"segment_export.csv\"; "
                 f"filename*=UTF-8''{quote(fname)}",
                 "X-Export-Rows": str(len(rows)),
                 "X-Export-Note": EXPORT_NOTE})


# ---------------- 自定义数据上传接口 ----------------
@app.route("/api/upload", methods=["POST"])
def upload_custom():
    """上传 CSV/Excel 文件（multipart file）或粘贴文本（JSON {text}），合并入星型模型并失效缓存"""
    try:
        filename = ""
        if request.files and "file" in request.files:
            f = request.files["file"]
            data = f.read()
            filename = f.filename or ""
            raw = parse_tabular(data, filename)
        else:
            body = request.get_json(silent=True) or {}
            text = body.get("text", "").strip()
            if not text:
                return jsonify({"error": "请拖入文件或粘贴表格数据",
                                "metric_note": CUSTOM_METRIC_NOTE}), 400
            raw = parse_tabular(text.encode("utf-8"), filename=body.get("name", ""))

        ext, report, batch_id = build_extension(raw)
        ensure_batch_dir()
        ext.to_csv(os.path.join(BATCH_DIR, batch_id + ".csv"), index=False)
        # 保留原始上传行（用于明细表脏数据着色展示）
        try:
            raw.dropna(how="all").to_csv(os.path.join(BATCH_DIR, batch_id + ".orig.csv"),
                                          index=False, encoding="utf-8-sig")
        except Exception:
            pass  # 列名重复等极端情况无法保存原始行时，明细表退回扩展行展示
        meta = {"name": filename or "粘贴文本",
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "rows": int(len(ext)), "mapping": report}
        with open(os.path.join(BATCH_DIR, batch_id + ".json"), "w", encoding="utf-8") as fp:
            json.dump(meta, fp, ensure_ascii=False, indent=2)
        invalidate_cache()
        return jsonify({"ok": True,
                        "batch": {"id": batch_id, "name": meta["name"],
                                  "rows": meta["rows"], "time": meta["time"]},
                        "mapping": report, "metric_note": CUSTOM_METRIC_NOTE})
    except ValueError as e:
        return jsonify({"error": str(e), "metric_note": CUSTOM_METRIC_NOTE}), 400
    except Exception as e:
        return jsonify({"error": f"解析失败: {e}", "metric_note": CUSTOM_METRIC_NOTE}), 400

@app.route("/api/upload/preview", methods=["POST"])
def upload_preview():
    """上传解析+清洗预览（不并入大屏）：返回脏/正常数据明细与清洗统计，暂存 pending 等待确认"""
    try:
        filename = ""
        if request.files and "file" in request.files:
            f = request.files["file"]
            data = f.read()
            filename = f.filename or ""
            raw = parse_tabular(data, filename)
        else:
            body = request.get_json(silent=True) or {}
            text = body.get("text", "").strip()
            if not text:
                return jsonify({"error": "请拖入文件或粘贴表格数据",
                                "metric_note": CUSTOM_METRIC_NOTE}), 400
            raw = parse_tabular(text.encode("utf-8"), filename=body.get("name", ""))
            filename = body.get("name", "") or "粘贴文本"

        ext, report, batch_id = build_extension(raw)
        orig = raw.dropna(how="all")

        # 暂存 pending：用 JSON 序列化（DataFrame 内嵌 to_json 字符串），
        # 不用 pickle——pickle.load 可执行任意代码，一旦服务暴露即形成反序列化漏洞。
        # 多 pending 并存：并发上传时每个预览写入自己的 pending_id 文件，
        # 不做整目录清空（rmtree 会误删其他请求刚写入的暂存）；确认/放弃后删除自身。
        os.makedirs(PENDING_DIR, exist_ok=True)
        pending_id = datetime.now().strftime("%Y%m%d%H%M%S%f")
        pending = {
            "name": filename,
            "report": report,
            "ext": ext.to_json(orient="split", date_format="iso", force_ascii=False),
            "orig": orig.to_json(orient="split", date_format="iso", force_ascii=False),
        }
        with open(os.path.join(PENDING_DIR, pending_id + ".json"), "w", encoding="utf-8") as fp:
            json.dump(pending, fp, ensure_ascii=False)

        rows_out = classify_dirty(orig, report)
        dirty_summary = {"total": len(rows_out), "dropped": 0, "fixed": 0}
        for row in rows_out:
            if row["row_status"] == "dropped":
                dirty_summary["dropped"] += 1
            elif row["row_status"] == "fixed":
                dirty_summary["fixed"] += 1
        stats = [
            {"label": "清洗后数据行", "value": f"{len(ext):,}"},
            {"label": "正常数据行", "value": f"{dirty_summary['total'] - dirty_summary['dropped'] - dirty_summary['fixed']:,}"},
            {"label": "脏数据丢弃行", "value": f"{dirty_summary['dropped']:,}"},
            {"label": "已修正异常行", "value": f"{dirty_summary['fixed']:,}"},
            {"label": "清洗后金额(万)", "value": f"{float(ext['TotalAmount'].sum())/10000:,.1f}"},
        ]
        return jsonify({"ok": True, "pending_id": pending_id, "name": filename,
                        "stats": stats, "mapping": report,
                        "dirty_summary": dirty_summary,
                        "table": rows_out[:100], "columns": [str(c) for c in orig.columns],
                        "metric_note": CUSTOM_METRIC_NOTE})
    except ValueError as e:
        return jsonify({"error": str(e), "metric_note": CUSTOM_METRIC_NOTE}), 400
    except Exception as e:
        return jsonify({"error": f"解析失败: {e}", "metric_note": CUSTOM_METRIC_NOTE}), 400


@app.route("/api/upload/confirm", methods=["POST"])
def upload_confirm():
    """确认预览：commit=true 将清洗后数据集并入大屏（写入批次）；否则丢弃暂存"""
    body = request.get_json(silent=True) or {}
    pid = _safe_batch_id(body.get("pending_id"))
    pjson = os.path.join(PENDING_DIR, pid + ".json")
    if not pid or not os.path.exists(pjson):
        return jsonify({"error": "预览数据不存在或已过期，请重新上传",
                        "metric_note": CUSTOM_METRIC_NOTE}), 404
    try:
        if not body.get("commit", False):
            os.remove(pjson)
            return jsonify({"ok": True, "committed": False, "metric_note": CUSTOM_METRIC_NOTE})
        with open(pjson, "r", encoding="utf-8") as fp:
            saved = json.load(fp)
        ext = pd.read_json(io.StringIO(saved["ext"]), orient="split")
        orig = pd.read_json(io.StringIO(saved["orig"]), orient="split")
        report, name = saved["report"], saved["name"]
        ensure_batch_dir()
        # 原子写：先写临时文件再重命名，避免并发请求读到半写批次
        # （preview 的 build_extension 会并发读取 batches 目录）
        tmp_csv = os.path.join(BATCH_DIR, pid + ".tmp.csv")
        ext.to_csv(tmp_csv, index=False)
        os.replace(tmp_csv, os.path.join(BATCH_DIR, pid + ".csv"))
        try:
            tmp_orig = os.path.join(BATCH_DIR, pid + ".tmp.orig.csv")
            orig.to_csv(tmp_orig, index=False, encoding="utf-8-sig")
            os.replace(tmp_orig, os.path.join(BATCH_DIR, pid + ".orig.csv"))
        except Exception:
            pass  # 列名重复等极端情况无法保存原始行时，明细表退回扩展行展示
        meta = {"name": name or "上传数据",
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "rows": int(len(ext)), "mapping": report}
        tmp_meta = os.path.join(BATCH_DIR, pid + ".tmp.json")
        with open(tmp_meta, "w", encoding="utf-8") as fp:
            json.dump(meta, fp, ensure_ascii=False, indent=2)
        os.replace(tmp_meta, os.path.join(BATCH_DIR, pid + ".json"))
        os.remove(pjson)
        invalidate_cache()
        return jsonify({"ok": True, "committed": True,
                        "batch": {"id": pid, "name": meta["name"],
                                  "rows": meta["rows"], "time": meta["time"]},
                        "metric_note": CUSTOM_METRIC_NOTE})
    except Exception as e:
        return jsonify({"error": f"导入失败: {e}", "metric_note": CUSTOM_METRIC_NOTE}), 500


# ---------------- 数据清洗接口（三色分流：绿=通过 / 黄=转换确认区 / 红=剔除候选） ----------------
# 红线：黄色(可转换/需确认)永不自动删除；只有确认后的干净数据才写入大屏批次目录；
# 所有转换/剔除均有 SQLite 日志，可追溯、可回滚。规则配置见 rules_config.json。

def _clean_load_pending(pid):
    """读取清洗预览暂存 -> (raw_df, scan)；不存在返回 (None, None)"""
    pid = _safe_batch_id(pid)
    pjson = os.path.join(PENDING_DIR, pid + ".json")
    if not pid or not os.path.exists(pjson):
        return None, None
    with open(pjson, "r", encoding="utf-8") as fp:
        payload = json.load(fp)
    raw = pd.read_json(io.StringIO(payload["raw"]), orient="split")
    scan = _clean_mod.ScanResult.from_dict(payload["scan"])
    return raw, scan


def _clean_save_pending(pid, raw, scan, name):
    """暂存清洗预览（原始表 + 扫描结果；确认/放弃后删除自身，多 pending 并存）"""
    pid = _safe_batch_id(pid)
    if not pid:
        raise ValueError("非法的暂存 ID")
    os.makedirs(PENDING_DIR, exist_ok=True)
    with open(os.path.join(PENDING_DIR, pid + ".json"), "w", encoding="utf-8") as fp:
        json.dump({"pid": pid, "name": name,
                   "raw": raw.to_json(orient="split", date_format="iso", force_ascii=False),
                   "scan": scan.to_dict()}, fp, ensure_ascii=False)


def _clean_engine_guard():
    """清洗引擎未就绪时统一返回 503（不影响大屏其他功能）"""
    if _CLEAN_ENGINE is None:
        return jsonify({"error": "清洗引擎未加载（rules_config.json 缺失或损坏），请恢复配置文件后重启服务",
                        "metric_note": CUSTOM_METRIC_NOTE}), 503
    return None


@app.route("/api/clean/preview", methods=["POST"])
def clean_preview():
    """上传解析 + 规则扫描（不并入大屏）：三色统计卡片 + 黄区对照表 + 红区剔除清单，暂存 pending"""
    guard = _clean_engine_guard()
    if guard:
        return guard
    try:
        filename = ""
        if request.files and "file" in request.files:
            f = request.files["file"]
            data = f.read()
            filename = f.filename or ""
            raw = parse_tabular(data, filename)
        else:
            body = request.get_json(silent=True) or {}
            text = body.get("text", "").strip()
            if not text:
                return jsonify({"error": "请拖入文件或粘贴表格数据",
                                "metric_note": CUSTOM_METRIC_NOTE}), 400
            raw = parse_tabular(text.encode("utf-8"), filename=body.get("name", ""))
            filename = body.get("name", "") or "粘贴文本"
        raw = raw.dropna(how="all").reset_index(drop=True)
        if raw.empty:
            return jsonify({"error": "上传内容为空，未解析到任何数据行",
                            "metric_note": CUSTOM_METRIC_NOTE}), 400
        scan = _CLEAN_ENGINE.scan(raw, filename)
        pid = "C" + datetime.now().strftime("%Y%m%d%H%M%S%f")
        _clean_save_pending(pid, raw, scan, filename)
        s = scan.stats
        stats = [
            {"label": "数据总行", "value": f"{s['total']:,}", "level": "total"},
            {"label": "绿色·格式正常", "value": f"{s['green']:,}", "level": "green"},
            {"label": "黄色·待转换确认", "value": f"{s['yellow']:,}", "level": "yellow"},
            {"label": "红色·剔除候选", "value": f"{s['red']:,}", "level": "red"},
        ]
        yt, rt = scan.yellow_table(), scan.red_table()
        return jsonify({"ok": True, "pending_id": pid, "name": filename,
                        "stats": stats, "columns": scan.columns,
                        "yellow_table": yt[:1000], "red_table": rt[:1000],
                        "yellow_more": len(yt) > 1000, "red_more": len(rt) > 1000,
                        "rules_version": scan.rules_version,
                        "metric_note": CUSTOM_METRIC_NOTE})
    except ValueError as e:
        return jsonify({"error": str(e), "metric_note": CUSTOM_METRIC_NOTE}), 400
    except Exception as e:
        return jsonify({"error": f"扫描失败: {e}", "metric_note": CUSTOM_METRIC_NOTE}), 400


@app.route("/api/clean/transform", methods=["POST"])
def clean_transform():
    """黄区转换确认：批量确认(confirm_all) / 单条修改(standard) / 标记无法解析(unparseable)。
    黄色数据只会被转换或被人工标记，绝不自动删除。"""
    guard = _clean_engine_guard()
    if guard:
        return guard
    body = request.get_json(silent=True) or {}
    pid = _safe_batch_id(body.get("pending_id"))
    raw, scan = _clean_load_pending(pid)
    if scan is None:
        return jsonify({"error": "预览数据不存在或已过期，请重新上传",
                        "metric_note": CUSTOM_METRIC_NOTE}), 404
    try:
        errs = scan.apply_transform(body.get("items") or [],
                                    confirm_all=bool(body.get("confirm_all")))
        _clean_save_pending(pid, raw, scan, scan.name)
        return jsonify({"ok": True, "errors": errs,
                        "pending_yellow": scan.pending_yellow_count(),
                        "stats": scan.stats, "metric_note": CUSTOM_METRIC_NOTE})
    except Exception as e:
        return jsonify({"error": f"转换确认失败: {e}", "metric_note": CUSTOM_METRIC_NOTE}), 500


@app.route("/api/clean/remove", methods=["POST"])
def clean_remove():
    """红色剔除确认 + 影响评估（剔除行数/占比/涉及GMV）。剔除比例 >5% 返回 warn 提示回查规则。"""
    guard = _clean_engine_guard()
    if guard:
        return guard
    body = request.get_json(silent=True) or {}
    pid = _safe_batch_id(body.get("pending_id"))
    raw, scan = _clean_load_pending(pid)
    if scan is None:
        return jsonify({"error": "预览数据不存在或已过期，请重新上传",
                        "metric_note": CUSTOM_METRIC_NOTE}), 404
    try:
        impact = scan.removal_impact(_CLEAN_ENGINE.cfg["thresholds"]["removal_ratio_warn"],
                                     _CLEAN_ENGINE.cfg["thresholds"]["removal_ratio_warn_msg"])
        if body.get("confirm"):
            scan.red_confirmed = True
            _clean_save_pending(pid, raw, scan, scan.name)
        return jsonify({"ok": True, "impact": impact,
                        "confirmed": scan.red_confirmed,
                        "metric_note": CUSTOM_METRIC_NOTE})
    except Exception as e:
        return jsonify({"error": f"剔除确认失败: {e}", "metric_note": CUSTOM_METRIC_NOTE}), 500


@app.route("/api/clean/generate", methods=["POST"])
def clean_generate():
    """生成干净数据：校验黄区全部处理、红区已确认 -> 写 SQLite（批次+日志）->
    干净批次写入大屏目录 -> 大屏立即可见。只有确认后的干净数据才进入大屏。"""
    guard = _clean_engine_guard()
    if guard:
        return guard
    body = request.get_json(silent=True) or {}
    pid = _safe_batch_id(body.get("pending_id"))
    pjson = os.path.join(PENDING_DIR, pid + ".json")
    raw, scan = _clean_load_pending(pid)
    if scan is None:
        return jsonify({"error": "预览数据不存在或已过期，请重新上传",
                        "metric_note": CUSTOM_METRIC_NOTE}), 404
    try:
        clean, removed = scan.build_clean_df(raw)   # 黄区未处理 / 红区未确认时抛 ValueError
        if len(clean) == 0:
            raise ValueError("清洗后无可用数据行：请检查剔除清单或返回转换确认区处理黄色数据")
        ext, report, _ = build_extension(clean, allow_negative=True)
        batch_id = pid
        ensure_batch_dir()
        # 原子写批次（大屏 load_extension 只读 .csv 批次，天然只含确认后的干净数据）
        tmp_csv = os.path.join(BATCH_DIR, batch_id + ".tmp.csv")
        ext.to_csv(tmp_csv, index=False)
        os.replace(tmp_csv, os.path.join(BATCH_DIR, batch_id + ".csv"))
        try:
            tmp_orig = os.path.join(BATCH_DIR, batch_id + ".tmp.orig.csv")
            clean.to_csv(tmp_orig, index=False, encoding="utf-8-sig")
            os.replace(tmp_orig, os.path.join(BATCH_DIR, batch_id + ".orig.csv"))
        except Exception:
            pass
        # ---- 日志与健康度（SQLite）----
        removed_rows = len(removed)
        removed_ratio = removed_rows / scan.stats["total"] if scan.stats["total"] else 0.0
        removed_gmv = sum(g[2] for g in removed)
        transform_cnt = manual_cnt = 0
        store = _CLEAN_ENGINE.store
        ver = scan.rules_version
        amt_col = scan.mapping.get("amount")
        for r in scan.rows:
            for y in r["yellows"]:
                if not y["resolved"]:
                    continue
                if y["resolved"]["action"] == "confirm":
                    std = y["resolved"]["standard"]
                    if str(std) != str(y["original"]):
                        action, transform_cnt = "transform", transform_cnt + 1
                    else:
                        action, manual_cnt = "keep_manual", manual_cnt + 1
                    store.add_log(batch_id, y["rule"], y["field"], y["original"],
                                  str(std), action, y["note"], 1, ver)
                else:
                    store.add_log(batch_id, y["rule"], y["field"], y["original"], None,
                                  "mark_unparseable", y["note"], 1, ver)
        # 红区剔除：按原因聚合记日志
        reason_cnt = {}
        for _idx, reason, _gmv in removed:
            reason_cnt[reason] = reason_cnt.get(reason, 0) + 1
        for reason, n in reason_cnt.items():
            store.add_log(batch_id, "剔除规则", amt_col or "", None, None,
                          "remove", reason, n, ver)
        # 绿区直接通过：聚合记一条
        if scan.stats["green"] > 0:
            store.add_log(batch_id, "格式校验", amt_col or "", None, None,
                          "pass", f"格式正常，直接通过 {scan.stats['green']} 行",
                          scan.stats["green"], ver)
        store.commit_batch(batch_id, scan.name, scan.stats, transform_cnt, manual_cnt,
                           removed_rows, round(removed_ratio, 4), round(removed_gmv, 2), ver)
        # ---- 批次 meta（含清洗摘要，供大屏明细/健康度展示）----
        meta = {"name": scan.name, "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "rows": int(len(ext)), "mapping": report, "clean": True,
                "health": {"format_normalized": transform_cnt,
                           "manual_confirmed": manual_cnt,
                           "removed": removed_rows},
                "removed_ratio": round(removed_ratio, 4),
                "removed_gmv": round(removed_gmv, 2),
                "rules_version": ver}
        tmp_meta = os.path.join(BATCH_DIR, batch_id + ".tmp.json")
        with open(tmp_meta, "w", encoding="utf-8") as fp:
            json.dump(meta, fp, ensure_ascii=False, indent=2)
        os.replace(tmp_meta, os.path.join(BATCH_DIR, batch_id + ".json"))
        os.remove(pjson)
        invalidate_cache()
        return jsonify({"ok": True, "committed": True,
                        "batch": {"id": batch_id, "name": meta["name"],
                                  "rows": meta["rows"], "time": meta["time"]},
                        "health": meta["health"],
                        "impact": {"rows": removed_rows,
                                   "ratio": round(removed_ratio, 4),
                                   "gmv": round(removed_gmv, 2)},
                        "metric_note": CUSTOM_METRIC_NOTE})
    except ValueError as e:
        return jsonify({"error": str(e), "metric_note": CUSTOM_METRIC_NOTE}), 400
    except Exception as e:
        return jsonify({"error": f"生成干净数据失败: {e}", "metric_note": CUSTOM_METRIC_NOTE}), 500


@app.route("/api/clean/rollback", methods=["POST"])
def clean_rollback():
    """批次回滚：从大屏数据源移除该干净批次 + SQLite 标记 rolled_back + 回滚日志"""
    guard = _clean_engine_guard()
    if guard:
        return guard
    body = request.get_json(silent=True) or {}
    bid = _safe_batch_id(body.get("batch_id"))
    if not bid or not os.path.exists(os.path.join(BATCH_DIR, bid + ".csv")):
        return jsonify({"error": "批次不存在或已被回滚", "metric_note": CUSTOM_METRIC_NOTE}), 404
    for ext_name in (".csv", ".orig.csv", ".json"):
        p = os.path.join(BATCH_DIR, bid + ext_name)
        if os.path.exists(p):
            os.remove(p)
    _CLEAN_ENGINE.store.rollback_batch(bid)
    invalidate_cache()
    return jsonify({"ok": True, "rolled_back": bid, "metric_note": CUSTOM_METRIC_NOTE})


@app.route("/api/clean/state")
def clean_state():
    """清洗批次列表 + 数据健康度汇总（格式标准化/人工确认/剔除）"""
    guard = _clean_engine_guard()
    if guard:
        return guard
    batches = _CLEAN_ENGINE.store.list_batches()
    committed = [b for b in batches if b["status"] == "committed"]
    health = {
        "format_normalized": sum(b["transform_cnt"] or 0 for b in committed),
        "manual_confirmed": sum(b["manual_keep_cnt"] or 0 for b in committed),
        "removed": sum(b["removed_rows"] or 0 for b in committed),
        "committed_batches": len(committed),
        "clean_rows": sum(b["total_rows"] or 0 for b in committed)
        - sum(b["removed_rows"] or 0 for b in committed),
    }
    return jsonify({"batches": batches, "health": health,
                    "metric_note": CUSTOM_METRIC_NOTE})


@app.route("/api/clean/logs")
def clean_logs():
    """清洗日志导出：?batch_id= 全部/单批，format=csv 下载（默认 json）"""
    guard = _clean_engine_guard()
    if guard:
        return guard
    bid = request.args.get("batch_id", "").strip()
    fmt = request.args.get("format", "json").strip()
    logs = _CLEAN_ENGINE.store.list_logs(bid or None)
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["规则名", "字段", "原始值", "标准值", "处理动作", "原因", "影响行数", "时间", "版本", "批次"])
        for lg in logs:
            w.writerow([lg["rule_name"], lg["field"], lg["original_value"], lg["standard_value"],
                        lg["action"], lg["reason"], lg["affected_rows"],
                        lg["created_at"], lg["rules_version"], lg["batch_id"]])
        return Response(buf.getvalue().encode("utf-8-sig"), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=clean_logs.csv"})
    return jsonify({"logs": logs, "metric_note": CUSTOM_METRIC_NOTE})


@app.route("/api/custom/state")
def custom_state():
    """已上传批次列表"""
    batches = _batch_meta_list()
    return jsonify({"batches": batches,
                    "total_rows": sum(b["rows"] for b in batches),
                    "metric_note": CUSTOM_METRIC_NOTE})


@app.route("/api/custom/delete", methods=["POST"])
def custom_delete():
    """删除单个批次"""
    body = request.get_json(silent=True) or {}
    bid = _safe_batch_id(body.get("batch_id"))
    if not bid or not os.path.exists(os.path.join(BATCH_DIR, bid + ".csv")):
        return jsonify({"error": "批次不存在", "metric_note": CUSTOM_METRIC_NOTE}), 404
    os.remove(os.path.join(BATCH_DIR, bid + ".csv"))
    for ext_name in (".json", ".orig.csv"):
        p = os.path.join(BATCH_DIR, bid + ext_name)
        if os.path.exists(p):
            os.remove(p)
    invalidate_cache()
    return jsonify({"ok": True, "metric_note": CUSTOM_METRIC_NOTE})


@app.route("/api/custom/clear", methods=["POST"])
def custom_clear():
    """清空全部上传批次，恢复基线数据"""
    if os.path.exists(BATCH_DIR):
        shutil.rmtree(BATCH_DIR)
    ensure_batch_dir()
    if os.path.exists(PENDING_DIR):
        shutil.rmtree(PENDING_DIR)
    invalidate_cache()
    return jsonify({"ok": True, "metric_note": CUSTOM_METRIC_NOTE})


@app.route("/api/custom/analyze")
def custom_analyze():
    """对全部上传扩展行自动分析：数值统计 + 月度趋势 + 品类占比 + 明细表"""
    ext = load_extension()
    if ext is None or len(ext) == 0:
        return jsonify({"empty": True, "metric_note": CUSTOM_METRIC_NOTE})

    amt = pd.to_numeric(ext["TotalAmount"], errors="coerce")
    qty = pd.to_numeric(ext["Quantity"], errors="coerce")
    stats = [
        {"label": "上传订单数", "value": f"{len(ext):,}"},
        {"label": "累计金额(万)", "value": f"{amt.sum()/10000:,.1f}"},
        {"label": "平均单笔金额(元)", "value": f"{amt.mean():,.0f}"},
        {"label": "累计数量", "value": f"{int(qty.sum()):,}"},
        {"label": "涉及客户数", "value": f"{ext['CustomerKey'].nunique():,}"},
        {"label": "涉及商品数", "value": f"{ext['ProductKey'].nunique():,}"},
    ]

    trend = []
    if "OrderDate" in ext.columns:
        ym = pd.to_datetime(ext["OrderDate"], errors="coerce")
        if ym.notna().any():
            g = ext.assign(_ym=ym.dt.to_period("M").astype(str)).groupby("_ym")["TotalAmount"].sum()
            trend = [{"ym": str(k), "value": f2(v / 10000)} for k, v in g.items()]

    cat_pie = ext.groupby("Category")["TotalAmount"].sum().sort_values(ascending=False)
    category_pie = [{"name": str(k), "value": f2(v / 10000)} for k, v in cat_pie.items()]

    # 明细表：优先展示原始上传行（脏数据着色），无原始文件时退回扩展行
    cols, table = [], []
    dirty_summary = {"total": 0, "dropped": 0, "fixed": 0}
    orig_files = sorted(f for f in os.listdir(BATCH_DIR) if f.endswith(".orig.csv"))
    if orig_files:
        for f in orig_files:
            bid = f[:-len(".orig.csv")]
            o = pd.read_csv(os.path.join(BATCH_DIR, f), dtype=str, keep_default_na=False)
            m = {}
            jp = os.path.join(BATCH_DIR, bid + ".json")
            if os.path.exists(jp):
                try:
                    with open(jp, "r", encoding="utf-8") as fp:
                        m = json.load(fp).get("mapping", {})
                except Exception:
                    m = {}
            for c in o.columns:
                if c not in cols:
                    cols.append(c)
            for row in classify_dirty(o, m):
                dirty_summary["total"] += 1
                if row["row_status"] == "dropped":
                    dirty_summary["dropped"] += 1
                elif row["row_status"] == "fixed":
                    dirty_summary["fixed"] += 1
                table.append(row)
        table = table[:100]
        stats += [
            {"label": "脏数据丢弃行", "value": f"{dirty_summary['dropped']:,}"},
            {"label": "已修正异常行", "value": f"{dirty_summary['fixed']:,}"},
        ]
        note = CUSTOM_METRIC_NOTE + " 明细表着色：红色=无效/已丢弃，橙色=已修正或兜底。"
    else:
        cols = ["TransactionID", "OrderDate", "CustomerKey", "ProductName", "Category", "Quantity", "TotalAmount"]
        raw_tab = ext[cols].head(100).astype({"TotalAmount": float}).to_dict(orient="records")
        table = [{"cells": {k: (f2(v) if k == "TotalAmount" else _fmt_cell(v)) for k, v in r.items()},
                  "flags": {}, "row_status": "ok", "row_msg": ""} for r in raw_tab]
        note = CUSTOM_METRIC_NOTE

    return jsonify({"stats": stats, "trend": trend, "category_pie": category_pie,
                    "table": table, "columns": cols, "dirty_summary": dirty_summary,
                    "metric_note": note})


# ==================== 页面0：四板块融合总览（一张图） ====================
def page_overview(df, rfm, pool, tier, year, month=""):
    """页0总览：四客群月度销售额堆叠序列（口径与页1一致：tier 按 RFM 客群筛客户池，year/month 筛时间）。
    高价值/唤醒/ROI 趋势直接复用 p2/p3/p4 数据，前端在同一坐标系内融合。"""
    pool1 = pool if not tier else pool[pool["RFM_Segment"] == tier]
    if len(pool1) == 0:
        return {"empty": True}
    keys = set(pool1["CustomerKey"])
    dff = df[df["CustomerKey"].isin(keys)]
    if year:
        dff = dff[dff["Year"] == int(year)]
    if month:
        dff = dff[dff["Month"] == int(month)]
    if len(dff) == 0:
        return {"empty": True}

    # 客群顺序与页1饼图一致（客户数降序），保证颜色一一对应
    seg_order = pool1["RFM_Segment"].value_counts().index.tolist()
    dff = dff.assign(Seg=dff["RFM_Segment"].fillna("未分层"))
    g = dff.groupby(["YearMonth", "Seg"])["TotalAmount"].sum().unstack(fill_value=0)
    g = g.reindex(full_months(g.sum(axis=1), df["YearMonth"].min(), df["YearMonth"].max()).index,
                  fill_value=0)
    seg_order += [c for c in g.columns if c not in seg_order]

    seg_series = []
    for s in seg_order:
        seg_series.append({"name": str(s),
                           "data": [f2(g.loc[i].get(s, 0) / 10000) for i in g.index]})
    return {"months": [str(i) for i in g.index], "seg_series": seg_series}


# ==================== 页面1：RFM 客群分层总览（4.0：4 KPI + 3 图） ====================
def page_rfm_overview(dff, pool1):
    if len(dff) == 0 or len(pool1) == 0:
        return {"empty": True}
    total_sales = float(dff["TotalAmount"].sum())
    aov = float(dff["TotalAmount"].mean())

    # 窗口内活跃客户集合（已按 year/month 过滤的交易客户）：
    # 饼图/F&M柱/散点统一取该集合，保证饼图扇区人数之和 == KPI「购买客户数」
    active_keys = set(dff["CustomerKey"].dropna().unique())
    pool_win = pool1[pool1["CustomerKey"].isin(active_keys)]
    if len(pool_win) == 0:
        return {"empty": True}

    # 4.0 页1 KPI：仅保留 4 个核心指标（手册第6步）
    kpi = [
        {"label": "购买客户数", "value": f"{dff['CustomerKey'].nunique():,}", "kpi_id": "p1_customers"},
        {"label": "交易笔数", "value": f"{len(dff):,}", "kpi_id": "p1_orders"},
        {"label": "销售金额(万)", "value": f"{total_sales/10000:,.1f}", "kpi_id": "p1_sales"},
        {"label": "笔单价(元)", "value": f"{aov:,.0f}", "kpi_id": "p1_aov"},
    ]

    # 客群结构占比（值=筛选期内购买客户数，口径与 KPI「购买客户数」一致）
    seg_pie = pie_list(pool_win["RFM_Segment"])

    # 各群平均消费频次(F) / 平均消费金额(M)（簇状柱形图，窗口内活跃客户）
    seg_fm = pool_win.groupby("RFM_Segment").agg(fm=("F", "mean"), mm=("M", "mean")).reset_index()
    seg_avg_fm = [{"name": str(r["RFM_Segment"]), "f": f2(r["fm"]), "m": f2(r["mm"])}
                  for _, r in seg_fm.iterrows()]

    # RFM 客群分布散点图：X=R_days(近度), Y=F(频次), 大小=M(金额)（窗口内活跃客户）
    scatter = pool_win[["R_days", "F", "M", "RFM_Segment"]].rename(
        columns={"RFM_Segment": "seg"})
    if len(scatter) > 3000:
        scatter = scatter.sample(3000, random_state=42)
    scatter_list = [{"r": int(x["R_days"]), "f": int(x["F"]), "m": f2(x["M"]),
                     "seg": x["seg"]} for _, x in scatter.iterrows()]

    return {"kpi": kpi, "seg_pie": seg_pie, "seg_avg_fm": seg_avg_fm,
            "scatter": scatter_list}


# ==================== 页面2：高价值客户分析 ====================
def page_high_value(df, rfm, pool, category="", year="", month=""):
    """手册第5步页2：对比组固定为 高价值 vs 非高价值（营销层级 CustomerTier 口径，
    与 RFM 客群同名不同义，前端有口径标注）；客户池随全局筛选过滤。
    year/month 作用于本页趋势与品类图（筛选期内交易）；复购率 KPI 保持客户级口径。"""
    grp = "高价值客户"
    gA = pool[pool["CustomerTier"] == grp]
    gB = pool[pool["CustomerTier"] != grp]
    if len(gA) == 0 or len(gB) == 0:
        return {"empty": True}

    # 复购率 = 客户中订单数 F >= 2 的占比（客户级口径）
    rep_a = float((gA["F"] >= 2).mean() * 100)
    rep_b = float((gB["F"] >= 2).mean() * 100)
    lift = float((rep_a - rep_b) / rep_b * 100) if rep_b else 0.0

    keys_a = set(gA["CustomerKey"])
    dff_a = df[df["CustomerKey"].isin(keys_a)]
    if year:
        dff_a = dff_a[dff_a["Year"] == int(year)]
    if month:
        dff_a = dff_a[dff_a["Month"] == int(month)]
    if category:
        dff_a = dff_a[dff_a["Category"] == category]

    kpi = [
        {"label": f"{grp}复购率", "value": f"{rep_a:.1f}%", "kpi_id": "p2_rep_hv"},
        {"label": f"非{grp}复购率", "value": f"{rep_b:.1f}%", "kpi_id": "p2_rep_other"},
        {"label": "复购率提升幅度", "value": f"{lift:+.1f}%", "kpi_id": "p2_rep_lift"},
    ]

    # 高价值客户月度销售趋势（补齐到全局数据范围，前端裁前导 0）
    monthly = dff_a.groupby("YearMonth")["TotalAmount"].sum()
    monthly = full_months(monthly, df["YearMonth"].min(), df["YearMonth"].max())
    trend = [{"ym": str(i), "sales": f2(monthly.loc[i] / 10000)} for i in monthly.index]

    # 各品类销售金额排名
    top_cat = dff_a.groupby("Category")["TotalAmount"].sum().sort_values(ascending=False).reset_index()
    top_category = [{"name": r["Category"], "sales": f2(r["TotalAmount"] / 10000)}
                    for _, r in top_cat.iterrows()]

    return {"kpi": kpi, "trend": trend, "top_category": top_category,
            "grp_name": grp}


# ==================== 页面3：沉睡客户唤醒 ====================
def page_awaken(df, rfm, pool, category="", year="", month=""):
    """手册第5步页3：本页固定分析沉睡客户（营销层级 CustomerTier 口径，
    与 RFM 客群同名不同义，前端有口径标注）；客户池随全局筛选过滤。
    year/month 作用于本页唤醒趋势与对比图（筛选期内交易）；唤醒率 KPI 保持客户级口径。"""
    sleep = pool[pool["CustomerTier"] == "沉睡客户"]
    if len(sleep) == 0:
        return {"empty": True}

    awake_cnt = int(sleep["IsAwakened"].sum())
    sleep_cnt = int(len(sleep))
    rate = awake_cnt / sleep_cnt * 100 if sleep_cnt else 0.0

    keys = set(sleep["CustomerKey"])
    dff_s = df[df["CustomerKey"].isin(keys)]
    if year:
        dff_s = dff_s[dff_s["Year"] == int(year)]
    if month:
        dff_s = dff_s[dff_s["Month"] == int(month)]
    if category:
        dff_s = dff_s[dff_s["Category"] == category]

    kpi = [
        {"label": "沉睡客户数", "value": f"{sleep_cnt:,}", "kpi_id": "p3_sleep_cnt"},
        {"label": "唤醒客户数", "value": f"{awake_cnt:,}", "kpi_id": "p3_awake_cnt"},
        {"label": "唤醒率", "value": f"{rate:.1f}%", "kpi_id": "p3_awake_rate"},
    ]

    # 沉睡客户唤醒订单月度趋势（补齐到全局数据范围）
    monthly = dff_s.groupby("YearMonth")["IsAwakenedOrder"].sum()
    monthly = full_months(monthly, df["YearMonth"].min(), df["YearMonth"].max())
    awaken_trend = [{"ym": str(i), "cnt": int(monthly.loc[i])} for i in monthly.index]

    # 唤醒 vs 未唤醒订单对比（按月）
    stack = dff_s.groupby(["YearMonth", "IsAwakenedOrder"])["TransactionID"].count().unstack(fill_value=0)
    stack = stack.reindex(full_months(stack.sum(axis=1), df["YearMonth"].min(), df["YearMonth"].max()).index,
                          fill_value=0)
    stack_list = [{"ym": str(i),
                   "awake": int(stack.loc[i].get(True, 0)),
                   "normal": int(stack.loc[i].get(False, 0))} for i in stack.index]

    return {"kpi": kpi, "awaken_trend": awaken_trend, "stack": stack_list}


# ==================== 页面4：营销产出分析 ====================
def page_marketing(campf, camp_y, camp_all, strat=""):
    """页4：KPI 按 year+strat 筛选口径；策略对比图用 camp_y（year 过滤、不过滤 strat，
    仅前端高亮，保证多柱对比）；年度ROI 图用全量 camp 按 strat 过滤、不过滤 year
    （仅前端高亮，保证多年序列不被裁成单点）。"""
    if len(campf) == 0:
        return {"empty": True}

    roi_all = float(campf["Revenue"].sum() / campf["Cost"].sum())

    # 早期/近期（手册：Stage 字段）
    early = campf[campf["Stage"].str.startswith("早期")]
    recent = campf[campf["Stage"].str.startswith("近期")]
    roi_early = float(early["Revenue"].sum() / early["Cost"].sum()) if len(early) else 0.0
    roi_recent = float(recent["Revenue"].sum() / recent["Cost"].sum()) if len(recent) else 0.0
    lift = float(roi_recent / roi_early) if roi_early and roi_recent else 0.0

    kpi = [
        {"label": "整体ROI", "value": f"{roi_all:.2f}", "kpi_id": "p4_roi_all"},
        {"label": "早期营销ROI", "value": f"{roi_early:.2f}" if roi_early else "-", "kpi_id": "p4_roi_early"},
        {"label": "近期营销ROI", "value": f"{roi_recent:.2f}" if roi_recent else "-", "kpi_id": "p4_roi_recent"},
        {"label": "ROI提升倍数", "value": f"{lift:.2f}x" if lift else "-", "kpi_id": "p4_roi_lift"},
    ]

    # StrategyGroup 成本+收入（各营销策略组成本与收入对比，不受 strat 联动裁剪）
    g = camp_y.groupby("StrategyGroup").agg(cost=("Cost", "sum"), rev=("Revenue", "sum")).reset_index()
    strategy = [{"name": r["StrategyGroup"], "cost": f2(r["cost"] / 10000),
                 "rev": f2(r["rev"] / 10000),
                 "roi": f2(r["rev"] / r["cost"])} for _, r in g.iterrows()]

    # 年度 ROI 趋势（整体ROI逐年变化，不受 year 筛选裁剪；strat 联动可继续过滤）
    camp_yr = camp_all if not strat else camp_all[camp_all["StrategyGroup"] == strat]
    yr = camp_yr.groupby("Year").agg(cost=("Cost", "sum"), rev=("Revenue", "sum")).reset_index()
    yearly_roi = [{"year": int(r["Year"]), "roi": f2(r["rev"] / r["cost"])}
                  for _, r in yr.iterrows()]

    return {"kpi": kpi, "strategy": strategy, "yearly_roi": yearly_roi}


# ==================== 页5：EDA 报告生成 ====================
@app.route("/api/eda/options")
def eda_options():
    """返回 EDA 页可勾选的维度选项（客群/品类/年份）"""
    df, rfm, camp = load_data()
    return jsonify({
        "segments": sorted(rfm["RFM_Segment"].dropna().unique().tolist()),
        "categories": sorted(df["Category"].dropna().unique().tolist()),
        "years": sorted(df["Year"].unique().tolist()),
    })


@app.route("/api/eda/generate", methods=["POST"])
def eda_generate():
    """按勾选范围与章节一键生成 EDA PDF 报告"""
    from eda_report_gen import generate_eda_report  # 延迟导入：仅生成报告时才依赖 matplotlib
    df, rfm, camp = load_data()
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get("sections"), list) or not body["sections"]:
        return jsonify({"error": "请至少勾选一个分析章节"}), 400
    try:
        pdf_path, meta = generate_eda_report(df, rfm, camp, body)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"报告生成失败: {e}"}), 500
    return jsonify({"filename": os.path.basename(pdf_path),
                    "url": f"/api/eda/download/{os.path.basename(pdf_path)}",
                    "meta": meta})


@app.route("/api/eda/download/<path:filename>")
def eda_download(filename):
    """下载已生成的 EDA 报告"""
    from eda_report_gen import EDA_DIR  # 延迟导入
    return send_from_directory(EDA_DIR, filename, as_attachment=True)


@app.route("/api/eda/list")
def eda_list():
    """已生成报告历史列表"""
    from eda_report_gen import list_reports  # 延迟导入
    return jsonify({"reports": list_reports()})


if __name__ == "__main__":
    # 安全说明：本服务已启用登录鉴权（飞书/钉钉/微信扫码，默认演示模式），
    # 且上传接口会写本地文件。仅供本机 / 可信内网演示使用，
    # host 必须保持 127.0.0.1，严禁改为 0.0.0.0 暴露外网。
    print("=" * 56)
    print("  RFM 可视化大屏服务启动（四页版 · 登录版）")
    print("  请访问: http://127.0.0.1:5000 （未登录将进入扫码登录页）")
    print("  登录通道配置: login_config.json （默认演示扫码，离线可用）")
    print("=" * 56)
    app.run(host="127.0.0.1", port=5000, debug=False)
