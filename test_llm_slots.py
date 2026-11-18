# -*- coding: utf-8 -*-
"""⑨ 本地 LLM 意图解析 · 单元测试（不依赖 Flask、不联网）

覆盖：
  A. 地址白名单（SSRF）
  B. HTTP 层：不走代理、不跟随跳转、体积上限、超时
  C. schema 校验：非法槽位一律拒绝（验收第 3 条）
  D. 端到端：LLM 可用时走 preslots 聚合；不可用/非法时回退规则并标注
  E. 槽位注入：未知键、额外字段不会进入聚合
运行：python test_llm_slots.py
"""
import io
import json
import socket
import sys
import unittest
import urllib.error
import urllib.request

import pandas as pd

import ask_parser
import llm_slots as L

VOCAB = {"segments": ["高价值客户", "大众客户", "潜力客户", "沉睡客户"],
         "categories": ["家居生活", "服饰鞋包", "数码电子", "食品生鲜", "美妆护肤"],
         "strategies": ["大水漫灌", "精准滴灌"]}


def _df():
    rows = []
    for i in range(40):
        y, m = 2025, (i % 12) + 1
        rows.append({"TransactionID": f"T{i:04d}", "CustomerKey": 1000 + i,
                     "Year": y, "Month": m, "YearMonth": f"{y}-{m:02d}",
                     "Category": VOCAB["categories"][i % 5],
                     "TotalAmount": 100.0 + i * 10, "Quantity": 1})
    return pd.DataFrame(rows)


def _rfm():
    segs = VOCAB["segments"]
    return pd.DataFrame([{"CustomerKey": 1000 + i, "CustomerID": f"C{i:04d}",
                          "RFM_Segment": segs[i % 4], "CustomerTier": segs[i % 4],
                          "Gender": "男" if i % 2 else "女", "F": (i % 5) + 1,
                          "IsAwakened": bool(i % 3 == 0)} for i in range(40)])


def _camp():
    return pd.DataFrame([{"CampaignName": "春促", "StrategyGroup": "精准滴灌",
                          "StartDate": "2025-03-01", "Year": 2025,
                          "Cost": 10000.0, "Revenue": 30000.0},
                         {"CampaignName": "夏促", "StrategyGroup": "大水漫灌",
                          "StartDate": "2025-06-01", "Year": 2025,
                          "Cost": 20000.0, "Revenue": 22000.0}])


class A_UrlWhitelist(unittest.TestCase):
    def test_loopback_ok(self):
        for u in ("http://127.0.0.1:11434", "http://localhost:11434",
                  "http://[::1]:11434", "https://127.0.0.1:11434/"):
            self.assertTrue(L.url_check(u)[0], u)

    def test_public_and_sneaky_rejected(self):
        bad = ["http://8.8.8.8:11434", "http://api.openai.com", "https://evil.com/ollama",
               "http://127.0.0.1@evil.com:11434", "http://user:pw@127.0.0.1:11434",
               "file:///etc/passwd", "gopher://127.0.0.1:11434",
               "http://127.0.0.1:11434/?x=1", "ftp://127.0.0.1", "", None, 123,
               "http://127.0.0.1:99999", "http://sub.127.0.0.1.nip.io:11434"]
        for u in bad:
            ok, why = L.url_check(u)
            self.assertFalse(ok, f"应拒绝但通过了: {u!r}")
            self.assertTrue(why)

    def test_env_proxy_ignored(self):
        """opener 必须显式用空 ProxyHandler 构造：否则环境里的 HTTP_PROXY 会把
        本机请求转去公网（沙箱里 HTTP_PROXY 确实存在，这条不是假想风险）。"""
        import inspect
        self.assertIn("ProxyHandler({})", inspect.getsource(L._build_opener))
        op = L._build_opener()
        non_empty = [h for h in op.handlers
                     if getattr(h, "proxies", None)]
        self.assertEqual(non_empty, [], "存在带代理的 handler")
        # 空的 ProxyHandler 不会注册 http_open，绝无代理转发
        self.assertFalse(any(getattr(h, "http_open", None) is not None
                             and type(h).__name__ == "ProxyHandler" for h in op.handlers))

    def test_redirect_handler_disabled(self):
        h = [h for h in L._build_opener().handlers
             if isinstance(h, urllib.request.HTTPRedirectHandler)]
        self.assertTrue(h)
        self.assertIsNone(h[0].redirect_request(None, None, 302, "Found", {}, "http://evil.com"))


