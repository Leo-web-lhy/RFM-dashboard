# -*- coding: utf-8 -*-
"""
RFM 大屏 - 登录鉴权模块（飞书 / 钉钉 / 微信 扫码登录）

设计要点
--------
1. 三通道登录入口，默认 demo（演示扫码）模式：
   - demo 模式：二维码为本地模拟（前端按 ticket 哈希绘制伪二维码），
     点击「模拟手机扫码」即完成登录。零外部依赖、离线可用。
   - oauth 模式（预留）：在 login_config.json 中填入企业开放平台的
     app_id / app_secret 并将 mode 改为 "oauth" 后，方可对接真实扫码。
     真实对接还需要公网回调地址（内网穿透或公网服务器），未配置时自动
     回退 demo 模式，避免大屏无法登录。
2. 会话：Flask 签名 Cookie（HttpOnly + SameSite=Lax），密钥持久化在
   .login_secret 文件（首启自动生成），重启后旧会话仍然有效。
3. 防护：
   - 未登录访问页面 → 302 跳 /login；未登录访问 /api/* → 401 JSON；
   - /api/llm/toggle 的「仅本机 + 专属请求头」二次校验不受影响；
   - ticket 一次性、120 秒过期、用后即焚，并发加锁；
   - 登录态有效期默认 12 小时（login_config.json 可改）。
4. 本模块通过 init_login(app) 一行接入主程序，不改动原有业务路由。
"""

import os
import json
import time
import secrets
import threading
from datetime import timedelta
from urllib.parse import quote

from flask import (session, redirect, jsonify, request,
                   render_template, render_template_string)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "login_config.json")
SECRET_FILE = os.path.join(BASE_DIR, ".login_secret")

DEFAULT_CONFIG = {
    "session_hours": 12,          # 登录态有效期（小时）
    "qr_ttl_seconds": 120,        # 二维码有效期（秒）
    "channels": {
        "feishu":   {"name": "飞书", "color": "#3370FF", "enabled": True,
                      "mode": "demo", "app_id": "", "app_secret": ""},
        "dingtalk": {"name": "钉钉", "color": "#1677FF", "enabled": True,
                      "mode": "demo", "app_id": "", "app_secret": ""},
        "wechat":   {"name": "微信", "color": "#07C160", "enabled": True,
                      "mode": "demo", "app_id": "", "app_secret": ""}
    }
}

# ---------------- 配置加载 ----------------
def load_config():
    """读取 login_config.json；缺失/损坏时用默认配置并落盘，保证不因配置问题起不来"""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            if isinstance(user_cfg, dict):
                for k in ("session_hours", "qr_ttl_seconds"):
                    if isinstance(user_cfg.get(k), (int, float)) and user_cfg[k] > 0:
                        cfg[k] = user_cfg[k]
                uc = user_cfg.get("channels")
                if isinstance(uc, dict):
                    for key, ch in cfg["channels"].items():
                        if key in uc and isinstance(uc[key], dict):
                            ch.update({k2: v2 for k2, v2 in uc[key].items()
                                       if k2 in ch})
        except Exception as e:
            print(f"[login] login_config.json 读取失败，使用默认配置: {e}")
    else:
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    return cfg


def _load_secret():
    """会话签名密钥：首次启动自动生成并持久化，重启后会话不失效"""
    try:
        with open(SECRET_FILE, "r", encoding="utf-8") as f:
            s = f.read().strip()
        if len(s) >= 32:
            return s
    except Exception:
        pass
    s = secrets.token_hex(32)
    try:
        with open(SECRET_FILE, "w", encoding="utf-8") as f:
            f.write(s)
    except Exception as e:
        print(f"[login] .login_secret 写入失败（每次重启需重新登录）: {e}")
    return s


# ---------------- 扫码 ticket 状态机 ----------------
# ticket -> {"status": waiting/scanned/confirmed, "channel":..., "ts":..., "user":...}
_TICKETS = {}
_TICKET_LOCK = threading.Lock()
CFG = load_config()

CHANNEL_USER = {  # demo 模式下各通道的模拟登录身份
    "feishu":   {"name": "飞书用户",  "uid": "fs_demo"},
    "dingtalk": {"name": "钉钉用户",  "uid": "dt_demo"},
    "wechat":   {"name": "微信用户",  "uid": "wx_demo"},
}


def _channel_ok(key):
    ch = CFG["channels"].get(key)
    return bool(ch and ch.get("enabled"))


def _new_ticket(channel):
    """生成一次性扫码 ticket；顺带清理过期票据"""
    now = time.time()
    with _TICKET_LOCK:
        for t in [k for k, v in _TICKETS.items()
                  if now - v["ts"] > CFG["qr_ttl_seconds"] or v["status"] == "confirmed"]:
            _TICKETS.pop(t, None)
        tk = secrets.token_urlsafe(24)
        _TICKETS[tk] = {"status": "waiting", "channel": channel, "ts": now, "user": None}
        return tk, int(now + CFG["qr_ttl_seconds"])


def _ticket_status(tk):
    with _TICKET_LOCK:
        v = _TICKETS.get(tk)
        if not v:
            return None
        if v["status"] != "confirmed" and time.time() - v["ts"] > CFG["qr_ttl_seconds"]:
            v["status"] = "expired"
        return {"status": v["status"], "channel": v["channel"],
                "user": v["user"] if v["status"] == "confirmed" else None}


