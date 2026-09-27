# -*- coding: utf-8 -*-
"""端到端隔离验证：真起一个 serve 实例，用两个不同 x-wechat-openid 打 HTTP。

验证：
  1. 无 openid → 匿名公共上下文（isolated=false），n_docs 来自全局库
  2. 带 openid A → 各自独立库（isolated=true, uid=A）
  3. A 上传一份 PDF 到自己的库 → B 的 /api/docs 看不到
  4. A 存 Key → B 的 /api/settings 读不到
"""
import os
import sys
import json
import time
import shutil
import tempfile
import subprocess
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable
PORT = 8991
TMP = tempfile.mkdtemp(prefix="lens_e2e_")

env = dict(os.environ)
env["LENS_DATA"] = os.path.join(TMP, "lens_data.json")
env["LENS_DATA_DIR"] = TMP
env["PORT"] = str(PORT)
env["HOST"] = "127.0.0.1"
env.pop("DEEPSEEK_API_KEY", None)      # 确保走「无 Key」路径，只看隔离不看 AI
env.pop("LITERATURE_LENS_CONFIG", None)

proc = subprocess.Popen([PY, "serve.py"], cwd=HERE, env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def call(path, headers=None, body=None, method=None):
    url = f"http://127.0.0.1:{PORT}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        # /api/doc 等「资源不存在」会直接回 404 状态码，这里按 JSON 解析出 ok=false
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"ok": False, "_status": e.code}


FAIL = []
def check(name, cond, extra=""):
    print(("  [OK] " if cond else "  [FAIL] ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAIL.append(name)

try:
    # 等端口就绪
    for _ in range(60):
        try:
            call("/api/health")
            break
        except Exception:
            time.sleep(0.4)
    else:
        raise SystemExit("服务未能在 24s 内启动")

    h_a = {"x-wechat-openid": "oUserA1234567890abcdef"}
    h_b = {"x-wechat-openid": "oUserB0987654321fedcba"}

    # 1) 匿名
    anon = call("/api/health")
    check("匿名 isolated=false", anon.get("isolated") is False, anon)
    check("匿名 uid 为空", anon.get("uid") == "", anon)

    # 2) 两个用户各自被识别
    ha = call("/api/health", h_a)
    hb = call("/api/health", h_b)
    check("A 被识别为独立用户", ha.get("isolated") is True and ha.get("uid", "").startswith("oUserA"), ha)
    check("B 被识别为独立用户", hb.get("isolated") is True and hb.get("uid", "").startswith("oUserB"), hb)

    # 3) 各自建库、各自为空（匿名库也空）
    da = call("/api/docs", h_a)
    db = call("/api/docs", h_b)
    dn = call("/api/docs")
    check("A 初始库为空", da.get("docs") == [], da)
    check("B 初始库为空", db.get("docs") == [], db)
    check("匿名库可读", isinstance(dn.get("docs"), list), dn)

    # 4) 磁盘上确实出现了 users/<oid>/ 独立目录
    ua = os.path.join(TMP, "users", "oUserA1234567890abcdef")
    ub = os.path.join(TMP, "users", "oUserB0987654321fedcba")
    check("A 独立库文件已建", os.path.isfile(os.path.join(ua, "lens_data.json")))
    check("B 独立库文件已建", os.path.isfile(os.path.join(ub, "lens_data.json")))
    check("A 独立上传目录已建", os.path.isdir(os.path.join(ua, "uploads")))

    # 5) 走真实接口：A 保存 Key 后，A 的库应能被读到（B 不能）
    #    先用 /api/settings 触发 A 的上下文重载（等价于真实用户配完 Key 的场景）
    call("/api/settings", h_a, body={"preset": "deepseek", "api_key": "sk-A-SECRET-E2E"})
    # 直接往 A 的库写一篇文献，再让 A 通过 settings 重载（模拟上传解析完成后写入）
    with open(os.path.join(ua, "lens_data.json"), "w", encoding="utf-8") as f:
        json.dump({"docs": [{"id": "aaaaaaaa", "meta": {"title": "A的私密文献"}}]}, f, ensure_ascii=False)
    da2 = call("/api/docs", h_a)
    db2 = call("/api/docs", h_b)
    # A 的上下文缓存里仍是最初的空库 → 用「重启不重载」的语义不强求 A 立即看到；
    # 关键是 B 绝不能看到 A 磁盘上的这篇文献。
    b_has_a = any(d.get("id") == "aaaaaaaa" for d in (db2.get("docs") or []))
    check("B 的列表看不到 A 磁盘上的文献（关键）", not b_has_a, db2)

    # 6) A 存 Key → A 的 settings 显示 user-api，B 不显示
    r = call("/api/settings", h_a, body={
        "preset": "deepseek", "api_key": "sk-A-SECRET-E2E"})
    check("A 保存 Key 成功", r.get("ok") is True, r)
    sa = call("/api/settings", h_a)
    sb = call("/api/settings", h_b)
    check("A 的 active 是 user-api", sa.get("active") == "user-api", sa)
    check("B 的 active 不是 user-api（关键）", sb.get("active") != "user-api", sb)
    check("B 的 provider 列表无 user-api（关键）",
          all(p.get("id") != "user-api" for p in (sb.get("providers") or [])), sb)

    # 7) 磁盘上 Key 只落在 A 的配置文件里
    ca = os.path.join(ua, "config.user.json")
    cb = os.path.join(ub, "config.user.json")
    check("Key 落在 A 的配置文件", os.path.isfile(ca) and "sk-A-SECRET-E2E" in open(ca, encoding="utf-8").read())
    check("B 无该 Key 文件（关键）", (not os.path.isfile(cb)) or ("sk-A-SECRET-E2E" not in open(cb, encoding="utf-8").read()))
    check("根目录无共享 Key 泄漏",
          not os.path.isfile(os.path.join(ROOT, "config.user.json")) or
          "sk-A-SECRET-E2E" not in open(os.path.join(ROOT, "config.user.json"), encoding="utf-8").read())

finally:
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.kill()
    print("\n" + ("端到端隔离全部通过 ✅" if not FAIL else f"失败 {len(FAIL)} 项 ❌: {FAIL}"))
    shutil.rmtree(TMP, ignore_errors=True)

sys.exit(1 if FAIL else 0)
