"""FastAPI 服务端：静态页面 + 快照接口 + 健康检查 + WebSocket 实时群聊。

启动（在 ai-group 目录下）::

    .venv\\Scripts\\python.exe -m uvicorn group.app:app --port 8801

浏览器打开 http://127.0.0.1:8801 即可看到群聊界面。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import (
    MEMBERS_FILE,
    WEB_DIR,
    check_invariants,
    force_mock,
    load_config,
    probe_availability,
    reload_config,
    update_member_role,
)
from .config import user_data_dir, _data_dir_candidates
from .engine import GroupEngine, RunLimits
from .engine import _history_file
from .llm import LLMClient
from .detect import detect_key, test_model, test_models
from .members import (
    MemberError,
    add_member,
    clear_avatar,
    list_avatars,
    read_role_prompt,
    save_avatar,
    write_role_prompt,
    limits_view,
    presets_payload,
    remove_member,
    update_limits,
    update_member,
)

app = FastAPI(title="AI 群聊 · 多模型协作系统", version="1.0.0")

CFG = load_config()
# SMOKE_MOCK_ONLY=1（scripts/run.ps1 -MockOnly 会设置）→ 全部走离线通道，绝不调用真实模型
MOCK_ONLY = os.environ.get("SMOKE_MOCK_ONLY", "").strip() in {"1", "true", "yes"}
if MOCK_ONLY:
    force_mock(CFG)

CLIENT = LLMClient(CFG)


class Hub:
    """极简广播中心：把群聊事件推给所有在线浏览器。"""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.history: list[dict] = []  # 供刷新后的页面补历史
        self.max_history = 400
        self.lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    async def broadcast(self, event: dict) -> None:
        async with self.lock:
            self.history.append(event)
            if len(self.history) > self.max_history:
                self.history = self.history[-self.max_history :]
            dead: list[WebSocket] = []
            payload = json.dumps(event, ensure_ascii=False)
            for ws in list(self.clients):
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.clients.discard(ws)


HUB = Hub()


def limits_from_config(cfg) -> RunLimits:
    """把 models.yaml 的 ``limits:`` 段变成引擎真正使用的 RunLimits。

    必须定义在 ``ENGINE = ...`` 之前 —— 模块是顺序执行的，放后面会 NameError。
    """
    spec = getattr(cfg, "limits", None)
    if spec is None:
        return RunLimits()
    return RunLimits(
        max_messages=int(spec.max_messages),
        max_depth=int(spec.max_depth),
        max_context_chars=int(spec.max_context_chars),
    )


ENGINE = GroupEngine(CFG, CLIENT, HUB.broadcast, limits_from_config(CFG))


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class ChatIn(BaseModel):
    text: str = ""
    sender: str = "用户"
    # 仅对离线 mock 通道生效的演示开关（quiet=不 @ 任何人，ghost=@ 一个不存在的成员）
    mock_hint: str = ""
    # 附件：[{kind: "image"|"file", name, mime, dataUrl?, text?, size?}]
    # 文本文件的 text 会被注入消息（模型真能读到）；图片只做展示，见 engine._merge_attachments
    attachments: list[dict] = []
    # 「这条发给谁」："group" = 群聊，"dm:<成员id>" = 和该成员的 1:1 私聊
    channel: str = "group"


class MemberRoleIn(BaseModel):
    """运行时改角色：把 id 指定的成员设成 captain 或 member。"""

    id: str
    role: str


class MemberIn(BaseModel):
    """界面「添加成员」：用户只需要填名字、提供商、模型，以及该提供商的 API Key。

    * ``provider`` 取 presets 里的 id，或 ``custom``（此时必须给 base_url）；
    * ``api_key`` 只写进 server/.env，**不写 YAML**；
    * 留空 api_key 表示「我已经配过这个提供商的密钥了」，照样能加成员。
    """

    name: str
    provider: str
    model: str
    apiKey: str = ""
    baseUrl: str = ""
    role: str = "member"
    duty: str = ""
    avatar: str = ""
    template: str = ""
    # 角色提示词正文。填了就直接用它当系统提示词，不用再去 config/roles/ 改文件。
    rolePrompt: str = ""


class MemberEditIn(BaseModel):
    """编辑成员：**只改传进来的字段**。最常用的是只传 apiKey 换密钥。"""

    name: str = ""
    model: str = ""
    apiKey: str = ""
    baseUrl: str = ""
    duty: str = ""
    avatar: str = ""
    maxTurnsPerRun: int | None = None
    temperature: float | None = None
    maxTokens: int | None = None
    # 角色提示词整篇覆盖（空字符串 = 不改）
    rolePrompt: str = ""


class DetectKeyIn(BaseModel):
    """识别一把 API Key 属于哪家。Key 只在内存里用于探测，不落盘、不回显。"""

    key: str = ""
    baseUrl: str = ""


class TestModelIn(BaseModel):
    """验证某个模型能不能用：发一次最小真实调用。"""

    provider: str = ""
    model: str = ""
    key: str = ""
    baseUrl: str = ""


class TestModelsIn(BaseModel):
    """批量实测一批模型能不能用。"""

    provider: str = ""
    models: list[str] = []
    key: str = ""
    baseUrl: str = ""


class AvatarIn(BaseModel):
    """自定义头像。target 为空或 "user" 表示用户自己，否则是成员 id。"""

    target: str = "user"
    dataUrl: str = ""


class LimitsIn(BaseModel):
    """单轮额度护栏。只传要改的那几项。"""

    max_messages: int | None = None
    max_depth: int | None = None
    max_context_chars: int | None = None


def _apply_config(new_cfg) -> None:
    """把重新装载的配置热替换到正在运行的引擎与客户端上。

    ``load_config`` 带 lru_cache，改完 models.yaml 必须先失效再取；取到的新对象
    不会自动替换旧引用，所以这里显式把 ``CFG / CLIENT.cfg / ENGINE.cfg`` 一起换成新对象——
    这样**不改任何启动参数、不重启进程**，下一轮派发就用新角色。
    """
    global CFG
    CFG = new_cfg
    if MOCK_ONLY:
        # mock 模式下重新装载会带回真实 provider，必须重新压回离线通道
        force_mock(CFG)
    CLIENT.cfg = CFG
    ENGINE.cfg = CFG
    # 护栏也跟着换 —— 否则改了「单轮最多发言」还要重启才生效
    ENGINE.limits = limits_from_config(CFG)



@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/snapshot")
async def snapshot() -> JSONResponse:
    """当前群状态 + 完整聊天记录 + 额度与路由小结。

    顶层键与 WS hello 的 snapshot 保持一致（members/limits/run_stats/route_summary/messages），
    这样页面刷新后既能看到完整记录，也能立刻看到「本轮额度用了多少、有没有同源/降级/失败」，
    不必等下一次 WS 推送。
    """
    return JSONResponse(
        {
            "group": {
                "name": "AI 群 · 多模型协作",
                "members": ENGINE.members_view(),
                "recorder_route": CFG.recorder.route,
                "limits": ENGINE.limits_view(),
            },
            "members": ENGINE.members_view(),
            "limits": ENGINE.limits_view(),
            "run_stats": ENGINE.run_stats(),
            "route_summary": ENGINE.route_summary(),
            "messages": [m.to_dict() for m in ENGINE.messages],
            "running": ENGINE._running,
        }
    )


@app.get("/api/health")
async def health() -> JSONResponse:
    """一眼看清每个 provider 到底能不能用——这是接入多厂商后最需要的信息。"""
    return JSONResponse(
        {
            "ok": True,
            "time": time.time(),
            "mock_only": MOCK_ONLY,
            # 用户数据放在哪 —— 排查「聊天记录 / 头像去哪了」时最有用。
            # 这两项刻意放在系统目录而不是程序目录里，换版本时才不会跟着丢。
            "dataDir": _user_path(user_data_dir()),
            "dataDirCandidates": [str(x) for x in _data_dir_candidates()],
            "dataDirFallback": str(user_data_dir()).endswith("server\\data"),
            "historyFile": _user_path(_history_file()),
            "workspaceDir": _user_path(__import__("group.workspace", fromlist=["workspace_dir"]).workspace_dir()),
            "historyCount": len(ENGINE.messages),
            "adapters": _adapter_status(),
            "limits": ENGINE.limits_view(),
            "invariants": check_invariants(CFG),
            "providers": [
                {"provider": a.provider, "ok": a.ok, "detail": a.detail}
                for a in probe_availability(CFG)
            ],
            "members": [
                {
                    "id": a.id,
                    "name": a.display_name,
                    "role": a.role,
                    "route": a.route,
                    "fallback_route": a.fallback_route,
                    "enabled": a.enabled,
                    "can_dispatch": a.can_dispatch,
                    "max_turns_per_run": a.max_turns_per_run,
                }
                for a in CFG.agents
            ],
        }
    )


@app.get("/api/diagnostics")
async def diagnostics() -> JSONResponse:
    """每次真实模型调用的记录（路由 / 目标 host / 成败 / 报错 / 字数 / 耗时）。

    用途：核对「声明的路由到底有没有被调用、有没有被静默换成别的模型」——
    事件流里的 route 与实际调用记录必须一一对应。日志里**不含任何密钥**。
    """
    return JSONResponse(
        {
            "mock_only": MOCK_ONLY,
            "call_log": CLIENT.call_log[-120:],
            "run_stats": ENGINE.run_stats(),
            "limits": ENGINE.limits_view(),
        }
    )


@app.get("/api/ag2")
async def ag2_status() -> JSONResponse:
    """AG2 适配层状态：装了能用就报可用，没装就报降级说明（主流程不受影响）。

    这里只回报「能不能把成员编译成 llm_config」，不回传密钥本身。
    """
    info = _adapter_status()
    compiled: list[dict] = []
    for spec in CFG.agents:
        if not spec.enabled:
            continue
        entry = {
            "id": spec.id,
            "route": spec.route,
            "fallback_route": spec.fallback_route,
            "llm_config_ready": False,
            "error": "",
        }
        try:
            from .ag2_adapter import build_llm_config

            build_llm_config(CFG, CLIENT, spec)
            entry["llm_config_ready"] = True
        except Exception as exc:
            entry["error"] = str(exc)
        compiled.append(entry)
    info["members"] = compiled
    info["build_ag2_group"] = (
        "可用（AG2 已安装）" if info.get("ag2") else "不可用：需 pip install ag2，主流程不受影响"
    )
    return JSONResponse(info)


@app.post("/api/chat")
async def chat(payload: ChatIn) -> JSONResponse:
    """非流式入口，方便用 curl / 脚本驱动（结果通过 WebSocket 广播给页面）。"""
    if not payload.text.strip() and not payload.attachments:
        return JSONResponse({"ok": False, "error": "文本为空"}, status_code=400)
    if ENGINE._running:
        return JSONResponse({"ok": False, "error": "上一轮讨论仍在进行"}, status_code=409)
    asyncio.create_task(
        ENGINE.handle_user_message(
            payload.text, payload.sender, payload.mock_hint, payload.attachments,
            payload.channel,
        )
    )
    return JSONResponse(
        {"ok": True, "mode": "streaming-over-websocket", "limits": ENGINE.limits_view()}
    )


@app.post("/api/reset")
async def reset() -> JSONResponse:
    if ENGINE._running:
        return JSONResponse({"ok": False, "error": "讨论进行中，无法重置"}, status_code=409)
    ENGINE.messages.clear()
    ENGINE.agent_state.clear()
    HUB.history.clear()
    ENGINE.save_history()
    try:
        from .usage import clear_today

        clear_today()
    except Exception:  # noqa: BLE001
        pass   # 清空也要落盘，否则重启又会把旧记录读回来
    await HUB.broadcast({"kind": "cleared", "ts": time.time()})
    return JSONResponse({"ok": True})


@app.post("/api/member-role")
async def member_role(payload: MemberRoleIn) -> JSONResponse:
    """运行时把某位成员设为组长/组员，并把角色写进 config/models.yaml（重启仍生效）。

    * 记录员不可改 → 400；
    * 升为组长时原组长自动降为 member（全局只有一个 captain）；
    * 群必须保留一个组长，降级最后一名组长 → 400；
    * 讨论进行中不允许改 → 409。
    """
    if ENGINE._running:
        return JSONResponse({"ok": False, "error": "讨论进行中，无法改角色"}, status_code=409)
    try:
        new_cfg, changed = update_member_role(payload.id, payload.role)
    except KeyError:
        return JSONResponse({"ok": False, "error": f"没有这个成员：{payload.id}"}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    # 热替换：不重启进程，下一轮派发就用新角色
    _apply_config(new_cfg)
    await ENGINE.emit_state()  # 把新的成员表推给所有在线页面
    members = ENGINE.members_view()
    return JSONResponse(
        {
            "ok": True,
            "changed": changed,
            "captains": [m["id"] for m in members if m["role"] == "captain"],
            "persisted_to": str(MEMBERS_FILE),
            "members": members,
        }
    )


@app.get("/api/member-presets")
async def member_presets() -> JSONResponse:
    """给「添加成员」对话框用：预设提供商 + 各自当前有没有可用凭据。

    **不回显任何密钥**，只报 hasKey —— 和凭据解析一样的克制。
    """
    return JSONResponse({"ok": True, "presets": presets_payload()})


@app.post("/api/member")
async def create_member(payload: MemberIn) -> JSONResponse:
    """新增一位群成员。

    用户只需要提供：名字 + 提供商 + 模型 + 该提供商的 API Key。
    Key 写进 ``server/.env``（不进 YAML），成员条目追加到 ``config/models.yaml``，
    并为它生成一份**独立**的角色提示词文件。
    """
    if ENGINE._running:
        return JSONResponse({"ok": False, "error": "讨论进行中，无法添加成员"}, status_code=409)
    try:
        new_cfg, created = add_member(
            name=payload.name,
            provider=payload.provider,
            model=payload.model,
            base_url=payload.baseUrl,
            api_key=payload.apiKey,
            role=payload.role,
            duty=payload.duty,
            avatar=payload.avatar,
            template=payload.template,
            role_prompt=payload.rolePrompt,
        )
    except MemberError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    _apply_config(new_cfg)
    await ENGINE.emit_state()
    return JSONResponse(
        {
            "ok": True,
            "created": created,
            "persisted_to": str(MEMBERS_FILE),
            "members": ENGINE.members_view(),
        }
    )


@app.delete("/api/member/{agent_id}")
async def delete_member(agent_id: str) -> JSONResponse:
    """删除一位群成员（连同它的角色提示词文件）。组长与记录员有保护。"""
    if ENGINE._running:
        return JSONResponse({"ok": False, "error": "讨论进行中，无法删除成员"}, status_code=409)
    try:
        new_cfg, name = remove_member(agent_id)
    except KeyError:
        return JSONResponse({"ok": False, "error": f"没有这个成员：{agent_id}"}, status_code=404)
    except MemberError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    _apply_config(new_cfg)
    await ENGINE.emit_state()
    return JSONResponse({"ok": True, "removed": agent_id, "name": name, "members": ENGINE.members_view()})


@app.patch("/api/member/{agent_id}")
async def edit_member(agent_id: str, payload: MemberEditIn) -> JSONResponse:
    """编辑成员信息（改名字 / 模型 / 头像 / 职责 / 发言次数 / temperature / 换 API Key）。

    只修改传进来的字段，其余保持不动 —— 用户最常用的就是「只换 Key」。
    """
    if ENGINE._running:
        return JSONResponse({"ok": False, "error": "讨论进行中，无法修改成员"}, status_code=409)
    try:
        new_cfg, updated = update_member(
            agent_id,
            name=payload.name,
            model=payload.model,
            api_key=payload.apiKey,
            base_url=payload.baseUrl,
            duty=payload.duty,
            avatar=payload.avatar,
            max_turns_per_run=payload.maxTurnsPerRun,
            temperature=payload.temperature,
            max_tokens=payload.maxTokens,
            role_prompt=payload.rolePrompt,
        )
    except KeyError:
        return JSONResponse({"ok": False, "error": f"没有这个成员：{agent_id}"}, status_code=404)
    except MemberError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    _apply_config(new_cfg)
    await ENGINE.emit_state()
    return JSONResponse({"ok": True, "updated": updated, "members": ENGINE.members_view()})


@app.get("/api/limits")
async def get_limits() -> JSONResponse:
    """当前的单轮额度护栏（发言数 / @ 深度 / 上下文上限）与取值范围。"""
    return JSONResponse({"ok": True, "limits": limits_view()})


@app.put("/api/limits")
async def put_limits(payload: LimitsIn) -> JSONResponse:
    """改护栏并持久化到 models.yaml，立刻生效（不需要重启）。"""
    if ENGINE._running:
        return JSONResponse({"ok": False, "error": "讨论进行中，无法修改设置"}, status_code=409)
    values = {
        key: getattr(payload, key)
        for key in ("max_messages", "max_depth", "max_context_chars")
        if getattr(payload, key) is not None
    }
    try:
        new_cfg, view = update_limits(values)
    except MemberError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    _apply_config(new_cfg)
    await ENGINE.emit_state()
    return JSONResponse({"ok": True, "limits": view, "applied": ENGINE.limits_view()})


@app.post("/api/detect-key")
async def detect_key_route(payload: DetectKeyIn) -> JSONResponse:
    """把 Key 发到各家 /models 端点探测归属，顺带带回可用模型列表。

    不用格式猜的原因：`sk-` 前缀被 OpenAI/DeepSeek/Moonshot/通义 等一大批共用，
    看形状分不出来；返回 200 才是事实。
    """
    key = (payload.key or "").strip()
    if not key:
        return JSONResponse({"ok": False, "error": "请先填写 API Key"}, status_code=400)
    try:
        result = await detect_key(key, payload.baseUrl or "")
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "error": f"探测失败：{type(exc).__name__}: {exc}"}, status_code=500
        )
    return JSONResponse(result)


@app.post("/api/test-model")
async def test_model_route(payload: TestModelIn) -> JSONResponse:
    """用一次最小真实调用验证某个模型到底能不能用。

    `/models` 只能说明端点列出了它，说明不了你的账号开通了它 ——
    所以这里真发一次请求（只要 1 个 token，成本可忽略）。
    如果请求里没带 key，就用配置里已保存的那把。
    """
    model_id = (payload.model or "").strip()
    if not model_id:
        return JSONResponse({"ok": False, "error": "请先填写模型 id"}, status_code=400)

    key = (payload.key or "").strip()
    if not key:
        provider_id = (payload.provider or "").strip()
        prov = CFG.providers.get(provider_id)
        if prov is not None:
            from .config import resolve_credential

            key, _src = resolve_credential(prov.credentials)
    if not key:
        return JSONResponse(
            {"ok": False, "error": "需要 API Key（配置里也没有可用的）"}, status_code=400
        )

    try:
        result = await test_model(payload.provider, model_id, key, payload.baseUrl)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "error": f"测试失败：{type(exc).__name__}: {exc}"}, status_code=500
        )
    return JSONResponse(result)


@app.post("/api/test-models")
async def test_models_route(payload: TestModelsIn) -> JSONResponse:
    """并发实测一批模型，找出账号真正能调用的那些。

    为什么需要这个：平台「免费额度」页和 /models 列出的都只是**目录**，
    账号能不能真调用要另说（常见 403 Access to model denied）。
    一次只要 1 个 token，几十个模型也就几秒钟。
    """
    models = [str(m).strip() for m in (payload.models or []) if str(m).strip()]
    if not models:
        return JSONResponse({"ok": False, "error": "没有要测试的模型"}, status_code=400)

    key = (payload.key or "").strip()
    if not key:
        prov = CFG.providers.get((payload.provider or "").strip())
        if prov is not None:
            from .config import resolve_credential

            key, _src = resolve_credential(prov.credentials)
    if not key:
        return JSONResponse(
            {"ok": False, "error": "需要 API Key（配置里也没有可用的）"}, status_code=400
        )

    try:
        result = await test_models(payload.provider, models, key, payload.baseUrl)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "error": f"批量测试失败：{type(exc).__name__}: {exc}"}, status_code=500
        )
    return JSONResponse(result)


@app.get("/api/member-role-content/{agent_id}")
async def member_role_content(agent_id: str) -> JSONResponse:
    """读出某位成员当前的系统提示词，给「编辑成员」界面预填。"""
    try:
        text = read_role_prompt(agent_id)
    except KeyError:
        return JSONResponse({"ok": False, "error": f"没有这个成员：{agent_id}"}, status_code=404)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "error": f"读取失败：{type(exc).__name__}: {exc}"}, status_code=500
        )
    agent = CFG.by_id(agent_id)
    return JSONResponse({
        "ok": True,
        "id": agent_id,
        "roleFile": agent.role_file if agent else "",
        "text": text,
        "chars": len(text),
    })


@app.get("/api/avatars")
async def get_avatars() -> JSONResponse:
    """一次取回所有自定义头像（dataURL）。

    头像现在是**文件**（server/config/avatars/），跟角色提示词一个存法 ——
    可见、可备份、换版本不丢、拷 config/ 就能带走。
    """
    try:
        data = list_avatars()
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"读取失败：{exc}"}, status_code=500)
    return JSONResponse({"ok": True, "avatars": data})


@app.post("/api/avatar")
async def put_avatar(payload: AvatarIn) -> JSONResponse:
    """存一张自定义头像：写文件 + 把路径写进 models.yaml。"""
    if ENGINE._running:
        return JSONResponse({"ok": False, "error": "讨论进行中，稍后再改"}, status_code=409)
    try:
        rel, name = save_avatar(payload.target or "user", payload.dataUrl)
    except KeyError:
        return JSONResponse({"ok": False, "error": f"没有这个成员：{payload.target}"}, status_code=404)
    except MemberError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"保存失败：{exc}"}, status_code=500)

    _apply_config(reload_config())
    await ENGINE.emit_state()
    return JSONResponse({"ok": True, "target": payload.target or "user",
                         "path": rel, "file": name})


@app.delete("/api/avatar/{target}")
async def delete_avatar(target: str) -> JSONResponse:
    """恢复默认头像：删文件 + 清掉 models.yaml 里的 avatar。"""
    if ENGINE._running:
        return JSONResponse({"ok": False, "error": "讨论进行中，稍后再改"}, status_code=409)
    try:
        removed = clear_avatar(target)
    except KeyError:
        return JSONResponse({"ok": False, "error": f"没有这个成员：{target}"}, status_code=404)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"删除失败：{exc}"}, status_code=500)

    _apply_config(reload_config())
    await ENGINE.emit_state()
    return JSONResponse({"ok": True, "target": target, "removed": removed})


@app.post("/api/open-workspace")
async def open_workspace() -> JSONResponse:
    """在资源管理器里打开工作区目录。

    没有这个入口，用户根本不知道成员产出的文件放哪了 ——
    数据目录可能在 LOCALAPPDATA，也可能在程序文件夹，没人猜得到。

    **不能用 subprocess.Popen(['explorer', ...])**：
    本程序是 PyInstaller 打包的，启动时它改掉了进程的 DLL 搜索路径（指向 _internal）。
    子进程 explorer.exe 继承这个环境后会加载到冲突的 DLL，
    直接弹「应用程序无法正常启动(0xc0000142)」。
    os.startfile 走 ShellExecute —— 由系统 shell 起进程，
    我们这边的 DLL 路径不会传过去，所以没这个问题。
    """
    import os
    import subprocess
    import sys as _sys

    from .workspace import workspace_dir

    path = workspace_dir()
    errors: list[str] = []

    # 首选 ShellExecute
    try:
        if hasattr(os, "startfile"):
            os.startfile(str(path))  # type: ignore[attr-defined]
            return JSONResponse({"ok": True, "path": _user_path(path), "via": "startfile"})
        if _sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
            return JSONResponse({"ok": True, "path": _user_path(path), "via": "open"})
        subprocess.Popen(["xdg-open", str(path)])
        return JSONResponse({"ok": True, "path": _user_path(path), "via": "xdg-open"})
    except Exception as exc:  # noqa: BLE001
        errors.append(f"startfile: {exc}")

    # 兜底：子进程方式。**必须清洗环境**，否则会重演 0xc0000142 ——
    # PyInstaller 会把 _internal 塞进 PATH / DLL 搜索路径，explorer 继承后会崩。
    if _sys.platform == "win32":
        try:
            env = dict(os.environ)
            env.pop("_MEIPASS2", None)
            env.pop("_PYI_APPLICATION_HOME_DIR", None)
            env.pop("_PYI_ARCHIVE_FILE", None)
            env.pop("_PYI_PARENT_PROCESS_LEVEL", None)
            # 去掉打包目录，让 explorer 用系统自己的 DLL
            internal = os.path.dirname(os.path.abspath(_sys.executable))
            parts = [p for p in env.get("PATH", "").split(os.pathsep)
                     if p and os.path.normcase(os.path.abspath(p)) != os.path.normcase(internal)]
            env["PATH"] = os.pathsep.join(parts)
            subprocess.Popen(["explorer", str(path)], env=env, close_fds=True)
            return JSONResponse({"ok": True, "path": _user_path(path), "via": "explorer(clean env)"})
        except Exception as exc:  # noqa: BLE001
            errors.append(f"explorer: {exc}")

    # 打不开**不算错误** —— 界面会给出路径让用户自己粘贴。
    # 返回 200 而不是 500：这是"自动打开没成功"，不是"接口坏了"。
    return JSONResponse(
        {"ok": False, "error": "；".join(errors) or "自动打开失败", "path": _user_path(path)},
        status_code=200,
    )


@app.get("/api/workspace")
async def get_workspace() -> JSONResponse:
    """列出工作区里的文件。

    这是「打开工作区」的**主路径** —— 直接在界面里看，
    不用去拉外部程序（那正是之前 0xc0000142 的来源）。
    """
    from .workspace import all_files, workspace_dir

    try:
        files = all_files()
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    total = sum(f["bytes"] for f in files)
    return JSONResponse({
        "ok": True,
        "dir": _user_path(workspace_dir()),
        "files": files,
        "count": len(files),
        "totalBytes": total,
    })


@app.get("/api/workspace/file")
async def get_workspace_file(path: str) -> JSONResponse:
    """读工作区里某个文件的内容，供界面预览。

    路径经过 workspace.safe_rel 校验 —— 只能读工作区内的文件。
    """
    from .workspace import read_file, safe_rel

    rel = safe_rel(path)
    if rel is None:
        return JSONResponse({"ok": False, "error": "路径不合法"}, status_code=400)
    body = read_file(rel)
    if body is None:
        return JSONResponse({"ok": False, "error": f"没有这个文件：{rel}"}, status_code=404)
    # 太长的不整份给界面，前端也渲染不动
    limit = 200_000
    truncated = len(body) > limit
    return JSONResponse({
        "ok": True, "path": rel,
        "content": body[:limit] if truncated else body,
        "truncated": truncated,
        "chars": len(body),
    })


def _user_path(p) -> str:
    r"""把内部路径转成**能粘进资源管理器**的形式。

    Windows 的 \?\ 扩展长度前缀在程序内部好用（绕过 MAX_PATH），
    但粘到资源管理器地址栏是打不开的 —— 所以给用户看的一律去掉。
    """
    text = str(p)
    if text.startswith("\\\\?\\"):
        text = text[4:]
    return text.replace("/", "\\")


@app.get("/api/usage")
async def get_usage() -> JSONResponse:
    """今日已累计的用量（原始计数）。

    侧栏的「今日消耗」原来只在前端内存里，大退就归零。
    前端现在拿这份当**基线**，再加上本次启动后的增量 ——
    计价公式仍然只有前端那一份。
    """
    try:
        from .usage import today as usage_today

        data = usage_today()
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return JSONResponse({"ok": True, **data})


@app.get("/api/balance")
async def get_balance(refresh: int = 0) -> JSONResponse:
    """各厂商的真实余额。

    **大部分厂商不给这个接口** —— 实测只有 DeepSeek 和 Kimi 有，
    其余返回 unsupported，界面会如实写「该厂商未提供余额接口」，
    而不是编一个数字。查不到的用本机累计消耗补位。
    """
    from .balance import all_balances

    try:
        data = all_balances(CFG, use_cache=not refresh)
    except Exception as exc:  # noqa: BLE001 —— 查余额失败不该让界面报错
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return JSONResponse({"ok": True, "balances": data, "time": time.time()})


@app.get("/api/tasks")
async def get_tasks() -> JSONResponse:
    """任务板。跨轮次、跨重启保留 —— 这就是「做到哪了」的权威来源。"""
    from .taskboard import all_tasks, counts

    try:
        return JSONResponse({"ok": True, "tasks": all_tasks(), "counts": counts()})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.delete("/api/tasks")
async def delete_tasks() -> JSONResponse:
    """清空任务板（用户手动清空聊天记录时一并调用）。"""
    from .taskboard import clear

    try:
        clear()
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return JSONResponse({"ok": True})


@app.post("/api/stop")
async def stop_run() -> JSONResponse:
    """立刻停止本轮讨论/干活。

    之前用户只能杀进程 —— 一轮里开着 Kimi 那种 20 秒的调用时，这很要命。
    """
    try:
        had = ENGINE.request_stop()
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return JSONResponse({"ok": True, "stopped": bool(had), "running": ENGINE._running})


def _adapter_status() -> dict:
    try:
        from .ag2_adapter import AG2_AVAILABLE, describe

        return describe()
    except Exception as exc:  # pragma: no cover
        return {"ag2": False, "detail": f"适配层加载失败：{exc}"}


# --------------------------------------------------------------------------- #
# WebSocket
# --------------------------------------------------------------------------- #
@app.websocket("/ws/group")
async def ws_group(ws: WebSocket) -> None:
    await HUB.connect(ws)
    try:
        await ws.send_text(
            json.dumps(
                {
                    "kind": "hello",
                    "ts": time.time(),
                    "snapshot": {
                        **ENGINE.snapshot(),
                        "recorder_route": CFG.recorder.route,
                        "adapters": _adapter_status(),
                    },
                },
                ensure_ascii=False,
            )
        )
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            action = data.get("action")
            if action == "chat":
                text = (data.get("text") or "").strip()
                atts = data.get("attachments") or []
                if not isinstance(atts, list):
                    atts = []
                if not text and not atts:
                    continue
                asyncio.create_task(
                    ENGINE.handle_user_message(
                        text, data.get("sender") or "用户",
                        data.get("mock_hint") or "", atts,
                        data.get("channel") or "group",
                    )
                )
            elif action == "ping":
                await ws.send_text(json.dumps({"kind": "pong", "ts": time.time()}))
            elif action == "snapshot":
                await ws.send_text(
                    json.dumps(
                        {"kind": "hello", "ts": time.time(), "snapshot": ENGINE.snapshot()},
                        ensure_ascii=False,
                    )
                )
            elif action == "reset":
                if not ENGINE._running:
                    ENGINE.messages.clear()
                    ENGINE.agent_state.clear()
                    HUB.history.clear()
                    await HUB.broadcast({"kind": "cleared", "ts": time.time()})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        HUB.disconnect(ws)


# 静态资源（web/ 目录）
if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
