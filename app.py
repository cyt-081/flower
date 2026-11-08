# -*- coding: utf-8 -*-
"""校园失物招领 · 云端版后端（Flask）。

职责分工（这是本项目的核心设计）：
  登记  POST /api/register  -> 调智谱 GLM 把杂乱自然语言抽成结构化字段，落盘 JSON（慢，几秒）
  查询  GET/POST /api/query -> 纯 Python 内存关键词打分，不碰大模型（快，几毫秒）

本地跑：python app.py        生产跑：gunicorn app:app
"""
import os
import re
import threading
import time

from flask import Flask, jsonify, render_template, request

import matcher
import zhipu_client
from store import Store

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_env_file():
    """本地开发读 .env；云平台直接用环境变量，已存在的变量优先，不会被覆盖。"""
    try:
        with open(os.path.join(BASE_DIR, ".env"), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, value = line.split("=", 1)
                name = name.strip()
                value = value.strip().strip('"').strip("'")
                if name and name not in os.environ:
                    os.environ[name] = value
    except FileNotFoundError:
        pass


load_env_file()
app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

store = Store()
MODEL = os.environ.get("ZHIPU_MODEL", "glm-4-flash")
ALLOW_CORS = os.environ.get("ALLOW_CORS", "1") == "1"
MAX_TEXT = int(os.environ.get("MAX_TEXT", "600"))
RATE_LIMIT = int(os.environ.get("RATE_LIMIT_PER_MIN", "12"))
STARTED_AT = time.time()

_index_rows = []
_index_count = -1
_index_lock = threading.Lock()
_rate_lock = threading.Lock()
_rate_hits = {}


def index_rows():
    """索引只在数据变化时重建，查询走内存。"""
    global _index_rows, _index_count
    with _index_lock:
        store.maybe_reload()
        if _index_count != store.count():
            _index_rows = matcher.build_index(store.all())
            _index_count = store.count()
        return _index_rows


def rate_ok(ip, limit=None, window=60):
    limit = limit or RATE_LIMIT
    now = time.time()
    with _rate_lock:
        bucket = _rate_hits.setdefault(ip, [])
        bucket[:] = [t for t in bucket if now - t < window]
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True


def mask_contact(text):
    """联系方式一律脱敏后再展示：手机留前 3 后 2，其它只留首字。"""
    text = (text or "").strip()
    if not text:
        return ""
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 8:
        return digits[:3] + "****" + digits[-4:]
    if len(text) == 1:
        return text + "**"
    return text[0] + "***"


def build_detail(fields):
    parts = []
    if fields.get("place"):
        parts.append("地点：%s" % fields["place"])
    if fields.get("features"):
        parts.append("特征：%s" % "、".join(fields["features"]))
    contact = mask_contact(fields.get("contact"))
    parts.append("联系：%s" % contact if contact else "联系：通过平台留言")
    return " · ".join(parts)


@app.after_request
def add_headers(resp):
    if ALLOW_CORS:
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


@app.route("/")
def home():
    return render_template("index.html", model=MODEL, llm_ready=bool(zhipu_client.api_key()), items=store.count())


@app.route("/api/health")
def health():
    return jsonify({
        "ok": True,
        "service": "campus-lostfound",
        "items": store.count(),
        "model": MODEL,
        "llm_ready": bool(zhipu_client.api_key()),
        "uptime_sec": round(time.time() - STARTED_AT, 1),
    })


@app.route("/api/register", methods=["POST", "OPTIONS"])
def register():
    if request.method == "OPTIONS":
        return ("", 204)
    ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "?").split(",")[0].strip()
    if not rate_ok(ip):
        return jsonify({"ok": False, "error": "登记太频繁了，请稍后再试"}), 429

    payload = request.get_json(silent=True) or {}
    text = (payload.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "请先写一段描述"}), 400
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT]

    started = time.time()
    fields, source = zhipu_client.extract(text)
    if payload.get("contact"):
        fields["contact"] = str(payload["contact"])[:40]
    fields["raw_text"] = text
    fields["detail"] = build_detail(fields)
    fields["extracted_by"] = source
    item = store.add(fields)
    index_rows()

    return jsonify({
        "ok": True,
        "item": item,
        "extracted_by": source,
        "llm_used": source == "llm",
        "cost_ms": round((time.time() - started) * 1000, 1),
    })


@app.route("/api/query", methods=["GET", "POST", "OPTIONS"])
def query():
    if request.method == "OPTIONS":
        return ("", 204)
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        q = (payload.get("q") or payload.get("query") or "").strip()
        limit = int(payload.get("limit") or 10)
    else:
        q = (request.args.get("q") or "").strip()
        limit = int(request.args.get("limit") or 10)
    limit = max(1, min(limit, 30))
    if not q:
        return jsonify({"ok": False, "error": "请输入要找的物品"}), 400

    started = time.time()
    rows = index_rows()
    cards = matcher.search(store.all(), rows, q, limit=limit)
    cost = round((time.time() - started) * 1000, 2)
    return jsonify({"ok": True, "query": q, "scanned": len(rows), "cost_ms": cost, "cards": cards})


@app.route("/api/items")
def items():
    limit = max(1, min(int(request.args.get("limit") or 20), 100))
    rows = sorted(store.all(), key=lambda r: r.get("created_at", ""), reverse=True)[:limit]
    return jsonify({"ok": True, "total": store.count(), "items": rows})


@app.errorhandler(404)
def not_found(_error):
    return jsonify({"ok": False, "error": "not found"}), 404


@app.errorhandler(500)
def server_error(_error):
    return jsonify({"ok": False, "error": "服务器内部错误"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
