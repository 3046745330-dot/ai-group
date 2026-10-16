"""配置装载：providers.yaml / models.yaml / roles/*.md / 环境变量 / 凭据解析。"""

from __future__ import annotations
import os
import re
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


def _ensure_utf8_output() -> None:
    """把 stdout/stderr 重配成 UTF-8（errors=replace）。

    为什么需要：打包成 exe 后（PyInstaller --no-window），stdout 落到
    Windows 默认代码页（这里是 GBK）。而日志里有中文和 `⚠` 这类字符 ——
    一打就抛 UnicodeEncodeError，**把那条日志整段弄丢**，比不打还糟。

    修在入口而不是逐个删字符：否则以后任何一条带中文/符号的新日志都可能再崩。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 —— 某些环境（已被替换的流）不支持
            pass


_ensure_utf8_output()



# --------------------------------------------------------------------------- #
# 路径解析（同时支持「源码运行」与「PyInstaller 冻结成 exe」两种形态）
# --------------------------------------------------------------------------- #
def _resource_root() -> Path:
    """只读资源（web/ 静态文件）所在目录。

    PyInstaller 单文件模式会把资源解包到临时目录 ``sys._MEIPASS``；
    源码运行时就是项目根目录。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent.parent


def _data_root() -> Path:
    """可写数据（config/ 配置）所在目录，按优先级探测：

    1. 环境变量 ``AIGROUP_DATA`` 显式指定；
    2. exe 同级目录（打包后用户能直接改配置）；
    3. 源码项目根目录；
    4. 兜底：用户主目录下的 ``.ai-group``（无写权限时也能跑）。
    """
    env = os.environ.get("AIGROUP_DATA", "").strip()
    if env:
        return Path(env)
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        if (exe_dir / "config").is_dir():
            return exe_dir
        # 首次运行：把随包资源里的 config 复制一份到 exe 同级，方便用户修改
        bundled = _resource_root() / "config"
        target = exe_dir / "config"
        if bundled.is_dir():
            try:
                import shutil
                shutil.copytree(bundled, target, dirs_exist_ok=True)
                return exe_dir
            except Exception:
                pass
        return exe_dir
    return Path(__file__).resolve().parent.parent


# 只读资源根（web/）
RESOURCE_ROOT = _resource_root()
# 可写数据根（config/）
PROJECT_ROOT = _data_root()
CONFIG_DIR = PROJECT_ROOT / "config"
ROLES_DIR = CONFIG_DIR / "roles"
WEB_DIR = RESOURCE_ROOT / "web"
# 源码目录（group/ 包的父目录）：冻结后模块已在包内，仅用于源码模式导入
SOURCE_ROOT = Path(__file__).resolve().parent.parent

DSH_CREDENTIALS = Path.home() / ".dsh" / ".credentials.yaml"


# --------------------------------------------------------------------------- #
# 用户数据目录
# --------------------------------------------------------------------------- #
_DATA_DIR_CACHE: Path | None = None


def _data_dir_candidates() -> list[Path]:
    """候选数据目录，**按优先级**排。

    顺序的理由：
      1. ``%LOCALAPPDATA%\\AIWorkGroup`` —— 系统给应用放私有数据的标准位置，
         用户换版本（重新解压覆盖文件夹）完全不受影响；
      2. ``%APPDATA%\\AIWorkGroup`` —— Roaming，ACL 与 Local 不同，某些环境只有它能写；
      3. ``我的文档\\AIWorkGroup`` —— 万一系统目录都不让写，这里至少**用户看得见、
         好备份**（`server/data` 藏在程序文件夹里，用户根本找不到）；
      4. ``server/data`` —— 最后兜底。能用，但换版本会跟着丢，所以排最后。
    """
    import os

    candidates: list[Path] = []
    for env in ("LOCALAPPDATA", "APPDATA"):
        base = os.environ.get(env)
        if base:
            candidates.append(Path(base) / "AIWorkGroup")
    home = os.environ.get("USERPROFILE") or os.environ.get("HOME")
    if home:
        candidates.append(Path(home) / "Documents" / "AIWorkGroup")
    candidates.append(CONFIG_DIR.parent / "data")
    return candidates


