"""DeepSeek 网页版协议的本地 HTTP 客户端。

不经过 WebView 的界面，只复用登录后的 token/cookie：

* 会话列表、历史消息、发送消息都直接走 chat.deepseek.com 的内部 API；
* 发送前按网页版的 DeepSeekHashV1 规则解一次 proof-of-work；
* SSE 增量解析成 (kind, text)，交给 GTK 控件渲染。

注意：这是非官方协议，DeepSeek 改版后可能失效；请只在自己的账号上使用。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterator, Optional

BASE = "https://chat.deepseek.com"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
)


class ApiError(Exception):
    pass


class AuthError(ApiError):
    pass


@dataclass
class Session:
    token: str
    cookies: dict[str, str] = field(default_factory=dict)
    user_agent: str = USER_AGENT
    user: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"token": self.token, "cookies": self.cookies,
                "user_agent": self.user_agent, "user": self.user}

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        return cls(
            token=str(data.get("token") or ""),
            cookies={str(k): str(v) for k, v in (data.get("cookies") or {}).items()},
            user_agent=str(data.get("user_agent") or USER_AGENT),
            user=dict(data.get("user") or {}),
        )


# ---------------------------------------------------------------------------
# DeepSeekHashV1（网页版 PoW 用的自定义 sponge，不是标准 SHA3）
#
# 算法来自网页版自带的 sha3_wasm / JS fallback：rate=136、capacity 参数 256、
# 输出 32 字节、padding 0x06、23 轮自定义 Keccak 变体。下面的 Python 版本和
# 网页版逐字节一致（已用网页版 JS worker 交叉验证），但速度较慢，仅作兜底。
# ---------------------------------------------------------------------------

_MASK = 0xFFFFFFFF
_RC = [
    0, 1, 0, 32898, 0x80000000, 32906, 0x80000000, 0x80008000, 0, 32907,
    0, 0x80000001, 0x80000000, 0x80008081, 0x80000000, 32777, 0, 138, 0, 136,
    0, 0x80008009, 0, 0x8000000a, 0, 0x8000808b, 0x80000000, 139,
    0x80000000, 32905, 0x80000000, 32771, 0x80000000, 32770, 0x80000000, 128,
    0, 32778, 0x80000000, 0x8000000a, 0x80000000, 0x80008081, 0x80000000, 32896,
    0, 0x80000001, 0x80000000, 0x80008008,
]
_ROT = [10, 7, 11, 17, 18, 3, 5, 16, 8, 21, 24, 4, 15, 23, 19, 13, 12, 2, 20, 14, 22, 9, 6, 1]
_PI = [1, 3, 6, 10, 15, 21, 28, 36, 45, 55, 2, 14, 27, 41, 56, 8, 25, 43, 62, 18, 39, 61, 20, 44]


def _swap(a, i, b, j):
    b[2 * j] = a[2 * i]
    b[2 * j + 1] = a[2 * i + 1]


def _chi(a, c):
    for base in range(0, 25, 5):
        for n in range(5):
            _swap(a, base + n, c, n)
        for n in range(5):
            i = 2 * (base + n)
            o = 2 * ((n + 1) % 5)
            f = 2 * ((n + 2) % 5)
            a[i] = (a[i] ^ ((~c[o]) & c[f])) & _MASK
            a[i + 1] = (a[i + 1] ^ ((~c[o + 1]) & c[f + 1])) & _MASK


def _rho_pi(a, c, w):
    _swap(a, 1, w, 0)
    for i in range(24):
        t = _ROT[i]
        r = _PI[i]
        _swap(a, t, c, 0)
        o, f = w[0], w[1]
        u = 32 - r
        s = 0 if r < 32 else 1
        w[s] = (((o << (r & 31)) & _MASK) | ((f >> (u & 31)) & _MASK)) & _MASK
        w[(s + 1) % 2] = (((f << (r & 31)) & _MASK) | ((o >> (u & 31)) & _MASK)) & _MASK
        _swap(w, 0, a, t)
        _swap(c, 0, w, 0)


def _theta(a, c, d, w):
    for t in range(5):
        n = 2 * t
        c[n] = (a[n] ^ a[n + 10] ^ a[n + 20] ^ a[n + 30] ^ a[n + 40]) & _MASK
        c[n + 1] = (a[n + 1] ^ a[n + 11] ^ a[n + 21] ^ a[n + 31] ^ a[n + 41]) & _MASK
    for t in range(5):
        _swap(c, (t + 1) % 5, w, 0)
        o, f = w[0], w[1]
        w[0] = ((o << 1) | (f >> 31)) & _MASK
        w[1] = ((f << 1) | (o >> 31)) & _MASK
        d[2 * t] = (c[((t + 4) % 5) * 2] ^ w[0]) & _MASK
        d[2 * t + 1] = (c[((t + 4) % 5) * 2 + 1] ^ w[1]) & _MASK
        for r in range(0, 25, 5):
            a[(r + t) * 2] = (a[(r + t) * 2] ^ d[2 * t]) & _MASK
            a[(r + t) * 2 + 1] = (a[(r + t) * 2 + 1] ^ d[2 * t + 1]) & _MASK


def _keccak(state):
    c = [0] * 10
    d = [0] * 10
    w = [0] * 2
    for rnd in range(1, 24):
        _theta(state, c, d, w)
        _rho_pi(state, c, w)
        _chi(state, c)
        state[0] = (state[0] ^ _RC[2 * rnd]) & _MASK
        state[1] = (state[1] ^ _RC[2 * rnd + 1]) & _MASK


def _xor_queue(q, state):
    for r in range(0, len(q), 8):
        n = r // 4
        state[n] = (state[n] ^ ((q[r + 7] << 24) | (q[r + 6] << 16)
                                | (q[r + 5] << 8) | q[r + 4])) & _MASK
        state[n + 1] = (state[n + 1] ^ ((q[r + 3] << 24) | (q[r + 2] << 16)
                                        | (q[r + 1] << 8) | q[r])) & _MASK


def _sponge_digest(data: bytes) -> bytes:
    state = [0] * 50
    queue = [0] * 136
    offset = 0
    for b in data:
        queue[offset] = b
        offset += 1
        if offset >= 136:
            _xor_queue(queue, state)
            _keccak(state)
            offset = 0
    q = list(queue)
    for i in range(offset, 136):
        q[i] = 0
    q[offset] = (q[offset] | 6) & 0xFF
    q[135] = (q[135] | 128) & 0xFF
    _xor_queue(q, state)
    _keccak(state)
    out = bytearray()
    for r in range(0, 32, 8):
        n = r // 4
        out.extend([
            state[n + 1] & 255, (state[n + 1] >> 8) & 255,
            (state[n + 1] >> 16) & 255, (state[n + 1] >> 24) & 255,
            state[n] & 255, (state[n] >> 8) & 255,
            (state[n] >> 16) & 255, (state[n] >> 24) & 255,
        ])
    return bytes(out)


def solve_pow_python(challenge: str, prefix: str, difficulty: int) -> Optional[int]:
    target = challenge.lower()
    data = prefix.encode("utf-8")
    for i in range(difficulty):
        if _sponge_digest(data + str(i).encode("ascii")).hex() == target:
            return i
    return None


def pow_header(challenge: dict, answer: int, target_path: str) -> str:
    payload = {
        "algorithm": challenge["algorithm"],
        "challenge": challenge["challenge"],
        "salt": challenge["salt"],
        "answer": answer,
        "signature": challenge["signature"],
        "target_path": target_path,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------

def _normalize_source(result: dict, index: int) -> dict:
    url = result.get("url") or result.get("link") or ""
    published = (result.get("publishedAt") or result.get("published_at")
                 or result.get("date") or "")
    try:
        stamp = float(published)
        if stamp > 10 ** 12:
            stamp /= 1000
        if stamp > 0:
            published = datetime.fromtimestamp(stamp).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError, OverflowError):
        published = str(published)
    cite = result.get("citeIndex")
    if isinstance(cite, int) and cite > 0:
        index = cite
    return {
        "index": index,
        "url": url,
        "title": result.get("title") or url,
        "site": (result.get("siteName") or result.get("site_name")
                 or result.get("site") or ""),
        "snippet": result.get("snippet") or result.get("summary") or "",
        "published": published,
        "icon": result.get("siteIcon") or result.get("site_icon") or "",
    }


def extract_sources(fragments) -> list[dict]:
    sources: list[dict] = []
    for frag in fragments or []:
        if not isinstance(frag, dict):
            continue
        if str(frag.get("type") or "").upper() not in ("TOOL_SEARCH", "SEARCH"):
            continue
        for result in frag.get("results") or []:
            if isinstance(result, dict):
                sources.append(_normalize_source(result, len(sources) + 1))
    return sources


def parse_sse(lines: Iterator[bytes]):
    """把 completion 的 SSE 增量转换成 (kind, text) 以及 meta。

    网页版的帧格式：先来一帧完整 snapshot（v.response.fragments），
    之后是 JSON Patch 风格的增量：
      {"p":"response/fragments","o":"APPEND","v":[{"type":"THINK","content":"..."}]}
      {"p":"response/fragments/-1/content","o":"APPEND","v":"..."}
      {"v":"..."}                     # 追加到当前 path
    """
    frag_types: list[str] = []
    active_path: Optional[str] = None
    meta: dict = {"search_results": []}
    sources: list[dict] = meta["search_results"]

    def add_fragments(frags):
        for frag in frags or []:
            if not isinstance(frag, dict):
                continue
            kind = frag.get("type") or "RESPONSE"
            frag_types.append(kind)
            if str(kind).upper() in ("TOOL_SEARCH", "SEARCH"):
                for result in frag.get("results") or []:
                    if isinstance(result, dict):
                        sources.append(_normalize_source(result, len(sources) + 1))
            content = frag.get("content")
            if isinstance(content, str) and content:
                yield kind, content

    def type_at(path: str) -> str:
        try:
            return frag_types[int(path.split("/")[-2])]
        except (ValueError, IndexError):
            return frag_types[-1] if frag_types else "RESPONSE"

    def apply(path: str, op: Optional[str], v):
        if op == "BATCH" and isinstance(v, list):
            for sub in v:
                if isinstance(sub, dict) and "p" in sub:
                    yield from apply(f"{path}/{sub['p']}", sub.get("o"), sub.get("v"))
        elif path.endswith("fragments") and isinstance(v, list):
            yield from add_fragments(v)
        elif path.endswith("/content") and isinstance(v, str) and op in (None, "APPEND"):
            yield type_at(path), v
        elif path.endswith("/results") and isinstance(v, list):
            for result in v:
                if isinstance(result, dict):
                    sources.append(_normalize_source(result, len(sources) + 1))
        elif "/results/" in path and path.rsplit("/", 1)[-1].isdigit() and isinstance(v, dict):
            sources.append(_normalize_source(v, len(sources) + 1))
        elif path.endswith("message_id") and isinstance(v, int):
            meta["message_id"] = v

    for raw in lines:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue

        v = obj.get("v")
        if isinstance(v, dict) and "response" in v:
            response = v["response"]
            for key in ("message_id", "id"):
                if isinstance(response.get(key), int):
                    meta["message_id"] = response[key]
            for key in ("parent_id", "request_message_id"):
                if isinstance(response.get(key), int):
                    meta["parent_id"] = response[key]
            yield from add_fragments(response.get("fragments"))
            active_path = "response/fragments/-1/content"
            continue

        if "p" in obj:
            active_path = obj["p"]
            yield from apply(active_path, obj.get("o"), v)
            continue

        if active_path:
            yield from apply(active_path, None, v)

    # 把 message_id 交给调用方（生成器返回值不方便，放在最后一条特殊事件里）
    if meta:
        yield "__meta__", json.dumps(meta)


# ---------------------------------------------------------------------------
# HTTP 客户端
# ---------------------------------------------------------------------------

class DeepSeekAPI:
    def __init__(self, session: Session, pow_solver=None,
                 base: str = BASE, timeout: float = 300.0):
        self.session = session
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.pow_solver = pow_solver  # callable(challenge: dict, target_path: str) -> str
        self._lock = threading.Lock()

    # -- 基础 HTTP ---------------------------------------------------------
    def _headers(self, extra: Optional[dict] = None, with_json: bool = False) -> dict:
        offset = int(datetime.now().astimezone().utcoffset().total_seconds())
        headers = {
            "authorization": f"Bearer {self.session.token}",
            "accept": "*/*",
            "user-agent": self.session.user_agent,
            "origin": self.base,
            "referer": self.base + "/",
            "x-app-version": "2.0.0",
            "x-client-version": "2.0.0",
            "x-client-platform": "web",
            "x-client-locale": "zh_CN",
            "x-client-bundle-id": "com.deepseek.chat",
            "x-client-timezone-offset": str(offset),
        }
        if self.session.cookies:
            headers["cookie"] = "; ".join(
                f"{k}={v}" for k, v in self.session.cookies.items()
            )
        if with_json:
            headers["content-type"] = "application/json"
        if extra:
            headers.update(extra)
        return headers

    def _url(self, path: str, query: Optional[dict] = None) -> str:
        url = self.base + path
        if query:
            clean = {k: v for k, v in query.items() if v is not None}
            url += "?" + urllib.parse.urlencode(clean, doseq=True)
        return url

    def _open(self, method: str, path: str, body: Optional[dict] = None,
              query: Optional[dict] = None, headers: Optional[dict] = None,
              stream: bool = False):
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self._url(path, query), data=data, method=method,
            headers=self._headers(headers, with_json=body is not None),
        )
        try:
            return urllib.request.urlopen(req, timeout=self.timeout), None
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            if exc.code in (401, 403):
                raise AuthError(f"HTTP {exc.code}: {raw[:300]!r}") from exc
            raise ApiError(f"HTTP {exc.code}: {raw[:300]!r}") from exc
        except urllib.error.URLError as exc:
            raise ApiError(f"网络错误: {exc.reason}") from exc

    def _json(self, method: str, path: str, body: Optional[dict] = None,
              query: Optional[dict] = None, headers: Optional[dict] = None) -> dict:
        resp, _ = self._open(method, path, body, query, headers)
        try:
            payload = json.loads(resp.read().decode("utf-8"))
        finally:
            resp.close()
        return self._unwrap(payload)

    @staticmethod
    def _unwrap(payload: dict) -> dict:
        code = payload.get("code")
        if code == 40002:
            raise AuthError("登录已失效（Missing Token），请重新登录。")
        if code == 40003:
            raise AuthError("登录已失效（Invalid Token），请重新登录。")
        if code != 0:
            raise ApiError(f"接口错误 {code}: {payload.get('msg')}")
        data = payload.get("data") or {}
        biz_code = data.get("biz_code", 0)
        if biz_code != 0:
            msg = data.get("biz_msg") or f"biz_code={biz_code}"
            raise ApiError(f"业务错误 {biz_code}: {msg}")
        return data.get("biz_data") or {}

    # -- 会话 --------------------------------------------------------------
    # -- 会话全文搜索 ------------------------------------------------------
    @staticmethod
    def _parts_text(value) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return "".join(str(p.get("text") or "") for p in value.get("parts") or []
                           if isinstance(p, dict))
        return ""

    def _search_prepare(self) -> None:
        try:
            self._json("GET", "/api/v0/index/prepare")
        except AuthError:
            raise
        except Exception:
            pass

    def search_chats(self, query: str, before_seq_id=None):
        """搜索会话全文，逐个 yield message/title/status 事件。"""
        self._search_prepare()
        body = {"query": query, "before_seq_id": before_seq_id}
        resp, _ = self._open("POST", "/api/v0/index/query", body=body, stream=True)
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "application/json" in ctype:
            raw = resp.read()
            resp.close()
            try:
                payload = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                raise ApiError(f"搜索返回异常: {raw[:300]!r}")
            raise ApiError(f"搜索失败: {payload.get('msg') or payload}")
        event = None
        data_lines: list[str] = []

        def handle():
            if not data_lines:
                return None
            payload = "\n".join(data_lines)
            try:
                data = json.loads(payload)
            except ValueError:
                return None
            if event == "item" and isinstance(data, dict):
                return {
                    "type": "message",
                    "id": "%s-%s" % (data.get("chat_session_id"), data.get("message_id")),
                    "session_id": data.get("chat_session_id"),
                    "message_id": data.get("message_id"),
                    "role": data.get("message_role"),
                    "title": self._parts_text(data.get("chat_session_title")),
                    "snippet": self._parts_text(data.get("content")),
                    "seq_id": data.get("seq_id"),
                    "timestamp": data.get("timestamp"),
                    "is_think": bool(data.get("is_think")),
                }
            if event == "chat_session_title_item" and isinstance(data, dict):
                return {
                    "type": "title",
                    "id": "title-%s" % data.get("id"),
                    "session_id": data.get("id"),
                    "message_id": None,
                    "role": None,
                    "title": self._parts_text(data.get("title")),
                    "snippet": "",
                    "seq_id": data.get("seq_id"),
                    "timestamp": data.get("timestamp"),
                }
            if event == "status" and isinstance(data, dict):
                return {"type": "status", "seq_id": data.get("queried_seq_id")}
            if event == "close":
                return {"type": "close",
                        "reason": (data or {}).get("close_reason")}
            return None

        try:
            for raw in resp:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("event:"):
                    event = line[len("event:"):].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[len("data:"):].strip())
                elif not line:
                    item = handle()
                    if item is not None:
                        yield item
                    if item and item.get("type") == "close":
                        return
                    event = None
                    data_lines = []
            item = handle()
            if item is not None:
                yield item
        finally:
            resp.close()

    def current_user(self) -> dict:
        data = self._json("GET", "/api/v0/users/current")
        if isinstance(data, dict) and isinstance(data.get("user"), dict):
            return data["user"]
        return data if isinstance(data, dict) else {}

    def create_chat_session(self) -> str:
        data = self._json("POST", "/api/v0/chat_session/create", body={})
        return str(data["chat_session"]["id"])

    def list_chats_page(self, count: int = 30,
                        cursor: Optional[dict] = None) -> tuple[list[dict], bool, Optional[dict]]:
        query = {"count": count}
        if cursor:
            query["lte_cursor.pinned"] = "true" if cursor.get("pinned") else "false"
            query["lte_cursor.updated_at"] = int(cursor.get("updated_at") or 0)
        data = self._json("GET", "/api/v0/chat_session/fetch_page", query=query)
        sessions = []
        for item in data.get("chat_sessions") or []:
            sessions.append({
                "id": item.get("id"),
                "title": item.get("title") or "未命名对话",
                "updated_at": item.get("updated_at") or 0,
                "pinned": bool(item.get("pinned")),
                "model_type": item.get("model_type") or "default",
            })
        has_more = bool(data.get("has_more"))
        next_cursor = None
        if has_more and sessions:
            last = sessions[-1]
            stamp = last["updated_at"]
            if stamp > 10 ** 12:
                stamp //= 1000
            next_cursor = {"pinned": last["pinned"], "updated_at": int(stamp)}
        return sessions, has_more, next_cursor

    def share_chat(self, chat_session_id: str,
                   message_ids: Optional[list[int]] = None) -> str:
        """Create a public share link for a conversation and return its URL."""
        if not message_ids:
            history = self.history(chat_session_id)
            message_ids = [m["id"] for m in history
                           if isinstance(m.get("id"), int)]
        if not message_ids:
            raise ApiError("当前会话没有可分享的消息")
        data = self._json(
            "POST", "/api/v0/share/create",
            body={"chat_session_id": chat_session_id,
                  "message_ids": message_ids},
        )
        share = data.get("share") if isinstance(data.get("share"), dict) else {}
        def pick(*keys):
            for source in (data, share):
                for key in keys:
                    value = source.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
            return ""

        url = pick("share_url", "shareUrl", "url", "link")
        share_id = pick("share_id", "shareId", "id")
        if url.startswith("/"):
            url = BASE + url
        if not url and share_id:
            url = f"{BASE}/share/{share_id}"
        if not url:
            raise ApiError("分享接口未返回链接")
        return url

    def list_chats(self, count: int = 30) -> list[dict]:
        sessions, _has_more, _cursor = self.list_chats_page(count)
        return sessions

    def check(self) -> int:
        """轻量健康检查：返回会话数，失败时抛异常。"""
        # count=1 会被服务端判为 illegal count，用和首页一致的 30
        return len(self.list_chats(30))

    # -- 文件 --------------------------------------------------------------
    def upload_file(self, path: str, model_type: str = "default",
                    thinking: bool = False) -> dict:
        """上传一个文件，返回服务端的 file 对象（含 id/status）。"""
        target = "/api/v0/file/upload_file"
        size = os.path.getsize(path)
        if size > 100 * 1024 * 1024:
            raise ApiError("文件超过 100MB")
        pow_value = self._pow(target)

        filename = os.path.basename(path)
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        boundary = "----DeepSeekNative" + uuid.uuid4().hex
        safe_name = filename.replace('"', "_")
        with open(path, "rb") as fh:
            data = fh.read()
        body = b"".join([
            ("--%s\r\n" % boundary).encode(),
            ('Content-Disposition: form-data; name="file"; filename="%s"\r\n'
             % safe_name).encode("utf-8"),
            ("Content-Type: %s\r\n\r\n" % ctype).encode(),
            data,
            ("\r\n--%s--\r\n" % boundary).encode(),
        ])
        headers = self._headers({
            "x-ds-pow-response": pow_value,
            "content-type": "multipart/form-data; boundary=%s" % boundary,
            "x-file-size": str(size),
            "x-model-type": model_type,
            "x-thinking-enabled": "1" if thinking else "0",
        })
        req = urllib.request.Request(
            self._url(target), data=body, method="POST", headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            raise ApiError(f"上传失败 HTTP {exc.code}: {raw[:300]!r}") from exc
        except urllib.error.URLError as exc:
            raise ApiError(f"上传网络错误: {exc.reason}") from exc
        try:
            payload = json.loads(resp.read().decode("utf-8"))
        finally:
            resp.close()
        return self._unwrap(payload)

    def fetch_files(self, file_ids: list[str]) -> list[dict]:
        data = self._json("GET", "/api/v0/file/fetch_files",
                          query={"file_ids": list(file_ids)})
        return data.get("files") or []

    def wait_file_ready(self, file_id: str, timeout: float = 120.0) -> dict:
        deadline = time.time() + timeout
        terminal_error = {
            "FAILED", "CONTENT_FILTER", "CONTENT_TOO_LONG", "CANCELLED",
            "CONTENT_EMPTY", "_CUSTOM_SYSTEM_ERROR_FAIL",
        }
        last = {}
        while time.time() < deadline:
            files = self.fetch_files([file_id])
            if files:
                last = files[0]
                status = str(last.get("status") or "").upper()
                if status == "SUCCESS":
                    if str(last.get("audit_result") or "").lower() == "reject":
                        raise ApiError("文件未通过内容审核")
                    return last
                if status in terminal_error:
                    raise ApiError(f"文件解析失败：{status}")
            time.sleep(0.8)
        raise ApiError("文件解析超时")

    def history(self, chat_session_id: str) -> list[dict]:
        data = self._json(
            "GET", "/api/v0/chat/history_messages",
            query={"chat_session_id": chat_session_id},
        )
        messages = []
        for item in data.get("chat_messages") or []:
            role = str(item.get("role") or "").upper()
            text_parts, think_parts = [], []
            for frag in item.get("fragments") or []:
                kind = str(frag.get("type") or "").upper()
                content = frag.get("content") or ""
                if kind == "THINK":
                    think_parts.append(content)
                else:
                    text_parts.append(content)
            messages.append({
                "id": item.get("message_id"),
                "parent_id": item.get("parent_id"),
                "role": "user" if role == "USER" else "assistant",
                "text": "".join(text_parts),
                "reasoning": "".join(think_parts),
                "model_type": item.get("model_type"),
                "sources": extract_sources(item.get("fragments")),
            })
        return messages

    # -- 发消息 ------------------------------------------------------------
    def _pow(self, target_path: str) -> str:
        if self.pow_solver is None:
            raise ApiError("没有可用的 PoW 求解器")
        data = self._json(
            "POST", "/api/v0/chat/create_pow_challenge",
            body={"target_path": target_path},
        )
        challenge = data.get("challenge")
        if not isinstance(challenge, dict):
            raise ApiError("create_pow_challenge 返回格式异常")
        return self.pow_solver(challenge, target_path)

    def _stream_chat(self, path: str, body: dict,
                     cancel: Optional[threading.Event] = None,
                     pow_target: str = "/api/v0/chat/completion") -> Iterator[tuple[str, str]]:
        headers = {"x-ds-pow-response": self._pow(pow_target)}
        resp, _ = self._open("POST", path, body=body, headers=headers, stream=True)
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "application/json" in ctype:
            raw = resp.read()
            resp.close()
            try:
                payload = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                raise ApiError(f"接口返回了非 SSE 内容: {raw[:300]!r}")
            code = payload.get("code")
            msg = payload.get("msg") or payload
            if code in (40002, 40003):
                raise AuthError(f"登录已失效：{msg}")
            raise ApiError(f"接口返回错误 {code}: {msg}")
        try:
            for kind, text in parse_sse(resp):
                if cancel is not None and cancel.is_set():
                    break
                yield kind, text
        finally:
            resp.close()

    def stream_completion(self, prompt: str, chat_session_id: str,
                          parent_message_id: Optional[int] = None,
                          model_type: Optional[str] = None,
                          thinking: bool = False, search: bool = False,
                          file_ids: Optional[list[str]] = None,
                          cancel: Optional[threading.Event] = None) -> Iterator[tuple[str, str]]:
        body = {
            "chat_session_id": chat_session_id,
            "parent_message_id": parent_message_id,
            "prompt": prompt,
            "ref_file_ids": list(file_ids or []),
            "thinking_enabled": thinking,
            "search_enabled": search,
            "action": None,
            "preempt": False,
        }
        if model_type:
            body["model_type"] = model_type
        yield from self._stream_chat("/api/v0/chat/completion", body, cancel)

    def regenerate(self, chat_session_id: str, child_message_id: int,
                   thinking: bool = False, search: bool = False,
                   cancel: Optional[threading.Event] = None) -> Iterator[tuple[str, str]]:
        body = {
            "chat_session_id": chat_session_id,
            "child_message_id": child_message_id,
            "search_enabled": search,
            "thinking_enabled": thinking,
        }
        yield from self._stream_chat("/api/v0/chat/regenerate", body, cancel)

    def edit_message(self, chat_session_id: str, message_id: int,
                     prompt: str, thinking: bool = False, search: bool = False,
                     file_ids: Optional[list[str]] = None,
                     cancel: Optional[threading.Event] = None) -> Iterator[tuple[str, str]]:
        body = {
            "chat_session_id": chat_session_id,
            "message_id": message_id,
            "ref_file_ids": list(file_ids or []),
            "prompt": prompt,
            "search_enabled": search,
            "thinking_enabled": thinking,
            "action": None,
        }
        yield from self._stream_chat("/api/v0/chat/edit_message", body, cancel)
