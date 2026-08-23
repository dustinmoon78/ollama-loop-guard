#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LoopDetector 单元测试：重复 / 空转 / 标点噪声 / 超时 / 正常流。

运行：python test_loop_detector.py   （无第三方依赖，断言失败即非零退出）
"""
import time

import ollama_guard as og


class Cfg:
    reasoning_char_limit = 20000
    content_repeat_span_min = 100
    repeat_span_min = 24
    max_total_sec = 120
    max_reasoning_sec = 60


def test_repetition_detected():
    det = og.LoopDetector(Cfg())
    chunk = "我们需要继续深入分析问题 我们需要继续深入分析问题 我们需要继续深入分析问题 我们需要继续深入分析问题 我们需要继续深入分析问题 我们需要继续深入分析问题 我们需要继续深入分析问题 我们需要继续深入分析问题 我们需要继续深入分析问题 "
    hit = det.feed_reasoning(chunk)
    assert hit == "reasoning repetition", hit


def test_no_false_positive_on_normal_text():
    det = og.LoopDetector(Cfg())
    text = "用户要求写一段中文说明，解释天空为什么是蓝色的。这涉及瑞利散射：大气中的分子与阳光相互作用，"
    text += "波长较短的蓝光被散射得最多，因此从地面看天空呈现蓝色。需要解释得简单直接，避免复杂术语。"
    hit = None
    for i in range(0, len(text), 16):
        hit = det.feed_reasoning(text[i:i + 16])
        assert hit is None, f"正常文本误报: {hit}"


def test_punctuation_noise_not_flagged():
    det = og.LoopDetector(Cfg())
    # 同一标点反复出现（如省略号），主体不含重复 → 不应判死循环
    hit = det.feed_reasoning("。，。，。，。，。，。，。，。，。，。，。，。，。，。，。，。，。，。，。，")
    assert hit is None, hit


def test_repeat_with_interleaved_punctuation_flagged():
    det = og.LoopDetector(Cfg())
    # 主体重复但夹杂标点/空白 → 剥离后主体够长，仍应判死循环
    chunk = "我们需要继续深入分析！我们需要继续深入分析？我们需要继续深入分析。我们需要继续深入分析；我们需要继续深入分析、我们需要继续深入分析——我们需要继续深入分析：我们需要继续深入分析。"
    hit = det.feed_reasoning(chunk)
    assert hit == "reasoning repetition", hit


def test_reasoning_overflow():
    det = og.LoopDetector(Cfg())
    hit = det.feed_reasoning("x" * (Cfg.reasoning_char_limit + 10))
    assert hit and hit.startswith("reasoning overflow"), hit


def test_content_repetition():
    det = og.LoopDetector(Cfg())
    chunk = "同样的内容反复输出 " * 30
    hit = det.feed_content(chunk)
    assert hit == "content repetition", hit


def test_reasoning_stall():
    det = og.LoopDetector(Cfg())
    time.sleep(0.01)  # 强制已过启动瞬间
    assert det.check_elapsed() is None  # 还没超时
    det.started = time.monotonic() - (Cfg.max_reasoning_sec + 5)
    assert det.check_elapsed() == "reasoning stall (60s, no output)"


def test_total_timeout():
    det = og.LoopDetector(Cfg())
    det.content_text = "已有输出"  # 有输出时 reasoning stall 不触发
    det.started = time.monotonic() - (Cfg.max_total_sec + 5)
    assert det.check_elapsed() == "total timeout (120s)"


def test_intervention_escalation():
    body = {"messages": [{"role": "user", "content": "hi"}], "thinking": {"type": "enabled"}}
    first = og.intervene_intervention(body, 0)
    assert first["messages"][0]["role"] == "system"
    assert "死循环" in first["messages"][0]["content"]
    assert first["thinking"]["type"] == "enabled"  # 第一次不动 thinking
    second = og.intervene_intervention(body, 1)
    assert second["thinking"]["type"] == "disabled"  # 第二次才禁用思考


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
