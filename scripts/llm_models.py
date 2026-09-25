# -*- coding: utf-8 -*-
"""
模型清单与能力探测 CLI —— 换模型时的自检工具

用法：
  python scripts/llm_models.py --list                 # 看已配置的 provider
  python scripts/llm_models.py --probe                # 全部探测一遍
  python scripts/llm_models.py --probe deepseek       # 只探一个
  python scripts/llm_models.py --add                  # 交互式新增 provider
  python scripts/llm_models.py --use kimi             # 切换 active_provider
  python scripts/llm_models.py --presets              # 列出内置预设的 base_url
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from config_loader import load_config, save_user_config, pick_active
from llm_client import CloudLLM, _norm_protocol

# 常见服务预设：换模型时照着填
PRESETS = {
    "deepseek":   ("DeepSeek",        "https://api.deepseek.com/v1",              "deepseek-chat"),
    "kimi":       ("Moonshot Kimi",   "https://api.moonshot.cn/v1",               "moonshot-v1-128k"),
    "qwen":       ("通义千问",         "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    "zhipu":      ("智谱 GLM",         "https://open.bigmodel.cn/api/paas/v4",     "glm-4-plus"),
    "siliconflow":("硅基流动",         "https://api.siliconflow.cn/v1",            "Qwen/Qwen2.5-72B-Instruct"),
    "openrouter": ("OpenRouter",      "https://openrouter.ai/api/v1",            "openai/gpt-4o-mini"),
    "openai":     ("OpenAI",          "https://api.openai.com/v1",               "gpt-4o-mini"),
    "anthropic":  ("Anthropic Claude","https://api.anthropic.com/v1",            "claude-sonnet-4-20250514"),
    "gemini":     ("Google Gemini",   "https://generativelanguage.googleapis.com/v1beta", "gemini-2.0-flash"),
    "ollama":     ("Ollama 本地",      "http://127.0.0.1:11434",                  "qwen2.5:7b"),
    "vllm":       ("vLLM / LM Studio","http://127.0.0.1:8000/v1",                "your-model"),
    "minimax":    ("MiniMax",         "https://api.minimax.chat/v1",             "abab6.5s-chat"),
    "baichuan":   ("百川",             "https://api.baichuan-ai.com/v1",          "Baichuan4"),
    "stepfun":    ("阶跃星辰",          "https://api.stepfun.com/v1",             "step-1-128k"),
    "doubao":     ("豆包 / 火山方舟",    "https://ark.cn-beijing.volces.com/api/v3", "doubao-pro-32k"),
}


def cmd_presets():
    print("内置预设（换模型时直接抄 base_url / model）：\n")
    print(f"{'别名':<14}{'服务':<18}{'model 示例':<40}base_url")
    print("-" * 110)
    for k, (name, url, model) in PRESETS.items():
        print(f"{k:<14}{name:<18}{model:<40}{url}")


def cmd_list(cfg):
    provs = cfg.get("providers") or []
    act = cfg.get("active_provider")
    print(f"配置文件：{', '.join(cfg.get('_loaded_from') or ['(无)'])}")
    print(f"生效 provider：{act}\n")
    if not provs:
        print("（未配置任何 provider）")
        return
    print(f"{'':2}{'id':<14}{'名称':<22}{'protocol':<14}{'model':<28}{'key':<6}roles")
    print("-" * 100)
    for p in provs:
        mark = "▶ " if p.get("id") == act else "  "
        keyed = "有" if (p.get("api_key") or "") else "缺"
        off = "" if p.get("enabled", True) else " [禁用]"
        print(f"{mark}{p.get('id',''):<14}{(p.get('name') or '')[:20]:<22}"
              f"{_norm_protocol(p):<14}{(p.get('model') or '')[:26]:<28}{keyed:<6}"
              f"{','.join(p.get('roles') or []) or '*'}{off}")


def cmd_probe(cfg, only=None):
    provs = cfg.get("providers") or []
    if only:
        provs = [p for p in provs if p.get("id") == only]
    if not provs:
        print("没有匹配的 provider")
        return 1
    bad = 0
    for p in provs:
        print(f"\n── 探测 {p.get('id')}（{p.get('name')} / {_norm_protocol(p)} / {p.get('model')}）")
        if p.get("enabled", True) is False:
            print("   跳过：已禁用")
            continue
        if not (p.get("api_key") or ""):
            print("   跳过：缺 api_key")
            bad += 1
            continue
        r = CloudLLM(cfg.get("providers")).probe(p.get("id"))
        if r["ok"]:
            print(f"   ✅ 连通    回复：{r['reply']}")
            print(f"   {'✅' if r['json_ok'] else '⚠️ '} JSON 模式："
                  f"{'可用' if r['json_ok'] else '不可用（会自动退化到提示词约束+宽松解析，仍可跑）'}")
        else:
            bad += 1
            print(f"   ❌ 失败    {r['error']}")
    print(f"\n汇总：{len(provs) - bad} 可用 / {len(provs)} 总计")
    return 0 if bad == 0 else 1


def cmd_add(cfg, args):
    pid = args.add
    preset = PRESETS.get(pid)
    print(f"新增 provider：{pid}")
    print("（回车表示接受括号内的默认值）\n")
    if preset:
        name, url, model = preset
        q = input(f"  服务名 [{name}]：").strip() or name
        u = input(f"  base_url [{url}]：").strip() or url
        m = input(f"  model [{model}]：").strip() or model
    else:
        q = input("  服务名（如 Kimi）：").strip() or pid
        u = input("  base_url（如 https://api.moonshot.cn/v1）：").strip()
        m = input("  model（如 moonshot-v1-128k）：").strip()
    k = input("  api_key（可写 ${MY_ENV_VAR} 走环境变量）：").strip()
    p = {"id": pid, "name": q, "base_url": u, "api_key": k, "model": m,
         "enabled": True, "roles": ["chat", "translate", "analyze"],
         "weight": 1}
    patch = {"providers": _merge_provider(cfg, p)}
    if not cfg.get("active_provider"):
        patch["active_provider"] = pid
    path = save_user_config(patch)
    print(f"\n已写入 {path}")
    print("接着可以跑：python scripts/llm_models.py --probe " + pid)
    return 0


def _merge_provider(cfg, newp):
    out = []
    replaced = False
    for p in (cfg.get("providers") or []):
        if p.get("id") == newp["id"]:
            out.append(newp)
            replaced = True
        else:
            out.append(p)
    if not replaced:
        out.append(newp)
    return out


def cmd_use(cfg, pid):
    ids = [p.get("id") for p in (cfg.get("providers") or [])]
    if pid not in ids:
        print(f"没有 id={pid} 的 provider。现有：{', '.join(ids)}")
        return 1
    path = save_user_config({"active_provider": pid})
    p = [x for x in cfg["providers"] if x.get("id") == pid][0]
    print(f"已切换 active_provider → {pid}（{p.get('name')} / {p.get('model')}）")
    print(f"写入 {path}")
    print("注意：已生成的数据不会自动更新，需要重跑 pipeline.py / _add_one.py")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--probe", nargs="?", const="__all__", default=None)
    ap.add_argument("--add", metavar="ID")
    ap.add_argument("--use", metavar="ID")
    ap.add_argument("--presets", action="store_true")
    ap.add_argument("--config", default=None)
    a = ap.parse_args()

    if a.presets:
        cmd_presets()
        return 0

    cfg = load_config(extra=a.config, require_provider=not a.add)

    if a.add:
        return cmd_add(cfg, a)
    if a.use:
        return cmd_use(cfg, a.use)
    if a.probe is not None:
        only = None if a.probe == "__all__" else a.probe
        return cmd_probe(cfg, only)
    cmd_list(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
