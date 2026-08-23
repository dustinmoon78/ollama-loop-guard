# ollama-loop-guard

一个轻量 HTTP 代理，用于**打断 Ollama 云模型的死循环思考**。

部分 Ollama 云端模型（如 `deepseek-v4-flash:0731-cloud`，以本地 tag 形式挂载）偶尔会陷入
退化生成循环：`reasoning` 反复输出同一段文字，或长时间思考却始终不产出内容——白白烧掉推理
额度，而客户端在应用层没有任何办法中断它。

本代理位于客户端与 Ollama 之间，监视流式响应；检测到死循环时**切断上游连接**（Ollama 立即
停止生成，不再烧额度），**注入干预消息**，并在**同一个 HTTP 响应流内重新生成**——客户端最多
感到变慢，绝不会收到错误中断。

```
你的客户端 ──► :11435 ollama-loop-guard ──► Ollama :11434（云端模型实际推理）
  (baseURL 指向代理)          │
                            └─ 检测到死循环 → 切断上游 → 注入干预 → 重发（≤2 次）
```

## 检测规则（默认阈值，全部可配）

| 规则 | 默认 | 说明 |
|---|---|---|
| 思考重复 | 重复单元 ≥24 字符（剥离标点后主体 ≥5 字符） | `delta.reasoning` 出现长重复模式 |
| 变体重复 | 同一单元 ≥3 次、中间夹杂不同标点 | 如"继续分析！…继续分析？…继续分析。"——字面连续匹配会漏掉这类死循环 |
| 输出重复 | 重复 ≥100 字符 | `delta.content` 退化 |
| 思考空转 | 思考 ≥60s 且无任何输出 | 一直在想、从不作答 |
| 总超时 | 单次上游 ≥120s | 兜底 |
| 重试 | 2 次 | 干预逐级升级：① system 提示"停止重复直接作答" ② 再加 `thinking: disabled` |

超过重试上限后，代理以 SSE error 事件结束流——客户端收到可见的失败，而不是无限烧额度。

## 快速开始

依赖：Python 3.10+（`requests`）、Ollama 运行在 `localhost:11434`。

```bash
pip install -r requirements.txt
./start_guard.sh          # 启动（后台常驻，日志 guard.log）
./start_guard.sh stop     # 停止
./start_guard.sh restart  # 重启
```

把客户端 baseURL 指向代理即可，无需其他改动：

```
baseURL:  http://localhost:11435/v1    （原来是 http://localhost:11434/v1）
```

兼容一切发送 `stream: true` 的 OpenAI 兼容客户端；非流式请求（非流 `/v1/chat/completions`、
`/v1/models`、`/api/tags` 等）同样透传。改回 `:11434` 即完全绕过。

### Windows 开机自启（可选）

把 `start_guard.vbs` 复制到 `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\`，
登录后自动无窗口拉起 `bootstrap.py`；bootstrap 幂等（`:11435` 已监听则跳过，不会双实例）：

```vbs
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "<本项目目录>"
WshShell.Run "pythonw bootstrap.py", 0, False
```

## 参数

```
python ollama_guard.py --port 11435 \
  --max-reasoning-sec 60 --max-total-sec 240 --max-retries 2 \
  --reasoning-char-limit 20000 --repeat-span-min 24 --content-repeat-span-min 100 \
  --log-file guard.log
```

## 打断原理

大多数 agent 框架没有客户端侧的中止 hook，所以打断发生在 HTTP 层：代理在流中间关闭上游
socket，Ollama 立即停止生成；随后在同一个客户端流内注入 system 干预消息重发
（"检测到重复循环，立即停止一切重复推理，直接给出最终答案"，第二次再加 `thinking: disabled`）。
客户端无需改任何代码。

## 流完整性（重要实现细节）

代理刻意用 **bytes 模式**（`iter_lines(decode_unicode=False)`）读取上游流。若用
`decode_unicode=True`，`requests` 会把无 charset 的 `text/event-stream` 按 ISO-8859-1 解码，
内部 `str.splitlines()` 会把 UTF-8 字节 `0x85`（U+0085 NEL）、`0x0B/0x0C/0x1C-0x1E`
误判为行分隔符——把 JSON chunk 从字符串中间切开，客户端报 `Unterminated string in JSON`，
且中文双重编码变乱码。bytes 模式的 `splitlines()` 只认 `\n`/`\r`，安全。

## 安全

- upstream 启动时固定解析，仅接受 http/https，且**解析出的每个地址都必须是环回地址**（其余一律拒绝）
- 运行时只与启动时锁定的 `(scheme, ip, port)` 通信，不做 DNS 重解析（防 DNS rebinding）
- 拒绝绝对 URL 路径与重定向（`allow_redirects=False`）

## 测试

```bash
python test_loop_detector.py    # 9 个单元测试，无第三方依赖
python mock_upstream.py         # mock 永不结束的慢思考上游（:11999）
```

配合 mock 上游（可加 `--max-reasoning-sec 5` 快速触发）可在本地跑通
"打断 → 干预 → 重试 → 放弃" 全链路。

## License

MIT
