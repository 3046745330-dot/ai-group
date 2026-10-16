"""API Key 归属识别。

思路：**不靠格式猜，靠探测**。
把 Key 依次发到各家的 ``/models`` 端点，谁回 200 就是谁。

为什么不能用格式判断：``sk-`` 前缀被 OpenAI / DeepSeek / Moonshot / 通义 /
硅基流动等一大批厂商共用，光看形状分不出来。探测拿到的是事实。

安全约定：
  · Key 只在内存里用于发请求，**不写日志、不回显、不落盘**；
  · 响应里只有服务商名与模型 id 列表。
"""

from __future__ import annotations

import asyncio
import hashlib
import time

import httpx

# 探测超时：单家 6 秒。并行发出，所以整体也就 6 秒上下。
PROBE_TIMEOUT = 6.0

# 结果缓存：同一把 Key 反复点「识别」不该反复扫。
# 只存哈希，不存 Key 本身。
_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 300.0


def _cache_key(key: str, base_url: str) -> str:
    raw = f"{key}|{base_url}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _candidates(base_url: str = "") -> list[tuple[str, str, str]]:
    """要探测的候选：(provider_id, 展示名, base_url)。

    候选来自 PRESETS —— 也就是这个 app 真正能添加成员的那些服务商。
    探测一个加不进来的厂商没有意义。
    """
    from .members import PRESETS

    out: list[tuple[str, str, str]] = []
    for preset in PRESETS:
        if preset.id == "custom":
            continue
        if not preset.base_url:
            continue
        out.append((preset.id, preset.label, preset.base_url.rstrip("/")))

    # 用户填了自定义地址：只探它一个，别去骚扰别家。
    if base_url.strip():
        return [("custom", "自定义端点", base_url.strip().rstrip("/"))]
    return out


# 明显不是对话模型的（语音合成/识别、向量、重排）——
# 它们出现在 /models 里很正常，但不该被当成"给你填进模型框"的默认值。
_NON_CHAT_HINTS = ("tts", "asr", "voice", "speech", "embedding", "embed", "rerank", "whisper")


def looks_like_chat_model(model_id: str) -> bool:
    low = (model_id or "").lower()
    return not any(h in low for h in _NON_CHAT_HINTS)


def split_models(models: list[str]) -> tuple[list[str], list[str]]:
    """把模型列表拆成（像对话模型的, 其余）。"""
    chat = [m for m in models if looks_like_chat_model(m)]
    other = [m for m in models if not looks_like_chat_model(m)]
    return chat, other


def _parse_models(payload) -> list[str]:
    """从各家五花八门的返回里尽量抠出模型 id。"""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        ids = []
        for item in data:
            if isinstance(item, dict) and item.get("id"):
                ids.append(str(item["id"]))
        if ids:
            return ids
    # 有的实现把列表塞在 data.models / models 下面
    for holder in (data, payload):
        if isinstance(holder, dict):
            inner = holder.get("models")
            if isinstance(inner, list):
                ids = []
                for item in inner:
                    if isinstance(item, dict) and item.get("id"):
                        ids.append(str(item["id"]))
                    elif isinstance(item, str):
                        ids.append(item)
                if ids:
                    return ids
    return []


