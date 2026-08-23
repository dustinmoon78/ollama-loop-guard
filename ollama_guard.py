#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ollama-loop-guard：Ollama 云模型死循环思考打断代理
====================================================
本地 HTTP 拦截代理：客户端(baseURL → 本代理端口) ──转发──► Ollama (:11434)

功能：
1. 透传 /v1/chat/completions 与其余路径（/v1/models、/api/tags 等）
2. 流式检测死循环（reasoning/content 重复退化、思考空转、总超时）
3. 命中 → 断开上游（Ollama 立即停止生成）→ 注入干预重发（最多 --max-retries 次）
   —— 重试在同一个 HTTP 响应流内继续（客户端无感知，只是变慢）
4. 重试仍死循环 → 以 SSE error 事件结束（不无限烧额度）

安全边界（SSRF 防护）：
- upstream 在启动时固定解析，仅允许 http/https 且解析结果必须全部为环回地址
- 运行时转发固定使用启动时锁定的 (scheme, ip, port)，不再做域名解析（防 DNS rebinding）
- 拒绝绝对 URL 路径、拒绝重定向（allow_redirects=False）

用法：
    python ollama_guard.py --port 11435
"""
import argparse
import ipaddress
import json
import logging
import re
import socket
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import requests

log = logging.getLogger("ollama-guard")

# 干预消息（按重试次数逐级升级）
INTERVENTIONS = [
    ("[系统干预] 检测到前一次生成陷入重复/死循环（重复输出或长时间无进展），已中断。"
     "请立即停止一切重复推理与试探，直接基于已有信息给出最终答案。"),
    ("[系统干预·第二次] 再次检测到重复循环。禁止继续推理，立即给出最终答案；"
     "若确实无法解决，请直接说明无法解决，不要重复尝试。"),
]


class LoopDetector:
    """流式死循环检测器（每个请求尝试一个实例）"""

    # 重复判定时剥离的标点/空白（用于主体长度判定）
    _STRIP = re.compile(r"[\s，。、；：！？,.?!;:()\[\]{}<>\"'`~\-—…\n\t\r]")
    # 带标点/空白变体的重复：同一句以不同标点反复出现（字面连续匹配会漏掉）
    _REP_VARIANT = re.compile(
        r"(.{6,100})[\s，。、；：！？,.?!;:()\[\]{}<>\"'`~\-—…]{1,8}"
        r"\1[\s，。、；：！？,.?!;:()\[\]{}<>\"'`~\-—…]{1,8}\1", re.S)

    def __init__(self, cfg):
        self.cfg = cfg
        self.reasoning_text = ""
        self.content_text = ""
        self.started = time.monotonic()
        self._rep = re.compile(r"(.{8,200})\1{2,}", re.S)

    def feed_reasoning(self, chunk: str):
        self.reasoning_text += chunk
        if len(self.reasoning_text) > self.cfg.reasoning_char_limit:
            return "reasoning overflow ({} chars)".format(self.cfg.reasoning_char_limit)
        if self._repeat(self.reasoning_text, self.cfg.repeat_span_min):
            return "reasoning repetition"
        return None

    def feed_content(self, chunk: str) -> str | None:
        self.content_text += chunk
        if self._repeat(self.content_text, self.cfg.content_repeat_span_min):
            return "content repetition"
        return None

    def check_elapsed(self) -> str | None:
        elapsed = time.monotonic() - self.started
        if self.cfg.max_total_sec and elapsed > self.cfg.max_total_sec:
            return "total timeout ({}s)".format(self.cfg.max_total_sec)
        if self.cfg.max_reasoning_sec and not self.content_text and elapsed > self.cfg.max_reasoning_sec:
            return "reasoning stall ({}s, no output)".format(self.cfg.max_reasoning_sec)
        return None

    def _repeat(self, text: str, span_min: int) -> bool:
        """窗口内是否存在长重复片段。

        字面连续重复用 _rep 匹配（须剥离标点后主体 ≥5 字符）；
        另用 _REP_VARIANT 匹配"同一句带不同标点反复输出"的变体
        （如 "我们需要继续分析！…我们需要继续分析？…我们需要继续分析。"），
        字面连续匹配会漏掉这类死循环。
        """
        if len(text) < span_min:
            return False
        text = text[-4096:]
        m = self._rep.search(text)
        if m:
            core = self._STRIP.sub("", m.group(1))
            if len(core) < 5:
                return False
            return len(m.group(0)) >= span_min
        return self._REP_VARIANT.search(text) is not None


def intervene_intervention(payload: dict, attempt: int) -> dict:
    """重发时注入干预：attempt 0 → system 提示；attempt 1 → 再加 thinking disabled"""
    body = dict(payload)
    msgs = list(body.get("messages", []))
    body["messages"] = [{"role": "system", "content": INTERVENTIONS[attempt]}] + msgs
    if attempt >= 1 and isinstance(body.get("thinking"), dict):
        body["thinking"] = {"type": "disabled"}
    return body


class GuardHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    guard = None  # GuardServer 实例

    # ---------------- 入口 ----------------
    def do_GET(self):  # noqa: N802
        self._forward(None)

    def do_POST(self):  # noqa: N802
        ln = int(self.headers.get("Content-Length", 0) or 0)
        self._forward(self.rfile.read(ln) if ln else None)

    def _forward(self, body):
        try:
            target = self._target_url()
            payload = None
            if body:
                try:
                    payload = json.loads(body.decode("utf-8"))
                except Exception:
                    payload = None
            if payload and payload.get("stream"):
                self._stream_proxy(target, payload)
            else:
                self._plain_proxy(target, body, payload)
        except ValueError as e:
            self._send_json_error(400, str(e))
        except (BrokenPipeError, ConnectionResetError):
            log.info("client disconnected, upstream aborted")
        except Exception:
            log.error("handler error:\n%s", traceback.format_exc())
            try:
                self._send_json_error(500, "internal error")
            except Exception:
                pass

    # ---------------- 安全与基础 ----------------
    def _target_url(self) -> str:
        """仅允许拼接固定 upstream 上合法的相对路径（路径白名单 + 拒绝绝对 URL）"""
        p = self.path
        if not p.startswith("/") or p.startswith("//"):
            raise ValueError("invalid request path")
        if any(ord(c) < 32 for c in p):
            raise ValueError("control chars in path")
        sp = urlsplit(p)
        if sp.scheme or sp.netloc:
            raise ValueError("absolute URL path not allowed")
        return self.guard.upstream + p

    def _upstream_headers(self) -> dict:
        hdrs = {}
        for k, v in self.headers.items():
            kl = k.lower()
            if kl in ("host", "content-length", "connection", "transfer-encoding", "accept-encoding"):
                continue
            hdrs[k] = v
        return hdrs

    def _log(self, fmt, *args):
        rid = self.headers.get("x-request-id", "-")
        log.info("[%s] %s", rid, fmt % args)

    def _send(self, status, ctype, body=None, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        if body is not None:
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.end_headers()
        self.wfile.flush()

    def _send_json_error(self, status, msg):
        body = json.dumps({"error": {"message": msg, "type": "ollama_loop_guard"}}).encode("utf-8")
        self._send(status, "application/json", body)

    # ---------------- chunked SSE 工具 ----------------
    def _chunk(self, s):
        b = s.encode("utf-8") if isinstance(s, str) else s
        self.wfile.write(b"%x\r\n%s\r\n" % (len(b), b))
        self.wfile.flush()

    def _chunk_end(self):
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _send_sse_event(self, text: str):
        self._chunk(text if text.endswith("\n\n") else text + "\n\n")

    # ---------------- 流式（核心） ----------------
    def _stream_proxy(self, url, payload):
        cfg = self.guard.cfg
        st = {"headers": False}
        attempt = 0
        while True:
            result = self._stream_attempt(url, payload, st)
            if result is None:
                return  # ok / client_gone
            if isinstance(result, tuple):  # 上游 HTTP 错误
                code, msg = result
                if not st["headers"]:
                    self._send_json_error(code, msg)
                else:
                    self._send_sse_event("data: " + json.dumps(
                        {"error": {"message": msg, "type": "upstream_error"}}, ensure_ascii=False) + "\n\n")
                    self._chunk_end()
                return
            # 死循环命中（result = reason 字符串）
            if attempt >= cfg.max_retries:
                self._log("GIVE UP after %d retries: %s", cfg.max_retries, result)
                self._send_sse_event("data: " + json.dumps(
                    {"error": {"message": "deadloop after %d retries (%s)" % (cfg.max_retries, result),
                               "type": "ollama_loop_guard_deadloop"}}, ensure_ascii=False) + "\n\n")
                self._chunk_end()
                return
            self._log("DEADLOOP(%s) -> retry %d (intervene)", result, attempt + 1)
            payload = intervene_intervention(payload, attempt)
            attempt += 1

    def _stream_attempt(self, url, payload, st):
        """单次上游尝试；同一响应流内继续。返回 None=完成；tuple=上游错误；str=死锁原因"""
        det = LoopDetector(self.guard.cfg)
        try:
            resp = requests.post(url, headers=self._upstream_headers(), json=payload,
                                 stream=True, timeout=(10, None), allow_redirects=False)
        except requests.exceptions.RequestException as e:
            return (502, "upstream unreachable: %s" % e)
        with resp:
            if resp.status_code != 200:
                return (resp.status_code, resp.text[:300])
            if not st["headers"]:
                self.send_response(200)
                self.send_header("Content-Type", resp.headers.get("Content-Type", "text/event-stream"))
                rid = resp.headers.get("x-request-id")
                if rid:
                    self.send_header("x-request-id", rid)
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                st["headers"] = True
            try:
                # 注意：必须用 decode_unicode=False（bytes）。上游 Content-Type 无 charset 时
                # decode_unicode=True 会按 ISO-8859-1 解码，iter_lines 的 splitlines() 会把 UTF-8
                # 中文里的 0x85/0x0B/0x0C 等字节当作换行符，把 JSON chunk 从字符串中间切开，
                # 客户端解析失败（ZCode "Unterminated string in JSON"）。bytes 模式只认 \n\r。
                for raw in resp.iter_lines(decode_unicode=False):
                    hit = None
                    if raw.startswith(b"data: "):
                        try:
                            ev = json.loads(raw[6:])
                            delta = (ev.get("choices") or [{}])[0].get("delta", {})
                            r, c = delta.get("reasoning"), delta.get("content")
                            if r:
                                hit = det.feed_reasoning(r)
                            elif c:
                                hit = det.feed_content(c)
                        except Exception:
                            pass
                    if not hit:
                        hit = det.check_elapsed()
                    if hit:
                        return hit  # 断开上游 → Ollama 停止
                    self._chunk(raw + b"\n")
                self._chunk_end()
                return None
            except (BrokenPipeError, ConnectionAbortedError):
                return "client_gone"
            except Exception:
                log.exception("stream loop error")
                return None

    # ---------------- 非流式 ----------------
    def _plain_proxy(self, url, body, payload):
        cfg = self.guard.cfg
        method = "POST" if body else "GET"
        attempt = 0
        while True:
            det = LoopDetector(cfg)
            req_body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else body
            try:
                resp = requests.request(method, url, headers=self._upstream_headers(), data=req_body,
                                        stream=False, timeout=(30, cfg.max_total_sec or 300),
                                        allow_redirects=False)
            except requests.exceptions.RequestException as e:
                self._send_json_error(502, "upstream unreachable: %s" % e)
                return
            with resp:
                data = resp.content
                if resp.status_code != 200:
                    self._send(resp.status_code, resp.headers.get("Content-Type", "application/json"), data)
                    return
                hit = None
                if payload:
                    try:
                        obj = json.loads(data)
                        msg = (obj.get("choices") or [{}])[0].get("message", {})
                        for i in range(0, len(msg.get("reasoning") or ""), 64):
                            hit = det.feed_reasoning(msg["reasoning"][i:i + 64])
                            if hit:
                                break
                        if not hit:
                            for i in range(0, len(msg.get("content") or ""), 64):
                                hit = det.feed_content(msg["content"][i:i + 64])
                                if hit:
                                    break
                    except Exception:
                        pass
                if not hit:
                    self._send(200, resp.headers.get("Content-Type", "application/json"), data)
                    return
                if attempt >= cfg.max_retries:
                    self._send_json_error(502, "deadloop in non-stream response after %d retries (%s)"
                                          % (cfg.max_retries, hit))
                    return
                self._log("non-stream deadloop(%s) -> retry %d", hit, attempt + 1)
                payload = intervene_intervention(payload, attempt)
                attempt += 1


class GuardServer:
    def __init__(self, cfg):
        self.cfg = cfg
        raw = cfg.upstream.rstrip("/")
        u = urlsplit(raw)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError("upstream must be http(s)://host[:port]")
        port = u.port or (443 if u.scheme == "https" else 80)
        try:
            infos = socket.getaddrinfo(u.hostname, port)
        except socket.gaierror as e:
            raise ValueError("cannot resolve upstream host %r: %s" % (u.hostname, e))
        addrs = [info[4][0] for info in infos]
        if not addrs or not all(ipaddress.ip_address(a).is_loopback for a in addrs):
            raise ValueError("upstream must resolve to loopback addresses only, got %r" % addrs)
        # 固定到启动时解析出的第一个环回地址，不再运行时解析（防 DNS rebinding）
        self.upstream = "%s://%s:%d" % (u.scheme, addrs[0], port)
        self.httpd = ThreadingHTTPServer((cfg.host, cfg.port), GuardHandler)
        GuardHandler.guard = self


def parse_args():
    p = argparse.ArgumentParser(description="Ollama 死循环思考打断代理")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=11435)
    p.add_argument("--upstream", default="http://localhost:11434")
    p.add_argument("--max-reasoning-sec", type=float, default=60, help="思考阶段无输出超过该秒数 → 打断")
    p.add_argument("--max-total-sec", type=float, default=120, help="单次上游请求总时长上限")
    p.add_argument("--max-retries", type=int, default=2, help="死锁重试次数（干预逐级升级）")
    p.add_argument("--reasoning-char-limit", type=int, default=20000, help="思考字符量上限")
    p.add_argument("--repeat-span-min", type=int, default=24, help="思考重复判定最小重复串长度")
    p.add_argument("--content-repeat-span-min", type=int, default=100, help="输出重复判定最小重复串长度")
    p.add_argument("--log-file", default=None, help="日志文件路径（默认 stdout）")
    return p.parse_args()


def main():
    cfg = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=open(cfg.log_file, "a", encoding="utf-8") if cfg.log_file else sys.stdout,
    )
    srv = GuardServer(cfg)
    log.info("ollama-loop-guard listening on http://%s:%d -> %s", cfg.host, cfg.port, srv.upstream)
    log.info("thresholds: reasoning_stall=%ss total=%ss retries=%d repeat_span=%d/%d",
             cfg.max_reasoning_sec, cfg.max_total_sec, cfg.max_retries,
             cfg.repeat_span_min, cfg.content_repeat_span_min)
    try:
        srv.httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("stopped")


if __name__ == "__main__":
    main()
