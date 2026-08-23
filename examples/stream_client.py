#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""使用示例：把任意 OpenAI 兼容客户端指向 ollama-loop-guard 代理。

本示例直接用 requests 演示流式调用（与代理无依赖关系，纯客户端视角）。
运行前先启动代理与 Ollama。
"""
import json

import requests

PROXY = "http://localhost:11435/v1"  # 代理（正常时用 11434）
MODEL = "deepseek-v4-flash:0731-cloud"

payload = {
    "model": MODEL,
    "messages": [{"role": "user", "content": "用中文解释天空为什么是蓝色的。"}],
    "stream": True,
    "max_tokens": 128,
}

with requests.post(f"{PROXY}/chat/completions", json=payload, stream=True, timeout=120) as r:
    r.raise_for_status()
    for line in r.iter_lines(decode_unicode=False):
        if not line.startswith(b"data: "):
            continue
        data = line[6:]
        if data == b"[DONE]":
            break
        chunk = json.loads(data)
        delta = (chunk.get("choices") or [{}])[0].get("delta", {})
        text = delta.get("content") or delta.get("reasoning") or ""
        if text:
            print(text, end="", flush=True)
print()
