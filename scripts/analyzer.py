# -*- coding: utf-8 -*-
"""
创新点分析 + 知识点生成引擎（模型无关版）

产出三层结构：
  1) profile   —— 论文画像：研究问题 / 领域 / 方法 / 数据 / 结论
  2) insights  —— 创新点卡片：每条含【创新点 / 为什么新 / 证据(带页码) / 关键知识点 ids】
  3) concepts  —— 知识点库：点击创新点后可展开的深度讲解（定义/原理/公式/类比/延伸）

设计原则：
- 所有结论必须锚定原文，evidence 带页码，找不到证据就标 unverified，不编造
- 知识点由创新点派生，点击即展开，满足「动态交互」
- 分两步调用：先 profile+insights（全局视野），再按需补 concepts（省 token）
- 模型无关：结构化输出经 coerce_list / coerce_dict 容错，不假设模型一定按
  「纯对象」或「纯数组」返回（不同模型遵守度差异很大）
"""
import json
import re

from llm_client import coerce_list, coerce_dict

SYSTEM_ANALYST = (
    "你是资深科研论文审稿人与学术情报分析师。你的职责是从论文中提炼真实的创新点，"
    "严格依据原文，绝不编造论文里没有的内容。若某项信息原文未提及，写「原文未提及」。"
    "所有判断都要给出原文依据。"
)

SYSTEM_TUTOR = (
    "你是耐心且严谨的科研导师，擅长把艰深概念讲清楚。"
    "讲解必须准确、有层次、可落地，不空谈。"
)

SYSTEM_MINDMAP = (
    "你是严谨的科研文献助理。你只能依据给定论文语境组织信息；"
    "问题超出原文时要明确标注『原文未提及』，不要补造事实。"
)

# ---------------- 第一步：论文画像 + 创新点 ----------------
PROFILE_PROMPT = """请阅读下面这篇论文的元信息与正文片段，完成两项任务。

【任务一】论文画像
【任务二】提炼创新点（这是重点，要挖真东西）

只输出一个 JSON 对象，不要任何额外文字，格式严格如下：

{{
  "profile": {{
    "title_zh": "标题中文译名",
    "domain": "所属细分领域（8字以内，如：农业水资源管理）",
    "research_question": "这篇论文要解决的核心科学问题（一句话，30字内）",
    "gap": "它指出前人研究的空白/不足是什么（一句话）",
    "method": "采用的核心方法/模型（列出关键方法名，逗号分隔）",
    "data": "数据来源与样本（具体说明，原文未提及则写「原文未提及」）",
    "findings": ["主要结论1", "主要结论2", "主要结论3"],
    "limitations": "作者自述的局限（原文未提及则写「原文未提及」）",
    "keywords": ["关键词1","关键词2","关键词3","关键词4"]
  }},
  "insights": [
    {{
      "id": "I1",
      "title": "创新点标题（12字以内，要具体，不要写「研究方法创新」这种空话）",
      "type": "理论创新 | 方法创新 | 数据创新 | 应用创新 | 发现创新",
      "what": "这个创新点具体是什么（40-70字，讲清做了什么事）",
      "why_new": "为什么它相对于前人工作是新的（40-70字，要对比）",
      "evidence": "支撑它的原文依据（直接引用原文关键句，可截取）",
      "page": "该依据所在页码（必须取正文中 (pN) 标注里的 N，不要写 1）",
      "impact": "它对领域意味着什么（30字内）",
      "concept_ids": ["C1", "C2"],
      "confidence": "高 | 中 | 低"
    }}
  ],
  "concepts": [
    {{
      "id": "C1",
      "name": "知识点名称（如：谢泼德引理）",
      "kind": "理论 | 方法 | 指标 | 数据 | 现象",
      "one_liner": "一句话定义（25字内）",
      "explain": "展开讲解（120-200字，说清它是什么、怎么来的、解决什么问题）",
      "how": "在本文中如何被使用（40-60字）",
      "formula": "核心公式或表达式（没有就写空字符串）",
      "pitfall": "常见误解或使用注意点（30字内）",
      "related": ["关联知识点名称1", "关联知识点名称2"]
    }}
  ]
}}

要求：
1. insights 提炼 3-5 条，按重要性排序，必须具体到「做了什么」，不能是套话
2. concepts 提炼 4-8 个，覆盖本研究最关键的概念/方法/指标，是理解这些创新点所必需的
3. concept_ids 必须指向 concepts 中真实存在的 id
4. 所有内容用简体中文
5. confidence：原文有明确表述=高；需要推断=中；证据薄弱=低
6. 不要输出 JSON 以外的任何文字
7. 【页码必须准确】正文每个段落前都有 (pN) 标注，那是它在原 PDF 中的真实页码。
   evidence 的 page 字段必须填你引用的那段话所属的 N。禁止一律填 1。
   尽量从【方法】【结果】【讨论/结论】等章节取材，不要只盯着摘要。

{meta}

正文片段：
{body}
"""


