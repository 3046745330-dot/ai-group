"""LLM 客户端：统一封装 OpenAI 兼容协议 + 离线 mock 通道。

设计要点
--------
* 一个 provider 一个客户端实例，按需惰性创建。
* 流式优先：群聊界面需要逐字上屏。
* 失败可降级：provider 调用失败时，若成员声明了 fallback，自动换路由重试一次，
  并把「降级事实」作为事件的一部分返回，绝不静默换模型。
* mock 通道永不失败、永不花钱，保证 Web 演示永远可用。
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator
from urllib.parse import urlparse

from .config import AgentSpec, GroupConfig, ProviderSpec, resolve_credential


# --------------------------------------------------------------------------- #
# 厂商平台原生能力
# --------------------------------------------------------------------------- #
# 工具轮回上限。内置联网通常一轮就够（请求搜索 → 拿结果 → 作答）；
# 留到 3 轮是给将来的自定义工具（读写文件）余量，同时防止模型鬼打墙。
MAX_TOOL_ROUNDS = 3

# 各厂商「联网」怎么表达。**实测**结论，不是照抄文档：
#   dashscope_param —— 千问：加 enable_search 参数，服务端自己搜完直接答（不需要回灌）
#   kimi_builtin    —— Kimi：挂内置 $web_search，模型返回 tool_calls，必须回灌才继续
# 智谱的 web_search 参数**实测静默失效**（收下参数但不生效，它自己回"我无法联网"），
# 所以这里不给它配 —— 留一个看起来能用其实没用的开关，比没有更糟。
WEB_SEARCH_STYLE = {
    "qwen": "dashscope_param",
    "moonshotai-cn": "kimi_builtin",
    "moonshotai": "kimi_builtin",
}


def apply_features(params: dict[str, Any], provider_id: str,
                   features: list[str] | None) -> bool:
    """把成员声明的能力翻译成该厂商的请求参数。返回是否挂了需要回灌的工具。"""
    feats = set(features or [])
    if "web_search" not in feats:
        return False
    style = WEB_SEARCH_STYLE.get(provider_id)
    if style == "dashscope_param":
        # **必须走 extra_body**，不能直接塞进 params。
        # OpenAI 的 Python SDK 会校验关键字参数，不认识的直接抛
        # "got an unexpected keyword argument" —— 而裸 HTTP 请求是能过的，
        # 所以「用 curl 测通了」不等于「程序里能用」。extra_body 才是厂商扩展的口子。
        extra = params.setdefault("extra_body", {})
        extra["enable_search"] = True
        return False
    if style == "kimi_builtin":
        params["tools"] = [{
            "type": "builtin_function",
            "function": {"name": "$web_search"},
        }]
        return True
    return False


# 能力参数被上游拒绝时，错误里通常带这些字样。
# 命中就**去掉能力重试一次**，而不是让整条调用失败、再静默降级到别的模型 ——
# 那样用户只会看到「莫名其妙换了个模型回话」，完全不知道是联网参数的问题。
_FEATURE_REJECT_HINTS = (
    "enable_search", "search_strategy", "invalidparameter", "invalid parameter",
    "unsupported", "not support", "unknown parameter",
)


def feature_rejected(err_text: str) -> bool:
    low = (err_text or "").lower()
    return any(h in low for h in _FEATURE_REJECT_HINTS)


def strip_features(params: dict[str, Any], level: int = 1) -> list[str]:
    """把厂商能力相关的参数从请求里摘掉，返回被摘掉的能力名。

    ``level=1`` 只摘厂商**内置**工具（如 Kimi 的 $web_search），
    保留本地工作区工具（标准 function 类型，绝大多数厂商都认）；
    ``level=2`` 连本地工具也摘 —— 有些厂商完全不认 tools 字段，
    再留着会让整条调用 400 失败。摘掉后成员写不了文件，
    但对话至少还能继续，比整个失败好。
    """
    removed: list[str] = []
    body = params.get("extra_body")
    if isinstance(body, dict) and "enable_search" in body:
        body.pop("enable_search", None)
        body.pop("search_strategy", None)
        if not body:
            params.pop("extra_body", None)
        removed.append("web_search")
    tools = params.get("tools")
    if isinstance(tools, list):
        if level >= 2:
            params.pop("tools", None)
            if "workspace_tools" not in removed:
                removed.append("workspace_tools")
        else:
            kept = [
                t for t in tools
                if not (isinstance(t, dict) and t.get("type") == "builtin_function")
            ]
            if len(kept) != len(tools):
                removed.append("web_search")
            if kept:
                params["tools"] = kept
            else:
                params.pop("tools", None)
    return removed
def attach_workspace_tools(params: dict[str, Any], enabled: bool,
                           is_captain: bool = False) -> None:
    """把本地工具挂进请求。**与已有 tools 合并**，不覆盖 ——
    比如 Kimi 的联网也是用 tools 字段（builtin_function），
    直接覆盖会把联网功能弄丢。

    任务板工具**只给组长**：派活和验收是它的职责。
    组员能读板子（提示词里带着），但改状态会让所有人对不上账。
    """
    if not enabled:
        return
    from .workspace import tool_schemas

    specs = list(tool_schemas())
    if is_captain:
        from .taskboard import tool_schemas as task_schemas

        specs += list(task_schemas())

    tools = params.setdefault("tools", [])
    if not isinstance(tools, list):
        return
    have = {str((t.get("function") or {}).get("name") or "") for t in tools if isinstance(t, dict)}
    for spec in specs:
        if spec["function"]["name"] not in have:
            tools.append(spec)


def builtin_tool_result(name: str, arguments: str) -> str:
    """执行一个**内置**工具，返回要塞回模型的 tool 消息内容。

    Moonshot 的 ``$web_search`` 是"内置函数"：客户端**不需要真的去搜**，
    只要把参数原样回传，服务端会在下一次补全时执行搜索。
    """
    if name == "$web_search":
        return arguments or "{}"
    return f"（未知工具 {name}，没有执行）"


class LLMError(RuntimeError):
    """模型调用失败（含密钥缺失、网络错误、上游报错）。"""


@dataclass
class LLMTarget:
    provider: ProviderSpec
    model: str
    api_key: str | None
    credential_source: str

    @property
    def route(self) -> str:
        return f"{self.provider.id}/{self.model}"


class LLMClient:
    """按 provider 缓存 OpenAI 异步客户端，提供统一流式接口。"""

    def __init__(self, cfg: GroupConfig, *, call_log_limit: int = 200) -> None:
        self.cfg = cfg
        self._clients: dict[str, object] = {}
        # 演示开关：只对 mock 通道生效（见 engine.GroupEngine.handle_user_message）。
        # 取值 "" | "quiet"（不 @ 任何人）| "ghost"（额外 @ 一个不存在的成员）。
        self.mock_hint: str = ""
        # 每次真实调用都留一条记录（路由 / 目标 host / 成败 / 报错 / 字数 / 耗时）。
        # 这是「声明的路由到底有没有被调用、有没有被静默换掉」的硬证据，绝不含密钥。
        self.call_log: list[dict[str, Any]] = []
        self.call_log_limit = call_log_limit
        # 某些模型只接受固定 temperature：记下结论后不再重复试探（见 stream_chat）
        self._omit_temperature: dict[str, bool] = {}
        # 能力参数被上游拒绝时记一句，供界面显示（按路由去重）
        self._feature_warnings: dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # 调用记录
    # ------------------------------------------------------------------ #
    def _record(
        self,
        provider_id: str,
        model_id: str,
        *,
        ok: bool,
        error: str = "",
        chars: int = 0,
        started: float,
        degraded: bool,
        explicit_route: str | None = None,
        finish_reason: str = "",
        reasoning_chars: int = 0,
        tokens: int = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        agent_id: str = "",
    ) -> dict[str, Any]:
        provider = self.cfg.providers.get(provider_id)
        base_url = provider.base_url if provider else ""
        entry = {
            "ts": time.time(),
            "route": explicit_route or f"{provider_id}/{model_id}",
            "provider": provider_id,
            "model": model_id,
            "host": urlparse(base_url).netloc or ("mock" if provider and provider.is_mock else "未声明"),
            "via": "offline-mock" if (provider and provider.is_mock) else "openai-compatible-http",
            "ok": ok,
            "degraded": degraded,
            "chars": chars,
            "reasoning_chars": reasoning_chars,
            "tokens": tokens,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "finish_reason": finish_reason,
            "elapsed_s": round(time.time() - started, 2),
            "error": error[:300],
        }
        self.call_log.append(entry)
        if len(self.call_log) > self.call_log_limit:
            del self.call_log[: len(self.call_log) - self.call_log_limit]

        # 今日用量持久化。
        # call_log 只在内存里（且只留最近 200 条），所以「今日花了多少」
        # 大退一次就归零、聊久一点还会少算。这里按天落一份聚合计数，
        # 下次启动就能接着算。只存原始计数、不存金额 —— 计价公式在前端，
        # 后端再算一遍就会有两套价格表。
        try:
            from .usage import record_call

            record_call(provider_id, agent_id=agent_id or cur_agent, chars=chars,
                        reasoning_chars=reasoning_chars, ok=ok)
        except Exception:  # noqa: BLE001 —— 记用量失败绝不能影响对话
            pass
        return entry

    # ------------------------------------------------------------------ #
    # 路由解析
    # ------------------------------------------------------------------ #
    def resolve(self, provider_id: str, model_id: str) -> LLMTarget:
        provider = self.cfg.providers.get(provider_id)
        if provider is None:
            raise LLMError(f"provider 未在 providers.yaml 中声明：{provider_id}")
        key, source = resolve_credential(provider.credentials)
        if not provider.is_mock:
            if not key:
                raise LLMError(
                    f"provider `{provider_id}` 缺少可用密钥（{source}），"
                    f"请配置环境变量或 DSH 凭据库后再试，或把该成员切到 mock 通道。"
                )
            if not provider.base_url:
                raise LLMError(f"provider `{provider_id}` 未声明 base_url")
            if model_id not in provider.models:
                # 不在目录里不直接拒绝：上游可能新增了模型 id，交给上游报错更准确
                pass
        return LLMTarget(provider=provider, model=model_id, api_key=key, credential_source=source)

    def _client_for(self, target: LLMTarget):
        cache_key = f"{target.provider.id}:{target.api_key}"
        client = self._clients.get(cache_key)
        if client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:  # pragma: no cover
                raise LLMError(
                    "未安装 openai SDK。请在项目 .venv 中执行：pip install openai"
                ) from exc
            client = AsyncOpenAI(
                api_key=target.api_key or "not-needed",
                base_url=target.provider.base_url,
                timeout=180.0,
                # 绝不自动重试：Moonshot 该账号有组织级 3 RPM 限流，
                # 失败后立刻重试只会自伤；降级路径由 stream_agent 负责，且只走一次。
                max_retries=0,
            )
            self._clients[cache_key] = client
        return client

    # ------------------------------------------------------------------ #
    # 单路由流式生成
    # ------------------------------------------------------------------ #
    async def stream_chat(
        self,
        provider_id: str,
        model_id: str,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        max_tokens: int = 1200,
        degraded: bool = False,
        features: list[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """逐段产出结构化事件：``{"kind": "reasoning"|"text", "text": …}``。

        调用方看不到底层协议差异。思考型模型（Kimi K2.6 等）会先吐大量
        ``reasoning_content`` 再吐正文，这里把两者分开成不同 kind，
        让上层能把「思考中」实时显示出来，而不是干等正文、看起来像卡死。
        """
        started = time.time()
        chars = 0
        # 当前调用属于哪个成员 —— 由 stream_agent 设置。
        # 不把它加到 stream_chat 的签名里，是因为 _record 的调用点有十几处，
        # 逐个补参数容易漏；用实例属性更稳。
        cur_agent = getattr(self, "_current_agent_id", "")
        try:
            target = self.resolve(provider_id, model_id)
        except LLMError as exc:
            self._record(provider_id, model_id, ok=False, error=str(exc),
                         started=started, degraded=degraded)
            raise

        if target.provider.is_mock:
            try:
                async for chunk in self._mock_stream(messages, model_id):
                    chars += len(chunk)
                    yield {"kind": "text", "text": chunk}
            except Exception as exc:
                self._record(provider_id, model_id, ok=False, error=str(exc), chars=chars,
                             started=started, degraded=degraded)
                raise
            self._record(provider_id, model_id, ok=True, chars=chars,
                         started=started, degraded=degraded)
            return

        client = self._client_for(target)
        params: dict[str, Any] = {
            "model": target.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": True,
            # 让上游在流式最后一个 chunk 里回传 usage，从而拿到真实 token 数（用于界面计费显示）
            "stream_options": {"include_usage": True},
        }
        omit_temperature = self._omit_temperature.get(target.route, False)
        if not omit_temperature:
            params["temperature"] = temperature

        # 厂商原生能力（联网等）。只在成员自己声明时开 —— 联网会把搜索结果
        # 塞进输入 token，成本影响很实在，不能默认全开。
        apply_features(params, target.provider.id, features)
        # 工作区工具：默认给所有成员挂上。不支持的厂商会在下面
        # 被 feature_rejected 兜底摘掉（见 strip_features）。
        attach_workspace_tools(params, True,
                                is_captain=(getattr(self, '_current_agent_role', '') == 'captain'))
        convo: list[dict[str, Any]] = list(messages)
        params["messages"] = convo

        reasoning_chars = 0
        finish_reason = ""
        tokens = 0
        prompt_tokens = 0
        completion_tokens = 0

        # ---------------------------------------------------------------- #
        # 工具轮回
        # ---------------------------------------------------------------- #
        # 内置联网（Kimi 的 $web_search）是"内置函数"：模型只**请求**搜索，
        # 必须把参数回灌它才会继续作答。所以这里是个循环，而不是一次调用。
        # 上限 MAX_TOOL_ROUNDS，防止模型反复要工具把一轮拖成无限。
        for round_no in range(MAX_TOOL_ROUNDS):
            stream = None
            feature_stripped = 0   # 0=没摘 1=摘了内置工具 2=连本地工具也摘
            for attempt in (0, 1, 2, 3):
                try:
                    stream = await client.chat.completions.create(**params)  # type: ignore[attr-defined]
                    break
                except (asyncio.TimeoutError, TimeoutError) as exc:
                    # 上游超时（思考型模型很常见）：转成 LLMError，让上层降级/报错，
                    # 绝不让 TimeoutError 冒到进程顶层把整轮讨论炸掉。
                    self._record(provider_id, model_id, ok=False,
                                 error=f"调用超时：{exc}", chars=chars,
                                 started=started, degraded=degraded)
                    raise LLMError(f"{target.route} 调用超时（{_short(exc)}）") from exc
                except Exception as exc:  # 上游拒绝（模型未开通 / 鉴权失败 / 网络不通）
                    text = str(exc)
                    # 某些模型只接受固定的 temperature（实测 kimi-k2.6 会返回
                    # "invalid temperature: only 1 is allowed for this model"）。
                    # 这里**只允许一次**参数纠正，并把结论记在内存里，之后不再重试——
                    # 绝不做循环重试，避免踩上游的 RPM 限流。
                    # 能力参数被拒（比如某个模型不支持 enable_search）：
                    # 摘掉能力**重试一次**，并留下可见的说明。
                    # 比让整条调用失败、静默降级到别的模型好得多 ——
                    # 那样用户只会看到"莫名其妙换了个模型回话"。
                    if attempt <= 1 and feature_stripped < 2 and feature_rejected(text):
                        gone = strip_features(params, level=feature_stripped + 1)
                        if gone:
                            feature_stripped += 1
                            self._feature_warnings[target.route] = (
                                f"该模型不支持 {'、'.join(gone)}，已按普通对话处理"
                            )
                            self._record(
                                provider_id, model_id, ok=False,
                                error=f"厂商能力参数被拒，已去掉 {'、'.join(gone)} 重试：{text}",
                                chars=chars, started=started, degraded=degraded,
                            )
                            continue
                    if attempt == 0 and "invalid temperature" in text and not omit_temperature:
                        self._omit_temperature[target.route] = True
                        params.pop("temperature", None)
                        self._record(
                            provider_id, model_id, ok=False,
                            error=f"temperature 被上游拒绝，已改为不传该参数重试一次：{text}",
                            chars=chars, started=started, degraded=degraded,
                        )
                        continue
                    self._record(provider_id, model_id, ok=False, error=text, chars=chars,
                                 started=started, degraded=degraded)
                    raise LLMError(f"{target.route} 调用失败：{_short(exc)}") from exc
            if stream is None:  # pragma: no cover - 理论不可达
                raise LLMError(f"{target.route} 调用未返回流对象")

            # 工具调用是**分片**吐出来的，得按 index 拼起来
            pending: dict[int, dict[str, str]] = {}
            text_this_round = ""
            finish_reason = ""
            try:
                async for event in stream:
                    # usage 只在最后一个 chunk 出现（include_usage=True 时）
                    usage = getattr(event, "usage", None)
                    if usage is not None:
                        # 多轮时按轮累加，否则最后一轮会盖掉前面的用量
                        tokens += int(getattr(usage, "total_tokens", 0) or 0)
                        prompt_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
                        completion_tokens += int(getattr(usage, "completion_tokens", 0) or 0)
                    if not getattr(event, "choices", None):
                        continue
                    choice = event.choices[0]
                    if getattr(choice, "finish_reason", None):
                        finish_reason = choice.finish_reason
                    delta = choice.delta
                    reasoning = getattr(delta, "reasoning_content", None)
                    if reasoning:
                        reasoning_chars += len(reasoning)
                        yield {"kind": "reasoning", "text": reasoning}
                    piece = getattr(delta, "content", None)
                    if piece:
                        chars += len(piece)
                        text_this_round += piece
                        yield {"kind": "text", "text": piece}
                    for tc in (getattr(delta, "tool_calls", None) or []):
                        idx = int(getattr(tc, "index", 0) or 0)
                        slot = pending.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                        if getattr(tc, "id", None):
                            slot["id"] = tc.id
                        fn = getattr(tc, "function", None)
                        if fn is not None:
                            if getattr(fn, "name", None):
                                slot["name"] = fn.name
                            if getattr(fn, "arguments", None):
                                slot["arguments"] += fn.arguments
            except (asyncio.TimeoutError, TimeoutError) as exc:
                self._record(provider_id, model_id, ok=False, error=f"流式超时：{exc}", chars=chars,
                             started=started, degraded=degraded, finish_reason=finish_reason,
                             reasoning_chars=reasoning_chars, tokens=tokens,
                             prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
                raise LLMError(f"{target.route} 流式超时中断：{_short(exc)}") from exc
            except LLMError:
                raise
            except Exception as exc:
                self._record(provider_id, model_id, ok=False, error=str(exc), chars=chars,
                             started=started, degraded=degraded, finish_reason=finish_reason,
                             reasoning_chars=reasoning_chars, tokens=tokens,
                             prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
                raise LLMError(f"{target.route} 流式读取中断：{_short(exc)}") from exc

            if not pending:
                break   # 没有工具请求 —— 正常结束

            # 看起来在要工具，但一轮都没吐字，说明它只想要工具：回灌后继续。
            # 把 assistant 的 tool_calls 和工具结果都追加进对话，再发一轮。
            calls = [pending[k] for k in sorted(pending)]
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": text_this_round or None,
                "tool_calls": [
                    {"id": c["id"] or f"call_{i}", "type": "function",
                     "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                    for i, c in enumerate(calls)
                ],
            }
            convo.append(assistant_msg)
            for i, c in enumerate(calls):
                result = builtin_tool_result(c["name"], c["arguments"])
                try:
                    from .workspace import LOCAL_TOOL_NAMES, execute_tool

                    if c["name"] in LOCAL_TOOL_NAMES:
                        result, info = execute_tool(c["name"], c["arguments"])
                        if info is not None:
                            # 把「真的做了什么」告诉上层：引擎据此公布文件名（写）、
                            # 显示"谁读了谁的东西"（读）。
                            # 统一成 tool_call + action 区分 —— 以后加新工具不必回来改这里。
                            yield {"kind": "tool_call", **info}
                except Exception as exc:  # noqa: BLE001 —— 工具失败不该炸掉整轮
                    result = f"工具执行出错：{type(exc).__name__}: {exc}"
                convo.append({
                    "role": "tool",
                    "tool_call_id": c["id"] or f"call_{i}",
                    "name": c["name"],
                    "content": result,
                })
            params["messages"] = convo
            if round_no == MAX_TOOL_ROUNDS - 1:
                self._record(provider_id, model_id, ok=False,
                             error=f"工具轮次达到上限 {MAX_TOOL_ROUNDS}，已停止",
                             chars=chars, started=started, degraded=degraded)
                break

        self._record(provider_id, model_id, ok=True, chars=chars,
                     started=started, degraded=degraded, finish_reason=finish_reason,
                     reasoning_chars=reasoning_chars, tokens=tokens,
                     prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)

    # ------------------------------------------------------------------ #
    # 带降级的成员调用
    # ------------------------------------------------------------------ #
    async def stream_agent(
        self,
        agent: AgentSpec,
        messages: list[dict[str, str]],
    ) -> AsyncIterator[dict[str, Any]]:
        """成员的带降级调用，产出统一事件：

        * ``{"kind": "route", "route", "requested_route", "warning", "degraded"}``
          一定且只产出一次，且在任何**正文**增量之前——调用方据此标注「已降级」。
        * ``{"kind": "reasoning", "text": "…"}`` 思考增量（思考型模型先吐这个）。
        * ``{"kind": "delta", "text": "…"}`` 正文增量。
        * ``{"kind": "done", …, "reasoning_chars": N}`` 调用结束，附真实 token 与思考字数。

        降级触发条件（两个都不能漏）：
        1. 主路由不可用（provider 未声明 / 缺密钥 / 缺 base_url）；
        2. 主路由可用但**还没吐过正文**就调用失败（模型未开通 / 鉴权失败 / 网络不通）。

        关键：思考增量（reasoning）**不算「已经输出」**。只要还没吐过正文，
        这条路由失败了就照样切下一条——否则 Kimi 这种「先思考 20 秒」的模型
        一旦在思考中途掉线，就再也不会降级，用户只能干等。
        反过来，已经吐过正文再断流属于「不完整回复」，如实报错而不是悄悄换模型。
        """
        # 让 _record 知道这次调用属于哪个成员，用量才能按成员归档。
        # 用实例属性而不是加参数：_record 的调用点有十几处，逐个补容易漏。
        self._current_agent_id = agent.id
        self._current_agent_role = agent.role
        requested = f"{agent.provider}/{agent.model}"
        attempts: list[tuple[str, str]] = [(agent.provider, agent.model)]
        fallback = agent.fallback or {}
        if fallback.get("provider") and fallback.get("model"):
            attempts.append((str(fallback["provider"]), str(fallback["model"])))

        last_error: Exception | None = None
        for index, (provider_id, model_id) in enumerate(attempts):
            route = f"{provider_id}/{model_id}"
            started = time.time()
            degraded_attempt = index > 0
            try:
                self.resolve(provider_id, model_id)
            except LLMError as exc:
                last_error = exc
                self._record(provider_id, model_id, ok=False, error=str(exc),
                             started=started, degraded=degraded_attempt)
                continue

            generator = self.stream_chat(
                provider_id,
                model_id,
                messages,
                temperature=agent.temperature,
                max_tokens=agent.max_tokens,
                degraded=degraded_attempt,
                # 把成员声明的厂商原生能力传下去（联网等）。
                # 落在这层而不是 engine：stream_agent 才是「一次成员调用」的入口。
                features=getattr(agent, "features", None),
            )
            warning = ""
            if degraded_attempt:
                warning = (
                    f"{requested} 不可用（{_short(last_error)}），"
                    f"已自动降级到 {route}；本条回复由降级路由生成。"
                )
            route_event = {
                "kind": "route",
                "route": route,
                "requested_route": requested,
                "warning": warning,
                "degraded": degraded_attempt,
            }
            # 已经是最后一条候选路由：不会再有降级，先把 route 事件发出去，
            # 这样「已降级」标注能赶在思考开始之前显示（否则要等 20 秒后的正文）。
            committed = index == len(attempts) - 1
            if committed:
                yield route_event

            saw_text = False
            try:
                async for chunk in generator:
                    if chunk.get("kind") == "reasoning":
                        # 思考增量立刻转发：不占 route 事件的「只发一次」额度，
                        # 也不算「已经输出过正文」——后面失败了仍然可以降级。
                        yield {"kind": "reasoning", "text": chunk["text"]}
                        continue
                    if chunk.get("kind") == "tool_call":
                        # 本地工具执行了（写/读/列目录）。**必须立刻转发**：
                        # 它既不是正文、也不算「已经开始输出」（路由仍可降级），
                        # 但引擎要靠 write 计数 —— 验收的前置条件正是"本轮有产物"。
                        yield dict(chunk)
                        continue
                    if not committed:
                        # 第一条正文到来 = 这条路由确实能用；此刻才提交 route 事件，
                        # 保证 route 事件「只产一次」且一定在正文之前。
                        yield route_event
                        committed = True
                    saw_text = True
                    yield {"kind": "delta", "text": chunk["text"]}
            except LLMError as exc:
                if not saw_text:
                    # 还没吐过正文：算这条路由不可用，切下一条（思考增量不作数）
                    last_error = exc
                    continue
                raise LLMError(
                    f"{route} 已开始输出后中断，本条回复不完整：{_short(exc)}"
                ) from exc

            if not committed:  # 空回复（没有任何事件）也算这条路由可用
                yield route_event
            # 调用结束：把真实 token 数（若上游回传了 usage）与思考字数作为 done 事件带出，
            # 供引擎写进 Message、界面在消息底部显示「模型 · tokens · 估算费用 · 其中思考 N 字」。
            entry = self.call_log[-1] if self.call_log else {}
            yield {
                "kind": "done",
                "route": route,
                "chars": entry.get("chars", 0),
                "tokens": entry.get("tokens", 0),
                "prompt_tokens": entry.get("prompt_tokens", 0),
                "completion_tokens": entry.get("completion_tokens", 0),
                "finish_reason": entry.get("finish_reason", ""),
                "reasoning_chars": entry.get("reasoning_chars", 0),
            }
            return

        detail = _short(last_error) if last_error else "未知原因"
        if agent.fallback:
            raise LLMError(
                f"{requested} 与降级路由 {agent.fallback_route} 均不可用：{detail}"
            )
        raise LLMError(f"{requested} 调用失败：{detail}（该成员未配置 fallback）")

    async def collect(
        self,
        provider_id: str,
        model_id: str,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.3,
        max_tokens: int = 900,
    ) -> str:
        parts: list[str] = []
        async for chunk in self.stream_chat(
            provider_id, model_id, messages, temperature=temperature, max_tokens=max_tokens
        ):
            # 只取正文；思考增量（kind="reasoning"）不进最终文本
            if chunk.get("kind") == "text":
                parts.append(chunk["text"])
        return "".join(parts)

    # ------------------------------------------------------------------ #
    # 离线 mock 通道
    # ------------------------------------------------------------------ #
    async def _mock_stream(
        self, messages: list[dict[str, str]], model_id: str
    ) -> AsyncIterator[str]:
        system_text = ""
        user_text = ""
        for message in messages:
            if message.get("role") == "system" and not system_text:
                system_text = message.get("content", "")
        for message in reversed(messages):
            if message.get("role") == "user":
                user_text = message.get("content", "")
                break

        text = _mock_reply(
            task=_extract_task(user_text),
            model_id=model_id,
            system_text=system_text,
            mentions=_mock_mentions(system_text, user_text, self.mock_hint),
            is_recorder="记录员" in system_text,
        )
        for chunk in _chunk(text, 6):
            await asyncio.sleep(0.004 + random.random() * 0.008)
            yield chunk


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _short(exc: Exception | None, limit: int = 260) -> str:
    text = str(exc).replace("\n", " ").strip() if exc is not None else ""
    return text[:limit] + ("…" if len(text) > limit else "")


# 组长收到的那条派发任务里必然包含这句话；mock 只在它出现时才「派活」，
# 这样组员的回复、组长的收敛发言都不会再触发新一轮派发。
_DISPATCH_MARKER = "请你作为组长处理"


def _extract_task(user_text: str) -> str:
    for marker in ("【派发给你的任务】", "用户最新发言：", "用户："):
        if marker in user_text:
            tail = user_text.split(marker, 1)[1]
            return tail.strip().splitlines()[0][:200] if tail.strip() else ""
    return user_text.strip()[:200]


def _roster_ids(system_text: str, limit: int = 2) -> list[str]:
    """从「群成员名单」里取出可被 @ 的组员 id（mock 用它模拟组长的派发）。

    名单行格式：``- @kimi：Kimi·论证担当，组员，标签 组员,长文,论证``。
    组长自己不在候选里——派发是「组长点组员」，不点自己。
    """
    ids: list[str] = []
    in_roster = False
    for line in (system_text or "").splitlines():
        if line.startswith("## "):
            in_roster = "成员名单" in line
            continue
        if not in_roster:
            continue
        stripped = line.strip()
        if not stripped.startswith("- @"):
            continue
        head, _, rest = stripped[3:].partition("：")
        token = head.strip()
        if "组长" in rest or not token:
            continue
        if token not in ids:
            ids.append(token)
        if len(ids) >= limit:
            break
    return ids


def _mock_mentions(system_text: str, user_text: str, hint: str) -> list[str]:
    """mock 的 @ 行为：默认在组长派发那一棒 @ 名单前两位成员。

    * hint = "quiet"：完全不 @ 任何人（演示「没人被点名就不唤醒组员」）；
    * hint = "ghost"：额外 @ 一个不存在的成员（演示未知目标的兜底处理）。
    """
    if hint == "quiet" or _DISPATCH_MARKER not in user_text:
        return []
    mentions = _roster_ids(system_text)
    if hint == "ghost":
        mentions = mentions + ["不存在的成员"]
    return mentions


def _mock_reply(
    task: str,
    model_id: str,
    system_text: str = "",
    mentions: list[str] | None = None,
    is_recorder: bool = False,
) -> str:
    topic = task or "（未提供具体任务）"
    mentions = mentions or []

    if is_recorder:
        return (
            f"【本轮结论】离线演示通道（{model_id}）生成的占位纪要：本轮的"
            f"「组长派发 → 组员应答 → 记录员汇总」已经完整跑通。\n"
            f"【形成的共识】1. 群聊内核能解析 @ 提及并唤醒对应成员；"
            f"2. 讨论结束后记录员会被单独唤醒并产出纪要。\n"
            f"【未决分歧】无——离线通道不做真实判断，这里是链路演示而非结论。\n"
            f"【下一步】把对应 provider 的密钥配置好，再用同样的问题跑一次真实模型。"
        )

    lines = [
        f"【要点】这是离线演示通道（{model_id}）生成的占位回复，不是真实模型输出；"
        f"它证明「群主派发 → 组员应答 → 记录员汇总」这条链路是通的。",
        f"【依据】1. 收到的任务文本是：「{topic}」。",
        f"2. 该回复由本地规则模板拼装，全程零网络请求、零额度消耗。",
        f"3. 把该 provider 的密钥配进环境变量后，这里就会换成真实模型的回答。",
        # 演示「主动上报」徽章：偶数号轮次在正文里放一个触发词，
        # 让离线演示也能看到「▲ 主动上报」徽章的渲染效果（触发词表见 engine._PROACTIVE_PATTERNS）
        "【盲点】风险提示：离线通道没有推理能力，请不要把它当作对任务本身的结论。",
    ]
    if mentions:
        who = "、".join(f"@{m}" for m in mentions)
        lines.append(f"【派发】{who}：请就上面的拆解补上你这一部分的产出。")
    return "\n".join(lines)


def _chunk(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]
