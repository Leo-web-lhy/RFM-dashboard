# -*- coding: utf-8 -*-
"""parse_tabular 边界场景测试（pytest）。

全部用例使用 tmp_path / 内联样例数据生成，不依赖任何本地文件。
运行：pytest -q test_parse_tabular.py
"""
import os
import sys
import io

import pytest
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dashboard_app as m


def _xlsx_bytes(tmp_path):
    """生成一份真实 xlsx（ZIP 魔数）并返回其字节"""
    p = tmp_path / "sample.xlsx"
    pd.DataFrame({
        "日期": ["2026-01-01", "2026-01-02"],
        "品类": ["智能硬件", "美妆个护"],
        "金额": [12800, 9900],
        "数量": [2, 3],
    }).to_excel(p, index=False)
    return p.read_bytes()


def test_xlsx_disguised_as_csv(tmp_path):
    """① xlsx 伪装 .csv：按内容（魔数）识别为 Excel，正常解析而非天书报错"""
    df = m.parse_tabular(_xlsx_bytes(tmp_path), "改名.csv")
    assert list(df.columns) == ["日期", "品类", "金额", "数量"]
    assert df["金额"].tolist() == [12800, 9900]


def test_real_csv_large(tmp_path):
    """② 真 CSV（内联生成 200 行）：行数、列名、首行值正确"""
    rows = ["日期,品类,商品,客户ID,金额,数量,备注"]
    for i in range(1, 201):
        rows.append(f"2026-01-{(i % 28) + 1:02d},智能硬件,无人机Pro,CUST-{i},{12800 + i},2,正常")
    df = m.parse_tabular(("\r\n".join(rows)).encode("utf-8"), "large.csv")
    assert df.shape == (200, 7)
    assert list(df.columns)[:4] == ["日期", "品类", "商品", "客户ID"]
    assert df.iloc[0]["客户ID"] == "CUST-1"


def test_small_csv(tmp_path):
    """③ 小 CSV：值逐项正确"""
    df = m.parse_tabular("客户ID,金额\nC-1,100\nC-2,250\n".encode("utf-8"), "small.csv")
    assert df.shape == (2, 2)
    assert df["客户ID"].tolist() == ["C-1", "C-2"]
    assert df["金额"].tolist() == [100, 250]


def test_gbk_csv(tmp_path):
    """④ GBK 编码：解码正常，中文列名与值可用"""
    buf = io.StringIO()
    buf.write("日期,品类,商品,客户ID,金额,数量,备注\r\n"
              "2026-01-05,智能硬件,无人机Pro,CUST-99,12800,2,正常\r\n")
    df = m.parse_tabular(buf.getvalue().encode("gb18030"), "gbk.csv")
    assert list(df.columns) == ["日期", "品类", "商品", "客户ID", "金额", "数量", "备注"]
    assert df.iloc[0]["品类"] == "智能硬件"
    assert int(df.iloc[0]["金额"]) == 12800


def test_binary_junk(tmp_path):
    """⑤ 二进制垃圾伪装 .csv：明确提示二进制数据并抛 ValueError"""
    with pytest.raises(ValueError, match="二进制"):
        m.parse_tabular(bytes(range(256)) * 8, "junk.csv")


def test_pasted_text(tmp_path):
    """⑥ 粘贴文本（无文件名）：正常解析"""
    df = m.parse_tabular("客户ID,金额\nC-1,100\nC-2,250\n".encode("utf-8"), "")
    assert df.shape == (2, 2)
    assert df["金额"].tolist() == [100, 250]


def test_ragged_rows(tmp_path):
    """⑦ 列数不齐：容错解析不抛异常，缺列填 NaN"""
    bad = "a,b,c\n1,2,3\n4,5\n6,7,8\n9,10,11\n".encode("utf-8")
    df = m.parse_tabular(bad, "bad.csv")
    assert df.shape == (4, 3)
    assert df["c"].isna().sum() == 1


def test_xlsx_normal_suffix(tmp_path):
    """⑧ 真 xlsx 正常后缀：按 Excel 解析，结果与①一致"""
    df = m.parse_tabular(_xlsx_bytes(tmp_path), "正常.xlsx")
    assert list(df.columns) == ["日期", "品类", "金额", "数量"]
    assert df["金额"].tolist() == [12800, 9900]


def test_empty_file(tmp_path):
    """⑨ 空文件：提示内容为空并抛 ValueError"""
    with pytest.raises(ValueError, match="为空"):
        m.parse_tabular(b"", "empty.csv")
