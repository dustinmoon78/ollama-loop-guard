# ollama-loop-guard

> [中文文档 (Chinese)](README.zh-CN.md)

A lightweight HTTP proxy that **breaks out of infinite thinking loops** in Ollama cloud models.

Some Ollama cloud models (e.g. `deepseek-v4-flash:0731-cloud`, mounted locally as a tag) can
occasionally get stuck in a degenerate generation loop: `reasoning` repeats the same text over
and over, or thinks for minutes without producing any content — burning tokens and stalling
your coding agent / chat client with no way to interrupt it from the application layer.

This proxy sits between your client and Ollama, watches the streaming response, and when a
loop is detected it **cuts the upstream connection** (Ollama stops generating immediately,
no more tokens burned), **injects an intervention message**, and **re-generates inside the
same HTTP response stream** — the client sees the request slow down at most, never an error.

```
Your client ──► :11435 ollama-loop-guard ──► Ollama :11434 (cloud model inference)
  (point baseURL at the proxy)      │
                                   └─ loop detected → cut upstream → inject intervention → retry (≤2)
```

## Detection rules (all configurable)

| Rule | Default | Description |
|---|---|---|
| Reasoning repetition | repeat unit ≥24 chars (core ≥5 chars after stripping punctuation) | long repeated patterns in `delta.reasoning` |
| Variant repetition | same unit ≥3× with interleaved punctuation | `"think more…! think more…? think more…."` — literal-repeat regex misses this |
| Content repetition | repeat ≥100 chars | degenerated `delta.content` |
| Reasoning stall | ≥60 s of reasoning with zero content | thinking forever, never answering |
| Total timeout | ≥120 s per upstream attempt | safety net |
| Retries | 2 | escalating intervention: ① system prompt "stop repeating, answer directly" ② + `thinking: disabled` |

After `max-retries` the proxy ends the stream with an SSE error event — your client gets a
visible failure instead of burning quota forever.

## Empty-response & truncated-thinking handling

Besides dead loops, cloud models intermittently return an **empty response** (the stream ends
with zero content and zero reasoning) or get **truncated mid-thinking** (`finish_reason: length`
with reasoning but no content). The proxy retries both transparently inside the same response
stream:

| Case | Trigger | Action | After retries exhausted |
|---|---|---|---|
| Empty response | stream ends, no content & no reasoning | resend verbatim, no intervention (`--retry-empty`, `--max-empty-retries`, default 1) | **end the stream silently** (emit `[DONE]`, no error) so a client-side fallback (e.g. a Stop hook) can continue the turn |
| Truncated thinking | `finish_reason: length`, only reasoning produced | append a "continue" user message and resend with `max_tokens` ×4 (clamped to 1M) | SSE error `ollama_loop_guard_truncated` |

Empty-response retries deliberately do **not** inject an intervention prompt — the model is not
stuck, the upstream just returned nothing. Ending silently lets client-side auto-continue logic
(ZCode's Stop hook, etc.) pick up the turn without the user ever seeing an error.

## Quick start

Requirements: Python 3.10+ with `requests`, Ollama running on `localhost:11434`.

```bash
pip install -r requirements.txt
./start_guard.sh          # start (background, logs to guard.log)
./start_guard.sh stop     # graceful stop: waits for active streams (≤60s), then exits
./start_guard.sh restart  # restart
```

`stop` performs a **graceful shutdown**: it touches a marker file, the guard stops
accepting new connections and waits for in-flight streams to finish (up to
`--drain-timeout`, default 60 s) before exiting. Only if that times out does it
force-kill. This keeps your session from being cut mid-generation when the proxy
is restarted.

Point your client at the proxy — that's the only change needed:

```
baseURL:  http://localhost:11435/v1    (was http://localhost:11434/v1)
```

Works with any OpenAI-compatible client that sends `stream: true`, and also proxies
non-streaming requests (`/v1/chat/completions` non-stream, `/v1/models`, `/api/tags`, …).
Revert by switching back to `:11434`.

### Windows autostart (optional)

Copy `start_guard.vbs` to `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\`.
It launches `bootstrap.py` windowless at logon; bootstrap is idempotent (skips if `:11435`
is already listening, so no double instance):

```vbs
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "<this project dir>"
WshShell.Run "pythonw bootstrap.py", 0, False
```

## Options

```
python ollama_guard.py --port 11435 \
  --max-reasoning-sec 60 --max-total-sec 240 --max-retries 2 \
  --reasoning-char-limit 20000 --repeat-span-min 24 --content-repeat-span-min 100 \
  --retry-empty --max-empty-retries 1 \
  --log-file guard.log
```

- `--retry-empty` / `--max-empty-retries` (default 1): retry empty responses verbatim (no
  intervention) up to N times; after that the stream ends silently.
- Truncated thinking reuses the same `--max-empty-retries` budget, but appends a "continue"
  message and scales `max_tokens` ×4 (clamped to 1 000 000) on retry.

## How the interruption works

A client-side abort hook is not available in most agent stacks, so the interruption happens
at the HTTP layer: the proxy closes the upstream socket mid-stream, which makes Ollama stop
generating immediately. It then re-sends the request with an injected system message
("detected a repeated loop — stop all repeated reasoning and answer directly", escalating to
`thinking: disabled` on the second retry) inside the same client stream. No client code changes.

Empty-response and truncated-thinking retries do **not** go through this path — no intervention
prompt is injected, and (for empty responses) a retry budget is kept separate from the dead-loop
`max-retries`.

## Stream integrity (important implementation detail)

The proxy reads the upstream stream in **bytes mode** (`iter_lines(decode_unicode=False)`)
on purpose. With `decode_unicode=True`, `requests` decodes a charset-less
`text/event-stream` as ISO-8859-1, and the internal `str.splitlines()` treats UTF-8 bytes
`0x85` (U+0085 NEL), `0x0B/0x0C/0x1C-0x1E` as line separators — slicing JSON chunks in the
middle of a string. The client then fails with `Unterminated string in JSON`, and the content
is double-encoded mojibake. Bytes-mode `splitlines()` only splits on `\n`/`\r` — safe.

Another integrity rule: every interruption/retry path emits a blank line before resuming the
stream, so two consecutive SSE events never get glued onto one line (which would make a
client fail with `JSON parsing failed` on two JSON objects sharing a line).

## Security

- Upstream is resolved once at startup; only `http(s)` is accepted and **every resolved
  address must be loopback** (rejects anything else)
- At runtime the proxy talks to the startup-locked `(scheme, ip, port)` only — no DNS
  rebinding attacks
- Absolute URL paths and redirects are rejected (`allow_redirects=False`)
- The proxy listens on `127.0.0.1` by default; do not bind it to `0.0.0.0` — it has no
  authentication and forwards to your local Ollama

## Tests

```bash
python test_loop_detector.py     # 13 unit tests, no third-party deps
python mock_upstream.py          # mock never-ending slow-reasoning upstream on :11999
python mock_empty_trunc.py       # mock empty-response / truncated-thinking / ok modes on :11998
```

Run the mock upstream (optionally with `--max-reasoning-sec 5`) to exercise the full
interrupt → intervene → retry → give-up chain locally. `mock_empty_trunc.py` exercises the
empty-response retry and truncated-thinking auto-continue paths (set `"mode": "trunc"`,
`"empty"`, or `"ok"` in the request body).

## License

MIT