def _confirm_ticket(tk):
    """demo 模式：模拟手机扫码确认，票据置为 confirmed 并绑定身份"""
    with _TICKET_LOCK:
        v = _TICKETS.get(tk)
        if not v or v["status"] == "confirmed":
            return None
        if time.time() - v["ts"] > CFG["qr_ttl_seconds"]:
            v["status"] = "expired"
            return {"status": "expired"}
        ch = v["channel"]
        u = dict(CHANNEL_USER.get(ch, {"name": "访客", "uid": "guest"}))
        u["channel"] = ch
        v["status"] = "confirmed"
        v["user"] = u
        return {"status": "confirmed", "user": u}


# ---------------- 会话工具 ----------------
def _session_valid(user):
    if not user or "t" not in user:
        return False
    ttl = CFG["session_hours"] * 3600
    return time.time() - user.get("t", 0) <= ttl


def _do_login(user):
    session.clear()
    session["user"] = dict(user, t=time.time())
    session.permanent = True


# ---------------- Flask 接入 ----------------
def init_login(app):
    app.secret_key = _load_secret()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=CFG["session_hours"]),
    )

    _WHITELIST_EXACT = {"/login", "/favicon.ico", "/logout", "/api/logout"}
    _WHITELIST_PREFIX = ("/api/login", "/static")

    @app.route("/login")
    def login_page():
        if _session_valid(session.get("user")):
            return redirect("/")
        # 通道卡服务端渲染：即使 JS 未执行（慢网络/降级环境）也能看到入口
        chs = [{"key": k, "name": v["name"], "color": v["color"]}
               for k, v in CFG["channels"].items() if v.get("enabled")]
        return render_template("login.html", channels=chs,
                               qr_ttl=int(CFG["qr_ttl_seconds"]))

    @app.route("/api/login/channels")
    def login_channels():
        out = []
        for key, ch in CFG["channels"].items():
            if ch.get("enabled"):
                out.append({"key": key, "name": ch["name"],
                            "color": ch["color"],
                            "mode": ch.get("mode", "demo")})
        return jsonify({"channels": out,
                        "qr_ttl": CFG["qr_ttl_seconds"]})

    @app.route("/api/login/qr", methods=["POST"])
    def login_qr_new():
        body = request.get_json(silent=True) or {}
        ch = str(body.get("channel", ""))
        if not _channel_ok(ch):
            return jsonify({"ok": False, "error": "登录通道不可用"}), 400
        tk, expire = _new_ticket(ch)
        return jsonify({"ok": True, "ticket": tk, "expire": expire,
                        "status": "waiting"})

    @app.route("/api/login/qr/status")
    def login_qr_status():
        tk = request.args.get("ticket", "")
        st = _ticket_status(tk) if tk else None
        if st is None:
            return jsonify({"ok": False, "error": "ticket 无效"}), 404
        # confirmed 时顺带建立会话（同源轮询，Cookie 直接生效）
        if st["status"] == "confirmed" and st.get("user") and not _session_valid(session.get("user")):
            _do_login(st["user"])
        return jsonify({"ok": True, **st})

    @app.route("/api/login/qr/confirm", methods=["POST"])
    def login_qr_confirm():
        """demo 模式专用：模拟手机端扫码确认。oauth 模式下由开放平台回调完成。"""
        body = request.get_json(silent=True) or {}
        tk = str(body.get("ticket", ""))
        if not tk:
            return jsonify({"ok": False, "error": "缺少 ticket"}), 400
        st = _confirm_ticket(tk)
        if st is None:
            return jsonify({"ok": False, "error": "ticket 无效或已确认"}), 404
        if st["status"] == "expired":
            return jsonify({"ok": False, "error": "二维码已过期，请刷新"}), 410
        # 确认即登录：手机端「同意」的那一刻建立会话（不再依赖前端轮询）
        if st.get("user"):
            _do_login(st["user"])
        return jsonify({"ok": True, "status": "confirmed"})

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect("/login")

    @app.route("/api/logout", methods=["POST"])
    def logout_api():
        session.clear()
        return jsonify({"ok": True})

    @app.route("/api/login/me")
    def login_me():
        u = session.get("user")
        if _session_valid(u):
            return jsonify({"ok": True, "user": {"name": u["name"],
                           "channel": u["channel"], "t": u["t"]}})
        return jsonify({"ok": False, "user": None})

    # -------- 全局拦截：未登录一律挡在门外（这是接入的唯一生效点） --------
    @app.before_request
    def _login_guard():
        path = request.path
        if path in _WHITELIST_EXACT or path.startswith(_WHITELIST_PREFIX):
            if path == "/login" and _session_valid(session.get("user")):
                return redirect("/")
            return None
        if _session_valid(session.get("user")):
            return None
        # 登录态过期 / 不存在：页面请求 302，接口请求 401
        if path.startswith("/api/"):
            return jsonify({"ok": False, "error": "未登录或登录已过期",
                            "login": "/login"}), 401
        return redirect("/login?next=" + quote(path))
