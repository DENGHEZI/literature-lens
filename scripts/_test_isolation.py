# -*- coding: utf-8 -*-
"""多租户隔离自测：验证不同 openid 的文献库 / 上传目录 / API 配置互不可见。

用法（在 D:\\LiteratureLens 下）：
    python scripts/_test_isolation.py
不起真实服务，直接 import serve 的隔离辅助函数做单元级验证。
"""
import os
import sys
import json
import shutil
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 用临时目录做数据根，避免污染真实 data/
TMP = tempfile.mkdtemp(prefix="lens_iso_")
os.environ["LENS_DATA_DIR"] = TMP
os.environ["LENS_DATA"] = os.path.join(TMP, "lens_data.json")

import serve  # noqa: E402

FAIL = []
def check(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        FAIL.append(name)

# 1) 路径规划：两个用户、一个匿名，必须落到三处互不相交的目录
p_a = serve._ctx_paths("oUserA1234567890")   # 16 位，合法
p_b = serve._ctx_paths("oUserB0987654321")
p_x = serve._ctx_paths("")
check("用户A数据文件独立", p_a["data"] != p_b["data"] and p_a["data"] != p_x["data"])
check("用户A上传目录独立", p_a["upload"] != p_b["upload"])
check("用户A配置独立", p_a["config"] != p_b["config"] and p_a["config"] != p_x["config"])
check("匿名走全局旧路径", p_x["data"] == os.environ["LENS_DATA"])
check("A 落在 users/<oid>/ 下", ("users" in p_a["data"]) and ("oUserA1234567890" in p_a["data"]))

# 2) openid 清洗：含路径/特殊字符一律拒绝（宁可回退公共上下文，也不接受可疑 id）
check("拒绝非法字符（不洗白）", serve._safe_oid("oAbC!@#1234567890") == "")
check("拒绝路径穿越", serve._safe_oid("../../etc/passwd") == "")
check("拒绝含点号", serve._safe_oid("oUser.A1234567890") == "")
check("拒绝过短", serve._safe_oid("abc") == "")
check("拒绝过长", serve._safe_oid("a" * 65) == "")
check("接受 28 位微信 openid 形态", serve._safe_oid("o" + "Ab1_" * 6 + "xyz") != "")
check("接受 32 位 hex（本地自测 id）", serve._safe_oid("a" * 32) == "a" * 32)

# 3) 上下文构建：各自建库、各自 provider、互不引用同一对象
ctx_a = serve._user_ctx("oUserA1234567890")
ctx_b = serve._user_ctx("oUserB0987654321")
check("A 建了独立库文件", os.path.isfile(p_a["data"]))
check("B 建了独立库文件", os.path.isfile(p_b["data"]))
check("A/B 库对象不同", ctx_a["data"] is not ctx_b["data"])
check("A/B 上传目录不同", ctx_a["upload_dir"] != ctx_b["upload_dir"])
check("A/B LLM 对象不同", ctx_a["llm"] is not ctx_b["llm"])
check("A/B doc_index 不同", ctx_a["doc_index"] is not ctx_b["doc_index"])

# 4) 写入 A 的库，B 不得看见
with open(p_a["data"], "w", encoding="utf-8") as f:
    json.dump({"docs": [{"id": "aaaaaaaa", "meta": {"title": "A 的文献"}}]}, f, ensure_ascii=False)
ctx_a2 = serve._reload_ctx(ctx_a)
check("A 看到自己的文献", "aaaaaaaa" in ctx_a2["doc_index"])
check("B 看不到 A 的文献", "aaaaaaaa" not in ctx_b["doc_index"])

# 5) A 存 Key，B 不得读到
serve.save_user_config({"active_provider": "user-api",
                        "providers": [{"id": "user-api", "base_url": "https://a.example/v1",
                                       "api_key": "sk-A-SECRET", "model": "m-a"}]},
                       path=p_a["config"])
ctx_a3 = serve._reload_ctx(ctx_a)
ctx_b3 = serve._reload_ctx(ctx_b)
a_keys = [x.get("api_key") for x in (ctx_a3["cfg"].get("providers") or [])]
b_keys = [x.get("api_key") for x in (ctx_b3["cfg"].get("providers") or [])]
check("A 读到自己的 Key", "sk-A-SECRET" in a_keys)
check("B 读不到 A 的 Key（关键）", "sk-A-SECRET" not in b_keys)

# 6) 任务归属：A 的任务 B 不可见
serve._set_task("t1", status="done", uid="oUserA1234567890")
t = serve.TASKS["t1"]
check("A 的任务 A 可见", serve._task_visible(t, "oUserA1234567890"))
check("A 的任务 B 不可见（关键）", not serve._task_visible(t, "oUserB0987654321"))

print("\n" + ("全部通过 ✅" if not FAIL else f"失败 {len(FAIL)} 项 ❌: {FAIL}"))
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