class Analyzer:
    """创新点分析。pid 指定用哪个 provider；不指定则按 analyze 角色自动挑。"""

    def __init__(self, llm, pid=None):
        self.llm = llm
        self.pid = pid
        self.role = "analyze"

    def _call(self, prompt, system=None, max_tokens=7000, temperature=0.2):
        if self.pid:
            return self.llm.chat_json(prompt, system=system, max_tokens=max_tokens,
                                      temperature=temperature, pid=self.pid)
        pid = (self.llm.pick_for(self.role) or {}).get("id")
        return self.llm.chat_json(prompt, system=system, max_tokens=max_tokens,
                                  temperature=temperature, pid=pid)

    # ---------- 画像 + 创新点 + 知识点 ----------
    def analyze(self, meta, body_text, max_tokens=None):
        prompt = PROFILE_PROMPT.format(meta=meta, body=body_text[:16000])
        data = self._call(prompt, system=SYSTEM_ANALYST,
                          max_tokens=max_tokens or 7000, temperature=0.2)
        data = coerce_dict(data)
        if not isinstance(data, dict):
            return None
        return self._normalize(data)

    @staticmethod
    def _normalize(data):
        prof = data.get("profile") or {}
        out = {
            "profile": {
                "title_zh": prof.get("title_zh", ""),
                "domain": prof.get("domain", ""),
                "research_question": prof.get("research_question", ""),
                "gap": prof.get("gap", ""),
                "method": prof.get("method", ""),
                "data": prof.get("data", ""),
                "findings": [f for f in (prof.get("findings") or []) if f],
                "limitations": prof.get("limitations", ""),
                "keywords": [k for k in (prof.get("keywords") or []) if k],
            },
            "insights": [],
            "concepts": [],
        }
        raw_ins = coerce_list(data.get("insights") or []) or []
        for i, ins in enumerate(raw_ins, 1):
            if not isinstance(ins, dict) or not ins.get("title"):
                continue
            out["insights"].append({
                "id": ins.get("id") or f"I{i}",
                "title": ins.get("title", ""),
                "type": ins.get("type", "方法创新"),
                "what": ins.get("what", ""),
                "why_new": ins.get("why_new", ""),
                "evidence": ins.get("evidence", ""),
                "page": ins.get("page", ""),
                "impact": ins.get("impact", ""),
                "concept_ids": [c for c in (ins.get("concept_ids") or []) if c],
                "confidence": ins.get("confidence", "中"),
            })
        raw_con = coerce_list(data.get("concepts") or []) or []
        for i, c in enumerate(raw_con, 1):
            if not isinstance(c, dict) or not c.get("name"):
                continue
            out["concepts"].append({
                "id": c.get("id") or f"C{i}",
                "name": c.get("name", ""),
                "kind": c.get("kind", "方法"),
                "one_liner": c.get("one_liner", ""),
                "explain": c.get("explain", ""),
                "how": c.get("how", ""),
                "formula": c.get("formula", ""),
                "pitfall": c.get("pitfall", ""),
                "related": [r for r in (c.get("related") or []) if r],
            })
        return out

    # ---------- 知识点深挖（点击时按需） ----------
    def deepen(self, concept, paper_ctx):
        prompt = (
            f"围绕知识点「{concept['name']}」，结合下面论文语境，产出更深入的讲解。\n"
            "只输出 JSON 对象：\n"
            "{\n"
            '  "deep": "深入讲解（200-300字，讲原理、推导思路、适用条件、与相邻概念的区别）",\n'
            '  "example": "一个具体例子或数值说明（60-100字，原文有好例子就引用）",\n'
            '  "chain": ["理解它的前置概念1","前置概念2"],\n'
            '  "misconception": "最容易搞错的地方（40字内）",\n'
            '  "extend": ["延伸阅读方向1","延伸阅读方向2"]\n'
            "}\n"
            "不要输出 JSON 以外文字。\n\n"
            f"论文语境：\n{paper_ctx[:3000]}\n\n"
            f"已有信息：{concept.get('one_liner','')} {concept.get('explain','')}"
        )
        try:
            d = self._call(prompt, system=SYSTEM_TUTOR,
                           max_tokens=1800, temperature=0.35)
        except Exception:
            return None
        if not isinstance(d, dict):
            return None
        return {
            "deep": d.get("deep", ""),
            "example": d.get("example", ""),
            "chain": [x for x in (d.get("chain") or []) if x],
            "misconception": d.get("misconception", ""),
            "extend": [x for x in (d.get("extend") or []) if x],
        }

    def question_map(self, question, paper_ctx, profile=None):
        """把用户问题组织成一棵可视化的文献证据导图。

        该调用刻意使用独立的 ``mindmap`` 角色：部署者可以让翻译模型与
        推理/导图模型分别走不同的用户自备 API，而不会影响既有分析流水线。
        """
        question = (question or "").strip()[:500]
        if not question:
            return None
        profile_text = json.dumps(profile or {}, ensure_ascii=False)
        prompt = (
            "针对用户问题，依据论文语境生成一棵层级思维导图（树状）的 JSON 对象。\n"
            "只输出 JSON，不要 Markdown，结构如下：\n"
            '{"title":"问题的简短改写（18字内）","summary":"直接回答（80-140字，必须说明依据范围）",'
            '"nodes":[{"label":"一级主题（14字内）","evidence":"原文依据或原文未提及（55字内）",'
            '"children":["要点（28字内）",'
            '{"label":"二级子主题（14字内）","evidence":"依据（可选）","children":["三级要点（28字内）"]}]}]}\n'
            "规则：nodes 给 3-5 个一级分支；每个分支 2-4 个二级节点；需要展开时二级节点可再带 children（三级）。"
            "整张图呈现清晰树状层级（根→分支→要点→细项），不要平铺；"
            "所有结论必须可由语境支持；不确定或未出现的信息写『原文未提及』。\n\n"
            f"用户问题：{question}\n\n论文画像：{profile_text[:1800]}\n\n"
            f"论文语境：\n{(paper_ctx or '')[:10000]}"
        )
        try:
            pid = (self.llm.pick_for("mindmap") or {}).get("id")
            data = self.llm.chat_json(prompt, system=SYSTEM_MINDMAP,
                                      max_tokens=2600, temperature=0.2, pid=pid)
        except Exception:
            return None
        data = coerce_dict(data)
        if not isinstance(data, dict):
            return None

        # 递归归一化：支持 children 为字符串或 {label,evidence,children}（多级树）
        def norm_node(x, depth):
            if isinstance(x, str):
                t = x.strip()
                return {"label": t[:80]} if t else None
            if not isinstance(x, dict):
                return None
            label = str(x.get("label") or x.get("text") or "").strip()
            if not label:
                return None
            node = {"label": label[:48]}
            ev = str(x.get("evidence") or "").strip()
            if ev:
                node["evidence"] = ev[:180]
            if depth < 3:  # 至多再展开一层，避免无限/过深
                kids = []
                for c in (coerce_list(x.get("children") or []) or [])[:5]:
                    n = norm_node(c, depth + 1)
                    if n:
                        kids.append(n)
                if kids:
                    node["children"] = kids
            return node

        nodes = []
        for item in (coerce_list(data.get("nodes") or []) or [])[:5]:
            n = norm_node(item, 0)
            if n:
                nodes.append(n)
        if not nodes:
            return None
        return {"title": str(data.get("title") or question).strip()[:80],
                "summary": str(data.get("summary") or "原文未提及").strip()[:600],
                "nodes": nodes}


