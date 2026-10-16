"""群聊内核：任务分配 / @触发 / 回复汇总。

这是题目第 3 条的落地模块，也是整套系统的中枢。它自己不做任何推理，
只做四件事：

1. **派发**：把用户发言交给组长 Agent，解析其回复里的 @提及，生成待发言队列。
2. **触发**：按队列依次唤醒被 @ 的成员，把「群聊记录 + 派给他的任务」喂给他。
3. **收敛**：每轮讨论结束后，把组长重新叫回来做一次收敛发言。
4. **汇总**：由记录员 Agent 把整轮讨论压成一段给用户看的结论。
5. **广播**：全程以事件流形式推给 Web 界面，实现逐字上屏。

失控防护（都是真金白银的额度，必须硬约束）：
* ``max_messages``：单轮**派发链尝试次数**上限（组长 + 被 @ 的组员），记录员纪要不占配额；
* 成员级 ``max_turns_per_run``：单个成员一轮最多**成功发言**几次（失败调用不计次）；
* ``max_depth``：@ 触发深度上限，防止组员互相 @ 无限套娃；
* ``max_context_chars``：喂给模型的 system + user 总字符数上限，超出时丢弃最旧的中间发言
  并广播 ``context_trimmed`` 事件（本轮用户的原话永远保留，且钉住的首条也受预算约束）。

单轮模型调用总数的上界 = ``max_messages`` + 1（记录员纪要，一轮最多一次）。
所有「被忽略 / 被截断 / 被跳过的 @」都会发 notice 并写明原因——静默忽略等于让界面说谎。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Awaitable, Callable

from .config import AgentSpec, GroupConfig, extract_mentions
from .llm import LLMClient, LLMError
import re
import json

EventSink = Callable[[dict], Awaitable[None]]


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class Message:
    id: str
    sender: str
    sender_name: str
    sender_avatar: str
    role: str  # user | captain | member | recorder | system
    content: str
    mentions: list[str] = field(default_factory=list)
    route: str = ""           # 实际生效的 provider/model
    requested_route: str = ""  # 配置声明的 provider/model
    warning: str = ""         # 降级提示
    error: str = ""           # 调用失败原因（失败也要留痕，纪要才不会装作完整）
    created_at: float = field(default_factory=time.time)
    run_id: str = ""
    turn: int = 0
    meta: dict = field(default_factory=dict)
    # 真实 token 计量（上游 usage 回传；mock 通道为 0）。用于消息底部显示「模型 · tokens · 估算费用」
    tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # 附件（图片/文件）：界面渲染用。文本文件的内容已并入 content，这里只留展示信息
    attachments: list[dict] = field(default_factory=list)
    # 「回复某条消息」的引用（问题三：引用条显示被回复内容的摘要）
    reply_to: str = ""        # 被回复消息的 id
    reply_summary: str = ""   # 被回复消息的内容摘要（展示用）
    # 「主动上报」徽章：这条发言被判定为需要直接呈报给用户的重要结论
    proactive: bool = False
    # 本条发言里模型产出的思考字数（思考型模型先思考后答题；界面显示「其中思考 N 字」）
    reasoning_chars: int = 0
    # 这条消息属于哪个会话：群聊（"group"）还是某个成员的私聊（"dm:<成员id>"）。
    #
    # **默认值是向后兼容的关键**：旧的 history.json 里没有这个字段，
    # _load_history 用 `raw.get("channel") or "group"` 读出来 —— 旧消息自动算群聊，
    # 不丢、不崩。照 Grok Bot 的做法：每个 agent 有自己跟用户的 1:1 私聊。
    channel: str = "group"

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# 聊天记录持久化
# --------------------------------------------------------------------------- #
# 保留最近多少条。群聊一轮能出好几条，500 条够看很久，文件也就几百 KB。
HISTORY_MAX = 500



# 组长验收用的任务文本。
# 关键约束全在这三句里：**发言数不等于产出**、**真的完成就说完成**、
# **不要为了让谁说话而 @**。少了任何一句，组长都会变成"永远在挑刺"的角色。
_ACCEPT_TASK = """【验收】现在轮到你以组长的身份做验收 —— 检查用户这一轮的要求**是不是真的完成了**。

用户的原话：
{seed}

工作区里现在的文件：
{digest}

请严格按照下面三条走：

1. **逐条对照**用户的要求：哪些已经完成、哪些没有。依据只能是两种东西 ——
   工作区里的**实际文件**，或组员**确实给出了**用户要的答案。
   **"大家都发过言了"不算完成**，发言不等于产出。

2. **如果确实完成了**：给一段简短的交付说明（三行以内，说清产出了什么、在哪），
   **不要 @ 任何人**。

3. **如果没完成**：@ 具体的人，说清还差什么、要它做什么。
   不要为了让谁说话而 @，也不要重复已经做完的事。

