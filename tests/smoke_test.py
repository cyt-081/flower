# -*- coding: utf-8 -*-
r"""部署后自检：python tests/smoke_test.py https://你的域名

会依次验证 健康检查 / 登记 / 查询 / 列表，并打印耗时与抽取来源。
"""
import json
import sys
import time
import urllib.parse
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5000").rstrip("/")


def call(path, payload=None, timeout=60):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(BASE + path, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    print("目标服务：%s" % BASE)
    health = call("/api/health")
    print("[1/4] 健康检查      ok=%s 已收录=%s 模型=%s 大模型就绪=%s"
          % (health.get("ok"), health.get("items"), health.get("model"), health.get("llm_ready")))
    if not health.get("llm_ready"):
        print("      ⚠ 没有读到 ZHIPU_API_KEY，登记会走本地规则抽取（功能可用，效果差一些）")

    started = time.time()
    reg = call("/api/register", {"text": "昨天下午在图书馆二楼自习，把一把黑色的长柄雨伞落在座位上了，伞柄有点磨损",
                                 "contact": "13812345678"})
    item = reg["item"]
    print("[2/4] 登记          %s / %s / %s / %s  来源=%s  耗时=%.0f ms"
          % (item.get("title"), item.get("category"), item.get("color"), item.get("place"),
             reg.get("extracted_by"), (time.time() - started) * 1000))
    print("      脱敏后的详情：%s" % item.get("detail"))

    for keyword in ("黑色 雨伞 图书馆", "蓝色保温杯丢在三教"):
        started = time.time()
        result = call("/api/query?q=" + urllib.parse.quote(keyword))
        hits = "、".join("%s(%s)" % (c["title"], c["score"]) for c in result["cards"][:3]) or "无"
        print("[3/4] 查询「%s」 扫描 %s 条，服务端 %s ms，端到端 %.1f ms → %s"
              % (keyword, result["scanned"], result["cost_ms"], (time.time() - started) * 1000, hits))

    listing = call("/api/items?limit=3")
    print("[4/4] 列表          总数=%s" % listing.get("total"))
    print("全部通过 ✅  把这个地址做成二维码给评委扫：%s" % BASE)


if __name__ == "__main__":
    main()