# ---------------- 元信息拼装 ----------------

def build_meta(doc):
    """把文献元信息整理成给模型的文本。

    联网检索来的记录往往只有摘要（没有全文），元信息就成了判断依据的大头，
    因此这里要把 cited_by / 来源库 / 链接一并带上。
    """
    lines = []
    if doc.get("title"):
        lines.append(f"标题：{doc['title']}")
    if doc.get("authors"):
        lines.append(f"作者：{doc['authors']}")
    if doc.get("year"):
        lines.append(f"年份：{doc['year']}")
    if doc.get("journal"):
        lines.append(f"期刊：{doc['journal']}")
    if doc.get("doi"):
        lines.append(f"DOI：{doc['doi']}")
    if doc.get("cited_by"):
        lines.append(f"被引次数：{doc['cited_by']}")
    if doc.get("source"):
        lines.append(f"来源库：{doc['source']}")
    if doc.get("abstract"):
        lines.append(f"摘要：{doc['abstract'][:1500]}")
    if doc.get("web_url"):
        lines.append(f"链接：{doc['web_url']}")
    # 明确告知模型：只有摘要，不要臆造全文才有的细节
    if doc.get("abstract_only"):
        lines.append("【重要】本文只有摘要，没有全文。只能依据摘要做判断，"
                     "凡摘要未提及的一律写「原文未提及」，不得编造具体数据与方法细节。")
    return "\n".join(lines) if lines else "（无元信息）"


