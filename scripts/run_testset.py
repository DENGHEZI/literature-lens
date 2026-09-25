# -*- coding: utf-8 -*-
"""
指定测试集：LiyuAgent raw 3 篇 + 农户用水行为文献库 2 篇
模型无关：provider 按角色自动路由（可用 --provider 覆盖）
"""
import os
import sys
import json
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from config_loader import load_config
from llm_client import CloudLLM, LLMError
from translator import Translator
from analyzer import Analyzer
from pipeline import process_one, build_engine
from sources import discover_liyu_raw, discover_farmers

ROOT = os.path.dirname(HERE)
CONFIG = load_config()
OUT_DIR = CONFIG["output"]["dir"]
DATA_DIR = os.path.join(ROOT, "data")

# 选定测试样本（按文件名关键词精确锁定）
PICKS_LIYU = [
    "005_Agents Catching Agents",
    "01_[IF29.6]_2023_Green construction for low-carbon cities",
    "01_IF27.7_2020_A_comparative_analysis_of_gradient_boosting",
]
PICKS_FARMERS = [
    "01_2021_农业现代化研究_灌溉水价与技术进步对农业用水强度的影响",
    "01_2026_Small-scale irrigators' intentions to adapt water use",
]


def pick(recs, keys):
    out, used = [], set()
    for k in keys:
        for r in recs:
            if r["filename"] in used:
                continue
            if k.lower() in r["filename"].lower():
                out.append(r)
                used.add(r["filename"])
                break
    return out


def main(provider=None, deep=True):
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(OUT_DIR, exist_ok=True)

    a = discover_liyu_raw(CONFIG["sources"]["liyu_raw"])
    b = discover_farmers(CONFIG["sources"]["farmers_water"])
    print(f"[i] 可发现：LiyuAgent raw {len(a)} 篇 / 农户用水库 {len(b)} 篇")

    recs = pick(a, PICKS_LIYU) + pick(b, PICKS_FARMERS)
    print(f"[i] 本次测试集 {len(recs)} 篇：")
    for r in recs:
        print("    -", r["filename"][:78])
    if not recs:
        print("！！未匹配到测试样本")
        return

    llm, tr, az, tr_pid, az_pid = build_engine(provider, provider)
    tname = llm.provider(tr_pid)["name"]
    aname = llm.provider(az_pid)["name"]
    print(f"[+] 翻译模型 = {tname}   分析模型 = {aname}（云端 API，未使用本地模型）\n")

    docs, failed = [], []
    t_all = time.time()
    for i, r in enumerate(recs, 1):
        print(f"[{i}/{len(recs)}] {r['filename'][:72]}")
        try:
            d = process_one(r, llm, tr, az, max_pages=10)
            if deep and d.get("concepts"):
                for c in d["concepts"]:
                    dd = az.deepen(c, "\n".join(
                        x["source"] for x in d["bilingual"][:30])[:2500])
                    if dd:
                        c["detail"] = dd
            docs.append(d)
            print(f"      ✓ 创新点 {len(d['insights'])} · 知识点 {len(d['concepts'])} · "
                  f"用时 {d['stats']['elapsed_s']}s")
        except Exception as e:
            print(f"      !! 失败 {type(e).__name__}: {e}"[:170])
            failed.append({"filename": r["filename"],
                           "error": f"{type(e).__name__}: {e}"[:200]})

    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": aname,
        "models": {"translate": tname, "analyze": aname},
        "provider": {"translate": tr_pid, "analyze": az_pid},
        "model_note": "全部由云端 API 模型完成，未调用本地 Ollama / llama-server",
        "stats": {
            "n_docs": len(docs), "n_failed": len(failed),
            "llm_calls": llm.stats["calls"],
            "by_provider": llm.stats.get("by_provider", {}),
            "insights": sum(len(d["insights"]) for d in docs),
            "concepts": sum(len(d["concepts"]) for d in docs),
            "elapsed_s": round(time.time() - t_all, 1),
        },
        "failed": failed,
        "docs": docs,
    }
    out = os.path.join(DATA_DIR, "lens_data.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"\n[+] 数据写入 {out}")
    print(f"[+] 统计 {payload['stats']}")
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default=None)
    ap.add_argument("--no-deep", action="store_true")
    ap.add_argument("--config", default=None)
    a = ap.parse_args()
    if a.config:
        CONFIG.update(load_config(extra=a.config))
    main(provider=a.provider, deep=not a.no_deep)
