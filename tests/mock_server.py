#!/usr/bin/env python3
"""Local mock of the DeepSeek web API for testing the native client."""
import base64
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, __file__.rsplit('/', 2)[0])
from api import _sponge_digest  # noqa: E402

SALT = "mock-salt"
EXPIRE_AT = 1234567890
ANSWER = 7
PREFIX = f"{SALT}_{EXPIRE_AT}_"
CHALLENGE = _sponge_digest((PREFIX + str(ANSWER)).encode()).hex()
MESSAGE_ID = 4242
UPLOADED = {}
SESSIONS = [
    {"id": "chat-1", "title": "本地 Mock 对话", "updated_at": 1794740000,
     "pinned": False, "model_type": "default"},
    {"id": "chat-2", "title": "英伟达 CEO 是谁", "updated_at": 1794730000,
     "pinned": False, "model_type": "default"},
    {"id": "chat-3", "title": "Ubuntu 快捷键", "updated_at": 1794720000,
     "pinned": False, "model_type": "default"},
    {"id": "chat-4", "title": "Python 快速排序", "updated_at": 1794710000,
     "pinned": False, "model_type": "default"},
    {"id": "chat-5", "title": "旧对话", "updated_at": 1794700000,
     "pinned": False, "model_type": "default"},
]
if os.environ.get("MOCK_COUNT"):
    count = int(os.environ["MOCK_COUNT"])
    SESSIONS = [{"id": "chat-%d" % i, "title": "测试会话 %d" % i,
                 "updated_at": 1794740000 - i * 3600,
                 "pinned": False, "model_type": "default"}
                for i in range(1, count + 1)]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print("[mock]", fmt % args, flush=True)

    def _send_json(self, payload, status=200):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _ok(self, biz):
        self._send_json({"code": 0, "msg": "", "data": {"biz_code": 0, "biz_msg": "", "biz_data": biz}})

    def _check_auth(self):
        if self.headers.get("Authorization") != "Bearer test-token":
            self._send_json({"code": 40003, "msg": "Authorization Failed", "data": None}, 401)
            return False
        return True

    def _send_sse(self, frames):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for frame in frames:
            self.wfile.write(("data: " + json.dumps(frame, ensure_ascii=False) + "\n\n").encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True

    def do_GET(self):
        if not self._check_auth():
            return
        path = urlparse(self.path).path
        if path == "/api/v0/chat_session/fetch_page":
            query = parse_qs(urlparse(self.path).query)
            count = int((query.get("count") or ["30"])[0])
            cursor = (query.get("lte_cursor.updated_at") or [None])[0]
            items = SESSIONS
            if cursor:
                items = [x for x in items if x["updated_at"] <= int(cursor)]
            page = items[:count]
            self._ok({"has_more": len(items) > len(page), "chat_sessions": page})
        elif path == "/api/v0/users/current":
            self._ok({"id": 10086, "email": "mock@example.com",
                      "id_profile": {"name": "Mock 用户",
                                     "email": "mock@example.com"}})
        elif path == "/api/v0/index/prepare":
            self._ok({})
        elif path == "/api/v0/file/fetch_files":
            ids = parse_qs(urlparse(self.path).query).get("file_ids", [])
            ids = [x for group in ids for x in group.split(",")]
            self._ok({"files": [
                {"id": fid, "file_name": UPLOADED.get(fid, "file.txt"),
                 "file_size": 1234, "status": "SUCCESS", "is_image": False}
                for fid in ids
            ]})
        elif path == "/api/v0/chat/history_messages":
            self._ok({"cache_control": "REPLACE", "chat_messages": [
                {"message_id": 1, "parent_id": None, "role": "USER",
                 "fragments": [{"type": "REQUEST", "content": "你好"}]},
                {"message_id": 2, "parent_id": 1, "role": "ASSISTANT",
                 "fragments": [{"type": "RESPONSE", "content": "你好，这是历史消息。"}]},
            ]})
        else:
            self._send_json({"code": 404, "msg": "not found"}, 404)

    def do_POST(self):
        if not self._check_auth():
            return
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if path == "/api/v0/file/upload_file":
            match = re.search(rb'filename="([^"]*)"', raw)
            filename = match.group(1).decode("utf-8", "replace") if match else "file.bin"
            fid = "file-%d" % (len(UPLOADED) + 1)
            UPLOADED[fid] = filename
            self._ok({"id": fid, "file_name": filename, "file_size": len(raw),
                      "status": "SUCCESS", "is_image": False,
                      "audit_result": "pass"})
            return
        body = json.loads(raw or b"{}")
        if path == "/api/v0/chat_session/create":
            self._ok({"chat_session": {"id": "chat-new", "title": "", "updated_at": 0,
                                       "title_type": "USER", "pinned": False,
                                       "version": 1, "model_type": "default"}})
        elif path == "/api/v0/share/create":
            self._ok({"share_id": "mock-share"})
        elif path == "/api/v0/chat/create_pow_challenge":
            self._ok({"challenge": {
                "algorithm": "DeepSeekHashV1", "challenge": CHALLENGE, "salt": SALT,
                "difficulty": 50, "signature": "mock-signature",
                "expire_at": EXPIRE_AT, "expire_after": 3600, "target_path": path,
            }})
        elif path == "/api/v0/index/query":
            query = str(body.get("query") or "")
            events = [
                ("item", {
                    "chat_session_id": "chat-2",
                    "message_id": 12,
                    "message_role": "ASSISTANT",
                    "parent_message_id": 11,
                    "timestamp": 1794730000,
                    "seq_id": 12,
                    "chat_session_title": {"parts": [{"text": "英伟达 CEO 是谁"}]},
                    "content": {"parts": [{"text": "搜索结果：" + query}]},
                    "is_think": False,
                    "files": [],
                }),
                ("chat_session_title_item", {
                    "id": "chat-3",
                    "title": {"parts": [{"text": "Ubuntu 快捷键"}]},
                    "timestamp": 1794720000,
                    "seq_id": 11,
                    "model_type": "default",
                }),
                ("status", {"queried_seq_id": 12}),
                ("close", {"close_reason": "success"}),
            ]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            for event, data in events:
                self.wfile.write(("event: %s\ndata: %s\n\n" % (
                    event, json.dumps(data, ensure_ascii=False))).encode())
                self.wfile.flush()
            self.close_connection = True
        elif path == "/api/v0/chat/regenerate":
            header = self.headers.get("x-ds-pow-response") or ""
            try:
                decoded = json.loads(base64.b64decode(header))
                assert decoded["answer"] == ANSWER
            except Exception as exc:
                self._send_json({"code": 40301, "msg": f"bad pow: {exc}"}, 403)
                return
            self._send_sse([
                {"v": {"response": {"message_id": 5001, "parent_id": 1,
                                    "fragments": [
                    {"type": "RESPONSE", "content": "这是重新生成的回答，"}]}}},
                {"v": "已替换原来的回答。"},
            ])
        elif path == "/api/v0/chat/edit_message":
            header = self.headers.get("x-ds-pow-response") or ""
            try:
                decoded = json.loads(base64.b64decode(header))
                assert decoded["answer"] == ANSWER
            except Exception as exc:
                self._send_json({"code": 40301, "msg": f"bad pow: {exc}"}, 403)
                return
            self._send_sse([
                {"v": {"response": {"message_id": 5002, "parent_id": body.get("message_id"),
                                    "fragments": [
                    {"type": "RESPONSE",
                     "content": "这是编辑后的回答：%s" % body.get("prompt", "")}]}}},
            ])
        elif path == "/api/v0/chat/completion":
            header = self.headers.get("x-ds-pow-response") or ""
            try:
                decoded = json.loads(base64.b64decode(header))
                assert decoded["answer"] == ANSWER
                assert decoded["challenge"] == CHALLENGE
            except Exception as exc:
                self._send_json({"code": 40301, "msg": f"bad pow: {exc}"}, 403)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            ref_ids = body.get("ref_file_ids") or []
            text = "收到 %d 个附件；你好，" % len(ref_ids) if ref_ids else "你好，"
            fragments = [
                {"type": "THINK", "content": "先想一下。"},
            ]
            if body.get("search_enabled"):
                fragments.append({
                    "type": "TOOL_SEARCH",
                    "results": [{
                        "url": "https://example.com/deepseek",
                        "title": "DeepSeek 官方介绍",
                        "snippet": "DeepSeek 是一家专注于通用人工智能的公司。",
                        "siteName": "example.com",
                        "publishedAt": "2026-11-01",
                        "provider": "mock",
                    }],
                })
                text = "根据资料[citation:1]，"
            fragments.append({"type": "RESPONSE", "content": text})
            frames = [
                {"v": {"response": {"message_id": MESSAGE_ID, "parent_id": 1,
                                    "fragments": fragments}}},
                {"p": "response/fragments/-1/content", "o": "APPEND", "v": "这是原生客户端收到的流式回答"},
                {"v": "。"},
            ]
            for frame in frames:
                self.wfile.write(("data: " + json.dumps(frame, ensure_ascii=False) + "\n\n").encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self.close_connection = True
        else:
            self._send_json({"code": 404, "msg": "not found"}, 404)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"mock listening on 127.0.0.1:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