def pick_body_blocks(blocks, max_chars=14000, exclude_abstract=False):
    """
    挑出最能代表论文的块，拼成分析用正文。

    要点（防止「证据全来自摘要」）：
      1. 按章节分区采样：摘要/引言 / 方法 / 结果 / 讨论·结论 各留配额，
         避免顺着正文截断时 14000 字全被摘要+引言吃掉。
      2. 每块带上 `p{页码}` 标注，让模型能引用真实页码而非默认第 1 页。
      3. 章节标题一并保留，给模型结构线索；但期刊名/论文标题等伪 heading 要剔除。
      4. exclude_abstract=True 时排除首页摘要正文，逼模型从正文深处找证据
         （摘要里常已含最漂亮的数字，模型会偷懒全引摘要 → 页码全是 p1）。
    """
    body = [b for b in blocks if b["kind"] != "formula"]
    if not body:
        return ""

    # --- 0) 剔除伪 heading（期刊名行、论文标题行、页眉噪音） ---
    def real_heading(s):
        s = (s or "").strip()
        if len(s) < 3:
            return False
        # 期刊名行：BMC xxx / Journal of xxx 之类整行
        if re.match(r"^(?:BMC|PLOS|Nature|Science|Cell|Frontiers\s+in|"
                    r"Journal\s+of|International\s+Journal\s+of|Sustainability|"
                    r"Agronomy|Agriculture|Land|Water|Plants|Horticulturae)\b",
                    s, re.I):
            return False
        # 无实际标题内容的一级泛化词
        if s.lower() in ("research", "article", "review", "original article",
                         "open access", "preface", "abstract"):
            return s.lower() == "abstract"
        # 含许可证/版权/邮箱/DOI 的不是标题
        if re.search(r"(creative\s+commons|licen[cs]e|©|@|doi\.org|http)", s, re.I):
            return False
        return True

    # --- 1) 切成章节段 ---
    secs, cur = [], {"title": "", "items": []}
    for b in body:
        if b["kind"] == "heading" and real_heading(b["text"]):
            # 首页的 heading 基本是刊名/论文标题/栏目名（"RESEARCH" 之类），不当章节
            if b.get("page") == 1:
                continue
            if cur["items"] or cur["title"]:
                secs.append(cur)
            cur = {"title": b["text"].strip(), "items": []}
        else:
            # 伪 heading 当普通段落处理（或忽略期刊名/标题行）
            if b["kind"] == "heading":
                s = (b["text"] or "").strip()
                if b.get("page") == 1:
                    continue
                if re.match(r"^(?:BMC|PLOS|Nature|Science|Cell|Frontiers\s+in|"
                            r"Journal\s+of|International\s+Journal\s+of)\b", s, re.I):
                    continue
                if re.search(r"(creative\s+commons|licen[cs]e|©|@|doi\.org|http)", s, re.I):
                    continue
            cur["items"].append(b)
    if cur["items"] or cur["title"]:
        secs.append(cur)
    if not secs:
        return ""

    # --- 2) 章节归类 ---
    def bucket(name):
        s = (name or "").lower()
        if any(k in s for k in ("abstract", "摘要", "introduction", "引言", "背景", "background")):
            return "intro"
        if any(k in s for k in ("method", "material", "方法", "材料", "数据", "data",
                                "study area", "sampling", "model", "analysis", "分析")):
            return "method"
        if any(k in s for k in ("result", "结果", "finding", "发现", "empirical")):
            return "result"
        if any(k in s for k in ("discussion", "conclusion", "讨论", "结论", "启示",
                                "implication", "summary", "总结", "limitation")):
            return "concl"
        return "other"

    groups = {"intro": [], "method": [], "result": [], "concl": [], "other": []}
    for s in secs:
        groups[bucket(s["title"])].append(s)

    # 排除摘要：摘要通常挤在首页且已含最亮眼的数字，会把模型引向「全引摘要」
    # 注意：很多期刊（如 BMC）摘要没有独立 "Abstract" 标题行，摘要正文直接跟在
    #      刊名/通讯作者之后 → 因此这里对【所有分区的 p1 段落】一律剔除
    if exclude_abstract:
        for key in ("intro", "method", "result", "concl", "other"):
            kept = []
            for s in groups[key]:
                title_l = (s["title"] or "").lower()
                if "abstract" in title_l or "摘要" in title_l:
                    continue
                items = [b for b in s["items"] if b.get("page") != 1]
                if not items:
                    continue
                kept.append({"title": s["title"], "items": items})
            groups[key] = kept

    # --- 3) 各分区配额 ---
    quotas = {"intro": 0.32, "method": 0.24, "result": 0.24, "concl": 0.20}
    parts, used = [], 0
    for key in ("intro", "method", "result", "concl"):
        budget = int(max_chars * quotas[key])
        got = 0
        for s in groups[key]:
            if got >= budget:
                break
            if s["title"]:
                parts.append(f"【{s['title']}】")
            for b in s["items"]:
                t = f"(p{b['page']}) {b['text']}"
                if got + len(t) > budget or used + len(t) > max_chars:
                    break
                parts.append(t)
                got += len(t)
                used += len(t)
    # --- 3b) 若某分区没吃饱（如摘要被剔除），把余量补给方法/结果/结论 ---
    if used < max_chars * 0.8:
        for key in ("method", "result", "concl", "other", "intro"):
            for s in groups[key]:
                if used >= max_chars:
                    break
                for b in s["items"]:
                    t = f"(p{b['page']}) {b['text']}"
                    if used + len(t) > max_chars:
                        break
                    parts.append(t)
                    used += len(t)
    # --- 4) 还有余量就补 other ---
    for s in groups["other"]:
        if used >= max_chars:
            break
        if s["title"]:
            parts.append(f"【{s['title']}】")
        for b in s["items"]:
            t = f"(p{b['page']}) {b['text']}"
            if used + len(t) > max_chars:
                break
            parts.append(t)
            used += len(t)
    return "\n\n".join(parts)
