"""各厂商的**真实余额**查询。

先说清楚一件事：**大部分厂商不提供余额接口。**

实测结果（用用户的真实 Key 挨个试过）：
    DeepSeek   GET /user/balance                ✓ 返回 total_balance
    Kimi       GET /v1/users/me/balance         ✓ 返回 available_balance
    千问/智谱/豆包/小米                           ✗ 404，没有这个接口

所以界面上**不能假装统一** —— 查得到的显示真余额，
查不到的如实显示「该厂商未提供」，另用**本机累计消耗**补位。
这比编一个数字诚实得多。

缓存：余额接口虽然免费，但没必要每次刷新都打一遍，默认 120 秒。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

# 缓存：{provider_id: (取到的时刻, 结果字典)}
_CACHE: dict[str, tuple[float, dict]] = {}
TTL = 120.0
TIMEOUT = 6.0


def _get(url: str, key: str) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:200]
    except Exception as exc:  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}"


def _deepseek(base_url: str, key: str) -> dict:
    """DeepSeek 官方余额接口。"""
    root = base_url.rstrip("/")
    # base_url 通常是 https://api.deepseek.com/v1，余额接口在根上
    if root.endswith("/v1"):
        root = root[:-3]
    status, body = _get(f"{root.rstrip('/')}/user/balance", key)
    if status != 200:
        return {"ok": False, "error": f"HTTP {status}：{body[:110]}"}
    try:
        data = json.loads(body)
    except Exception:
        return {"ok": False, "error": "返回不是合法 JSON"}
    infos = data.get("balance_infos") or []
    if not infos:
        return {"ok": False, "error": "返回里没有余额信息"}
    item = infos[0]
    return {
        "ok": True,
        "amount": str(item.get("total_balance", "")),
        "currency": str(item.get("currency", "CNY")),
        "available": bool(data.get("is_available", True)),
        "detail": {
            "赠送": item.get("granted_balance"),
            "充值": item.get("topped_up_balance"),
        },
    }


def _moonshot(base_url: str, key: str) -> dict:
    """Kimi / Moonshot 余额 + 配额。"""
    root = base_url.rstrip("/")
    status, body = _get(f"{root}/users/me/balance", key)
    if status != 200:
        return {"ok": False, "error": f"HTTP {status}：{body[:110]}"}
    try:
        data = json.loads(body).get("data") or {}
    except Exception:
        return {"ok": False, "error": "返回不是合法 JSON"}
    out = {
        "ok": True,
        "amount": str(data.get("available_balance", "")),
        "currency": "CNY",
        "detail": {
            "代金券": data.get("voucher_balance"),
            "现金": data.get("cash_balance"),
        },
    }
    # 顺带取配额（这个接口给的信息很全，是"实时额度"最理想的一家）
    st2, body2 = _get(f"{root}/users/me", key)
    if st2 == 200:
        try:
            d2 = json.loads(body2).get("data") or {}
            org = d2.get("organization") or {}
            use = d2.get("organization_usage") or {}
            out["quota"] = {
                "每分钟请求": org.get("max_request_per_minute"),
                "每分钟token": org.get("max_token_per_minute"),
                "每日token": org.get("max_token_per_day"),
                "总额度token": org.get("max_token_quota"),
                "今日已用token": use.get("cur_token_per_day"),
            }
        except Exception:
            pass
    return out


# provider_id → 查余额的实现。**这里没有的，就是厂商没提供接口。**
_PROBES = {
    "deepseek": _deepseek,
    "moonshotai-cn": _moonshot,
    "moonshotai": _moonshot,
}

# 给界面用的说明：为什么这家查不到
NO_ENDPOINT = "该厂商未提供余额接口"


def balance_for(provider_id: str, base_url: str, key: str,
                *, use_cache: bool = True) -> dict:
    probe = _PROBES.get(provider_id)
    if probe is None:
        return {"ok": False, "unsupported": True, "error": NO_ENDPOINT}
    if not key:
        return {"ok": False, "error": "没有可用密钥"}

    if use_cache:
        hit = _CACHE.get(provider_id)
        if hit and (time.time() - hit[0]) < TTL:
            return dict(hit[1], cached=True)

    try:
        result = probe(base_url, key)
    except Exception as exc:  # noqa: BLE001 —— 查余额失败绝不该影响别处
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    _CACHE[provider_id] = (time.time(), result)
    return dict(result, cached=False)


def all_balances(cfg, *, use_cache: bool = True) -> dict:
    """给每个**已启用成员**用到的 provider 查一次余额。"""
    from .config import resolve_credential

    seen: dict[str, dict] = {}
    for agent in cfg.agents + [cfg.recorder]:
        if not getattr(agent, "enabled", False):
            continue
        pid = agent.provider
        if pid in seen:
            continue
        prov = cfg.providers.get(pid)
        if prov is None or getattr(prov, "is_mock", False):
            continue
        key, _src = resolve_credential(prov.credentials)
        seen[pid] = balance_for(pid, prov.base_url or "", key or "",
                                use_cache=use_cache)
        # provider → 用在哪些成员上，界面好对应
        seen[pid]["members"] = [
            a.display_name for a in cfg.agents if a.enabled and a.provider == pid
        ]
    return seen


def clear_cache() -> None:
    _CACHE.clear()
