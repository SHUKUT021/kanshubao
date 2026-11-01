"""极简 WebSocket 客户端（RFC 6455 客户端侧），只用标准库。

语音流式识别和实时语音对话都走 wss。项目要保持「双击就能跑、不装第三方依赖」，
所以这里只实现客户端真正用得上的部分：握手、带掩码的发送、分片接收、ping/pong。
"""
import base64
import hashlib
import os
import socket
import ssl
import struct
import threading
from urllib.parse import urlparse

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WSError(Exception):
    pass


class WebSocketClient:
    def __init__(self, url: str, headers: dict | None = None,
                 timeout: float = 15.0, connect_timeout: float = 8.0):
        self.url = url
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.sock = None
        self.status = 0
        self.error_body = b""
        self._buf = b""
        self._send_lock = threading.Lock()
        self._closed = False

    # ---------- 连接 ----------
    def connect(self):
        u = urlparse(self.url)
        host = u.hostname or ""
        port = u.port or (443 if u.scheme == "wss" else 80)
        path = (u.path or "/") + (("?" + u.query) if u.query else "")
        if not host:
            raise WSError(f"地址不合法：{self.url}")
        try:
            raw = socket.create_connection((host, port), timeout=self.connect_timeout)
        except OSError as e:
            raise WSError(f"连不上 {host}:{port} — {e}") from e
        if u.scheme == "wss":
            ctx = ssl.create_default_context()
            try:
                raw = ctx.wrap_socket(raw, server_hostname=host)
            except ssl.SSLError as e:
                raw.close()
                raise WSError(f"TLS 握手失败：{e}") from e
        raw.settimeout(self.timeout)

        key = base64.b64encode(os.urandom(16)).decode()
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}" + (f":{port}" if port not in (80, 443) else ""),
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
            "User-Agent: KanShuBao/1.0",
        ]
        for k, v in self.headers.items():
            lines.append(f"{k}: {v}")
        raw.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

        buf = b""
        while b"\r\n\r\n" not in buf and len(buf) < 65536:
            b = raw.recv(4096)
            if not b:
                break
            buf += b
        head, _, rest = buf.partition(b"\r\n\r\n")
        text = head.decode("latin-1", "replace")
        first = text.splitlines()[0] if text else ""
        parts = first.split()
        self.status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        if self.status != 101:
            body = rest
            low = text.lower()
            if "content-length:" in low:
                try:
                    n = int(low.split("content-length:")[1].splitlines()[0].strip())
                except (IndexError, ValueError):
                    n = 0
                while n and len(body) < n:
                    b = raw.recv(4096)
                    if not b:
                        break
                    body += b
            self.error_body = body
            try:
                raw.close()
            except OSError:
                pass
            raise WSError(f"握手失败 HTTP {self.status}："
                          f"{body[:400].decode('utf-8', 'replace')}")
        want = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        if f"sec-websocket-accept: {want}".lower() not in text.lower():
            raw.close()
            raise WSError("握手响应里的 Sec-WebSocket-Accept 不对，可能是代理拦了")
        self.sock = raw
        self._buf = rest
        return self

    # ---------- 发送 ----------
    def send(self, data: bytes | str, opcode: int | None = None):
        if isinstance(data, str):
            data, opcode = data.encode(), (opcode if opcode is not None else OP_TEXT)
        payload = data
        with self._send_lock:
            self._send_frame(OP_BIN if opcode is None else opcode, payload)

    def _send_frame(self, opcode: int, payload: bytes):
        if not self.sock:
            raise WSError("还没连接")
        n = len(payload)
        hdr = bytearray([0x80 | opcode])
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack(">Q", n)
        mask = os.urandom(4)
        hdr += mask
        masked = bytearray(payload)
        for i in range(n):
            masked[i] ^= mask[i & 3]
        try:
            self.sock.sendall(bytes(hdr) + bytes(masked))
        except OSError as e:
            raise WSError(f"发送失败：{e}") from e

    # ---------- 接收 ----------
    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            try:
                b = self.sock.recv(max(4096, n - len(self._buf)))
            except socket.timeout:
                raise WSError("等待服务端响应超时")
            except OSError as e:
                raise WSError(f"读取失败：{e}") from e
            if not b:
                raise WSError("连接被对端关闭")
            self._buf += b
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _read_frame(self):
        b0, b1 = self._read_exact(2)
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._read_exact(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._read_exact(8))[0]
        mask = self._read_exact(4) if masked else None
        payload = self._read_exact(n) if n else b""
        if mask:
            payload = bytes(c ^ mask[i & 3] for i, c in enumerate(payload))
        return fin, opcode, payload

    def recv(self, timeout: float | None = None) -> tuple[int, bytes]:
        """收一条完整消息（自动拼接分片、自动回 pong）。"""
        if timeout is not None and self.sock:
            self.sock.settimeout(timeout)
        acc, acc_op = b"", OP_BIN
        while True:
            fin, op, payload = self._read_frame()
            if op == OP_PING:
                try:
                    self.send(payload, OP_PONG)
                except WSError:
                    pass
                continue
            if op == OP_PONG:
                continue
            if op == OP_CLOSE:
                raise WSError("服务端关闭了连接")
            if op == OP_CONT:
                acc += payload
            else:
                acc, acc_op = payload, op
            if fin:
                return acc_op, acc

    def settimeout(self, t: float):
        if self.sock:
            self.sock.settimeout(t)

    def close(self):
        if self._closed:
            return
        self._closed = True
        s, self.sock = self.sock, None
        if not s:
            return
        try:
            self._send_frame(OP_CLOSE, struct.pack(">H", 1000))
        except Exception:
            pass
        try:
            s.close()
        except OSError:
            pass