def describe_data_dir() -> str:
    """给日志/诊断用：说明数据目录选在哪、别的候选为什么没选上。

    之前这里是**静默尝试**，失败了完全看不出来 —— 结果数据悄悄落在程序文件夹里，
    用户换版本才发现记录没了。所以每个候选的成败都要留下痕迹。
    """
    import os

    lines: list[str] = []
    chosen = user_data_dir()
    for path in _data_dir_candidates():
        mark = "← 已选用" if path == chosen else ""
        lines.append(f"    {path}  {mark}".rstrip())
    lines.append(f"    （LOCALAPPDATA={os.environ.get('LOCALAPPDATA')!r}）")
    return "\n".join(lines)


def user_data_dir() -> Path:
    """放"属于用户、不该随版本更新丢失"的东西：聊天记录、工作区等。

    为什么不能直接放 ``server/`` 下：用户换新版本的常规做法是「重新解压覆盖文件夹」，
    那样 server/ 里的东西会被一起换掉 —— 聊天记录、头像、工作区就全没了。
    """
    global _DATA_DIR_CACHE
    if _DATA_DIR_CACHE is not None:
        return _DATA_DIR_CACHE

    failures: list[str] = []
    for path in _data_dir_candidates():
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            _DATA_DIR_CACHE = path
            # 只在第一次解析时打一次，免得刷屏
            print(f"[data] 数据目录 = {path}", flush=True)
            if failures:
                for f in failures:
                    print(f"[data]   上一个候选不可用：{f}", flush=True)
            if path == CONFIG_DIR.parent / "data":
                print("[data] ⚠️ 兜底到了程序文件夹里 —— 换版本会丢，"
                      "建议确认系统目录能否写入", flush=True)
            return path
        except Exception as exc:  # noqa: BLE001 —— 换下一个候选
            failures.append(f"{path} → {type(exc).__name__}: {exc}")
            continue

    # 全部失败（几乎不可能）：仍返回兜底，让调用方去处理写失败
    fallback = CONFIG_DIR.parent / "data"
    _DATA_DIR_CACHE = fallback
    print(f"[data] 所有候选都不可写，退回 {fallback}", flush=True)
    for f in failures:
        print(f"[data]   {f}", flush=True)
    return fallback


def migrate_legacy_data() -> list[str]:
    """把老位置（``server/data``）里的用户数据搬到新数据目录。

    改数据目录位置时**必须**搬，否则用户的历史记录会"看起来丢了"。
    只搬不覆盖：新位置已有同名文件就跳过。
    """
    import shutil

    target = user_data_dir()
    legacy = CONFIG_DIR.parent / "data"
    if target == legacy or not legacy.is_dir():
        return []

    moved: list[str] = []
    for name in ("history.json",):
        src = legacy / name
        dst = target / name
        if src.is_file() and not dst.exists():
            try:
                shutil.copyfile(src, dst)
                moved.append(name)
            except OSError:
                pass
    # 工作区整个目录搬
    src_ws = legacy / "workspace"
    dst_ws = target / "workspace"
    if src_ws.is_dir() and not dst_ws.exists():
        try:
            shutil.copytree(src_ws, dst_ws)
            moved.append("workspace/")
        except OSError:
            pass
    return moved


def _load_dotenv() -> None:
    """读取 exe 同级（或项目根）的 ``.env``，把键值注入 ``os.environ``。

    存在理由：打包成 exe 后，用户换台机器不会有 DSH 凭据库，
    也不一定会去设置系统环境变量。给一个「和 exe 放一起的 .env」是最省事的方式。
    不覆盖已存在的环境变量（系统环境变量优先）。
    """
    candidates = [PROJECT_ROOT / ".env", Path.cwd() / ".env"]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            for raw in path.read_text(encoding="utf-8-sig").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
        except Exception:
            continue
        break


# 模块导入即生效：让下游 resolve_credential 能读到 .env 里的密钥
_load_dotenv()


