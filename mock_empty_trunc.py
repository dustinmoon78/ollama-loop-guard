#!/usr/bin/env python3
"""mock 上游：模拟"只出思考然后截断"和"零输出"两种 0731 中断场景。
供本地验证 guard 的空响应重试 / 自动续思考逻辑。

模式由请求体里 messages 最后一条 user 内容控制：
  首请求                 -> 只出 reasoning 若干块, 然后 finish_reason=length 截断
  重试(含"自动续接")      -> 正常输出 content 结束
用法: python mock_empty_trunc.py
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802
        ln = int(self.headers.get("Content-Length", 0) or 0)
        body = json.loads(self.rfile.read(ln) or b"{}")
        msgs = body.get("messages") or []
        last = (msgs[-1].get("content") if msgs else "") or ""
        mode = body.get("mode", "trunc")  # trunc | empty | ok
        # 首请求(无续接标记) -> 截断; 续接/重试 -> 正常输出
        truncated = "自动续接" not in last and "系统干预" not in last
        # 模式由请求体里预留字段控制；默认截断，可 "empty" 测零输出
        mode = body.get("mode", "trunc")

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        out = []

        def emit(obj):
            self.wfile.write(("data: " + json.dumps(obj) + "\n\n").encode())
            self.wfile.flush()

        if mode == "empty" and truncated:
            # 完全零输出：无思考无内容
            emit({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        elif truncated:
            # 只出思考, 无内容, 然后 length 截断
            for i in range(3):
                emit({"choices": [{"delta": {"reasoning": "第%d轮推理中 " % i}}]})
            emit({"choices": [{"delta": {}, "finish_reason": "length"}]})
        else:
            emit({"choices": [{"delta": {"content": "这是最终答案。"}}]})
            emit({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        emit({"choices": []})  # [DONE]
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 11998), H).serve_forever()