另外：不要为了显得负责而故意挑刺。真的完成了，就说完成。\n\n**记得更新任务板**：验收完用 set_task 把每条任务的状态改成 done（或 blocked 并写明卡在哪）。板子会跨轮次保留，所以状态一定要写准 —— 下次接着干全靠它。
"""


def is_pass_content(text: str) -> bool:
    """识别「(pass)」—— 成员表示这轮没什么新东西。

    照 Grok Bot 的写法放宽一点：允许前后空白、允许大小写、
    允许带括号或不带、允许结尾多个句点。宁可多认几种，
    也不要让模型写了 pass 却被当成正文（那就白写了）。
    """
    t = (text or "").strip()
    if not t:
        return False
    t = t.strip("()（）").strip().rstrip("。.").strip()
    return t.lower() == "pass"

def _history_file():
    from .config import user_data_dir

    return user_data_dir() / "history.json"


def history_enabled() -> bool:
    """聊天记录持久化的总开关。

    自检 / 单测要设 ``AIGROUP_NO_HISTORY=1``：那些用例会连续创建几十个引擎，
    每个都从磁盘读回上一个存下的历史，「本轮几条发言」这类计数断言就会全部错乱。
    """
    import os

    return not os.environ.get("AIGROUP_NO_HISTORY")


@dataclass
class RunLimits:
    """单轮用户提问的资源上限，防止群聊无限循环烧额度。

    * ``max_messages``：派发链（组长 + 被 @ 的组员）**尝试次数**上限。
      2026-10-13 由 10 降到 6：一次「一句话回答」跑到 8 条发言太长了，
      每条都要重发一遍上下文，花费随条数线性上涨。6 条仍够
      「组长拆解 → 组员各自发言 → 互相反驳 → 组长收口」。
      （记录员纪要不占此配额；它现在默认关闭，想启用见 models.yaml 注释。）
    * ``max_depth``：@ 触发的最大层数（组长派发算第 1 层的入口）。
      **默认 3**：depth=2 时「组员 A 反驳组员 B」只够 1 个来回（B 回完 A 就被深度上限掐断）；
      3 层可以形成 2 个来回（A 说 → B 反驳 → A 再回应），而单轮总花费仍由
      ``max_messages``（尝试次数）与成员级 ``max_turns_per_run``（每成员最多 2 次成功发言）双重封顶，
      不会因此放开到失控。
    * ``max_context_chars``：喂给模型的 system + user 总字符数上限。
      2026-10-13 由 9000 降到 5000：每次调用都要重发 system + 聊天记录，
      这是单轮花费的主要来源（正文只有一两百字，输入却有一两千 token）。
      群聊是「一轮一议题」，5000 字足够带上本轮上下文。
    """
    # 组长验收轮数上限。0 = 关闭验收（退回老行为：派完就散会）。
    # 每多一轮 = 1 次组长调用 + 若干次组员调用，所以默认给得克制。
    max_accept_rounds: int = 2
    max_parallel: int = 3

    max_messages: int = 6
    max_depth: int = 3
    max_context_chars: int = 5000


# --------------------------------------------------------------------------- #
# @全体成员
# --------------------------------------------------------------------------- #
# 用户要求：@全体成员 / @所有人 要唤醒**全部**成员，**包含组长**。
# 别名做成一组，避免中英混用时漏掉某个写法。
ALL_MENTION_ALIASES = {
    "全体成员", "所有人", "全体", "全部", "大家",
    "all", "everyone", "everybody",
}


def is_all_mention(token: str) -> bool:
    return (token or "").strip().lower() in ALL_MENTION_ALIASES


# --------------------------------------------------------------------------- #
# 附件处理
# --------------------------------------------------------------------------- #
# 文本文件注入上限：一次贴进来 10 万字会把上下文预算撑爆，
# 而用户看到的是「AI 读到了我的文件」，所以宁可截断并如实标注。
ATTACH_TEXT_LIMIT = 4000


def _merge_attachments(text: str, attachments: list[dict]) -> tuple[str, list[dict]]:
    """把附件并进消息正文，返回 ``(新正文, 给界面渲染的附件列表)``。

    * 文本文件：内容内联进正文（超长截断并注明），模型因此真的能读到；
    * 图片：只在正文里留一行标注（**图片本身不发给模型** —— 群里并非每个成员都支持视觉，
      强行塞 image_url 会让不支持的那个成员整个调用失败）。
    """
    if not attachments:
        return text, []

    blocks: list[str] = []
    view: list[dict] = []
    for item in attachments:
        kind = str(item.get("kind") or "").lower()
        name = str(item.get("name") or "未命名")
        mime = str(item.get("mime") or "")
        if kind == "file":
            body = str(item.get("text") or "")
            note = ""
            # 前端读不出文字的二进制 / Office 文件：这里用标准库解析成纯文本。
            # 成员是纯 LLM，看不到文件本身，只能读这段文字 ——
            # 解析不出来就等于"没传"，必须如实说明原因，别让模型以为文件是空的。
            if not body and item.get("data"):
                import base64

                from .documents import extract_text

                try:
                    raw = base64.b64decode(str(item["data"]), validate=False)
                    body, note = extract_text(name, mime, raw)
                except Exception as exc:  # noqa: BLE001
                    body, note = "", f"解析失败：{type(exc).__name__}: {exc}"

            truncated = len(body) > ATTACH_TEXT_LIMIT
            if truncated:
                body = body[:ATTACH_TEXT_LIMIT]
            if body:
                head = f"【用户上传的文件：{name}】"
                if note:
                    head += f"（{note}）"
                blocks.append(
                    f"{head}\n```\n{body}\n```"
                    + (f"\n（内容过长，以上仅前 {ATTACH_TEXT_LIMIT} 字）" if truncated else "")
                )
            else:
                why = note or "内容为空"
                blocks.append(
                    f"【用户上传的文件：{name}】\n"
                    f"⚠️ 这个文件没能转成文字：{why}\n"
                    "请如实告诉用户你读不到它，并建议把内容复制成文本，或换一个能读的格式。"
                )
            view.append({
                "kind": "file", "name": name, "mime": mime,
                "size": int(item.get("size") or 0), "truncated": truncated,
                "note": note,
            })
        elif kind == "image":
            blocks.append(f"【用户发了一张图片：{name}】")
            view.append({
                "kind": "image", "name": name, "mime": mime,
                # dataUrl 只给界面显示缩略图用，不进模型请求
                "dataUrl": str(item.get("dataUrl") or ""),
            })
    if not blocks:
        return text, view
    head = text.strip() or "（我发了附件，请看看）"
    return head + "\n\n" + "\n\n".join(blocks), view


# --------------------------------------------------------------------------- #
# 群聊引擎
# --------------------------------------------------------------------------- #
class GroupEngine:
    def __init__(
        self,
        cfg: GroupConfig,
        client: LLMClient,
        sink: EventSink,
        limits: RunLimits | None = None,
    ) -> None:
        self.cfg = cfg
        self.client = client
        self.sink = sink
        self.limits = limits or RunLimits()

        # 先把上次的记录读回来，再让界面来取 —— 大退后还能接着看
        # 数据目录位置变过（老版本放 server/data）——先搬一次，别让历史"看起来丢了"
        try:
            from .config import migrate_legacy_data

            moved = migrate_legacy_data()
            if moved:
                print(f"[data] 已从旧位置搬来：{'、'.join(moved)}", flush=True)
        except Exception as exc:  # noqa: BLE001 —— 搬家失败不该拦住启动
            print(f"[data] 旧数据搬迁失败（不影响启动）：{exc}", flush=True)
        self.messages: list[Message] = self._load_history()
        self.agent_state: dict[str, str] = {}  # agent_id -> idle | thinking | writing | done | error
        self._lock = asyncio.Lock()
        self._running = False
        self.current_run_id = ""
        # 单轮预算：max_messages 只约束「派发链」上的成员发言（组长 + 被 @ 的组员）。
        # 记录员纪要不占这个配额，但一轮最多调用一次（_run_recorder_calls），
        # 因此单轮模型调用总数上界是 max_messages + 1。
        # _run_calls = 尝试次数（含失败，用于烧钱上界）
        # _run_spoke = 成功发言次数（用于成员级额度与统计，失败不算“发过言”）
        self._run_calls = 0
        self._run_writes = 0   # 本轮写进工作区的文件数（验收的前置条件）
        self._stop_requested = False   # 用户点了「停止」
        self._level_task = None        # 当前这一层的 gather，用于立刻取消在途请求
        self._run_spoke: dict[str, int] = {}
        self._run_recorder_calls = 0
        self._run_routes: dict[str, dict] = {}
        self._run_failures: list[dict] = []
        # 本轮每个成员累计的思考字数（agent_reasoning 事件里的 chars 用这个值）
        self._run_reasoning_chars: dict[str, int] = {}

    # ------------------------------------------------------------------ #
    # 成员
    # ------------------------------------------------------------------ #
    @property
    def active_agents(self) -> list[AgentSpec]:
        """可被派发的群成员（记录员不在其中：它只做最后汇总）。"""
        return self.cfg.active_agents

    def agent_by_id(self, agent_id: str) -> AgentSpec | None:
        return self.cfg.by_id(agent_id)

    # ------------------------------------------------------------------ #
    # @提及 解析
    # ------------------------------------------------------------------ #
    MIN_SUBSTRING_LEN = 4

    def resolve_mention_detail(self, token: str) -> tuple[AgentSpec | None, str]:
        """把 @ 里的字符串解析成成员，返回 ``(成员, 原因说明)``。

        匹配顺序：精确 id → 精确显示名 → 精确标签（要求唯一）→ 前缀子串（要求够长且唯一）。
        **解析不出来或存在歧义时返回 (None, 原因)**，由调用方发 notice——绝不静默选一个，
        因为「一个字母就悄悄把活派给某个人」既花钱又会让界面说谎。
        """
        raw = (token or "").strip()
        norm = raw.lower().rstrip("·・•")
        if not norm:
            return None, "空的 @ 目标"
        agents = self.active_agents

        for agent in agents:
            if norm == agent.id.lower():
                return agent, ""
        for agent in agents:
            if norm == agent.display_name.lower():
                return agent, ""

        tag_hits = [a for a in agents if any(norm == tag.lower() for tag in a.tags)]
        if len(tag_hits) == 1:
            return tag_hits[0], ""
        if len(tag_hits) > 1:
            return None, (
                f"标签「{raw}」同时属于 {'、'.join(a.id for a in tag_hits)}，"
                "有歧义，请直接 @ 成员 id"
            )

        if len(norm) < self.MIN_SUBSTRING_LEN:
            return None, (
                f"「{raw}」太短（不足 {self.MIN_SUBSTRING_LEN} 字符），"
                "为避免误命中已忽略；请写完整成员 id"
            )

        def candidates(key) -> list[AgentSpec]:
            hits = []
            for agent in agents:
                name = key(agent).lower()
                if norm in name and len(norm) * 2 > len(name):
                    hits.append(agent)
            return hits

        name_hits = candidates(lambda a: a.display_name)
        if len(name_hits) == 1:
            return name_hits[0], ""
        if len(name_hits) > 1:
            return None, (
                f"「{raw}」同时匹配 {'、'.join(a.id for a in name_hits)}，有歧义，请写完整 id"
            )

        id_hits = candidates(lambda a: a.id)
        if len(id_hits) == 1:
            return id_hits[0], ""
        if len(id_hits) > 1:
            return None, (
                f"「{raw}」同时匹配 {'、'.join(a.id for a in id_hits)}，有歧义，请写完整 id"
            )
        return None, f"群里没有成员「{raw}」"

    def resolve_mention(self, token: str) -> AgentSpec | None:
        """``resolve_mention_detail`` 的简化版（只要成员）。"""
        return self.resolve_mention_detail(token)[0]

    # ------------------------------------------------------------------ #
    # 广播
    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # 聊天记录持久化
    # ------------------------------------------------------------------ #
    def _load_history(self) -> list["Message"]:
        """启动时把聊天记录读回来。文件坏了就当没有，绝不让它拦住启动。"""
        if not history_enabled():
            return []
        path = _history_file()
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"[history] 读取失败，按空记录启动：{exc}")
            return []
        out: list[Message] = []
        for raw in (payload.get("messages") or [])[-HISTORY_MAX:]:
            try:
                out.append(Message(
                    id=str(raw.get("id") or uuid.uuid4().hex[:12]),
                    sender=str(raw.get("sender") or ""),
                    sender_name=str(raw.get("sender_name") or ""),
                    sender_avatar=str(raw.get("sender_avatar") or ""),
                    role=str(raw.get("role") or "member"),
                    content=str(raw.get("content") or ""),
                    attachments=raw.get("attachments") or [],
                    mentions=raw.get("mentions") or [],
                    route=str(raw.get("route") or ""),
                    requested_route=str(raw.get("requested_route") or ""),
                    warning=str(raw.get("warning") or ""),
                    error=str(raw.get("error") or ""),
                    created_at=float(raw.get("created_at") or time.time()),
                    run_id=str(raw.get("run_id") or ""),
                    turn=int(raw.get("turn") or 0),
                    tokens=int(raw.get("tokens") or 0),
                    prompt_tokens=int(raw.get("prompt_tokens") or 0),
                    completion_tokens=int(raw.get("completion_tokens") or 0),
                    reply_to=str(raw.get("reply_to") or ""),
                    reply_summary=str(raw.get("reply_summary") or ""),
                    proactive=bool(raw.get("proactive")),
                    reasoning_chars=int(raw.get("reasoning_chars") or 0),
                    # 旧记录没有这个字段 → `or "group"` 让它自动算群聊（向后兼容）
                    channel=str(raw.get("channel") or "group"),
                ))
            except Exception:  # noqa: BLE001 —— 单条坏掉不该拖垮整份记录
                continue
        if out:
            print(f"[history] 载入 {len(out)} 条历史消息 ← {path}")
        return out

    def save_history(self) -> None:
        """把聊天记录原子写盘（临时文件 + 替换，避免写一半崩掉留下坏文件）。"""
        if not history_enabled():
            return
        path = _history_file()
        payload = {
            "version": 1,
            "savedAt": time.time(),
            "messages": [m.to_dict() for m in self.messages[-HISTORY_MAX:]],
        }
        try:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=None),
                           encoding="utf-8")
            tmp.replace(path)
        except Exception as exc:  # noqa: BLE001 —— 写不进去也不能影响正常聊天
            print(f"[history] 写盘失败：{exc}")

    async def emit(self, kind: str, **payload) -> None:
        await self.sink({"kind": kind, "ts": time.time(), **payload})

    async def emit_state(self) -> None:
        await self.emit(
            "room_state",
            members=self.members_view(),
            limits=self.limits_view(),
            running=self._running,
        )

    # 成员实时状态的中文文案（给界面直接用）。
    # 取值枚举：待命 / 正在思考… / 正在撰写回复… / 已完成发言 / 调用失败。
    STATUS_TEXT = {
        "idle": "待命",
        "thinking": "正在思考…",
        "writing": "正在撰写回复…",
        "done": "已完成发言",
        "error": "调用失败",
    }

    @classmethod
    def status_text(cls, state: str) -> str:
        """把机器状态翻译成一句中文；未知状态一律按「待命」处理，绝不显示英文原文。"""
        return cls.STATUS_TEXT.get(state, cls.STATUS_TEXT["idle"])

    async def _set_state(self, agent_id: str, state: str) -> None:
        self.agent_state[agent_id] = state
        # state 是机器状态（保留给旧逻辑），status 是给界面直接显示的一行中文；
        # 两者一起推给 WebSocket，前端不用自己翻译。
        await self.emit(
            "agent_status",
            agent_id=agent_id,
            state=state,
            status=self.status_text(state),
        )

    async def _notice(self, message: str, level: str = "info") -> None:
        await self.emit("notice", message=message, level=level)

    # ------------------------------------------------------------------ #
    # 上下文拼装
    # ------------------------------------------------------------------ #
    def _role_brief(self, agent: AgentSpec) -> str:
        """从角色提示词里摘一句给组长看的「这人擅长什么」。

        取第一段非标题、非引用、非列表、非空白的正文，压成一行、截断到 24 字。
        摘不出来就返回空串（名单会退化成只写「组员」）。

        存在的意义：界面上已经不强制填「职责」了（用户直接写提示词），
        但组长派活时仍然要知道"谁能干什么"，所以从提示词里自动补一句。
        """
        prompt = getattr(agent, "system_prompt", "") or ""
        for raw in prompt.splitlines():
            line = raw.strip()
            if not line or line.startswith(("#", ">", "-", "*", "+", "|", "```")):
                continue
            line = re.sub(r"[\s\u3000]+", " ", line)
            return line[:24] + ("…" if len(line) > 24 else "")
        return ""

    def _member_roster(self) -> str:
        """当前成员名单 —— **每轮实时生成**，所以刚加/刚删的人立刻反映在这里。

        组长是照着这份名单挑人的，因此除了 id 与身份，还要带上 `duty`（职责说明），
        让它能按「谁能干什么」挑人，而不是凭记忆去 @ 某个固定名字。
        """
        lines = []
        for agent in self.active_agents:
            # 职责优先用配置里的；没填就从角色提示词里摘一句 ——
            # 界面上已经不强制填「职责」了（用户直接写提示词），
            # 但组长派活时仍然需要知道"谁能干什么"，所以这里自动补一个。
            brief = agent.duty or self._role_brief(agent)
            if agent.role == "captain":
                duty = f"组长（{brief}）" if brief else "组长（负责拆解与派发）"
            else:
                duty = f"组员（{brief}）" if brief else "组员"
            tags = ",".join(agent.tags) or "无"
            lines.append(f"- @{agent.id}：{agent.display_name}，{duty}，标签 {tags}")
        return "\n".join(lines)

    def _render(self, message: Message) -> str:
        if message.role == "recorder":
            return f"[记录员纪要] {message.content}"
        return f"[{message.sender_name}] {message.content}"

    def _transcript(self, budget_chars: int) -> tuple[str, int, bool]:
        """把群聊记录压成文本，返回 ``(文本, 丢弃条数, 是否已截断钉住的首条)``。

        三条硬规则：
        1. **本轮用户的原话永远优先保留**——记录从旧到新截断，连任务/约束都丢了的话，
           后续整轮讨论都会跑偏；
        2. **钉住的首条也受预算约束**：它自己超过预算时按预算截断（并如实上报），
           不允许「约束无限膨胀」绕过 max_context_chars；
        3. 优先用本轮消息：预算只够一部分时，先保本轮发言，更早轮次最后才带回
           （窗口不跨轮无限共享）；被丢弃的条数与原因都会作为 ``context_trimmed`` 事件广播。
        """
        all_lines = [
            (m.run_id, m.id, self._render(m))
            for m in self.messages
            if m.role != "system"
        ]
        if not all_lines:
            return "（暂无历史发言）", 0, False

        current = [item for item in all_lines if item[0] == self.current_run_id]
        earlier = [item for item in all_lines if item[0] != self.current_run_id]
        head_pool = current or all_lines
        head = head_pool[0]
        # 注意顺序：下面要 `reversed(rest)`，所以本轮的其余消息必须**排在更早轮次之后**，
        # 反转后才会「本轮优先占用预算」；写成 [head_pool[1:]] + earlier 会让反转先走到
        # earlier，等于把预算先给上一轮的旧发言（本轮反而被裁掉）。
        rest = earlier + [item for item in head_pool[1:]]

        pinned_text = head[2]
        truncated = False
        if len(pinned_text) > budget_chars:
            pinned_text = pinned_text[: max(0, budget_chars)]
            truncated = True

        picked: list[tuple[str, str]] = [(head[1], pinned_text)]
        used = len(pinned_text)
        dropped = 0
        for _run, mid, text in reversed(rest):
            if used + len(text) > budget_chars:
                dropped += 1
                continue
            picked.append((mid, text))
            used += len(text)

        order = {mid: idx for idx, (_r, mid, _t) in enumerate(all_lines)}
        picked.sort(key=lambda item: order.get(item[0], 0))
        return "\n\n".join(text for _mid, text in picked), dropped, truncated

    # ------------------------------------------------------------------ #
    # 引用回复与主动上报（视频里那款应用的两个交互特征）
    # ------------------------------------------------------------------ #
    def _resolve_reply_target(self, sender_id: str, task: str) -> tuple[str, str]:
        """解析这条发言是在「回复哪条消息」。

        判定规则（按优先级）：
        1. 用户的派发文本里显式写了「回复 @某消息」→ 暂不支持（消息没有短 id 可写）；
        2. 发言者是**被派发的成员**（task 里带「@xxx 被组长点了名」之类）→
           引用**派发给它的那条组长发言**（即最近一条 role=captain 的消息）；
        3. 其余情况 → 空（不显示引用条）。

        返回 (被回复消息 id, 被回复内容摘要)。
        """
        recent = [m for m in self.messages if m.run_id == self.current_run_id]
        # 被点名派发的成员 → 引用组长最近的拆解发言
        if "被组长点了名" in task or "请就上面的拆解" in task:
            for m in reversed(recent):
                if m.role == "captain" and m.content:
                    return m.id, m.content[:80]
        # 组长的收敛发言 → 引用用户最初的问题
        if sender_id and any(m.role == "captain" for m in recent if m.sender == sender_id):
            for m in recent:
                if m.role == "user" and m.content:
                    return m.id, m.content[:80]
        return "", ""

    # 触发词：出现这些关键字的发言视为「主动上报」（需要直接呈报给用户的重要结论）
    _PROACTIVE_PATTERNS = (
        "一针见血", "主动上报", "必须修正", "验收不通过", "风险提示",
        "严重问题", "立刻停止", "重要结论", "缺一即拒收", "报错", "阻塞",
    )

    def _is_proactive(self, content: str) -> bool:
        """是否标记为「主动上报」：命中任一触发词即视为重要结论。"""
        if not content:
            return False
        head = content[:400]
        return any(p in head for p in self._PROACTIVE_PATTERNS)

    async def _build_messages(
        self, agent: AgentSpec, task: str, extra: str = ""
    ) -> list[dict[str, str]]:
        system = agent.system_prompt
        system += (
            "\n\n---\n## 本群当前成员名单\n"
            f"{self._member_roster()}\n\n"
            "## 输出纪律\n"
            "- 直接给内容，不要复述规则、不要输出思考过程、不要用 markdown 代码块包整段发言。\n"
            "- 需要别人接手时才写 @成员id，且只能 @ 上面名单里存在的 id。\n"
            "- 你的发言会直接显示在群聊界面里，因此要像在群里说话一样自然。\n"
            # 2026-10-14 用户要求：思考过程也必须用中文。
            # 有些模型（实测智谱 GLM-5.3-Flash）默认用英文做内心独白 ——
            # 中文提问、中文回答，但推理链全是英文。而用户会打开「模型思考」那一页看，
            # 所以这里明确要求思考也用中文。
            "- **你的内部思考（推理过程）也必须用中文。** 不要用英文做内心独白 ——\n"
            "  用户会直接看到你的思考过程，中英混排既难读、也显得没在认真处理中文语境。\n"
            # 2026-10-14 用户明确要求：用户权限在组长之上。必须写进每一份提示词，
            # 否则组长会把自己的「裁决权」误当成群里的最高权威，去跟用户讨价还价。
            "\n## 群里的权限顺序（最高优先级）\n"
            "**用户 > 组长 > 组员。** 用户的话是最高指令，任何成员（包括组长）都必须服从；\n"
            "组长的「最终裁决权」只在成员之间生效 —— 它可以拍板组员的分歧，但**管不到用户**。"
        )
        if extra:
            system += f"\n\n## 本轮补充要求\n{extra}\n"
        # 「@ 的分寸」。**必须写在输出纪律里** ——
        # 群里每个人都能 @ 任何人（can_dispatch=True），没有这条约束，
        # 一个成员 @ 五个、五个再各 @ 五个，一轮就炸了。
        # 这段是照 Grok Bot 的做法写的（它在 agent-messaging.ts 里花了一整段讲这个）。
        system += (
            "\n## @ 别人的分寸（重要）\n"
            "群里的每个人都能直接 @ 别人、把对方叫起来。所以这条要自觉：\n"
            "- **@ 是真实的副作用** —— 它会真的唤醒一个人、花掉一次模型调用。"
            "像「替用户发消息」那样谨慎，不要顺手 @。\n"
            "- **一次最多 @ 1~2 个人**。要叫三个人以上，说明这事该由组长拆解，"
            "而不是你群发。\n"
            "- **不要为了显得周全而 @** —— 你没有新东西要问、要反驳、要补充，就不要 @。\n"
            "- **不要互相客套**：别人说了句话你就 @ 回去说「收到」「同意」，"
            "这种来回会没完没了。有实质内容才开口。\n"
            "- 用户明确说了「去问某某」「让大家都知道」时，那是授权，照做。\n"
            "\n"
            "## 没什么可说的时候\n"
            "如果你这一轮**确实没有新东西**要补充（别人已经把该说的说了、"
            "或者你只是被 @ 过来看热闹），就直接回复 `(pass)`。\n"
            "内核会认这个标记：它会**替你跳过这一轮发言**，不记你的账、也不占发言次数。\n"
            "**不要为了发言而发言。** 硬凑一句「我同意」比什么都不说更糟 —— "
            "它会让整轮讨论变成互相点头。\n"
        )


        # 工作区产出约定 + 当前清单。
        # 清单要给，否则成员不知道自己产出的东西别人看不看得到、也不知道已有啥。
        from .workspace import WRITE_PROTOCOL, digest as workspace_digest

        system += "\n\n" + WRITE_PROTOCOL
        try:
            listing = workspace_digest()
        except Exception:  # noqa: BLE001 —— 工作区读不了不该拦住整轮对话
            listing = "（读取失败）"
        system += f"\n## 当前工作区文件\n{listing}\n"

        # 任务板：让"做到哪了"跨轮次、跨重启留下来。
        # **未完成的排前面** —— 那才是要继续干的。
        from .taskboard import digest as task_digest

        try:
            board = task_digest()
        except Exception:  # noqa: BLE001
            board = "（读取失败）"
        system += f"\n## 任务板（跨轮次保留）\n{board}\n"

        # max_context_chars 是「喂给模型的 system + user 总字符数」上限：
        # system 提示词（角色 + 名单 + 纪律）同样占预算，不能只约束聊天记录。
        fixed = len(system) + len(task) + 64
        transcript, dropped, truncated = self._transcript(
            max(400, self.limits.max_context_chars - fixed)
        )
        if dropped or truncated:
            await self.emit(
                "context_trimmed",
                agent=agent.id,
                dropped=dropped,
                truncated_pinned=truncated,
                kept_chars=len(transcript),
                budget=self.limits.max_context_chars,
                message=(
                    f"上下文超限：{agent.display_name} 这一轮丢掉了 {dropped} 条较早的中间发言"
                    + ("，并且本轮用户原话本身过长、已按预算截断" if truncated else "")
                    + "；本轮用户原话与最近发言保留。"
                ),
            )

        # 任务里提到的文件，把内容附上 —— 模型看不到文件系统，
        # 组长说「参考 frontend/App.vue」而不塞内容，等于没说。
        attachment = ""
        try:
            from .workspace import bundle_for

            attachment = bundle_for(task)
        except Exception:  # noqa: BLE001
            attachment = ""
        user = (
            f"## 群聊记录（按时间先后）\n{transcript}\n\n"
            f"【派发给你的任务】\n{task}"
            + (f"\n\n## 任务里提到的文件内容\n{attachment}" if attachment else "")
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        if len(system) + len(user) > self.limits.max_context_chars:
            await self._notice(
                f"提示：{agent.display_name} 本轮输入 {len(system) + len(user)} 字符，"
                f"超过 max_context_chars={self.limits.max_context_chars}（角色提示词本身较长）。"
            )
        return messages

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    async def handle_user_message(
        self,
        text: str,
        sender: str = "用户",
        mock_hint: str = "",
        attachments: list[dict] | None = None,
        channel: str = "group",
    ) -> None:
        """用户发言入口。同一时刻只允许一轮讨论在跑。

        ``mock_hint`` 只影响离线 mock 通道，用于演示两个边界（对真实模型无任何作用）：
        ``quiet`` 让 mock 不 @ 任何人，``ghost`` 让 mock @ 一个不存在的成员。
        """
        # 只有附件、没有文字时也要能发出去（例如只发一张图）
        if not text.strip() and not attachments:
            return
        if self._lock.locked():
            await self.emit("error", message="上一轮讨论还没结束，请稍候再发。")
            return

        async with self._lock:
            self.client.mock_hint = mock_hint or ""
            try:
                await self._run(text.strip(), sender, attachments or [], channel)
            finally:
                self.client.mock_hint = ""

    async def _run(
        self, text: str, sender: str, attachments: list[dict] | None = None,
        channel: str = "group",
    ) -> None:
        self._running = True
        self._run_calls = 0
        # 每轮开始**必须**清掉停止标志。
        # 不清的话：点过一次「停止」→ _stop_requested 永远是 True →
        # 之后每一轮刚开场就自己 abort（界面上就是「已按你的要求停止本轮讨论」
        # +「没有任何成员被唤醒」），而且只能重启 app 才好。
        self._stop_requested = False
        self._level_task = None
        self._run_spoke = {}
        self._run_recorder_calls = 0
        self._run_routes = {}
        self._run_failures = []
        self._run_reasoning_chars = {}
        self.current_run_id = uuid.uuid4().hex[:8]
        run_id = self.current_run_id
        await self.emit("run_start", run_id=run_id)

        attachments = attachments or []
        # 文本文件的内容并进正文 —— 这样模型真的读得到（图片只留标注，见模块顶部说明）
        content, attach_view = _merge_attachments(text, attachments)
        user_msg = Message(
            id=uuid.uuid4().hex[:12],
            sender="user",
            sender_name=sender,
            sender_avatar="🧑",
            role="user",
            content=content,
            attachments=attach_view,
            run_id=run_id,
            channel=channel,
        )
        self.messages.append(user_msg)
        self.save_history()
        await self.emit("message", message=user_msg.to_dict())
        await self.emit_state()

        aborted = False
        try:
            if channel.startswith("dm:"):
                # 私聊：只叫这一个成员。不走组长、不派发、不验收 ——
                # 免得一轮私聊又拉起整个群。
                await self._dm_turn(channel, content, run_id)
            else:
                await self._dispatch_loop(run_id, seed_task=content)
                await self.emit_route_summary()
                await self._summarize(run_id)
            if sum(self._run_spoke.values()) == 0:
                # 没有任何成员成功发言：本轮实际为零产出，必须给出可判定的失败信号，
                # 而不是让上层只看到一条不起眼的 notice。
                aborted = True
                detail = "；".join(
                    f"{f['name']}@{f['route']}：{f['error'][:80]}" for f in self._run_failures
                ) or "没有任何成员被唤醒"
                await self.emit(
                    "error",
                    message=f"本轮讨论未能产出任何发言（aborted）：{detail}",
                    aborted=True,
                )
        except LLMError as exc:
            aborted = True
            await self.emit("error", message=f"本轮讨论中断：{exc}", aborted=True)
        except Exception as exc:  # 兜住任何意外，保证服务端不崩
            aborted = True
            await self.emit("error", message=f"内部错误：{type(exc).__name__}: {exc}", aborted=True)
        finally:
            self._running = False
            # 失败/思考中的状态跨轮必须复位（上一轮 error 不该污染下一轮）
            for agent in [*self.active_agents, self.cfg.recorder]:
                if self.agent_state.get(agent.id, "idle") != "idle":
                    await self._set_state(agent.id, "idle")
            await self.emit("run_end", run_id=run_id, aborted=aborted)
            await self.emit_state()

    # ------------------------------------------------------------------ #
    # 派发循环
    # ------------------------------------------------------------------ #
    # 私聊（1:1）
    # ------------------------------------------------------------------ #
    async def _dm_turn(self, channel: str, content: str, run_id: str) -> None:
        """一个成员的 1:1 私聊回合。

        照 Grok Bot：
          · "no one else is here" —— 只有你和它
          · 但它**在群里的发言**也进它的上下文（方案 A），带 [群聊: "..."] 标记
          · "Never reveal private one-on-one context" —— 私聊内容不得外泄到群里

        不走组长、不派发、不验收 —— 那是群聊的事。
        """
        agent_id = channel[3:].strip()
        agent = self.agent_by_id(agent_id)
        if agent is None:
            await self.emit("error", message=f"找不到成员：{agent_id}", aborted=True)
            return

        # 私聊历史：只取这个成员的（含刚存进去的用户这句）
        dm_hist = [
            m for m in self.messages
            if m.channel == channel and m.id != self.messages[-1].id
        ][-24:]

        # 它在群里的发言（方案 A：让它知道自己刚在群里说过什么）
        group_lines = []
        for m in self.messages:
            if m.channel != "group" or m.sender != agent.id:
                continue
            body = (m.content or "").strip()
            if body:
                group_lines.append(body)
        group_lines = group_lines[-8:]

        parts = [
            f"你正在和用户 **1:1 私聊**（不是群聊）。这里只有你和用户，"
            f"其他成员看不到这段对话。",
        ]
        if dm_hist:
            lines_ = []
            for m in dm_hist:
                who = "用户" if m.role == "user" else "你"
                lines_.append(f"{who}：{m.content}")
            parts.append("## 你们的私聊记录\n" + "\n".join(lines_))
        if group_lines:
            parts.append(
                "## 你之前在群里说过的话（供你参考，别在这里重复整段）\n"
                + "\n".join(f"[群聊] {t}" for t in group_lines)
            )
        parts.append("## 注意\n- 直接回复用户，用正常对话，**不要 @ 任何人**。\n"
                     "- 私聊内容**不要外泄到群里**（用户在私聊里说的话是私密的）。\n"
                     "- 需要产文件就照常用工具。")

        prompt = "\n\n".join(parts)
        await self._speak_counted(agent, prompt, run_id)

    # ------------------------------------------------------------------ #
    async def _user_direct_mentions(
        self, user_text: str, captain: AgentSpec
    ) -> tuple[list[tuple[str, str]] | None, bool]:
        """用户在消息里直接点名了谁，返回 ``(被点名的人, 是否 @了全体)``。

        * ``(None, False)``：**不走直呼路径**（谁都没 @、点到了组长、或只点到不存在的成员），
          此时仍由组长入场拆解派发；
        * ``([(token, "用户"), …], False)``：这些被点名的组员直接作为本轮**第 1 层入口**
          唤醒，组长的入场发言被完全跳过；
        * ``(..., True)``：**@全体成员** —— 全部成员都要说话，而且**包含组长**
          （用户明确要求「所有人都包含，包含组长」）。

        @ 的语义一律复用 ``extract_mentions`` + ``resolve_mention_detail``，不另写一套解析。
        点不到人的 @ 照旧发 notice，不静默跳过。
        """
        tokens = extract_mentions(user_text)
        if not tokens:
            return None, False
        # @全体成员：不做逐个解析，直接返回全部成员（组长交给调用方安排入场）
        if any(is_all_mention(t) for t in tokens):
            return [(a.id, "用户") for a in self.active_agents], True
        picked: list[tuple[str, str]] = []
        seen: set[str] = set()
        for token in tokens:
            agent, reason = self.resolve_mention_detail(token)
            if agent is None:
                await self._notice(f"@「{token}」没有被派发：{reason}", level="warn")
                continue
            if agent.id == captain.id or agent.role == "captain":
                # (a) 用户点了组长：按老路走，由组长入场拆解
                return None, False
            if agent.id in seen:
                continue
            seen.add(agent.id)
            picked.append((token, "用户"))
        return (picked or None), False

    async def _dispatch_loop(self, run_id: str, seed_task: str) -> None:
        captain = next((a for a in self.active_agents if a.role == "captain"), None)
        if captain is None:
            await self.emit("error", message="群配置里没有 captain 角色的成员，无法派发任务。")
            return

        depth = 0

        # 用户直接点名了组员（且没点组长）→ 这些组员就是本轮第 1 层入口，组长不入场。
        # 用户说的「我 @ 谁谁就说话，组长别老出来」就是这条路径。
        direct, all_mention = await self._user_direct_mentions(seed_task, captain)
        if direct is not None and all_mention:
            # @全体成员：组长也要说话（用户明确要求）。
            # 先让组长作为组长处理一次（它可能派活），再把**全部组员**排进第 1 层，
            # 这样「全体」是真的全体，而不是只看组长点了谁。
            await self._notice("收到 @全体成员：本轮所有成员都会被唤醒（含组长）。")
            task = (
                "用户 @ 了全体成员。**所有人已经被用户叫起来了，派活这件事用户已经做完了。**\n"
                "你这一轮**只需要说自己的观点/结论，不要 @ 任何成员** ——\n"
                "再 @ 就是把你上司已经下过的命令又下了一遍，还让同事被重复唤醒。\n"
                "请直接给出你对这个问题的看法。"
            )
            mentions, _ok = await self._speak_counted(captain, task, run_id)
            # 兜底：即使用户提示词没压住、组长还是 @ 了人，这里也一律忽略。
            # 用户的命令权最高，组长不该在「用户已经叫了全体」之后重复派发。
            if mentions:
                await self._notice(
                    "（用户 @全体成员 时派活权归用户，组长这一轮的 @ 已按规则忽略）"
                )
            named = [
                (a.id, "用户")
                for a in self.active_agents
                if a.id != captain.id
            ]
            entry_direct = True
        elif direct is not None:
            named = direct
            entry_direct = True
        else:
            # 其余情况仍由组长入场：谁都没 @ / 点到了组长 / 只点到不存在的成员
            task = (
                "用户刚刚在群里说了上面这句话。请你作为组长处理："
                "先给结论，如果需要其他人补充，就按规则 @ 对应成员派活；不需要就直接回答完。"
            )
            mentions, _ok = await self._speak_counted(captain, task, run_id)
            # 待唤醒队列带上「是谁点的名」：组员之间的互相点名要用「回应他人」的措辞派发，
            # 否则被唤醒的组员会以为还是组长在派活，只顾各说各话，形不成来回对话。
            named = [(token, captain.display_name) for token in mentions]
            entry_direct = False

        while (named and depth < self.limits.max_depth
               and self._budget_left() and not self._stop_requested):
            queue: list[tuple[AgentSpec, str]] = []
            queued: set[str] = set()
            for token, source in named:
                agent, reason = self.resolve_mention_detail(token)
                if agent is None:
                    await self._notice(f"@「{token}」没有被派发：{reason}", level="warn")
                    continue
                if agent.id == captain.id:
                    # 组长不会因为被 @ 而再发言：它的第二棒由内核在「收敛」阶段统一安排。
                    # 这条丢弃同样要有 notice——静默丢弃会让界面上的高亮 @ 变成谎话。
                    await self._notice(
                        f"@「{token}」指向组长（{captain.display_name}），不会触发新发言"
                        "（组长由内核在收敛阶段统一唤醒），已忽略。"
                    )
                    continue
                if agent.id in queued:
                    # 同一个成员被用不同写法 @ 了多次（@kimi / @Kimi·论证担当 / @论证）
                    # 只唤醒一次，否则会突破该成员的 max_turns_per_run。
                    await self._notice(
                        f"{agent.display_name} 被重复 @（「{token}」），已合并为一次派发。"
                    )
                    continue
                if self._run_spoke.get(agent.id, 0) >= agent.max_turns_per_run:
                    await self._notice(
                        f"{agent.display_name} 本轮已成功发言 "
                        f"{self._run_spoke[agent.id]} 次，达上限（{agent.max_turns_per_run} 次），跳过。"
                    )
                    continue
                queued.add(agent.id)
                if source == "用户":
                    # 用户直接点名：直接回答用户，不用等组长转述（这是「我 @ 谁谁说话」）。
                    # 注意这里**不写**「@成员id」：任务文本会被模型原样引用回来，
                    # 带上自己的 id 会让它误以为需要再 @ 一次、白白多跑一轮。
                    queue.append((
                        agent,
                        "用户直接点了你的名，请直接回答用户这一次的提问，给出你自己的结论。"
                        "不需要复述任务、也不需要把问题转交给组长。",
                    ))
                elif source and source != captain.display_name:
                    # 是别的组员 @ 的它：明确要求「直接回应那个人」，并在开头点名，
                    # 这是让群员之间产生真实来回对话的关键一棒。
                    queue.append((
                        agent,
                        f"@{agent.id}，{source} 在上一条发言里 @ 了你，观点可能与你不同。"
                        f"请直接回应它：同意就补充它没讲到的那一层，不同意就明确反驳并给出理由。"
                        f"发言开头先写「@{source}」，再说清你回应的是它哪一句话，"
                        "不要只是向组长汇报。",
                    ))
                else:
                    queue.append((agent, f"@{agent.id} 被组长点了名，请就上面的拆解给出你这一部分的产出。"))

            if not queue:
                break

            depth += 1
            # (token, 该发言者是否有派发权, 点名者显示名)
            pending: list[tuple[str, bool, str]] = []
            # ---------------- 同一层：并发派发 ----------------
            # 预算**共享**，所以先按剩余额度切片 —— 不切的话并发会突破 max_messages。
            remaining = max(0, self.limits.max_messages - self._run_calls)
            runnable = queue[:remaining]
            for agent, _t in queue[remaining:]:
                await self._notice(
                    f"本轮达到单轮发言上限（max_messages={self.limits.max_messages}），"
                    f"{agent.display_name} 未被唤醒。"
                )

            results = await self._run_level(runnable, run_id)

            for (agent, task_text), res in zip(runnable, results):
                if isinstance(res, BaseException):
                    # 一个成员炸了不该让整层静默失败 —— 如实报告，其余照常
                    await self._notice(
                        f"{agent.display_name} 发言时出现异常："
                        f"{type(res).__name__}: {res}",
                        level="error",
                    )
                    got, _ok = [], False
                else:
                    got, _ok = res
                if got and not agent.can_dispatch:
                    # 「沉默地丢掉 @」等于让界面说谎：前端会把任何 @ 渲染成高亮可点的名字
                    await self._notice(
                        f"{agent.display_name} 没有派发权（can_dispatch=false），"
                        f"其 @{'、'.join('@' + t for t in got)} 已被忽略（未唤醒任何人）。",
                        level="warn",
                    )
                for token in got:
                    # 组员也可以 @全体成员：展开成全部成员 id（含组长，交给下面的收敛判断）
                    if is_all_mention(token):
                        await self._notice(
                            f"{agent.display_name} @了全体成员，已唤醒所有人。"
                        )
                        for mate in self.active_agents:
                            if mate.id == agent.id:
                                continue
                            pending.append((mate.id, agent.can_dispatch, agent.display_name))
                        continue
                    pending.append((token, agent.can_dispatch, agent.display_name))

            # 本轮组员有没有明确 @ 组长（有的话组长理应出来回应）
            captain_called = False
            for token, _allowed, _src in pending:
                who, _reason = self.resolve_mention_detail(token)
                if who is not None and who.id == captain.id:
                    captain_called = True
                    break

            # 组员发言后，组长**不再做自动收尾总结** —— 用户明确要求
            #「组长不必要这么啰嗦，在分配任务的时候活跃就行了」。
            #
            # 之前这里是每轮必发一次「收敛/定稿」，结果是：用户 @ 谁问一句，
            # 组员答完后面还要多挂一条组长的长篇总结，既啰嗦又白烧一次调用。
            #
            # 现在只有一种情况让组长起来：**组员明确 @ 了它**（那是真的点名要它回应，
            # 不是自动总结）。此时给它的任务也收窄成「直接回那一句」，
            # 而不是「做一次收敛、吸收要点、摆明分歧」那种长活。
            if (
                captain_called
                and self._budget_left()
                and self._run_spoke.get(captain.id, 0) < captain.max_turns_per_run
            ):
                converge_task = (
                    "组员刚刚 @ 了你（见上方记录）。请只回它问的那一点："
                    "能答就一两句话答完，需要谁补充就 @ 那个人。"
                    "不要复述整轮讨论、不要写收尾总结、不要为了收口而收口。"
                )
                more, _ok = await self._speak_counted(captain, converge_task, run_id)
                for token in more:
                    pending.append((token, captain.can_dispatch, captain.display_name))

            next_named: list[tuple[str, str]] = []
            seen_tokens: set[str] = set()
            for token, allowed, source in pending:
                if allowed and token not in seen_tokens:
                    seen_tokens.add(token)
                    next_named.append((token, source))

            named = next_named

            if named and depth >= self.limits.max_depth:
                await self._notice(
                    f"已达 @ 触发深度上限（max_depth={self.limits.max_depth}），"
                    f"后续 {'、'.join(token for token, _ in named)} 的 @ 不再展开。",
                    level="warn",
                )
            elif named and not self._budget_left():
                await self._notice(
                    f"已达单轮发言上限（max_messages={self.limits.max_messages}），"
                    f"后续 {'、'.join(token for token, _ in named)} 的 @ 不再展开。",
                    level="warn",
                )

        if self._stop_requested:
            await self._notice("已按你的要求停止本轮讨论。", level="warn")
            return

        # 派发跑完 → 让组长验收：任务真的完成了吗？
        # 这是「目标闭环」那一步 —— 在此之前，跑完就散会，没人管活干完没有。
        await self._acceptance_loop(run_id, seed_task, captain)

# ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # 成员的工具调用（写 / 读 / 列目录）
    # ------------------------------------------------------------------ #
    async def _handle_tool_call(self, agent: AgentSpec, event: dict) -> None:
        """把成员的工具调用变成群里的可见事实。

        为什么读也要显示：读文件是**协作真正在发生**的证据。
        以前只看得到"谁说了什么"，看不出它在参考别人的产物 ——
        现在能直接看到「Kimi 读了 frontend/index.html」，
        一眼就知道它到底有没有去看别人写的接口。

        为什么写要计数：验收的前置条件就是"本轮有产物"（_run_writes）。
        """
        action = str(event.get("action") or "")
        path = str(event.get("path") or "")
        name = agent.display_name

        if action == "write":
            self._run_writes += 1
            await self.emit(
                "workspace_write",
                agent=agent.id,
                path=path,
                bytes=event.get("bytes", 0),
                created=event.get("created", False),
                via="tool",
            )
            await self._notice(f"{name} 产出了文件：{path}")
            return

        if action == "read":
            await self.emit(
                "workspace_read",
                agent=agent.id,
                path=path,
                chars=event.get("chars", 0),
                truncated=bool(event.get("truncated")),
            )
            await self._notice(f"{name} 读了 {path}")
            return

        if action == "read_miss":
            # 路径写错：模型自己会从工具返回里看到候选项并重试，
            # 这里只在群里留一句，免得看起来"什么都没发生"
            await self._notice(f"{name} 想读 {path}，但工作区里没有这个文件。", level="warn")
            return

        if action == "list":
            count = int(event.get("count") or 0)
            await self._notice(f"{name} 查看了工作区（{count} 个文件）")
            return

        if action == "task":
            # 组长登记/更新任务。**要说出来** —— 任务板的变化是"进度"本身，
            # 悄悄改等于用户不知道进度走到哪了。
            st = {"todo": "待办", "doing": "进行中", "done": "已完成",
                  "blocked": "卡住"}.get(str(event.get("status")), str(event.get("status")))
            await self.emit(
                "task_update",
                agent=agent.id,
                title=event.get("title", ""),
                status=event.get("status", ""),
                owner=event.get("owner", ""),
            )
            await self._notice(f"任务板更新：{event.get('title')} → {st}")
            return

    # ------------------------------------------------------------------ #
    # 同一层的并发执行
    # ------------------------------------------------------------------ #
    async def _run_level(self, runnable: list, run_id: str) -> list:
        """并行跑完一层成员，返回**与 runnable 等长**的结果列表。

        抽成独立方法有两个原因：
          · 派发循环已经很长了，把并发细节摘出去更好读；
          · 它可以单独测（是不是真并行、顺序稳不稳、异常会不会吞）。

        `asyncio.gather` 按**参数顺序**创建任务，而 `message_start` 是在
        `_speak` 的第一个 await 之前发出的 —— 所以起跑顺序是确定的，
        界面不会因为谁先返回而乱跳。

        `return_exceptions=True`：一个成员炸了不该让整层静默失败，
        异常原样放进结果里，由调用方逐个如实报告。
        """
        if not runnable:
            return []
        # 已经请求停止了就**一个都别再开**。
        # 协作式检查只写在派发循环的条件里，拦不住"已经进入这一层"的调用 ——
        # 用户点了停止，循环下一圈照样会起一批新请求。必须在这里也拦一道。
        if self._stop_requested:
            return []
        parish = max(1, int(getattr(self.limits, "max_parallel", 1) or 1))
        if len(runnable) > 1 and parish > 1:
            # 留一份引用，好让 request_stop 能立刻取消在途请求
            self._level_task = asyncio.gather(
                *(self._speak_counted(a, t, run_id) for a, t in runnable),
                return_exceptions=True,
            )
            try:
                return list(await self._level_task)
            finally:
                self._level_task = None
        out = []
        for a, t in runnable:
            if self._stop_requested:
                break
            out.append(await self._speak_counted(a, t, run_id))
        return out

    # ------------------------------------------------------------------ #
    # 打断
    # ------------------------------------------------------------------ #
    def request_stop(self) -> bool:
        """请求停止本轮讨论。返回"是否真的有东西被停下"。

        两层机制，缺一不可：
          · **协作式**：置标志位，派发循环在每次派发前检查，不再开新的；
          · **强制式**：直接 cancel 当前这一层的 gather —— 否则一个
            Kimi 的 20 秒调用会把"停止"拖到 20 秒后才生效，那就不叫打断了。
        """
        self._stop_requested = True
        task = self._level_task
        if task is not None and not task.done():
            task.cancel()
            return True
        return False

    def clear_stop(self) -> None:
        self._stop_requested = False

    # 组长验收：任务真的完成了吗
    # ------------------------------------------------------------------ #
    async def _acceptance_loop(self, run_id: str, seed_task: str,
                               captain: AgentSpec) -> None:
        """组长验收：判定完成 → 结束；未完成 → 再派一轮 → 再验收。

        这是「从 L1（会用工具）到 L2（目标闭环）」的那一步。
        在这之前，主派发循环跑完就散会，**没有任何人负责说"这活没干完"**。

        判定信号**复用现成的 @ 机制**，不发明新协议：
          · 组长 @ 了人    → 它认为还没完成；
          · 组长没 @ 任何人 → 它认为完成了。
        """
        if self._stop_requested:
            return
        total = max(0, int(getattr(self.limits, "max_accept_rounds", 0) or 0))
        if total <= 0:
            return
        # **没有产物就没有可验收的东西。**
        # 这一条既是设计判断，也避免把"组长每轮都冒出来"的啰嗦问题请回来：
        # 纯讨论的一轮（谁都没写文件）不该触发验收。
        if not getattr(self, "_run_writes", 0):
            return

        from .workspace import digest as workspace_digest

        for round_no in range(1, total + 1):
            if not self._budget_left():
                await self._notice(
                    f"验收未开始：已达单轮发言上限（max_messages={self.limits.max_messages}）。",
                    level="warn",
                )
                return
            if self._run_spoke.get(captain.id, 0) >= captain.max_turns_per_run:
                await self._notice(
                    f"验收跳过：{captain.display_name} 本轮已发言 "
                    f"{self._run_spoke[captain.id]} 次，达上限（{captain.max_turns_per_run}）。",
                    level="warn",
                )
                return

            try:
                listing = workspace_digest()
            except Exception:  # noqa: BLE001 —— 读不了工作区不该拦验收
                listing = "（读取失败）"

            # 注意 _run_writes 在整轮 _run 期间**只增不清**：
            # 返工轮能看到第一轮写下的产物，这正是我们要的语义。
            task = _ACCEPT_TASK.format(seed=(seed_task or "").strip()[:600], digest=listing)
            mentions, ok = await self._speak_counted(captain, task, run_id)
            if not ok:
                # 组长验收这一跳失败，如实说明 —— 不能悄悄当成"通过"
                await self._notice(
                    f"验收失败（{captain.display_name} 这一跳没成功），本轮不再返工。",
                    level="warn",
                )
                return

            named: list[AgentSpec] = []
            for token in mentions:
                who = self.resolve_mention(token)
                if who is not None and who.id != captain.id and who.id not in {a.id for a in named}:
                    named.append(who)

            if not named:
                # 没点名任何人 = 判定完成
                if round_no == 1:
                    await self._notice("组长验收：通过。")
                return

            if round_no >= total:
                await self._notice(
                    f"已达验收轮数上限（max_accept_rounds={total}），"
                    f"不再返工。组长认为还差：{'、'.join(a.display_name for a in named)}。",
                    level="warn",
                )
                return

            await self._notice(
                f"组长验收：未通过 —— 让 {'、'.join(a.display_name for a in named)} 继续补。"
            )
            for agent in named:
                if not self._budget_left():
                    break
                if self._run_spoke.get(agent.id, 0) >= agent.max_turns_per_run:
                    await self._notice(
                        f"{agent.display_name} 本轮发言已达上限（{agent.max_turns_per_run} 次），"
                        "跳过返工。",
                        level="warn",
                    )
                    continue
                redo = (
                    f"@你（{agent.id}），组长刚刚**验收没有通过**。请对照上面组长的验收意见，"
                    "只做它指出还没完成的那部分，并把结果落到工作区里。"
                    "不要重复已经做完的内容，也不要复述整轮讨论。"
                )
                await self._speak_counted(agent, redo, run_id)

    # ------------------------------------------------------------------ #
    # 单轮预算与路由统计
    # ------------------------------------------------------------------ #
    def _budget_left(self) -> bool:
        """单轮发言总数上限：只约束派发链（组长 + 被 @ 的组员）的尝试次数。"""
        return self._run_calls < self.limits.max_messages

    def _note_route(self, agent: AgentSpec, provider: str, route: str, degraded: bool) -> None:
        self._run_routes[agent.id] = {
            "name": agent.display_name,
            "role": agent.role,
            "declared": agent.route,
            "provider": provider,
            "route": route,
            "degraded": degraded,
            "failed": False,
        }

    def _note_failure(self, agent: AgentSpec, requested_route: str, error: str) -> None:
        entry = {
            "name": agent.display_name,
            "role": agent.role,
            "declared": requested_route,
            "provider": requested_route.split("/", 1)[0],
            "route": requested_route,
            "degraded": False,
            "failed": True,
            "error": error,
        }
        self._run_routes[agent.id] = entry
        self._run_failures.append(entry)

    def route_summary(self) -> dict:
        """本轮实际路由分布。

        「多模型群聊」的立身之本是跨厂商误差去相关；一旦降级让多个角色落回同一家厂商，
        界面看起来一切正常，结论却可能只是同一家的自辩。这里把它算成一个可见的事实。
        失败成员单独列出，**不**计入正常参与。
        """
        entries = [item for item in self._run_routes.values() if not item.get("failed")]
        failed = list(self._run_failures)
        counts: dict[str, int] = {}
        for item in entries:
            counts[item["provider"]] = counts.get(item["provider"], 0) + 1
        degraded = [item for item in entries if item["degraded"]]
        shared = {p: c for p, c in counts.items() if c >= 2}
        distinct = len([p for p in counts if p != "mock"])
        if not entries and not failed:
            text = "本轮没有成员发言。"
        elif shared:
            parts = "、".join(f"{p}×{c}" for p, c in sorted(shared.items(), key=lambda kv: -kv[1]))
            text = (
                f"⚠ 同源提示：本轮 {len(entries)} 个角色实际只落在 {len(counts)} 个 provider 上，"
                f"其中 {parts} 由同一个 provider（厂商/通道）承担，"
                f"且 {len(degraded)} 个角色是降级后落过去的。"
                "跨厂商去相关的效果已被打折，请谨慎看待结论。"
            )
        else:
            parts = "、".join(f"{p}×{c}" for p, c in sorted(counts.items(), key=lambda kv: -kv[1]))
            text = f"本轮 {len(entries)} 个角色分别落在不同厂商：{parts}。"
        if failed:
            text += (
                f" 另有 {len(failed)} 个成员调用失败、没有产出："
                + "、".join(f"{f['name']}（{f['route']}）" for f in failed)
                + "——结论存在缺口。"
            )
        return {
            "providers": counts,
            "distinct_real_providers": distinct,
            "degraded_count": len(degraded),
            "shared_providers": shared,
            "failed": failed,
            "text": text,
            "routes": entries,
        }

    async def emit_route_summary(self) -> dict:
        summary = self.route_summary()
        await self.emit(
            "route_summary",
            **{
                key: summary[key]
                for key in (
                    "providers",
                    "distinct_real_providers",
                    "degraded_count",
                    "shared_providers",
                    "failed",
                )
            },
            message=summary["text"],
            level="warn" if (summary["shared_providers"] or summary["failed"]) else "info",
        )
        return summary

    def run_stats(self) -> dict:
        """本轮实际消耗：尝试次数 / 成功发言 / 失败 / 记录员调用。

        「尝试」与「成功」必须分开统计：失败调用要计入花费上界，
        但不能算成「这个人发过言了」——否则界面会显示「已达发言上限」而实际一个字都没说。
        """
        return {
            "dispatch_calls": self._run_calls,
            "dispatch_speaks": sum(self._run_spoke.values()),
            "member_success_counts": dict(self._run_spoke),
            "failed_calls": len(self._run_failures),
            "recorder_calls": self._run_recorder_calls,
            "max_messages": self.limits.max_messages,
            "model_calls_per_run": self._run_calls + self._run_recorder_calls,
        }

    async def _speak_counted(self, agent: AgentSpec, task: str, run_id: str) -> tuple[list[str], bool]:
        mentions, ok = await self._speak(agent, task, run_id)
        self._run_calls += 1           # 尝试次数：无论成败都计入花费上界
        if ok:
            self._run_spoke[agent.id] = self._run_spoke.get(agent.id, 0) + 1
        else:
            self._run_spoke.setdefault(agent.id, 0)
        return mentions, ok

    # ------------------------------------------------------------------ #
    # 单个成员发言
    # ------------------------------------------------------------------ #
    async def _speak(self, agent: AgentSpec, task: str, run_id: str) -> tuple[list[str], bool]:
        emitted = self._run_calls
        await self._set_state(agent.id, "thinking")

        message_id = uuid.uuid4().hex[:12]
        requested_route = agent.route
        await self.emit(
            "message_start",
            id=message_id,
            sender=agent.id,
            sender_name=agent.display_name,
            sender_avatar=agent.avatar,
            role=agent.role,
            requested_route=requested_route,
            run_id=run_id,
        )

        messages = await self._build_messages(agent, task)
        text_parts: list[str] = []
        route = requested_route
        warning = ""
        usage: dict = {}  # 真实 token 计量（来自上游 usage；mock 为 0）
        msg_reasoning = 0  # 本条发言的思考字数（思考型模型会先思考后答题）
        reply_to, reply_summary = self._resolve_reply_target(agent.id, task)

        try:
            async for event in self.client.stream_agent(agent, messages):
                if event["kind"] == "route":
                    route = event["route"]
                    warning = event["warning"]
                    self._note_route(agent, route.split("/", 1)[0], route, event["degraded"])
                    if event["degraded"]:
                        # 降级必须可见：事件流里一条 warn notice + 一条 route_changed，
                        # 界面据此在气泡上打「已降级」标记。
                        await self._notice(warning, level="warn")
                        await self.emit(
                            "route_changed",
                            id=message_id,
                            requested_route=requested_route,
                            route=route,
                            degraded=True,
                            warning=warning,
                        )
                elif event["kind"] == "reasoning":
                    # 思考增量：边收边推，让界面实时显示「它正在想什么」。
                    # 注意状态保持 thinking，不切 writing——writing 只代表在写正文。
                    text = event.get("text", "")
                    msg_reasoning += len(text)
                    self._run_reasoning_chars[agent.id] = (
                        self._run_reasoning_chars.get(agent.id, 0) + len(text)
                    )
                    await self.emit(
                        "agent_reasoning",
                        agent=agent.id,
                        text=text,
                        # 该成员本轮累计思考字数（跨多次发言累加）
                        chars=self._run_reasoning_chars[agent.id],
                    )
                elif event["kind"] == "tool_call":
                    # 成员通过**真正的 function calling** 动了工作区（写/读/列目录）
                    await self._handle_tool_call(agent, event)
                elif event["kind"] == "done":
                    # 调用结束：拿真实 token 计量（mock 通道全为 0，界面会标注「离线」）
                    usage = {
                        "tokens": int(event.get("tokens", 0) or 0),
                        "prompt_tokens": int(event.get("prompt_tokens", 0) or 0),
                        "completion_tokens": int(event.get("completion_tokens", 0) or 0),
                        "reasoning_chars": int(event.get("reasoning_chars", 0) or 0),
                    }
                elif event["kind"] == "delta":
                    text_parts.append(event["text"])
                    if self.agent_state.get(agent.id) != "writing":
                        # 收到第一个正文增量：状态从「正在思考…」切成「正在撰写回复…」，
                        # 并通过 agent_status 事件（带 status 字段）推给 WebSocket 客户端。
                        await self._set_state(agent.id, "writing")
                    await self.emit("message_delta", id=message_id, delta=event["text"])
        except LLMError as exc:
            self._note_failure(agent, requested_route, str(exc))
            await self._set_state(agent.id, "error")
            # 失败也要留一条 Message：否则记录员会照常汇总出一份「完整」纪要，
            # 用户看到的是没有缺口的结论。
            failed = Message(
                id=message_id,
                sender=agent.id,
                sender_name=agent.display_name,
                sender_avatar=agent.avatar,
                role=agent.role,
                content="",
                route=route,
                requested_route=requested_route,
                warning=warning,
                error=str(exc),
                run_id=run_id,
                turn=emitted,
                reply_to=reply_to,
                reply_summary=reply_summary,
                reasoning_chars=msg_reasoning,
            )
            self.messages.append(failed)
            self.save_history()
            await self.emit(
                "message_end",
                id=message_id,
                content="",
                error=str(exc),
                message=failed.to_dict(),
            )
            await self._notice(f"{agent.display_name} 发言失败：{exc}", level="error")
            return [], False

        content = "".join(text_parts).strip()

        # 「(pass)」：成员明确表示这轮没什么新东西。
        # 照 Grok Bot 的做法（'If you have nothing new worth adding, send exactly "(pass)"'）。
        # 关键：**不能记成一条发言** —— 否则它还是占了 max_turns_per_run 和聊天框，
        # 就失去了"不刷屏"的意义。
        if is_pass_content(content):
            await self._set_state(agent.id, "done")
            await self.emit(
                "message_end",
                id=message_id,
                content="",
                route=route,
                requested_route=requested_route,
                warning=warning,
                passed=True,
            )
            await self._notice(f"{agent.display_name} 这轮没什么要补充的（pass）。")
            return [], True
        # 先把 ```file: 块抽出来真的写盘，再走后面的记录流程。
        # 抽完 content 里只剩「〔已产出文件：xxx〕」这样的占位，
        # 否则整份文件内容会在聊天框里再刷一遍（又长又没意义）。
        try:
            from .workspace import extract_writes

            content, wrote, failed = extract_writes(content)
            for w in wrote:
                await self.emit(
                    "workspace_write",
                    agent=agent.id,
                    path=w["path"],
                    bytes=w["bytes"],
                    created=w["created"],
                )
            self._run_writes += len(wrote)
            if wrote:
                await self._notice(
                    f"{agent.display_name} 产出了 {len(wrote)} 个文件："
                    + "、".join(w["path"] for w in wrote)
                )
            for f in failed:
                # 失败必须说出来 —— 静默丢弃会让用户以为文件写了，其实没写
                await self._notice(
                    f"{agent.display_name} 想写 {f['path']} 但没写成：{f['error']}",
                    level="warn",
                )
        except Exception as exc:  # noqa: BLE001 —— 写盘失败不该炸掉整轮
            await self._notice(f"处理产出文件时出错：{exc}", level="warn")
        if not content:
            # 常见原因：思考型模型把 max_tokens 全花在 reasoning_content 上，
            # 正文为空。绝不能静默记一条空气泡，必须显式说出来。
            await self._notice(
                f"{agent.display_name} 这一轮返回了空正文（路由 {route}，"
                f"max_tokens={agent.max_tokens}）。可能是思考内容占满了额度，建议调大 max_tokens。",
                level="warn",
            )
        raw_mentions = extract_mentions(content)

        recorded = Message(
            id=message_id,
            sender=agent.id,
            sender_name=agent.display_name,
            sender_avatar=agent.avatar,
            role=agent.role,
            content=content,
            mentions=raw_mentions,
            route=route,
            requested_route=requested_route,
            warning=warning,
            run_id=run_id,
            turn=emitted,
            tokens=usage.get("tokens", 0),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            # 优先用上游 done 事件回传的思考字数；没有就退回本地累计值
            reasoning_chars=usage.get("reasoning_chars") or msg_reasoning,
            reply_to=reply_to,
            reply_summary=reply_summary,
            proactive=self._is_proactive(content),
        )
        self.messages.append(recorded)
        self.save_history()
        await self.emit("message_end", id=message_id, content=content, message=recorded.to_dict())

        await self._set_state(agent.id, "done")
        await asyncio.sleep(0)  # 让出事件循环，保证前端能及时收到
        return raw_mentions, True

    # ------------------------------------------------------------------ #
    # 汇总
    # ------------------------------------------------------------------ #
    async def _summarize(self, run_id: str) -> None:
        recorder = self.cfg.recorder
        if not recorder.enabled:
            return
        if len([m for m in self.messages if m.run_id == run_id and m.role != "user"]) < 2:
            # 组长一句话就答完了，不需要再花一次调用来写纪要
            return

        await self._set_state(recorder.id, "thinking")
        self._run_recorder_calls += 1
        message_id = uuid.uuid4().hex[:12]
        requested_route = recorder.route
        await self.emit(
            "message_start",
            id=message_id,
            sender=recorder.id,
            sender_name=recorder.display_name,
            sender_avatar=recorder.avatar,
            role="recorder",
            requested_route=requested_route,
            run_id=run_id,
        )

        summary = self.route_summary()
        gaps = ""
        if self._run_failures:
            gaps = (
                "\n注意：本轮以下成员**调用失败、没有任何产出**，纪要必须如实写出这个缺口，"
                "不得当作大家已经讨论完整："
                + "；".join(
                    f"{f['name']}（声明 {f['declared']}，原因：{f['error'][:120]}）"
                    for f in self._run_failures
                )
            )
        task = (
            "上面是这一轮群聊的完整记录。请按你的输出结构写一份给用户看的小结。"
            "注意：只总结记录里出现过的事实。\n"
            f"另外，本轮的实际路由情况是：{summary['text']}\n"
            "你必须在纪要里如实体现这一点：追加一行以「【实际参与模型】」开头的说明，"
            "写清本轮每个角色实际由哪个 provider/model 发言、谁发生了降级、"
            "是否存在同一家厂商承担多个角色（同源）"
            "、以及谁调用失败导致结论有缺口。不要省略这一行。"
            f"{gaps}"
        )
        messages = await self._build_messages(recorder, task)
        parts: list[str] = []
        route = requested_route
        warning = ""
        usage: dict = {}
        msg_reasoning = 0
        try:
            async for event in self.client.stream_agent(recorder, messages):
                if event["kind"] == "route":
                    route = event["route"]
                    warning = event["warning"]
                    self._note_route(recorder, route.split("/", 1)[0], route, event["degraded"])
                    if event["degraded"]:
                        await self._notice(warning, level="warn")
                        await self.emit(
                            "route_changed",
                            id=message_id,
                            requested_route=requested_route,
                            route=route,
                            degraded=True,
                            warning=warning,
                        )
                elif event["kind"] == "reasoning":
                    # 记录员同样是思考型模型，思考过程一样要实时透出
                    text = event.get("text", "")
                    msg_reasoning += len(text)
                    self._run_reasoning_chars[recorder.id] = (
                        self._run_reasoning_chars.get(recorder.id, 0) + len(text)
                    )
                    await self.emit(
                        "agent_reasoning",
                        agent=recorder.id,
                        text=text,
                        chars=self._run_reasoning_chars[recorder.id],
                    )
                elif event["kind"] == "done":
                    usage = {
                        "tokens": int(event.get("tokens", 0) or 0),
                        "prompt_tokens": int(event.get("prompt_tokens", 0) or 0),
                        "completion_tokens": int(event.get("completion_tokens", 0) or 0),
                        "reasoning_chars": int(event.get("reasoning_chars", 0) or 0),
                    }
                elif event["kind"] == "delta":
                    parts.append(event["text"])
                    if self.agent_state.get(recorder.id) != "writing":
                        # 记录员同样要显示「正在撰写回复…」
                        await self._set_state(recorder.id, "writing")
                    await self.emit("message_delta", id=message_id, delta=event["text"])
        except LLMError as exc:
            self._note_failure(recorder, requested_route, str(exc))
            await self._set_state(recorder.id, "error")
            await self.emit(
                "message_end",
                id=message_id,
                content="".join(parts),
                route=route,
                requested_route=requested_route,
                warning=warning,
                error=str(exc),
            )
            await self._notice(f"汇总失败：{exc}", level="warn")
            return

        content = "".join(parts).strip()
        recorded = Message(
            id=message_id,
            sender=recorder.id,
            sender_name=recorder.display_name,
            sender_avatar=recorder.avatar,
            role="recorder",
            content=content,
            route=route,
            requested_route=requested_route,
            warning=warning,
            run_id=run_id,
            turn=self._run_recorder_calls,
            tokens=usage.get("tokens", 0),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            reasoning_chars=usage.get("reasoning_chars") or msg_reasoning,
        )
        self.messages.append(recorded)
        self.save_history()
        await self.emit("message_end", id=message_id, content=content, message=recorded.to_dict())
        await self._set_state(recorder.id, "idle")

    # ------------------------------------------------------------------ #
    # 快照
    # ------------------------------------------------------------------ #
    def _member_view(self, agent: AgentSpec) -> dict:
        return {
            "id": agent.id,
            "name": agent.display_name,
            "avatar": agent.avatar,
            "role": agent.role,
            "state": self.agent_state.get(agent.id, "idle"),
            # 界面直接用这一行中文；state 保留不动，兼容旧逻辑
            "status": self.status_text(self.agent_state.get(agent.id, "idle")),
            "route": agent.route,
            "fallback_route": agent.fallback_route,
            "tags": agent.tags,
            "max_turns_per_run": agent.max_turns_per_run,
            "can_dispatch": agent.can_dispatch,
            # 界面用：自定义职责描述；hidden=True 的成员不出现在左栏名单里
            "duty": agent.duty,
            "hidden": agent.hidden,
            # 2026-10-14 编辑成员面板要用这三个：把当前值回填进输入框，
            # 否则用户打开面板看到的是空白/默认值，一保存就把配置改坏了。
            "provider": agent.provider,
            "temperature": agent.temperature,
            "max_tokens": agent.max_tokens,
            "hasKey": self._provider_ready(agent.provider),
        }

    def _provider_ready(self, provider_id: str) -> bool:
        """该提供商当前有没有可用凭据（只看有无，绝不回显值）。"""
        prov = self.cfg.providers.get(provider_id)
        if prov is None:
            return False
        if prov.is_mock:
            return True
        from .config import resolve_credential

        key, _source = resolve_credential(prov.credentials)
        return bool(key)

    def members_view(self) -> list[dict]:
        """给界面用的成员列表：参与讨论的成员 + 记录员。"""
        view = [self._member_view(a) for a in self.active_agents]
        if self.cfg.recorder.enabled:
            view.append(self._member_view(self.cfg.recorder))
        return view

    def limits_view(self) -> dict:
        return {
            **asdict(self.limits),
            "max_model_calls_per_run": self.limits.max_messages + 1,
        }

    def snapshot(self) -> dict:
        return {
            "running": self._running,
            "run_id": self.current_run_id,
            "members": self.members_view(),
            "limits": self.limits_view(),
            "run_stats": self.run_stats(),
            "route_summary": self.route_summary(),
            "messages": [m.to_dict() for m in self.messages],
        }
