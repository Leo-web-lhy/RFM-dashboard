# -*- coding: utf-8 -*-
"""电商 RFM 客群分析 · 数据清洗引擎（三色分流 + SQLite 日志）

核心红线（本引擎所有规则共同遵守）：
  1. 格式不统一 ≠ 无效数据。语义等价金额必须「标准化转换并保留」，不得删除；
  2. 三色分流：绿=格式正常直接通过 / 黄=可转换或需人工确认（永不自动删除）/
     红=业务无效剔除候选（测试单、重复订单等，需人工确认 + 影响评估 + 可回滚）；
  3. 原始值保留、新增标准值字段；所有转换/剔除/人工确认均有日志，可追溯、可回滚。

仅依赖标准库 + pandas，与 Flask 解耦，便于单测与复用。
"""
import json
import os
import re
import sqlite3
import threading
from datetime import datetime

import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "rules_config.json")
DB_PATH = os.path.join(BASE_DIR, "clean_store.db")

GREEN, YELLOW, RED = "green", "yellow", "red"

# 内置列角色别名（金额/日期/订单号/状态等通用映射；客户ID三级映射见 rules_config.json）
COL_ALIASES = {
    "amount": ["totalamount", "amount", "sales", "金额", "销售额", "成交额", "销售金额", "交易金额"],
    "date": ["orderdate", "date", "日期", "订单日期", "交易日期", "下单时间", "下单日期"],
    "qty": ["quantity", "qty", "数量", "件数", "销量"],
    "product": ["productname", "商品", "商品名", "产品", "产品名", "品名"],
    "category": ["category", "品类", "类目", "分类"],
    "customer": ["customerkey", "customerid", "客户id", "客户编号", "客户号", "客户"],
    "order_id": ["orderid", "order_id", "订单号", "订单编号", "订单id", "订单号码"],
    "pay_date": ["支付时间", "支付日期", "paytime", "paymentdate", "pay_date", "paydate"],
    "status": ["订单状态", "状态", "status", "orderstatus", "订单状态码", "交易状态"],
    "remark": ["备注", "订单备注", "remark", "note", "memo", "留言"],
}

_EMPTY_VALS = ("", "nan", "none", "null", "na", "n/a", "-", "--", "无")


def _fmt_cell(v):
    """单元格展示值：NaN/空 -> 空串，浮点整数去掉小数尾巴"""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else str(v)
    return str(v)