async def _probe_one(client: httpx.AsyncClient, provider: str, label: str,
                     base: str, key: str) -> dict:
    url = f"{base}/models"
    result = {
        "provider": provider, "label": label, "baseUrl": base,
        "status": 0, "models": [], "verdict": "unknown", "detail": "",
    }
    try:
        resp = await client.get(url, headers={"Authorization": f"Bearer {key}"})
    except httpx.TimeoutException:
        result["verdict"] = "timeout"
        result["detail"] = "超时"
        return result
    except Exception as exc:  # noqa: BLE001 —— 网络层的任何异常都不该中断整轮探测
        result["verdict"] = "error"
        result["detail"] = type(exc).__name__
        return result

    result["status"] = resp.status_code
    if resp.status_code == 200:
        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001
            payload = None
        models = _parse_models(payload)
        chat, other = split_models(models)
        result["verdict"] = "hit"
        result["models"] = models
        result["chatModels"] = chat
        result["nonChatCount"] = len(other)
        # 措辞必须是「端点列出」而不是「你能用」——
        # /models 给的是平台目录，账号能不能真正调用要另外验证（见 test_model）。
        detail = f"端点列出 {len(models)} 个模型"
        if other:
            detail += f"，其中 {len(other)} 个是语音/向量类非对话模型"
        result["detail"] = detail
        return result

    if resp.status_code in (401, 403):
        # 明确拒绝 → 这把 Key 不属于这家
        result["verdict"] = "rejected"
        result["detail"] = f"HTTP {resp.status_code}"
        return result

    if resp.status_code == 404:
        # 端点不存在 → 探不出结论，不能算"不是它"
        result["verdict"] = "no-endpoint"
        result["detail"] = "该厂商没有 /models 端点"
        return result

    result["verdict"] = "unclear"
    result["detail"] = f"HTTP {resp.status_code} {resp.text[:60]}".strip()
    return result


async def detect_key(key: str, base_url: str = "") -> dict:
    """探测这把 Key 属于哪家。返回命中的服务商与各自可用模型。"""
    key = (key or "").strip()
    if not key:
        return {"ok": False, "error": "Key 为空"}

    ck = _cache_key(key, base_url)
    cached = _CACHE.get(ck)
    if cached and time.time() - cached[0] < _CACHE_TTL:
        out = dict(cached[1])
        out["cached"] = True
        return out

    targets = _candidates(base_url)
    async with httpx.AsyncClient(timeout=PROBE_TIMEOUT, follow_redirects=True) as client:
        results = await asyncio.gather(
            *[_probe_one(client, p, lab, base, key) for p, lab, base in targets]
        )

    hits = [r for r in results if r["verdict"] == "hit"]
    rejected = [r for r in results if r["verdict"] == "rejected"]
    unknown = [r for r in results if r["verdict"] in ("timeout", "error", "no-endpoint", "unclear")]

    out = {
        "ok": True,
        "hits": hits,
        "rejectedCount": len(rejected),
        "unclear": [{"provider": r["provider"], "label": r["label"],
                     "detail": r["detail"]} for r in unknown],
        "tried": len(results),
        "best": hits[0]["provider"] if len(hits) == 1 else "",
        "ambiguous": len(hits) > 1,
        "noHit": not hits,
        "cached": False,
    }
    _CACHE[ck] = (time.time(), out)
    return out


def clear_cache() -> None:
    _CACHE.clear()