class B_HttpGuards(unittest.TestCase):
    def setUp(self):
        self._orig = L.OLLAMA_URL

    def tearDown(self):
        L.OLLAMA_URL = self._orig

    def test_unreachable_returns_unavailable(self):
        L.OLLAMA_URL = "http://127.0.0.1:1"      # 必然拒绝连接
        with self.assertRaises(L.LlmUnavailable):
            L._http_json("GET", "/api/tags", None, 0.4)

    def test_bad_url_raises_before_connect(self):
        L.OLLAMA_URL = "http://example.com:11434"
        with self.assertRaises(L.LlmUnavailable) as c:
            L._http_json("GET", "/api/tags", None, 0.4)
        self.assertIn("回环", str(c.exception))

    def test_timeout_is_short_and_typed(self):
        self.assertLessEqual(L.LLM_TIMEOUT, 3.0)

    def test_oversize_response_rejected(self):
        """伪造一个返回超长响应体的 opener：必须被体积上限拦下"""
        big = b'{"a":"' + b"x" * (L.MAX_RESP_BYTES + 10) + b'"}'

        class _R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.close()

        class _O:
            def open(self, req, timeout=None):
                return _R(big)

        orig = L._OPENER
        L._OPENER = _O()
        try:
            with self.assertRaises(L.LlmUnavailable) as c:
                L._http_json("GET", "/api/tags", None, 0.4)
            self.assertIn("超过上限", str(c.exception))
        finally:
            L._OPENER = orig

    def test_oversize_payload_rejected(self):
        with self.assertRaises(L.LlmUnavailable):
            L._http_json("POST", "/api/chat", {"x": "y" * (L.MAX_REQ_BYTES + 1)}, 0.4)


class C_SchemaValidation(unittest.TestCase):
    def v(self, raw):
        return L.validate_slots(raw, VOCAB)

    def test_valid_passes(self):
        s, why = self.v({"year": 2025, "month": 3, "seg": "高价值客户", "metric": "sales"})
        self.assertIsNotNone(s, why)
        self.assertEqual((s["year"], s["month"], s["seg"], s["metric"]),
                         (2025, 3, "高价值客户", "sales"))

    def test_alias_and_type_coercion(self):
        s, why = self.v({"year": "2025", "month": "3", "seg": "高价值",
                         "category": "服饰", "metric": "销售额", "topn": "3"})
        self.assertIsNotNone(s, why)
        self.assertEqual((s["seg"], s["category"], s["metric"], s["topn"]),
                         ("高价值客户", "服饰鞋包", "sales", 3))

    def test_illegal_seg_rejected(self):
        """验收第 3 条：模型编造客群 → 必须被拦下"""
        s, why = self.v({"seg": "超级无敌VIP客户", "metric": "sales"})
        self.assertIsNone(s)
        self.assertIn("seg", why)

    def test_illegal_values_rejected(self):
        cases = [
            {"metric": "profit"},
            {"metric": "销售额; DROP TABLE"},
            {"dim": "province"},
            {"order": "random"},
            {"gender": "其他"},
            {"category": "不存在品类"},
            {"strat": "随便投"},
            {"year": 1800},
            {"year": "2025年3月"},
            {"month": 13},
            {"month": 0},
            {"topn": 0},
            {"topn": 999},
            {"topn": 2.5},
            {"latest": "maybe"},
            {"year": True},
        ]
        for raw in cases:
            s, why = self.v(raw)
            self.assertIsNone(s, f"应拒绝: {raw}")
            self.assertTrue(why)

    def test_not_object_rejected(self):
        for raw in (["2025"], "2025年3月销售额", 42, None):
            s, why = self.v(raw)
            self.assertIsNone(s)

    def test_empty_slots_rejected(self):
        s, why = self.v({"year": None, "month": None})
        self.assertIsNone(s)
        self.assertIn("任何可用槽位", why)

    def test_too_many_keys_rejected(self):
        s, why = self.v({f"k{i}": 1 for i in range(30)})
        self.assertIsNone(s)

    def test_latest_zeroes_time(self):
        s, _ = self.v({"latest": True, "year": 2020, "metric": "sales"})
        self.assertTrue(s["latest"])
        self.assertIsNone(s["year"])


