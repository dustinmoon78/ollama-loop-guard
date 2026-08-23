#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bootstrap：幂等启动 ollama-loop-guard（供开机自启 / 手动调用）。

- 探测 127.0.0.1:11435 已有服务 → 直接退出（避免 Windows 双绑定双实例坑）
- 未运行 → 无窗口拉起 ollama_guard.py，等待 3 秒确认
用法: python bootstrap.py
"""
import os
import socket
import subprocess
import sys
import time

PORT = 11435
HERE = os.path.dirname(os.path.abspath(__file__))


def is_alive() -> bool:
    s = socket.socket()
    s.settimeout(1)
    try:
        s.connect(("127.0.0.1", PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


def main() -> int:
    if is_alive():
        print("guard already running on :%d" % PORT)
        return 0
    logf = open(os.path.join(HERE, "guard.log"), "a", encoding="utf-8")
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen(
        [sys.executable, "-u", os.path.join(HERE, "ollama_guard.py"),
         "--port", str(PORT), "--log-file", os.path.join(HERE, "guard.log")],
        cwd=HERE, stdout=logf, stderr=logf, creationflags=flags,
    )
    time.sleep(3)
    if is_alive():
        print("guard started on :%d" % PORT)
        return 0
    print("guard FAILED to start, check guard.log")
    return 1


if __name__ == "__main__":
    sys.exit(main())
