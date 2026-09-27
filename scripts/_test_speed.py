# -*- coding: utf-8 -*-
"""并发翻译的正确性 + 加速验证（不依赖真实 LLM）。

通过替换底层 _call（模拟网络耗时 + 走真实 translate_batch 的缓存逻辑）来验证：
  1) 并发批数正确性（每段都有译文且对齐）
  2) 并发相对串行明显加速
  3) 二次同文命中缓存近乎瞬时
"""
import re
import time
import importlib.util
import os

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("translator", os.path.join(HERE, "translator.py"))
tr_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tr_mod)
Translator = tr_mod.Translator

_RX = re.compile(r"⟦B(\d+)⟧")


def fake_call(self, prompt, system=None, max_tokens=2400, temperature=0.2, json_mode=False):
    time.sleep(0.05)  # 模拟单次 LLM 往返
    idxs = _RX.findall(prompt)
    if not idxs:
        return ""  # extract_terms / translate_one 用不到，返回空即可
    # 注意：真实译文长度 > 2，translate_batch 把 len<=2 视为缺失；
    # 这里返回足够长的串（译N文）以模拟真实译文，避免被误判为空。
    return "\n\n".join("⟦B%s⟧ 译%s文" % (i, i) for i in idxs)


def build_blocks(n):
    return [{"idx": i, "text": "block-%d-content" % i, "kind": "para"} for i in range(1, n + 1)]


def test_correct_and_faster():
    blocks = build_blocks(20)            # max_chars 调小 -> 多批，触发并发
    tr = Translator(None, max_chars=20)
    Translator._call = fake_call         # 注入假 LLM

    # 串行基线
    t0 = time.time()
    res_serial = tr.translate_blocks(blocks, max_workers=1)
    dt_serial = time.time() - t0

    # 并发（2 线程）
    tr2 = Translator(None, max_chars=20)
    Translator._call = fake_call
    t0 = time.time()
    res_par = tr2.translate_blocks(blocks, max_workers=2)
    dt_par = time.time() - t0

    # 缓存命中：同 tr2 已写入缓存，再跑应近瞬时
    t0 = time.time()
    res_cache = tr2.translate_blocks(blocks, max_workers=2)
    dt_cache = time.time() - t0

    assert all(res_par.get(i) == "译%d文" % i for i in range(1, 21)), "并发结果缺失/错乱"
    assert all(res_serial.get(i) == "译%d文" % i for i in range(1, 21)), "串行结果缺失/错乱"
    assert all(res_cache.get(i) == "译%d文" % i for i in range(1, 21)), "缓存结果错乱"

    print("正确: 20 段全部命中, 译文对齐 OK")
    print("串行 %.3fs | 并发(2) %.3fs | 缓存 %.3fs" % (dt_serial, dt_par, dt_cache))
    assert dt_par < dt_serial * 0.8, "并发未加速: serial=%.3f par=%.3f" % (dt_serial, dt_par)
    assert dt_cache < 0.05, "缓存未生效: cache=%.3f" % dt_cache
    print("加速验证通过: 并发约 %.1fx，缓存近瞬时" % (dt_serial / max(dt_par, 1e-6)))


if __name__ == "__main__":
    test_correct_and_faster()
    print("ALL_OK")
