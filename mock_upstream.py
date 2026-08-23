#!/usr/bin/env python3
"""mock 上游：模拟 Ollama 慢速思考流（每 3 秒一个 reasoning chunk，永不结束）。
仅供本地测试 ollama-loop-guard 的打断链路使用。"""
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        i = 0
        try:
            for _ in range(40):
                ev = {"choices": [{"delta": {"reasoning": "思考中第%d轮 我们需要继续深入分析 不要停 " % i}}]}
                line = "data: " + json.dumps(ev) + "\n\n"
                self.wfile.write(line.encode())
                self.wfile.flush()
                i += 1
                time.sleep(3)
        except Exception:
            pass
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 11999), H).serve_forever()
