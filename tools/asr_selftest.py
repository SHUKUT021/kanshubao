"""不联网的流式识别自测：本地假服务端跑完整一轮，校验二进制协议自洽。

用法：python3 tools/asr_selftest.py
"""
import base64
import gzip
import hashlib
import json
import os
import socket
import struct
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

import asr  # noqa: E402
import ws    # noqa: E402

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def server_frame(payload: bytes, opcode: int = ws.OP_BIN, flags: int = 0) -> bytes:
    n = len(payload)
    out = bytearray([0x80 | opcode])
    if n < 126:
        out.append(n)
    elif n < 65536:
        out.append(126)
        out += struct.pack(">H", n)
    else:
        out.append(127)
        out += struct.pack(">Q", n)
    return bytes(out) + payload


def read_exact(conn, n):
    buf = b""
    while len(buf) < n:
        b = conn.recv(n - len(buf))
        if not b:
            raise RuntimeError("对端提前关闭")
        buf += b
    return buf


def read_client_frame(conn):
    b0, b1 = read_exact(conn, 2)
    op = b0 & 0x0F
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack(">H", read_exact(conn, 2))[0]
    elif n == 127:
        n = struct.unpack(">Q", read_exact(conn, 8))[0]
    assert b1 & 0x80, "客户端发出的帧必须带掩码"
    mask = read_exact(conn, 4)
    payload = read_exact(conn, n) if n else b""
    payload = bytes(c ^ mask[i & 3] for i, c in enumerate(payload))
    return op, payload


def server_response(obj: dict, flags: int, seq: int | None) -> bytes:
    body = gzip.compress(json.dumps(obj, ensure_ascii=False).encode())
    head = asr._header(asr.FULL_SERVER_RESPONSE, flags, asr.JSON_SERIAL, asr.GZIP)
    out = head
    if seq is not None:
        out += struct.pack(">i", seq)
    out += struct.pack(">I", len(body)) + body
    return out


def run_server(port_holder, result):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port_holder.append(srv.getsockname()[1])
    conn, _ = srv.accept()
    try:
        req = b""
        while b"\r\n\r\n" not in req:
            req += conn.recv(4096)
        headers = {}
        for line in req.decode("latin-1").split("\r\n")[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        accept = base64.b64encode(
            hashlib.sha1((headers["sec-websocket-key"] + GUID).encode()).digest()).decode()
        conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                      "Connection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        result["headers"] = headers

        # 首包：JSON 配置
        op, data = read_client_frame(conn)
        assert op == ws.OP_BIN
        assert data[0] >> 4 == 1 and (data[0] & 0x0F) == 1, f"头字节不对 {data[0]:#x}"
        assert data[1] >> 4 == asr.FULL_CLIENT_REQUEST, "首包类型不对"
        size = struct.unpack(">I", data[4:8])[0]
        cfg = json.loads(gzip.decompress(data[8:8 + size]).decode())
        result["config"] = cfg

        # 音频包
        total, seqs, last_seen = 0, [], False
        while True:
            op, data = read_client_frame(conn)
            if op == ws.OP_CLOSE:
                break
            mtype = data[1] >> 4
            flags = data[1] & 0x0F
            assert mtype == asr.AUDIO_ONLY_REQUEST, f"音频包类型 {mtype}"
            if flags == asr.LAST_NO_SEQUENCE:
                assert struct.unpack(">I", data[4:8])[0] == 0, "尾包必须为空"
                last_seen = True
            else:
                seq = struct.unpack(">i", data[4:8])[0]
                size = struct.unpack(">I", data[8:12])[0]
                seqs.append(seq)
                total += size
                chunk = gzip.decompress(data[12:12 + size])
                result.setdefault("bytes", 0)
                result["bytes"] += len(chunk)
            if last_seen:
                break
        result.update({"total_frames": len(seqs), "seqs_ok": seqs == list(range(1, len(seqs) + 1))})

        # 两条中间结果 + 一条最终结果
        conn.sendall(server_frame(server_response(
            {"result": {"text": "第三题的", "utterances": [{"text": "第三题的", "definite": False}]}},
            asr.POS_SEQUENCE, 1)))
        time.sleep(0.05)
        conn.sendall(server_frame(server_response(
            {"result": {"text": "第三题的第二小问怎么做",
                        "utterances": [{"text": "第三题的第二小问怎么做", "definite": True}]}},
            asr.NEG_SEQUENCE, -2)))
        time.sleep(0.2)
    except Exception as e:  # noqa: BLE001
        result["server_error"] = f"{type(e).__name__}: {e}"
    finally:
        try:
            conn.close()
        except OSError:
            pass
        srv.close()


def main() -> int:
    holder, result = [], {}
    t = threading.Thread(target=run_server, args=(holder, result), daemon=True)
    t.start()
    time.sleep(0.1)
    url = f"ws://127.0.0.1:{holder[0]}/api/v3/sauc/bigmodel"
    cfg = {"asr_key": "test-key", "asr_url": url, "asr_uid": "selftest"}
    partials = []
    session = asr.StreamASR(cfg, on_partial=partials.append)
    session.start()
    # 1 秒 16k/16bit 单声道 = 32000 字节，分 10 次推，验证内部按 100ms 切包
    pcm = bytes(32000)
    for i in range(10):
        session.push(pcm[i * 3200:(i + 1) * 3200])
    text = session.finish(timeout=3)
    session.close()
    t.join(timeout=3)

    checks = [
        ("握手带鉴权头", result.get("headers", {}).get("x-api-key") == "test-key"),
        ("资源 ID 正确", result.get("headers", {}).get("x-api-resource-id") == asr.DEFAULT_RESOURCE),
        ("首包 JSON 音频参数", (result.get("config") or {}).get("audio", {}).get("rate") == 16000),
        ("音频分 10 包", result.get("total_frames") == 10),
        ("序号连续", result.get("seqs_ok") is True),
        ("音频总字节", result.get("bytes") == 32000),
        ("中间结果上屏", partials[:1] == ["第三题的"]),
        ("最终文本", text == "第三题的第二小问怎么做"),
        ("无服务端异常", "server_error" not in result),
    ]
    ok = True
    for name, passed in checks:
        print(("✅" if passed else "❌") + " " + name)
        ok = ok and bool(passed)
    if not ok:
        print("\n调试信息：", json.dumps(result, ensure_ascii=False)[:800], partials)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
