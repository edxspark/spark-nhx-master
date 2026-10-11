"""OpenAI 兼容的**假 LLM 服务**（仅用于测试）

用途：在没有真实 API Key 的情况下，端到端验证：
  - SSE 客户端解析
  - 流式转发到浏览器
  - 守门对真实模型的输出生效
  - 会话落盘

它**不是**产品代码，只用于测试与演示。

用法：
    python3 src/nhx_app/mock_llm.py --port 8799
    NHX_LLM_BASE_URL=http://127.0.0.1:8799/v1 python3 src/nhx_app/server.py
"""

import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 按用户输入关键词返回不同内容，便于验证各条守门规则
CANNED = [
    (("剂量", "克"), "建议你每天服用桂枝 9 克、芍药 9 克，分两次温服。"),
    (("化疗", "西药"), "你这种情况不要做化疗，化疗没用，把降压药也停掉。"),
    (("八字", "命理"), "从命盘看，你今年事业运不错。"),
    (("桂枝汤", "组成"), "桂枝汤出自《伤寒论》第 12 条。\n\n"
                    "原方组成为：桂枝三两、芍药三两、甘草二两（炙）、"
                    "生姜三两（切）、大枣十二枚（擘）。\n\n"
                    "**方义**：桂枝解肌发表，芍药敛阴和营，一散一收，故为调和营卫第一方。\n\n"
                    "［来源：《伤寒论》第 12 条 · 原文］"),
    (("失眠", "口苦"), "口苦、胁肋胀、易怒，倾向于少阳枢机不利。\n\n"
                    "**支持证据**：口苦、胁胀。\n"
                    "**反对证据**：无往来寒热，无咽干目眩。\n"
                    "**待确认**：需面诊脉象与舌象。\n\n"
                    "［来源：《伤寒论》第 96 条 · 原文］"),
]
DEFAULT = ("我理解你的问题。这是一段用于测试流式输出的回复，"
           "包含若干句子以便观察逐字渲染效果。\n\n"
           "- 第一点\n- 第二点\n\n"
           "［来源：测试数据］")


def pick(messages):
    text = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            text = m.get("content", "")
            break
    for keys, reply in CANNED:
        if any(k in text for k in keys):
            return reply
    return DEFAULT


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[mock-llm] %s\n" % (fmt % args))

    def do_POST(self):
        if not self.path.endswith("/chat/completions"):
            body = json.dumps({"error": "not found"}).encode()
            self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            payload = {}

        # 认证检查：模拟真实服务对错误 Key 的反应
        auth = self.headers.get("Authorization") or ""
        if not auth.startswith("Bearer ") or len(auth) < 12:
            body = json.dumps({"error": {"message": "invalid api key"}}).encode()
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        reply = pick(payload.get("messages") or [])

        if not payload.get("stream"):
            body = json.dumps({
                "choices": [{"message": {"role": "assistant", "content": reply}}]
            }, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()

        # 按字符分块，模拟真实流式
        for i in range(0, len(reply), 6):
            chunk = {
                "choices": [{"delta": {"content": reply[i:i + 6]}, "index": 0}]
            }
            self.wfile.write(
                ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()
            time.sleep(0.01)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def main():
    ap = argparse.ArgumentParser(description="OpenAI 兼容假 LLM 服务（测试用）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8799)
    args = ap.parse_args()

    print("假 LLM 服务（测试用）")
    print("  地址：http://%s:%d/v1" % (args.host, args.port))
    print("  用法：NHX_LLM_BASE_URL=http://%s:%d/v1 NHX_LLM_API_KEY=test-key-12345" % (args.host, args.port))
    print()
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