class D_EndToEndFallback(unittest.TestCase):
    """不真的连 Ollama：替换 _http_json 观察两条路径的差异"""

    def setUp(self):
        self.df, self.rfm, self.camp = _df(), _rfm(), _camp()
        self._orig_post = L._http_json
        self._orig_state = dict(L._STATE)
        L.set_enabled(True)
        L._clear_trip()

    def tearDown(self):
        L._http_json = self._orig_post
        L.set_enabled(False)
        L._STATE.update(self._orig_state)
        L._clear_trip()

    def _fake(self, content):
        def f(method, path, payload=None, timeout=None):
            if isinstance(content, Exception):
                raise content
            return {"message": {"role": "assistant", "content": content}}
        return f

    def test_llm_path_uses_preslots_same_numbers(self):
        L._http_json = self._fake(json.dumps(
            {"year": 2025, "month": 3, "metric": "sales"}))
        a = L.ask("三月卖了多少", self.df, self.rfm, self.camp, None)
        b = ask_parser.answer("2025年3月销售额", self.df, self.rfm, self.camp, None)
        self.assertEqual(a["parse_mode"], "llm")
        self.assertEqual(a["value"], b["value"])
        self.assertNotIn("（规则解析）", a["answer"])

    def test_illegal_slot_falls_back_with_marker(self):
        L._http_json = self._fake(json.dumps({"seg": "不存在的客群", "metric": "sales"}))
        a = L.ask("2025年3月销售额", self.df, self.rfm, self.camp, None)
        self.assertEqual(a["parse_mode"], "rule-fallback")
        self.assertTrue(a["answer"].endswith("（规则解析）"))
        self.assertIn("seg", a["parse_note"])

    def test_timeout_falls_back(self):
        L._http_json = self._fake(socket.timeout("timed out"))
        a = L.ask("2025年3月销售额", self.df, self.rfm, self.camp, None)
        self.assertEqual(a["parse_mode"], "rule-fallback")
        self.assertTrue(a["answer"].endswith("（规则解析）"))

    def test_unreachable_falls_back(self):
        L._http_json = self._fake(urllib.error.URLError("refused"))
        a = L.ask("2025年3月销售额", self.df, self.rfm, self.camp, None)
        self.assertEqual(a["parse_mode"], "rule-fallback")

    def test_garbage_content_falls_back(self):
        for c in ("我不知道", "<html>502</html>", "{不是json}", "[1,2,3]"):
            L._clear_trip()
            L._http_json = self._fake(c)
            a = L.ask("2025年3月销售额", self.df, self.rfm, self.camp, None)
            self.assertEqual(a["parse_mode"], "rule-fallback", c)

    def test_fenced_json_accepted(self):
        L._http_json = self._fake('```json\n{"year":2025,"metric":"sales"}\n```')
        a = L.ask("2025年销售额", self.df, self.rfm, self.camp, None)
        self.assertEqual(a["parse_mode"], "llm")

    def test_extra_keys_never_reach_aggregation(self):
        """注入：多给的键（sql/eval/function）不得进入槽位"""
        L._http_json = self._fake(json.dumps(
            {"year": 2025, "metric": "sales", "__class__": "x", "sql": "drop", "eval": "1"}))
        a = L.ask("2025年销售额", self.df, self.rfm, self.camp, None)
        self.assertEqual(a["parse_mode"], "llm")
        self.assertNotIn("sql", a["slots"])
        self.assertNotIn("eval", a["slots"])

    def test_breaker_makes_fallback_instant(self):
        """连接失败后 30 秒内不再重试：第二题不应再付出连接耗时"""
        calls = {"n": 0}

        def boom(method, path, payload=None, timeout=None):
            calls["n"] += 1
            raise L.LlmUnavailable("本地 LLM 未运行或不可达")

        L._http_json = boom
        a1 = L.ask("2025年3月销售额", self.df, self.rfm, self.camp, None)
        a2 = L.ask("2025年3月销售额", self.df, self.rfm, self.camp, None)
        self.assertEqual(calls["n"], 1, "熔断后仍在重试")
        self.assertEqual(a1["parse_mode"], "rule-fallback")
        self.assertEqual(a2["parse_mode"], "rule-fallback")
        self.assertIn("秒内不再重试", a2["parse_note"])

    def test_breaker_recovers(self):
        L._http_json = self._fake(L.LlmUnavailable("down"))
        L.ask("2025年销售额", self.df, self.rfm, self.camp, None)
        self.assertTrue(L.cooldown_left()[0] > 0)
        L._clear_trip()
        L._http_json = self._fake(json.dumps({"year": 2025, "metric": "sales"}))
        a = L.ask("2025年销售额", self.df, self.rfm, self.camp, None)
        self.assertEqual(a["parse_mode"], "llm")


class E_SwitchSemantics(unittest.TestCase):
    def test_default_off(self):
        self.assertFalse(L.ENABLE_LLM)
        self.assertFalse(L.is_enabled())

    def test_toggle_roundtrip(self):
        L.set_enabled(True)
        self.assertTrue(L.is_enabled())
        L.set_enabled(False)
        self.assertFalse(L.is_enabled())

    def test_status_shape(self):
        st = L.status()
        for k in ("enabled", "locked", "model", "url_ok", "available", "reason"):
            self.assertIn(k, st)
        self.assertFalse(st["enabled"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
