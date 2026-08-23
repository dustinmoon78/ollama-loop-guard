# ollama-loop-guard

Ollama 云模型死循环思考打断代理。拦截客户端 → Ollama 的流式请求，检测思考/输出死循环
（重复退化、思考空转、总超时），**断开上游打断生成**，注入干预后**在同一响应流内重新生成**——
客户端无感知，不中断对话。

针对 Ollama 云端推理模型（如 `deepseek-v4-flash:0731-cloud`）在本地以 tag 形式挂载、
思考阶段可能陷入无限循环（reasoning 重复输出、长时间无进展）的场景设计，本地搭建
OpenAI 兼容中转时同样适用。

## 架构

```
ZCode / DSH / 任意客户端 ──► :11435 本代理 ──► Ollama :11434（cloud 模型实际推理）
       (baseURL 切到代理)         │
                                 └─ 检测死循环 → 断开上游 → 注入干预重发（≤2 次）
```

## 检测规则（默认阈值，全部可配）

| 规则 | 默认 | 说明 |
|---|---|---|
| 思考重复 | 重复串 ≥24 字符（剥离标点后主体 ≥5 字符） | `delta.reasoning` 出现长重复模式 |
| 输出重复 | 重复串 ≥100 字符 | `delta.content` 重复退化 |
| 思考空转 | 思考 ≥60s 且无任何输出 | reasoning 一直出但 content 为空 |
| 总超时 | 单次上游 ≥120s | 兜底 |
| 重试 | 2 次 | 干预逐级升级：① system 提示"停止重复直接作答" ② 再加 `thinking:disabled` |

命中后：断开上游（Ollama 立即停止生成，不再烧推理额度）→ 注入干预重发；
重试仍死循环 → 以 SSE error 事件结束（客户端可见失败，不无限重试）。

## 启动

```bash
./start_guard.sh          # 启动（后台常驻，日志 guard.log）
./start_guard.sh stop     # 停止
./start_guard.sh restart  # 重启
```

Windows 开机自启（可选）：把 `start_guard.vbs` 复制到
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\`，
登录后自动无窗口拉起（幂等：11435 已监听则跳过，不会双实例）：

```vbs
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "<此项目目录>"
WshShell.Run "pythonw bootstrap.py", 0, False
```

依赖：Python 3.10+（`requests`）、Ollama（默认上游 `http://localhost:11434`）。

## 参数

```
python ollama_guard.py --port 11435 \
  --max-reasoning-sec 60 --max-total-sec 120 --max-retries 2 \
  --reasoning-char-limit 20000 --repeat-span-min 24 --content-repeat-span-min 100 \
  --log-file guard.log
```

## 接入客户端

把客户端的 Ollama baseURL 从 `http://localhost:11434/v1` 改为 `http://localhost:11435/v1`，
其余不变。回退：改回 11434 即完全绕过。

## 安全边界

- upstream 启动时固定解析，仅允许 http/https 且解析结果必须全部为环回地址
- 运行时转发固定使用启动时锁定的 (scheme, ip, port)，不再做域名解析（防 DNS rebinding）
- 拒绝绝对 URL 路径、拒绝重定向（`allow_redirects=False`）

## 流完整性（重要实现细节）

上游流式响应（`Content-Type: text/event-stream` 无 charset）必须用 **bytes 模式**
读取转发（`iter_lines(decode_unicode=False)`）。若用 `decode_unicode=True`，
requests 会按 ISO-8859-1 解码，内部 `str.splitlines()` 会把 UTF-8 中文里的
`0x85`(U+0085 NEL)、`0x0B/0x0C/0x1C-0x1E` 字节误判为行分隔符，把 JSON chunk
从字符串中间切开，客户端报 `Unterminated string in JSON`，且中文双重编码变乱码。
bytes 的 `splitlines()` 只认 `\n`/`\r`，安全。

## 测试

- 单元：LoopDetector 重复/空转/标点噪声/超时
- mock 上游：`mock_upstream.py` 模拟永不结束的慢速思考流（每 3 秒一个 reasoning chunk，
  端口 11999），验证打断 → 干预重发 → 超限 → SSE error 全链路
- 真实 Ollama 云模型：正常请求无误打断；大 `max_tokens` 复杂请求不误触发

## License

MIT