class CleanStore:
    """SQLite 单文件：干净数据批次 + 清洗日志。

    日志表字段（与需求对齐）：
    规则名 / 字段 / 原始值 / 标准值 / 处理动作 / 原因 / 影响行数 / 时间 / 版本
    所有写入持锁 + WAL，可追溯；rollback_batch 标记批次回滚并记日志。
    """

    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_schema()

    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self):
        with self._lock, self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS clean_batches (
                  batch_id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'committed',
                  total_rows INTEGER, green_rows INTEGER, yellow_rows INTEGER, red_rows INTEGER,
                  transform_cnt INTEGER, manual_keep_cnt INTEGER, removed_rows INTEGER,
                  removed_ratio REAL, removed_gmv REAL,
                  created_at TEXT, rules_version TEXT
                );
                CREATE TABLE IF NOT EXISTS clean_logs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  batch_id TEXT NOT NULL,
                  rule_name TEXT, field TEXT,
                  original_value TEXT, standard_value TEXT,
                  action TEXT, reason TEXT,
                  affected_rows INTEGER DEFAULT 1,
                  created_at TEXT, rules_version TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_logs_batch ON clean_logs(batch_id);
                """
            )

    def add_log(self, batch_id, rule, field, orig, std, action, reason, n=1, version=""):
        """写一条清洗日志。orig/std 传 None 表示该规则无单值对照（聚合日志）"""
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO clean_logs(batch_id, rule_name, field, original_value,"
                " standard_value, action, reason, affected_rows, created_at, rules_version)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (batch_id, rule, field, orig, std, action, reason, n,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"), version))

    def commit_batch(self, batch_id, name, stats, transform_cnt, manual_keep_cnt,
                     removed_rows, removed_ratio, removed_gmv, version):
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO clean_batches(batch_id, name, status, total_rows,"
                " green_rows, yellow_rows, red_rows, transform_cnt, manual_keep_cnt,"
                " removed_rows, removed_ratio, removed_gmv, created_at, rules_version)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (batch_id, name, "committed", stats["total"], stats["green"],
                 stats["yellow"], stats["red"], transform_cnt, manual_keep_cnt,
                 removed_rows, removed_ratio, removed_gmv,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"), version))

    def rollback_batch(self, batch_id):
        """批次回滚：状态置 rolled_back 并记回滚日志；返回是否命中批次"""
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT total_rows FROM clean_batches WHERE batch_id=?", (batch_id,)).fetchone()
            if row is None:
                return False
            conn.execute("UPDATE clean_batches SET status='rolled_back' WHERE batch_id=?", (batch_id,))
            conn.execute(
                "INSERT INTO clean_logs(batch_id, rule_name, field, original_value,"
                " standard_value, action, reason, affected_rows, created_at, rules_version)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (batch_id, "批次回滚", "", None, None, "rollback",
                 "整批回滚：已从大屏数据源移除", int(row["total_rows"]),
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ""))
            return True

    def list_batches(self):
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM clean_batches ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def list_logs(self, batch_id=None):
        sql = "SELECT * FROM clean_logs"
        args = ()
        if batch_id:
            sql += " WHERE batch_id=?"
            args = (batch_id,)
        sql += " ORDER BY id"
        with self._lock, self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]


class ScanResult:
    """一次扫描结果：行级三色 + 黄区字段清单 + 红区剔除原因（可 JSON 序列化，供 pending 暂存）"""

    def __init__(self, columns, mapping, rules_version, rows, stats, name=""):
        self.columns = list(columns)
        self.mapping = mapping
        self.rules_version = rules_version
        self.rows = rows          # [{idx, cells{col:str}, status, yellows[], reds[], dedup_of, remove_confirmed, _amt_std}]
        self.stats = stats        # {total, green, yellow, red}
        self.name = name
        self.red_confirmed = False

    def to_dict(self):
        return {"columns": self.columns, "mapping": self.mapping,
                "rules_version": self.rules_version, "rows": self.rows,
                "stats": self.stats, "name": self.name,
                "red_confirmed": self.red_confirmed}

    @classmethod
    def from_dict(cls, d):
        s = cls(d.get("columns", []), d.get("mapping", {}), d.get("rules_version", ""),
                d.get("rows", []), d.get("stats", {}), d.get("name", ""))
        s.red_confirmed = bool(d.get("red_confirmed"))
        return s

    # ---------- 视图 ----------
    def yellow_table(self):
        """黄区对照表：原始值 → 建议标准值（转换确认区数据源）"""
        return [{"row": r["idx"], "cells": r["cells"], "yellows": r["yellows"],
                 "status": r["status"]} for r in self.rows if r["yellows"]]

    def red_table(self):
        """红区剔除清单：行 + 原因"""
        return [{"row": r["idx"], "cells": r["cells"], "reds": r["reds"],
                 "dedup_of": r.get("dedup_of")} for r in self.rows if r["reds"]]

    def pending_yellow_count(self):
        """黄区未处理条数（未确认转换且未标记无法解析）"""
        return sum(1 for r in self.rows for y in r["yellows"] if y["resolved"] is None)

    def unparseable_rows(self):
        """人工标记无法解析的行（生成干净数据时剔除，计入影响评估）"""
        out = []
        for r in self.rows:
            for y in r["yellows"]:
                if y["resolved"] and y["resolved"]["action"] == "unparseable":
                    out.append(r["idx"])
                    break
        return out

    # ---------- 操作 ----------
    def apply_transform(self, items, confirm_all=False):
        """黄区确认：items=[{row, field, rule?, action(confirm/unparseable), standard?}]
        同一行同字段可能有多条黄项（如 金额单位换算 + 极端金额复核），rule 用于精确定位；
        rule 缺省时匹配该行该字段第一条未处理黄项。
        confirm_all=True 时批量确认所有「高置信度自动建议」的未处理项（用建议值）"""
        by_key = {(r["idx"], y["field"], y["rule"]): y
                  for r in self.rows for y in r["yellows"]}
        by_field = {}
        for r in self.rows:
            for y in r["yellows"]:
                by_field.setdefault((r["idx"], y["field"]), []).append(y)
        errs = []
        if confirm_all:
            for r in self.rows:
                for y in r["yellows"]:
                    if y["resolved"] is None and y["confidence"] == "auto" \
                            and y["suggested"] is not None:
                        y["resolved"] = {"action": "confirm", "standard": y["suggested"]}
        for it in items or []:
            key = (int(it.get("row", -1)), str(it.get("field", "")))
            rule = str(it.get("rule", ""))
            y = None
            if rule:
                y = by_key.get((key[0], key[1], rule))
            else:
                for c in by_field.get(key, []):
                    if c["resolved"] is None:
                        y = c
                        break
            if y is None:
                errs.append(f"第 {it.get('row', '?')} 行字段「{it.get('field', '')}」不在待确认清单中或已处理")
                continue
            action = it.get("action", "confirm")
            if action == "confirm":
                if "standard" in it:
                    std = it.get("standard")
                else:
                    std = y["suggested"]
                if std is None:
                    errs.append(f"第 {key[0]} 行「{key[1]}」缺少标准值，无法确认转换")
                    continue
                # 允许显式空字符串：如确认「无法归属」保留空值（归入未归属客户）
                y["resolved"] = {"action": "confirm", "standard": std}
            elif action == "unparseable":
                y["resolved"] = {"action": "unparseable", "standard": None}
            else:
                errs.append(f"第 {key[0]} 行「{key[1]}」收到未知动作「{action}」")
        self.recompute_stats()
        return errs

    def recompute_stats(self):
        """黄区确认后重算行状态（黄色→绿色，前端可视），红色保持"""
        s = {"total": len(self.rows), "green": 0, "yellow": 0, "red": 0}
        for r in self.rows:
            if r["reds"]:
                status = RED
            elif any(y["resolved"] is None for y in r["yellows"]):
                status = YELLOW
            else:
                status = GREEN
            r["status"] = status
            s[status] += 1
        self.stats = s

    def removal_impact(self, warn_threshold, warn_msg):
        """剔除影响评估：红区行 + 人工标记无法解析行。返回 {rows, total, ratio, gmv, warn, warn_msg}"""
        removed = self._removed_detail()
        ratio = removed["rows"] / self.stats["total"] if self.stats["total"] else 0.0
        return {"rows": removed["rows"], "total": self.stats["total"],
                "ratio": round(ratio, 4), "gmv": round(removed["gmv"], 2),
                "warn": ratio > warn_threshold, "warn_msg": warn_msg}

    def _removed_detail(self):
        unparse = set(self.unparseable_rows())
        rows_n, gmv = 0, 0.0
        amt_col = self.mapping.get("amount")
        for r in self.rows:
            if r["reds"] or r["idx"] in unparse:
                rows_n += 1
                gmv += self._row_gmv_est(r, amt_col)
        return {"rows": rows_n, "gmv": gmv}

    def _row_gmv_est(self, r, amt_col):
        """行 GMV 估计：标准金额 > 黄区建议值 > 0（用于剔除影响评估）"""
        if amt_col and r.get("_amt_std") is not None:
            return float(r["_amt_std"])
        if amt_col:
            for y in r["yellows"]:
                if y["field"] == amt_col and y["suggested"] is not None:
                    try:
                        return float(y["suggested"])
                    except (TypeError, ValueError):
                        return 0.0
        return 0.0

    def build_clean_df(self, raw_df):
        """应用确认后的转换，产出干净 DataFrame + 剔除明细。

        规则：黄区必须全部处理；红区必须已确认；红区行与「标记无法解析」行剔除，
        其余行的黄区确认值写回对应列；金额列另生成「原始金额」「标准金额_元」两列；
        客户ID按 会员ID > 手机号 > 匿名ID 统一，无法归属者归入统一「未归属客户」。
        返回 (clean_df, removed[(idx, reason, gmv_est)])；未满足条件抛 ValueError。"""
        df = raw_df.dropna(how="all").reset_index(drop=True)
        pending = self.pending_yellow_count()
        if pending > 0:
            raise ValueError(f"还有 {pending} 条黄色数据未处理：请先完成转换确认或标记无法解析（黄色数据不会被自动删除）")
        if self.red_table() and not self.red_confirmed:
            raise ValueError("存在红色剔除数据未确认：请先查看剔除清单与影响评估并确认剔除")

        amt_col = self.mapping.get("amount")
        unparse = set(self.unparseable_rows())
        final_idx, removed = [], []
        for r in self.rows:
            if r["reds"]:
                removed.append((r["idx"], "；".join(r["reds"]), self._row_gmv_est(r, amt_col)))
                continue
            final_idx.append(r["idx"])
        for i in sorted(unparse):
            r = self.rows[i]
            note = "；".join(y["note"] for y in r["yellows"]
                             if y["resolved"] and y["resolved"]["action"] == "unparseable")
            removed.append((i, f"人工标记无法解析：{note}", self._row_gmv_est(r, amt_col)))
            if i in final_idx:
                final_idx.remove(i)

        clean = df.iloc[final_idx].copy()
        # 原始金额列必须在应用转换前保存（转换会把金额列改写为标准数值）
        if amt_col and amt_col in clean.columns:
            clean["原始金额"] = clean[amt_col].apply(_fmt_cell).values
        # 应用黄区确认值（客户ID统一另行处理）
        for i in final_idx:
            r = self.rows[i]
            for y in r["yellows"]:
                if not y["resolved"] or y["resolved"]["action"] != "confirm":
                    continue
                col = y["field"]
                if col == "__客户归属__" or col not in clean.columns:
                    continue
                std = y["resolved"]["standard"]
                # pandas 3.x 读 CSV 的字符串列是 str dtype，直接赋数值会抛 TypeError；先转 object
                if not pd.api.types.is_object_dtype(clean[col]):
                    clean[col] = clean[col].astype(object)
                if col == amt_col:
                    try:
                        clean.loc[i, col] = float(std)
                    except (TypeError, ValueError):
                        raise ValueError(f"第 {i + 1} 行确认的标准金额「{std}」不是有效数值，请返回转换确认区修改")
                else:
                    clean.loc[i, col] = std

        # 绿区金额标准化写回：扫描时已算出标准值（如 ¥1,299.00 → 1299.0），
        # 绿区行没有黄项，需在此写回金额列，保证「标准金额_元」为有效数值
        if amt_col and amt_col in clean.columns:
            if not pd.api.types.is_object_dtype(clean[amt_col]):
                clean[amt_col] = clean[amt_col].astype(object)
            for i in final_idx:
                r = self.rows[i]
                if any(y["field"] == amt_col for y in r["yellows"]):
                    continue
                if r.get("_amt_std") is not None:
                    clean.loc[i, amt_col] = float(r["_amt_std"])

        # 标准金额_元 双字段（原始金额已在转换前保存，见上）
        if amt_col and amt_col in clean.columns:
            clean["标准金额_元"] = pd.to_numeric(clean[amt_col], errors="coerce").values

        # 客户ID统一：客户ID列优先，否则 会员ID > 手机号 > 匿名ID；全空 → 「未归属客户」
        cust_col = self.mapping.get("customer")
        chain = [(role, self.mapping[role])
                 for role in self.cfg_cust_priority()
                 if self.mapping.get(role) and self.mapping[role] != cust_col]

        def pick_cust(row):
            if cust_col and cust_col in clean.columns:
                v = str(row[cust_col]).strip()
                if v and v.lower() not in _EMPTY_VALS:
                    return v
            for _, col in chain:
                v = str(row[col]).strip()
                if v and v.lower() not in _EMPTY_VALS:
                    return v
            return ""
        if cust_col or chain:
            unified = clean.apply(pick_cust, axis=1)
            if cust_col and cust_col in clean.columns:
                # pandas 3.x 字符串列是 str dtype，直接赋 object 数组会抛 TypeError；先转 object
                if not pd.api.types.is_object_dtype(clean[cust_col]):
                    clean[cust_col] = clean[cust_col].astype(object)
                clean[cust_col] = unified.values
            else:
                clean["客户ID"] = unified.values
        return clean, removed

    def cfg_cust_priority(self):
        return getattr(self, "_cust_priority", ["member_id", "phone", "anonymous_id"])


class CleanEngine:
    """清洗引擎：加载 rules_config.json，扫描数据 → 三色分流"""

    def __init__(self, config_path=CONFIG_PATH, store=None):
        with open(config_path, encoding="utf-8") as fp:
            self.cfg = json.load(fp)
        self.version = self.cfg.get("version", "1.0.0")
        self.store = store or CleanStore()
        self.today = pd.Timestamp(datetime.now().date())
        self._cust_priority = list(self.cfg["customer_id"]["priority"])

    # ---------- 列角色映射 ----------
    def _map_roles(self, cols):
        norm = {c: str(c).strip().lower() for c in cols}
        alias_sets = {k: [str(a).strip().lower() for a in v] for k, v in COL_ALIASES.items()}
        mapping = {}
        for role, aliases in alias_sets.items():
            for c, n in norm.items():
                if n in aliases and role not in mapping:
                    mapping[role] = c
        for role, aliases in self.cfg["customer_id"]["aliases"].items():
            low = [str(a).strip().lower() for a in aliases]
            for c, n in norm.items():
                if n in low and role not in mapping:
                    mapping[role] = c
        # 枚举组列
        enum_cols = []
        for gname, gcfg in self.cfg.get("enum_alias", {}).get("groups", {}).items():
            f_low = [str(a).strip().lower() for a in gcfg.get("field_aliases", [])]
            for c, n in norm.items():
                if n in f_low:
                    amap = {str(k).strip().lower(): v for k, v in gcfg.get("map", {}).items()}
                    enum_cols.append((gname, c, amap))
                    break
        return mapping, enum_cols

    # ---------- 单值标准化 ----------
    def _norm_amount(self, v):
        """金额标准化。返回 (std, level, rule, note, confidence)
        level: green=格式正常 / yellow=进转换确认区（永不自动删除）；
        confidence: auto=高置信度自动建议 / manual=需人工复核"""
        a_cfg = self.cfg["amount"]
        units, syms, seps = a_cfg["units"], a_cfg["currency_symbols"], a_cfg["thousand_separators"]
        if isinstance(v, (int, float)) and not (isinstance(v, float) and pd.isna(v)):
            f = float(v)
            if f < 0:
                return f, YELLOW, "负金额(疑似退款)", \
                    "金额为负，标记退款：M 净额口径下与支付行对冲，不删除", "manual"
            if f == 0:
                return 0.0, YELLOW, "零金额(疑似赠品)", \
                    "金额为 0，标记赠品：F 是否计频次可配置（默认不计），不删除", "manual"
            return f, GREEN, "金额数值正常", "", "auto"
        s = str(v).strip()
        if s.lower() in _EMPTY_VALS:
            return None, YELLOW, "金额为空", "金额为空，需人工补充或标记无法解析，不自动删除", "manual"
        t = s
        for sym in syms:
            t = t.replace(sym, "")
        for sep in seps:
            t = t.replace(sep, "")
        t = t.strip()
        # 遮挡位（x0,000元/年 类）：首位数字被遮挡，无法确定数值 → 人工复核，绝不自动删
        if re.search(r"[xX×＊*?]\s*[\d,，.\s]*\s*(亿元|万元|千元|亿|万|元|分)", t):
            return None, YELLOW, "金额含遮挡字符", \
                f"「{s}」数字位被遮挡(x)，无法确定具体数值，需人工修改或标记无法解析，不自动删除", "manual"
        # 纯数字（含千分位/货币符号，已去符号）
        if re.fullmatch(r"-?\d+(\.\d+)?", t):
            f = float(t)
            if f < 0:
                return f, YELLOW, "负金额(疑似退款)", \
                    f"「{s}」金额为负，标记退款：M 净额口径下与支付行对冲，不删除", "manual"
            if f == 0:
                return 0.0, YELLOW, "零金额(疑似赠品)", \
                    f"「{s}」金额为 0，标记赠品：F 是否计频次可配置（默认不计），不删除", "manual"
            return f, GREEN, "金额格式正常", f"含货币符号/千分位「{s}」，去符号后转数值 {f}", "auto"
        # 数字 + 单位（可选 /年 /月 等统计口径后缀）
        m = re.search(r"(-?\d+(?:\.\d+)?)\s*(亿元|万元|千元|亿|万|元|分)\s*(/年|/月|每年|每月)?", t)
        if m:
            f = float(m.group(1))
            std = round(f * units[m.group(2)], 4)
            note = f"单位「{m.group(2)}」按 ×{units[m.group(2)]} 换算为元"
            if m.group(3):
                note += f"；「{m.group(3)}」为统计口径后缀，保留在原始值中"
            return std, YELLOW, "金额单位换算", note, "auto"
        return None, YELLOW, "金额无法解析", \
            f"「{s}」无法解析为金额，需人工修改或标记无法解析，不自动删除", "manual"

    def _norm_date(self, v):
        """日期校验。返回 (std, level, rule, note, confidence)"""
        s = _fmt_cell(v)
        if s.lower() in _EMPTY_VALS:
            return None, YELLOW, "日期为空", "日期为空，需人工补充或标记无法解析，不自动删除", "manual"
        d = pd.to_datetime(s, errors="coerce")
        if pd.isna(d):
            return None, YELLOW, "日期无法解析", f"「{s}」无法解析为日期，需人工复核，不自动删除", "manual"
        tol = self.cfg["dates"]["future_tolerance_days"]
        if d > self.today + pd.Timedelta(days=tol):
            return str(d.date()), YELLOW, "未来日期", \
                f"「{s}」晚于当前日期，疑似未来日期，需人工复核，不自动删除", "manual"
        return str(d.date()), GREEN, "日期正常", "", "auto"

    # ---------- 扫描 ----------
    def scan(self, raw_df, name=""):
        """全量扫描：三色分流 + 黄区清单 + 红区清单 + 去重/极端金额/枚举规则"""
        cfg = self.cfg
        df = raw_df.dropna(how="all").reset_index(drop=True)
        mapping, enum_cols = self._map_roles(df.columns)
        amt_col = mapping.get("amount")
        date_col = mapping.get("date")
        pay_col = mapping.get("pay_date")
        status_col = mapping.get("status")
        remark_col = mapping.get("remark")
        order_col = mapping.get("order_id")
        cust_direct_col = mapping.get("customer")
        cust_chain = [(role, mapping[role]) for role in self._cust_priority if mapping.get(role)]

        rows, valid_amts, order_groups = [], [], {}
        for i in range(len(df)):
            r = {"idx": i,
                 "cells": {c: _fmt_cell(df.at[i, c]) for c in df.columns},
                 "status": GREEN, "yellows": [], "reds": [],
                 "dedup_of": None, "remove_confirmed": False, "_amt_std": None}
            # ---- 金额 ----
            if amt_col:
                std, level, rule, note, conf = self._norm_amount(df.at[i, amt_col])
                if level == GREEN:
                    r["_amt_std"] = std
                    if std is not None and std > 0:
                        valid_amts.append(std)
                else:
                    r["yellows"].append({"field": amt_col, "original": r["cells"].get(amt_col, ""),
                                         "suggested": std, "rule": rule, "confidence": conf,
                                         "note": note, "resolved": None})
                    if std is not None:
                        r["_amt_std"] = std
            # ---- 日期 ----
            if date_col:
                std, level, rule, note, conf = self._norm_date(df.at[i, date_col])
                if level != GREEN:
                    r["yellows"].append({"field": date_col, "original": r["cells"].get(date_col, ""),
                                         "suggested": std, "rule": rule, "confidence": conf,
                                         "note": note, "resolved": None})
            # ---- 支付早于下单 ----
            if pay_col and date_col and cfg["dates"].get("check_pay_before_order", True):
                pd_ = pd.to_datetime(df.at[i, pay_col], errors="coerce")
                od_ = pd.to_datetime(df.at[i, date_col], errors="coerce")
                if pd.notna(pd_) and pd.notna(od_) and pd_ < od_:
                    r["yellows"].append({"field": pay_col, "original": r["cells"].get(pay_col, ""),
                                         "suggested": None, "rule": "支付早于下单",
                                         "confidence": "manual",
                                         "note": f"支付时间 {pd_.date()} 早于下单时间 {od_.date()}，需人工复核，不自动删除",
                                         "resolved": None})
            # ---- 退款/取消状态 ----
            if status_col:
                sv = str(df.at[i, status_col]).strip()
                for kw in cfg["refund"]["refund_keywords"]:
                    if kw.lower() in sv.lower():
                        r["yellows"].append({"field": status_col, "original": r["cells"].get(status_col, ""),
                                             "suggested": sv, "rule": "退款/取消单",
                                             "confidence": "manual",
                                             "note": f"订单状态命中「{kw}」：按 RFM 口径标记，R 默认不算有效购买、F 计频次可配置、M 取净额",
                                             "resolved": None})
                        break
            # ---- 测试单/内部单（红色剔除候选）----
            test_hit = None
            fields_to_check = [c for c in (remark_col, status_col, order_col) if c]
            if not fields_to_check:
                fields_to_check = [c for c in df.columns if df[c].dtype == object]
            for c in fields_to_check:
                sv = str(df.at[i, c]).strip()
                for kw in cfg["test_orders"]["keywords"]:
                    if kw.lower() in sv.lower():
                        test_hit = kw
                        break
                if test_hit:
                    break
            if test_hit:
                r["reds"].append(f"测试单/内部单：命中关键词「{test_hit}」，按规则剔除（记录行数与影响评估）")
            # ---- 客户ID：客户ID列优先，否则 会员ID > 手机号 > 匿名ID；全空 → 无法归属 ----
            has_cust = False
            if cust_direct_col:
                v = str(df.at[i, cust_direct_col]).strip()
                if v and v.lower() not in _EMPTY_VALS:
                    has_cust = True
            if not has_cust:
                for _role, col in cust_chain:
                    v = str(df.at[i, col]).strip()
                    if v and v.lower() not in _EMPTY_VALS:
                        has_cust = True
                        break
            if not has_cust:
                r["yellows"].append({"field": "__客户归属__", "original": "", "suggested": None,
                                     "rule": "无法归属", "confidence": "manual",
                                     "note": cfg["customer_id"]["unattributable_note"], "resolved": None})
            # ---- 枚举别名/格式 ----
            for gname, col, amap in enum_cols:
                sv = str(df.at[i, col]).strip()
                if not sv:
                    continue
                key = sv.lower()
                if key in amap:
                    r["yellows"].append({"field": col, "original": sv, "suggested": amap[key],
                                         "rule": "枚举别名统一", "confidence": "auto",
                                         "note": f"「{sv}」按「{gname}」别名表映射为「{amap[key]}」，原始值保留",
                                         "resolved": None})
                elif sv != str(df.at[i, col]) and str(df.at[i, col]) != str(df.at[i, col]).strip():
                    r["yellows"].append({"field": col, "original": sv, "suggested": sv,
                                         "rule": "枚举格式修正", "confidence": "auto",
                                         "note": f"「{sv}」首尾含空白，建议修正为「{sv}」",
                                         "resolved": None})
            # ---- 订单ID收集（去重用）----
            if order_col:
                oid = str(df.at[i, order_col]).strip()
                if oid and oid.lower() not in _EMPTY_VALS:
                    order_groups.setdefault(oid, []).append(i)
            rows.append(r)

        # ---- 极端金额（P99×k，仅标记不删除）----
        if valid_amts and amt_col:
            ext_cfg = cfg["thresholds"]["extreme_amount"]
            p99 = float(np.percentile(valid_amts, 99))
            th = p99 * ext_cfg["p99_multiplier"]
            for r in rows:
                if r["reds"] or r["_amt_std"] is None:
                    continue
                if r["_amt_std"] > th:
                    r["yellows"].append({"field": amt_col, "original": r["cells"].get(amt_col, ""),
                                         "suggested": r["_amt_std"], "rule": "极端金额复核",
                                         "confidence": "manual",
                                         "note": f"金额 {r['_amt_std']:,.2f} 元 > P99({p99:,.2f} 元)×{ext_cfg['p99_multiplier']}，"
                                                 f"电商长尾明显，仅标记人工复核，不自动删除",
                                         "resolved": None})
        # ---- 去重：按订单ID保留最新一条 ----
        for oid, idxs in order_groups.items():
            if len(idxs) <= 1:
                continue
            if date_col:
                try:
                    ds = pd.to_datetime([str(df.at[i, date_col]) for i in idxs], errors="coerce")
                    keep = idxs[int(np.argmax(ds))] if ds.notna().all() else idxs[-1]
                except Exception:
                    keep = idxs[-1]
            else:
                keep = idxs[-1]
            for j in idxs:
                if j == keep:
                    continue
                rows[j]["reds"].append(f"重复订单：订单ID「{oid}」与第 {keep + 1} 行重复，按规则保留最新一条，本行剔除")
                rows[j]["dedup_of"] = keep
        # ---- 行状态与统计 ----
        stats = {"total": len(rows), "green": 0, "yellow": 0, "red": 0}
        for r in rows:
            r["status"] = RED if r["reds"] else (YELLOW if r["yellows"] else GREEN)
            stats[r["status"]] += 1
        return ScanResult(df.columns, mapping, self.version, rows, stats, name)


def load_engine(config_path=CONFIG_PATH, db_path=DB_PATH):
    """工厂：加载规则与存储（dashboard_app 使用）"""
    return CleanEngine(config_path=config_path, store=CleanStore(db_path=db_path))
