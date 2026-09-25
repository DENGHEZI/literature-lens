# -*- coding: utf-8 -*-
"""
联网检索 CLI —— 先看清单，再决定交给流水线处理哪些

用法：
  python scripts/lens_search.py "water scarcity farmer irrigation"
  python scripts/lens_search.py "灌溉水价" --sources openalex,crossref --n 8
  python scripts/lens_search.py "water scarcity" --oa-only --year-from 2020 --sort cited
  python scripts/lens_search.py "water" --json out.json      # 存成 JSON 供后续处理
  python scripts/lens_search.py --import-file cnki_refworks.txt   # 知网/维普题录导入预览

说明：知网 CNKI 与维普 CQVIP 没有公开 API 且强制反爬（实测 CNKI 返回"安全验证"页、
维普返回 HTTP 412），无法自动化抓取。请用 --import-file 导入站内的导出题录。
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from retriever import (search_all, Retriever, SOURCE_LABELS,
                       parse_export)


def show(recs, limit=None):
    print(f"\n共 {len(recs)} 条\n")
    print(f"{'#':>3} {'年份':<6}{'全文':<5}{'被引':>6} {'相关':<6} {'期刊':<26} 标题")
    print("-" * 118)
    for i, r in enumerate(recs[:limit] if limit else recs, 1):
        pdf = "PDF" if r.get("path") or r.get("pdf_url") else "摘要"
        src = (r.get("journal") or r.get("source") or "")[:24]
        print(f"{i:>3} {str(r.get('year') or ''):<6}{pdf:<5}{r.get('cited_by', 0):>6} "
              f"{r.get('relevance', '-'):<6} {src:<26} {(r.get('title') or '')[:58]}")
        if r.get("doi"):
            print(f"     DOI {r['doi']}" + (f"  |  {r['web_url'][:70]}" if r.get("web_url") else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?", default=None)
    ap.add_argument("--sources", default="openalex",
                    help="逗号分隔：openalex,arxiv,crossref,s2（默认 openalex）")
    ap.add_argument("--n", type=int, default=10, help="每个源取几条")
    ap.add_argument("--oa-only", action="store_true", help="只检索开放获取")
    ap.add_argument("--year-from", type=int, default=None)
    ap.add_argument("--year-to", type=int, default=None)
    ap.add_argument("--sort", default="relevance",
                    choices=["relevance", "cited", "date"])
    ap.add_argument("--min-relevance", type=int, default=2,
                    help="标题+摘要至少命中几个检索关键词，0=不过滤（默认 2，按检索式词数收敛）")
    ap.add_argument("--download", action="store_true", help="顺便下载 OA 全文")
    ap.add_argument("--json", default=None, help="结果存成 JSON")
    ap.add_argument("--import-file", default=None,
                    help="导入知网/维普导出题录（RefWorks/BibTeX/EndNote）")
    ap.add_argument("--import-format", default=None,
                    choices=["refworks", "bibtex", "endnote"])
    a = ap.parse_args()

    if a.import_file:
        if not os.path.isfile(a.import_file):
            print("文件不存在:", a.import_file)
            return 1
        recs = Retriever().from_export(a.import_file, a.import_format)
        print(f"[import] {a.import_file} → {len(recs)} 条")
        show(recs)
        if a.json:
            json.dump(recs, open(a.json, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
            print("\n已存", a.json)
        return 0

    if not a.query:
        ap.print_help()
        return 1

    srcs = tuple(s.strip() for s in a.sources.split(",") if s.strip())
    bad = [s for s in srcs if s not in SOURCE_LABELS]
    if bad:
        print("未知源:", bad, "可选:", list(SOURCE_LABELS))
        return 1

    print(f"[检索] {a.query}")
    print(f"       源 {', '.join(SOURCE_LABELS[s] for s in srcs)}"
          + ("  | 仅开放获取" if a.oa_only else "")
          + (f"  | {a.year_from}-{a.year_to}" if a.year_from or a.year_to else "")
          + f"  | 排序 {a.sort}")

    recs, errors = search_all(a.query, sources=srcs, per_source=a.n,
                              oa_only=a.oa_only, year_from=a.year_from,
                              year_to=a.year_to, sort=a.sort,
                              min_relevance=a.min_relevance)
    for e in errors:
        print("  [源失败]", e)

    if a.download:
        dest = os.path.join(os.path.dirname(HERE), "downloads")
        got = 0
        for r in recs:
            if Retriever().attach_pdfs([r], dest)[0]:
                got += 1
        print(f"[下载] OA 全文 {got}/{len(recs)} 条")

    show(recs)

    print("\n交给流水线处理（完整翻译 + 创新点分析）：")
    print(f'  python scripts/pipeline.py --source web --query "{a.query}" '
          f'--web-sources {",".join(srcs)} --limit {a.n}'
          + (" --oa-only" if a.oa_only else "")
          + (f" --year-from {a.year_from}" if a.year_from else "")
          + (f" --year-to {a.year_to}" if a.year_to else "")
          + (f" --sort {a.sort}" if a.sort != "relevance" else ""))

    if a.json:
        json.dump(recs, open(a.json, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print("\n已存", a.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