# --------------------------------------------------------------------------- #
# 凭据解析
# --------------------------------------------------------------------------- #
def _read_dsh_credential(ref: str) -> str | None:
    """从 DSH 凭据库读取 `llm-pi-ai/moonshotai-cn` 这类引用对应的密钥。

    凭据库结构（version 1）::

        refs:
          MOONSHOTAI_CN_API_KEY: sk-xxx
        records:
          deepseek-account-platform/default:
            kind: grant
            payload: {token: sk-xxx, ...}
    """
    if not DSH_CREDENTIALS.exists():
        return None
    try:
        data = yaml.safe_load(DSH_CREDENTIALS.read_text(encoding="utf-8")) or {}
    except Exception:
        return None

    # 1) refs 段：按完整引用名或末段名匹配
    refs = data.get("refs") or {}
    if ref in refs and isinstance(refs[ref], str):
        return refs[ref].strip()
    tail = ref.split("/")[-1]
    for key, value in refs.items():
        if key == tail and isinstance(value, str):
            return value.strip()

    # 2) records 段：取 payload 里的 token / secret / api_key
    records = data.get("records") or {}
    record = records.get(ref)
    if isinstance(record, dict):
        payload = record.get("payload") or {}
        for key in ("token", "api_key", "apiKey", "secret", "value"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def resolve_credential(specs: list[dict[str, str]]) -> tuple[str | None, str]:
    """按声明顺序解析密钥，返回 (密钥, 来源说明)。"""
    for spec in specs or []:
        if "env" in spec:
            value = os.environ.get(spec["env"], "").strip()
            if value:
                return value, f"env:{spec['env']}"
        if "dsh_credential" in spec:
            value = _read_dsh_credential(spec["dsh_credential"])
            if value:
                return value, f"dsh:{spec['dsh_credential']}"
    wanted = ", ".join(s.get("env") or f"dsh:{s.get('dsh_credential')}" for s in (specs or []))
    return None, f"未配置（{wanted or '无凭据声明'}）"


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
@dataclass
class ModelSpec:
    id: str
    label: str = ""
    context_window: int = 32768
    capabilities: list[str] = field(default_factory=list)


@dataclass
class ProviderSpec:
    id: str
    label: str
    api: str
    base_url: str
    credentials: list[dict[str, str]] = field(default_factory=list)
    models: dict[str, ModelSpec] = field(default_factory=dict)

    @property
    def is_mock(self) -> bool:
        return self.api == "mock"


@dataclass
class AgentSpec:
    id: str
    display_name: str
    role: str
    provider: str
    model: str
    avatar: str = "🤖"
    role_file: str = ""
    fallback: dict[str, str] | None = None
    temperature: float = 0.7
    max_tokens: int = 1200
    max_turns_per_run: int = 2
    can_dispatch: bool = False
    tags: list[str] = field(default_factory=list)
    enabled: bool = True
    system_prompt: str = ""
    # 界面用：成员在侧栏里显示的一句职责描述。留空则不显示该行。
    # 由用户在 config/models.yaml 里自行定义（不再从 tags 自动拼）。
    duty: str = ""
    # 界面用：是否在「团队成员」列表里隐藏（记录员、离线兜底等不参与讨论的角色用）。
    # 隐藏的成员仍然照常工作，只是不出现在左栏名单里。
    hidden: bool = False
    # 厂商平台原生能力。写在 config/models.yaml 的 features: 里。
    #   web_search —— 联网（千问是参数开关；Kimi 是内置工具 + 回灌；其它厂商暂不支持）
    # 为什么按**成员**配而不是按厂商：同一厂商不同模型能力不一样，
    # 而且联网会把搜索结果塞进输入 token，成本影响实在，得能逐个关。
    features: list[str] = field(default_factory=list)

    @property
    def route(self) -> str:
        return f"{self.provider}/{self.model}"

    @property
    def fallback_route(self) -> str:
        if not self.fallback:
            return ""
        return f"{self.fallback.get('provider', '?')}/{self.fallback.get('model', '?')}"

    @property
    def is_recorder(self) -> bool:
        return self.role == "recorder"


@dataclass
class LimitSpec:
    """单轮额度护栏。写在 models.yaml 的 ``limits:`` 段，界面「设置」里可改。

    默认值与 engine.RunLimits 保持一致；这里独立存一份，因为 config 不能反过来
    import engine（会形成循环依赖）。
    """

    max_messages: int = 6
    max_depth: int = 3
    max_context_chars: int = 5000
    # 组长验收轮数上限。
    #   0 = 关闭验收（退回到"派发完就散会"的老行为）；
    #   2 = 最多返工两轮，够覆盖"漏了东西"和"改完还不对"两种常见情况。
    # 每多一轮就是「1 次组长调用 + 若干次组员调用」，所以默认给得克制。
    max_accept_rounds: int = 2
    # 同一层里同时开跑的成员数上限。
    #   1 = 完全串行（退回老行为）；
    #   3 = 默认。串行的代价很实在：豆包 ≈8.7s、Kimi ≈23s，
    #       两个人串行就是 31 秒，并行只要 23 秒（取最慢的那个）。
    # 注意：**只并行同一层**，层与层之间仍串行 —— 组员互相反驳是跨层的，
    # 全并行会让后说话的人看不到前一个人的原话，反驳就没了。
    max_parallel: int = 3


@dataclass
class GroupConfig:
    providers: dict[str, ProviderSpec]
    agents: list[AgentSpec]
    recorder: AgentSpec
    limits: LimitSpec = field(default_factory=LimitSpec)

    @property
    def active_agents(self) -> list[AgentSpec]:
        """可被 @ 的群成员：已启用、且不是记录员（记录员只做最后汇总）。"""
        return [a for a in self.agents if a.enabled and not a.is_recorder]

    def by_id(self, agent_id: str) -> AgentSpec | None:
        for agent in self.agents:
            if agent.id == agent_id:
                return agent
        if self.recorder.id == agent_id:
            return self.recorder
        return None


# --------------------------------------------------------------------------- #
# 装载
# --------------------------------------------------------------------------- #
def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在：{path}")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _read_role(role_file: str) -> str:
    if not role_file:
        return ""
    path = ROLES_DIR / role_file
    if not path.exists():
        return f"（缺少角色提示词文件 {role_file}）"
    return path.read_text(encoding="utf-8").strip()


def _build_providers(raw: dict[str, Any]) -> dict[str, ProviderSpec]:
    providers: dict[str, ProviderSpec] = {}
    for pid, conf in (raw.get("providers") or {}).items():
        models = {
            mid: ModelSpec(
                id=mid,
                label=(mconf or {}).get("label", mid),
                context_window=int((mconf or {}).get("context_window", 32768)),
                capabilities=list((mconf or {}).get("capabilities", [])),
            )
            for mid, mconf in ((conf or {}).get("models") or {}).items()
        }
        providers[pid] = ProviderSpec(
            id=pid,
            label=(conf or {}).get("label", pid),
            api=(conf or {}).get("api", "openai-chat"),
            base_url=(conf or {}).get("base_url", ""),
            credentials=list((conf or {}).get("credentials") or []),
            models=models,
        )
    return providers


def _build_agents(raw: dict[str, Any]) -> list[AgentSpec]:
    agents: list[AgentSpec] = []
    for item in raw.get("agents") or []:
        spec = AgentSpec(
            id=item["id"],
            display_name=item.get("display_name", item["id"]),
            role=item.get("role", "member"),
            provider=item.get("provider", "mock"),
            model=item.get("model", ""),
            avatar=item.get("avatar", "🤖"),
            role_file=item.get("role_file", ""),
            fallback=item.get("fallback"),
            temperature=float(item.get("temperature", 0.7)),
            max_tokens=int(item.get("max_tokens", 1200)),
            max_turns_per_run=int(item.get("max_turns_per_run", 2)),
            can_dispatch=bool(item.get("can_dispatch", False)),
            tags=list(item.get("tags") or []),
            enabled=bool(item.get("enabled", True)),
            duty=str(item.get("duty") or "").strip(),
            hidden=bool(item.get("hidden", False)),
            features=[str(x).strip().lower()
                      for x in (item.get("features") or []) if str(x).strip()],
        )
        agents.append(spec)
    return agents


@lru_cache(maxsize=1)
def load_config() -> GroupConfig:
    providers = _build_providers(_load_yaml(CONFIG_DIR / "providers.yaml"))
    models_raw = _load_yaml(CONFIG_DIR / "models.yaml")
    agents = _build_agents(models_raw)

    # 系统提示词 = 角色文件正文（这就是「不同系统提示词定义职责」的落地）
    for agent in agents:
        agent.system_prompt = _read_role(agent.role_file)

    recorder = next((a for a in agents if a.is_recorder), None)
    if recorder is None:
        # models.yaml 里没有记录员条目时的兜底默认值。
        #
        # 2026-10-13 改：默认 **enabled=False** —— 交付配置里已经删掉了记录员
        # （用户不需要每轮再额外花一次模型调用去写纪要）。但引擎的「记录员汇总」
        # 能力要保留，所以这里仍然造一个对象，只是默认不启用：
        #   · 不启用 → engine._summarize 直接 return，零调用、零费用、不进名单
        #   · 想启用 → 在 models.yaml 里加回记录员条目并写 enabled: true
        recorder = AgentSpec(
            id="recorder",
            display_name="记录员·纪要",
            avatar="📝",
            role="recorder",
            role_file="recorder.md",
            provider="deepseek",
            model="deepseek-chat",
            temperature=0.2,
            max_tokens=900,
            max_turns_per_run=1,
            can_dispatch=False,
            tags=["汇总"],
            enabled=False,
            hidden=True,
        )
        recorder.system_prompt = _read_role(recorder.role_file)
        agents.append(recorder)

    # limits: 段（可选）—— 单轮发言数 / @ 深度 / 上下文上限
    limits = LimitSpec()
    raw_limits = models_raw.get("limits") or {}
    if isinstance(raw_limits, dict):
        for _key in ("max_messages", "max_depth", "max_context_chars", "max_accept_rounds"):
            if _key in raw_limits:
                try:
                    setattr(limits, _key, int(raw_limits[_key]))
                except (TypeError, ValueError):
                    pass

    return GroupConfig(providers=providers, agents=agents, recorder=recorder, limits=limits)


def reload_config() -> GroupConfig:
    """失效 ``@lru_cache`` 并重新读盘，返回新的配置对象。

    改完 ``models.yaml`` 必须调用它；调用方拿到新对象后还要把它挂回运行中的引擎
    （见 ``app._apply_config``），否则引擎里的 ``self.cfg`` 仍指向老对象。
    """
    load_config.cache_clear()
    return load_config()


# --------------------------------------------------------------------------- #
# 运行时改角色（持久化到 models.yaml）
# --------------------------------------------------------------------------- #
MEMBERS_FILE = CONFIG_DIR / "models.yaml"

_ID_LINE_RE = re.compile(r"^(\s*)-\s*id:\s*([^\s#]+)\s*$")
_ROLE_LINE_RE = re.compile(r"^(\s*)role:\s*.*$")


def _set_role_line(lines: list[str], agent_id: str, role: str) -> bool:
    """只替换指定成员块里的 ``role:`` 一行，其余注释与结构原样保留。

    返回是否命中；命中不了就让调用方报错，绝不整份重写 YAML（那会丢掉注释）。
    """
    in_block = False
    for index, line in enumerate(lines):
        matched = _ID_LINE_RE.match(line)
        if matched:
            in_block = matched.group(2).strip("\"'") == agent_id
            continue
        if not in_block:
            continue
        role_matched = _ROLE_LINE_RE.match(line)
        if role_matched:
            lines[index] = f"{role_matched.group(1)}role: {role}\n"
            return True
    return False


def update_member_role(agent_id: str, role: str) -> tuple[GroupConfig, list[str]]:
    """运行时改成员角色并持久化到 models.yaml，返回 ``(新配置, 被改动的成员 id)``。

    规则：
    * 只接受 ``captain`` / ``member``；
    * 记录员不可改（它不参与讨论，只做最后汇总）；
    * 升为组长时原来的组长自动降为 member，保证**全局只有一个 captain**；
    * 降级最后一名组长会被拒绝，避免出现「群里没有组长」的死状态。
    """
    if role not in ("captain", "member"):
        raise ValueError("role 只能是 captain 或 member")
    cfg = load_config()
    target = cfg.by_id(agent_id)
    if target is None:
        raise KeyError(agent_id)
    if agent_id == cfg.recorder.id or target.is_recorder:
        raise ValueError("记录员（recorder）的角色不允许修改：它不参与讨论，只负责最后汇总。")
    if role == "member" and target.role == "captain":
        others = [a for a in cfg.agents if a.enabled and a.role == "captain" and a.id != agent_id]
        if not others:
            raise ValueError("群里必须保留一个组长：请先把另一位成员设为组长，再降级当前组长。")

    lines = MEMBERS_FILE.read_text(encoding="utf-8").splitlines(keepends=True)
    changed: list[str] = []
    if not _set_role_line(lines, agent_id, role):
        raise ValueError(f"models.yaml 里找不到成员 {agent_id} 的 role 行")
    changed.append(agent_id)
    if role == "captain":
        # 自动把原来的组长降级，否则会出现双组长
        for other in cfg.agents:
            if other.enabled and other.role == "captain" and other.id != agent_id:
                if _set_role_line(lines, other.id, "member"):
                    changed.append(other.id)

    # newline="" 很关键：否则 Windows 下会把所有 \n 自动翻成 \r\n，整份文件的行尾都变了
    with MEMBERS_FILE.open("w", encoding="utf-8", newline="") as handle:
        handle.write("".join(lines))

    new_cfg = reload_config()
    written = new_cfg.by_id(agent_id)
    if written is None or written.role != role:
        raise RuntimeError("角色已写入 models.yaml，但重新装载后没有生效")
    return new_cfg, changed


# --------------------------------------------------------------------------- #
# 可用性探测
# --------------------------------------------------------------------------- #
# 字符类里除中英文数字外，还要容纳 @Kimi·论证担当 / @豆包・反驳 这类含间隔号的显示名，
# 否则一个 @ 会被切成两半，派发就丢了。
# 另外要求 @ 前面是行首或空白/标点：`请联系 zhang@kimi 复核` 这种正文里的邮箱
# 绝不能把人真的叫起来——那既花钱又会让界面看起来像被点名。
_MENTION_RE = re.compile(
    r"(?:(?<=^)|(?<=[\s\u3000，。；：、（）()【】\[\]「」\"'“”]))"
    r"@([A-Za-z0-9_\-\.\u4e00-\u9fff·・•]+)",
    re.MULTILINE,
)


def extract_mentions(text: str) -> list[str]:
    """从文本里抽取 @提及 的原始 id（保持出现顺序，大小写不敏感去重）。"""
    seen: list[str] = []
    lowered: set[str] = set()
    for match in _MENTION_RE.finditer(text or ""):
        token = match.group(1).rstrip("：:,，.。、）)】]·・•")
        if not token:
            continue
        key = token.lower()
        if key in lowered:
            continue
        lowered.add(key)
        seen.append(token)
    return seen


def strip_mentions(text: str) -> str:
    return _MENTION_RE.sub("", text or "").strip()


def force_mock(cfg: GroupConfig) -> GroupConfig:
    """把所有成员（含记录员）切到离线通道：零密钥、零网络、零额度。

    自检脚本的 ``--mock-only`` 与 ``run.ps1 -MockOnly`` 共用这一条路径，
    因此「离线演示」在任何入口下行为一致。
    """
    for agent in cfg.agents:
        agent.provider = "mock"
        agent.model = "mock-deep" if agent.is_recorder else "mock-fast"
        # 离线通道不会失败，不需要 fallback；留着反而会造出「mock → mock」这种假降级
        agent.fallback = None
    # 兜底：记录员不在 agents 列表里时（models.yaml 缺条目）也要一起切
    cfg.recorder.provider = "mock"
    cfg.recorder.model = "mock-deep"
    cfg.recorder.fallback = None
    return cfg


@dataclass
class Availability:
    provider: str
    ok: bool
    detail: str


# --------------------------------------------------------------------------- #
# 配置不变量
# --------------------------------------------------------------------------- #
def check_invariants(cfg: GroupConfig) -> list[str]:
    """检查配置里必须成立、但容易被后人改坏的规则，返回违规说明列表（空 = 全部满足）。

    不变量 1（跨厂商去相关）：**任何成员的 fallback 目标不得与主路由同厂**。
    否则「无 DeepSeek key 时组长降级」这种情况会让整个群塌缩成同一家厂商自辩，
    外观却完全正常——这是会让人误信结论的高危缺陷。
    """
    problems: list[str] = []
    for agent in [*cfg.agents, cfg.recorder]:
        if not agent.enabled or not agent.fallback:
            continue
        fallback_provider = str(agent.fallback.get("provider", "")).strip()
        if agent.provider == "mock" or fallback_provider == "mock":
            continue  # 离线通道只是演示，不构成「厂商」同源问题
        if fallback_provider and fallback_provider == agent.provider:
            problems.append(
                f"{agent.id}: fallback 与主路由同厂（{agent.provider} → {agent.route}），"
                "降级会退化成同厂自辩；请把 fallback 指向另一家厂商。"
            )
        if fallback_provider and fallback_provider not in cfg.providers:
            problems.append(
                f"{agent.id}: fallback provider `{fallback_provider}` 未在 providers.yaml 中声明。"
            )
    return problems


def probe_availability(cfg: GroupConfig) -> list[Availability]:
    """列出各 provider 的可用性（只做密钥存在性判断，不发请求）。"""
    result: list[Availability] = []
    for pid, provider in cfg.providers.items():
        if provider.is_mock:
            result.append(Availability(pid, True, "离线通道，无需密钥"))
            continue
        if not provider.base_url or not provider.models:
            result.append(Availability(pid, False, "缺少 base_url 或 models 声明"))
            continue
        key, source = resolve_credential(provider.credentials)
        result.append(
            Availability(pid, bool(key), f"密钥来源 {source}" if key else source)
        )
    return result
