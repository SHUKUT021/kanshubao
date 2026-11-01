"""豆包(火山方舟 Ark) 视觉接口，OpenAI 兼容协议。零第三方依赖。"""
import base64
import json
import os
import re
import urllib.error
import urllib.request


class ProviderError(Exception):
    pass


def api_key(cfg: dict) -> str:
    return (os.environ.get("ARK_API_KEY") or os.environ.get("DOUBAO_API_KEY")
            or cfg.get("api_key") or "")


def chat(cfg: dict, system: str, prompt: str, image_jpeg: bytes | None = None,
         max_tokens: int = 2500, temperature: float = 0.1, timeout: int = 120,
         audio_wav: bytes | None = None, thinking: str | None = None) -> str:
    key = api_key(cfg)
    if not key:
        raise ProviderError("未配置豆包 API Key。到火山方舟控制台创建 API Key，填进右上角「设置」。")

    base = (cfg.get("base_url") or "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")
    model = cfg.get("model") or "doubao-seed-1-6-250615"

    content: list[dict] = []
    if audio_wav:
        content.append({"type": "input_audio",
                        "input_audio": {"data": base64.b64encode(audio_wav).decode(),
                                        "format": "wav"}})
    if image_jpeg:
        b64 = base64.b64encode(image_jpeg).decode()
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    content.append({"type": "text", "text": prompt})

    body = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": content}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if thinking:
        body["thinking"] = {"type": thinking}
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:600]
        hint = ""
        if e.code == 401:
            hint = "（Key 无效或已过期）"
        elif e.code == 403:
            hint = "（Key 无该模型权限：确认方舟里该 Key 所属项目/子账号已开通此模型，或重新创建一个 API Key）"
        elif e.code == 404:
            hint = "（模型名或接入点 ID 不对，检查设置里的 model）"
        raise ProviderError(f"豆包接口 {e.code} {hint}: {detail}") from e
    except urllib.error.URLError as e:
        raise ProviderError(f"连不上豆包接口：{e.reason}（检查网络或代理）") from e
    except TimeoutError as e:
        raise ProviderError("豆包接口超时，稍后重试") from e

    try:
        msg = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise ProviderError(f"返回格式异常：{json.dumps(data)[:400]}") from e
    if isinstance(msg, list):
        msg = "".join(p.get("text", "") for p in msg if isinstance(p, dict))
    return msg or ""


def chat_stream(cfg: dict, system: str, prompt: str, image_jpeg: bytes | None = None,
                max_tokens: int = 2500, temperature: float = 0.1, timeout: int = 120,
                audio_wav: bytes | None = None, thinking: str | None = None):
    """流式返回增量文本。首句一到就能开始朗读，不用等整段生成完。"""
    key = api_key(cfg)
    if not key:
        raise ProviderError("未配置豆包 API Key。到火山方舟控制台创建 API Key，填进「设置」。")
    base = (cfg.get("base_url") or "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")
    model = cfg.get("model") or "doubao-seed-1-6-250615"
    content: list[dict] = []
    if audio_wav:
        content.append({"type": "input_audio",
                        "input_audio": {"data": base64.b64encode(audio_wav).decode(),
                                        "format": "wav"}})
    if image_jpeg:
        b64 = base64.b64encode(image_jpeg).decode()
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    content.append({"type": "text", "text": prompt})
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": content}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }
    if thinking:
        body["thinking"] = {"type": thinking}
    req = urllib.request.Request(
        f"{base}/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:600]
        hint = {401: "（Key 无效或已过期）",
                403: "（Key 无该模型权限）",
                404: "（模型名或接入点 ID 不对）"}.get(e.code, "")
        raise ProviderError(f"豆包接口 {e.code} {hint}: {detail}") from e
    except urllib.error.URLError as e:
        raise ProviderError(f"连不上豆包接口：{e.reason}（检查网络或代理）") from e
    except TimeoutError as e:
        raise ProviderError("豆包接口超时，稍后重试") from e
    with resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line or line.startswith(":"):
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            if line == "[DONE]":
                break
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("error"):
                raise ProviderError(f"豆包流式接口报错：{str(data['error'])[:300]}")
            try:
                delta = data["choices"][0]["delta"]
            except (KeyError, IndexError):
                continue
            piece = delta.get("content")
            if isinstance(piece, list):
                piece = "".join(p.get("text", "") for p in piece if isinstance(p, dict))
            if piece:
                yield piece


def extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    raise ProviderError(f"模型没有按要求返回 JSON：{text[:300]}")