async def test_model(provider_id: str, model_id: str, key: str,
                     base_url: str = "") -> dict:
    """用一次**最小真实调用**验证某个模型到底能不能用。

    `/models` 只说明端点列出了它，不说明你的账号开通了它 ——
    所以唯一可靠的办法是真发一条请求。这里只要 1 个 token，成本可忽略。
    """
    from .members import PRESETS

    base = (base_url or "").strip()
    if not base:
        preset = next((p for p in PRESETS if p.id == provider_id), None)
        if preset is None:
            return {"ok": False, "error": f"未知的提供商：{provider_id}"}
        base = preset.base_url
    if not base:
        return {"ok": False, "error": "缺少接口地址"}
    if not (key or "").strip():
        return {"ok": False, "error": "需要提供 API Key"}

    url = base.rstrip("/") + "/chat/completions"
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
        "stream": False,
    }
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.post(
                url, json=payload,
                headers={"Authorization": f"Bearer {key.strip()}"},
            )
    except httpx.TimeoutException:
        return {"ok": False, "error": "请求超时（20 秒）"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    if resp.status_code == 200:
        return {"ok": True, "model": model_id, "detail": "调用成功，这个模型可以用"}
    # 把上游的报错原样带回去，用户自己看得懂（未开通 / 模型名不对 / 余额不足…）
    body = resp.text[:300]
    return {
        "ok": False,
        "model": model_id,
        "status": resp.status_code,
        "error": _explain(resp.status_code, body),
        "raw": body,
    }


def _explain(status: int, body: str) -> str:
    """把常见错误翻译成人话，省得用户对着英文原文猜。"""
    low = body.lower()
    if "modelnotopen" in low or "not open" in low or "未开通" in body:
        return "这个模型你的账号没有开通"
    if "access to model denied" in low or "not eligible" in low:
        # 这句是千问/百炼最常见的坑：模型在 /models 目录里，但账号没开通它。
        # 光说"没权限"没用，要告诉用户下一步做什么。
        return "账号没有开通这个模型（在平台「模型广场」里开通后再试）"
    if status == 401:
        return "API Key 无效或已过期"
    if status == 403:
        return "没有权限调用这个模型"
    # 各家对「模型不存在」的报法不一样：有的 404，有的 400 + Unsupported/Not found。
    # 小米就是 400 + "Unsupported model" —— 只按状态码翻会漏掉。
    if (status == 404 or "unsupported" in low or "not found" in low
            or "does not exist" in low or "invalid model" in low
            or "unknown model" in low):
        return "模型 id 不存在或不被支持（检查拼写）"
    if status == 429:
        return "触发了限流，稍后再试"
    if "insufficient" in low or "balance" in low or "余额" in body:
        return "余额不足"
    return f"调用失败（HTTP {status}）"

# 批量实测的并发上限：太高会被限流并且反而更容易失败，
# 太低则 40 个模型要等太久。6 是实测比较稳的值。
BATCH_CONCURRENCY = 6
# 一次最多测多少个：防止用户对着一份 176 个模型的目录直接点下去。
BATCH_MAX = 60


async def test_models(provider_id: str, models: list[str], key: str,
                      base_url: str = "") -> dict:
    """并发实测一批模型，返回每个模型的可用性。

    每次调用只要 1 个 token，成本可忽略；真正的价值是**筛掉账号没开通的**。
    """
    from .members import PRESETS

    base = (base_url or "").strip()
    if not base:
        preset = next((p for p in PRESETS if p.id == provider_id), None)
        if preset is None:
            return {"ok": False, "error": f"未知的提供商：{provider_id}"}
        base = preset.base_url
    if not base:
        return {"ok": False, "error": "缺少接口地址"}
    if not (key or "").strip():
        return {"ok": False, "error": "需要提供 API Key"}

    wanted = [m for m in (models or []) if str(m).strip()][:BATCH_MAX]
    if not wanted:
        return {"ok": False, "error": "没有要测试的模型"}

    sem = asyncio.Semaphore(BATCH_CONCURRENCY)
    results: list[dict] = []

    async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
        async def one(model_id: str) -> None:
            async with sem:
                url = base.rstrip("/") + "/chat/completions"
                payload = {
                    "model": model_id,
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 1,
                    "stream": False,
                }
                entry = {"model": model_id, "ok": False, "error": ""}
                try:
                    resp = await client.post(
                        url, json=payload,
                        headers={"Authorization": f"Bearer {key.strip()}"},
                    )
                except httpx.TimeoutException:
                    entry["error"] = "超时"
                    results.append(entry)
                    return
                except Exception as exc:  # noqa: BLE001
                    entry["error"] = type(exc).__name__
                    results.append(entry)
                    return
                if resp.status_code == 200:
                    entry["ok"] = True
                else:
                    entry["status"] = resp.status_code
                    entry["error"] = _explain(resp.status_code, resp.text[:300])
                results.append(entry)

        await asyncio.gather(*[one(m) for m in wanted])

    order = {m: i for i, m in enumerate(wanted)}
    results.sort(key=lambda r: order.get(r["model"], 999))
    usable = [r["model"] for r in results if r["ok"]]
    return {
        "ok": True,
        "tested": len(results),
        "usable": usable,
        "results": results,
        "usableCount": len(usable),
        "failedCount": len(results) - len(usable),
    }
