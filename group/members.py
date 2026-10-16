"""用户在界面上增删群成员：预设提供商、写 models.yaml / providers.yaml / .env。

设计原则（和 update_member_role 一致）：
* **只做行级手术**，不整份重写 YAML —— 那会丢掉配置里的大段注释；
* 凭据不写进 YAML，只写 ``.env``（config._load_dotenv 会把它注入 os.environ，
  resolve_credential 照常解析），这样 models.yaml 里永远没有明文密钥；
* 每个成员**一份独立的角色提示词文件** —— 这是项目「一人一 Agent、一人一提示词」
  的硬要求，自检里有一条不变量专门校验它。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .config import (

    CONFIG_DIR,
    MEMBERS_FILE,
    PROJECT_ROOT,
    ROLES_DIR,
    GroupConfig,
    reload_config,
)

PROVIDERS_FILE = CONFIG_DIR / "providers.yaml"
ENV_FILE = PROJECT_ROOT / ".env"


# --------------------------------------------------------------------------- #
# 预设提供商：用户只需填 API Key，其余都替他填好
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Preset:
    id: str
    label: str
    base_url: str
    env: str
    models: tuple[str, ...]
    note: str = ""


PRESETS: tuple[Preset, ...] = (
    Preset(
        "deepseek", "DeepSeek", "https://api.deepseek.com/v1",
        "DEEPSEEK_API_KEY",
        ("deepseek-v4-flash", "deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"),
        "便宜，非思考型，适合当组长",
    ),
    Preset(
        "moonshotai-cn", "Kimi（Moonshot 国内站）", "https://api.moonshot.cn/v1",
        "MOONSHOTAI_CN_API_KEY",
        ("kimi-k2.6", "kimi-k2.7-code"),
        "深思模型：答几十字前会写上千字推理，慢且贵；账户是组织级 3 请求/分钟",
    ),
    Preset(
        "doubao", "豆包（火山方舟）", "https://ark.cn-beijing.volces.com/api/v3",
        "ARK_API_KEY",
        ("doubao-seed-2-1-pro-260915", "doubao-seed-2-1-turbo-260628",
         "doubao-seed-2-1-lite-260915", "deepseek-v3-2-251201",
         "glm-4-7-251222", "qwen3-32b-20250429"),
        "一个 key 覆盖方舟上的多家模型（豆包 / DeepSeek / GLM / Qwen 等 135 个）",
    ),
    Preset(
        "zhipu", "智谱 GLM", "https://open.bigmodel.cn/api/paas/v4",
        "ZHIPU_API_KEY",
        # 2026-10-13 用真实账号调 GET /models 拿到的清单（11 个），
        # 之前写的 glm-4-flash / glm-4-air / glm-4-plus 已经不在其中了。
        ("glm-5.3-flash", "glm-5.3-flashx", "glm-5.3", "glm-5.2", "glm-5.1",
         "glm-5-turbo", "glm-5", "glm-4.7", "glm-4.6", "glm-4.5", "glm-4.5-air"),
        "glm-5.3-flash 便宜；glm-5.3 更强。模型 id 大小写不敏感",
    ),
    Preset(
        # 2026-10-14 实测：端点 https://api.xiaomimimo.com/v1，
        # GET /models 返回 9 个（含 4 个 TTS/ASR 语音模型，不是对话模型），
        # chat/completions 实测可用。Key 形状 sk- + 48 位，与 Moonshot 撞形状，
        # 所以只能靠探测区分，不能靠格式。
        "mimo", "小米 MiMo", "https://api.xiaomimimo.com/v1",
        "MIMO_API_KEY",
        ("mimo-v2.6-pro", "mimo-v2.6-flash", "mimo-v2.6-pro-ultraspeed",
         "mimo-v2.5", "mimo-v2.5-pro"),
        "小米 MiMo；思考型模型（会返回 reasoning_content）",
    ),
    Preset(
        "qwen", "通义千问（阿里云百炼）", "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "DASHSCOPE_API_KEY",
        ("qwen-turbo", "qwen-plus", "qwen-max"),
        "新账号有免费额度",
    ),
    Preset(
        "openai", "OpenAI", "https://api.openai.com/v1",
        "OPENAI_API_KEY",
        ("gpt-5.1", "gpt-5.1-mini"),
        "国内直连需要自备代理",
    ),
    Preset(
        "custom", "自定义（任意 OpenAI 兼容端点）", "", "", (),
        "本地 Ollama / LM Studio / vLLM，或任何第三方网关",
    ),
)

PRESET_BY_ID = {p.id: p for p in PRESETS}

# 新建成员可选的角色提示词模板（决定「他干什么活」的说话方式）
ROLE_TEMPLATES = (
    ("member_analyst.md", "论证担当", "长文论证 · 深挖依据"),
    ("member_critic.md", "反驳担当", "快速反驳 · 挑风险"),
    ("member_engineer.md", "工程落地担当", "能不能真跑 · 要付多少代价"),
)

# 新成员的块首标记注释：删除时靠它认领「这一块是我的」
MEMBER_MARKER = "  # 由界面「添加成员」写入"
_ID_LINE_RE = re.compile(r"^\s*-\s*id:\s*([^\s#]+)\s*$")


class MemberError(ValueError):
    """入参不合法（会被接口映射成 400）。"""


def presets_payload() -> list[dict]:
    """给前端的预设列表。不回显任何密钥，只报告该提供商当前有没有可用凭据。"""
    from .config import load_config, resolve_credential

    cfg = load_config()
    out = []
    for preset in PRESETS:
        has_key = False
        if preset.id in cfg.providers:
            prov = cfg.providers[preset.id]
            key, _ = resolve_credential(prov.credentials)
            has_key = bool(key)
        out.append(
            {
                "id": preset.id,
                "label": preset.label,
                "baseUrl": preset.base_url,
                "models": list(preset.models),
                "note": preset.note,
                "hasKey": has_key,
                "existing": preset.id in cfg.providers,
            }
        )
    return out


def _slug_id(name: str, taken: set[str]) -> str:
    """把显示名转成成员 id。

    **保留中文**。@提及 的正则（config._MENTION_RE）本来就带 `\u4e00-\u9fff`，
    `@千问` 完全解析得出来。早先这里把中文全滤掉、退化成 `member`，
    结果成员叫「千问」、@ 出来却是 `@member`，非常反直觉（用户实际反馈过）。
    """
    # 字符类与 _MENTION_RE 保持一致：中英文数字 + _ - .
    base = re.sub(r"[^A-Za-z0-9_\-.\u4e00-\u9fff]+", "", name.lower()).strip("-_.")
    # 只剩数字同样要退化：`- id: 1` 会被 YAML 读成整数 1，成员 id 变成 int，
    # 之后所有按字符串比较的地方（查找、删除、@ 匹配）都会失配。
    if not base or base.isdigit():
        base = "member"
    candidate, index = base, 2
    while candidate in taken:
        candidate = f"{base}{index}"
        index += 1
    return candidate


def _taken_ids() -> set[str]:
    from .config import load_config

    cfg = load_config()
    return {a.id for a in cfg.agents} | {"captain", "recorder", "user"}


def _append_provider(preset: Preset) -> None:
    """把新提供商追加到 providers.yaml（已存在则跳过）。"""
    text = PROVIDERS_FILE.read_text(encoding="utf-8")
    if re.search(rf"^\s{{2}}{re.escape(preset.id)}:\s*$", text, re.MULTILINE):
        return
    block = (
        f"\n  # 由界面「添加成员」写入（{preset.label}）\n"
        f"  {preset.id}:\n"
        f"    label: {preset.label}\n"
        f"    api: openai-chat\n"
        f"    base_url: {preset.base_url}\n"
        f"    credentials:\n"
        f"      - env: {preset.env}\n"
        f"    models:\n"
        + "".join(
            f"      {m}:\n        label: {m}\n        context_window: 131072\n"
            f"        capabilities: [chat]\n"
            for m in preset.models
        )
    )
    with PROVIDERS_FILE.open("a", encoding="utf-8", newline="") as handle:
        handle.write(block)


def _append_custom_provider(provider_id: str, label: str, base_url: str, model: str) -> None:
    block = (
        f"\n  # 由界面「添加成员」写入（自定义端点）\n"
        f"  {provider_id}:\n"
        f"    label: {label}\n"
        f"    api: openai-chat\n"
        f"    base_url: {base_url}\n"
        f"    credentials:\n"
        f"      - env: {provider_id.upper().replace('-', '_')}_API_KEY\n"
        f"    models:\n"
        f"      {model}:\n        label: {model}\n        context_window: 131072\n"
        f"        capabilities: [chat]\n"
    )
    with PROVIDERS_FILE.open("a", encoding="utf-8", newline="") as handle:
        handle.write(block)


def _write_env(env_name: str, api_key: str) -> None:
    """把密钥写进 .env（**不写进 YAML**）。已存在的同名键就地替换。

    写完**立刻注入当前进程的 os.environ** —— 这一点必须做，否则会出一个
    很坑的现象：用户在界面上填了 Key、点添加、成员也加进群了，但这一轮发言
    仍然报「provider 缺少可用密钥」。

    原因：``config._load_dotenv()`` 只在**模块导入时**跑一次，而界面添加成员是
    **运行中**才把 Key 写进 .env 的。不注入就得重启应用才生效，用户会以为没配上。
    （这里刻意覆盖已有值：用户刚填的应该赢，而不是被进程里可能存在的旧值挡住。）
    """
    if not env_name or not api_key:
        return
    os.environ[env_name] = api_key
    line = f"{env_name}={api_key}"
    existing = ENV_FILE.read_text(encoding="utf-8").splitlines(keepends=True) if ENV_FILE.exists() else []
    pattern = re.compile(rf"^\s*{re.escape(env_name)}\s*=")
    for index, row in enumerate(existing):
        if pattern.match(row):
            existing[index] = line + "\n"
            break
    else:
        if existing and not existing[-1].endswith("\n"):
            existing[-1] += "\n"
        existing.append(line + "\n")
    ENV_FILE.write_text("".join(existing), encoding="utf-8", newline="")


def _write_role_file(agent_id: str, template: str, display: str, duty: str,
                     custom_prompt: str = "") -> str:
    """为新成员生成一份**独立**的角色提示词（自检要求一人一份、互不相同）。

    ``custom_prompt`` 非空时**直接用它当正文** —— 这就是「界面上直接写提示词」
    的落地方式，用户不必再去 config/roles/ 里改文件。
    """
    custom = (custom_prompt or "").strip()
    if custom:
        body = custom if custom.endswith("\n") else custom + "\n"
        lines = body.splitlines()
        # 没有标题就补一个，方便人在文件夹里一眼认出这是谁
        if not lines or not lines[0].startswith("#"):
            lines.insert(0, f"# 角色：{display}")
    else:
        source = ROLES_DIR / template
        body = source.read_text(encoding="utf-8") if source.exists() else "# 角色：组员\n"
        # 把标题行换成这位成员自己的，并补一行职责说明 —— 这样每份文件都不同
        lines = body.splitlines()
        if lines and lines[0].startswith("#"):
            lines[0] = f"# 角色：{display}（{duty or '组员'}）"
        else:
            lines.insert(0, f"# 角色：{display}（{duty or '组员'}）")
    lines.insert(1, f"\n> 本文件由界面生成，专属于成员 `{agent_id}`。可直接改，也可在界面里改。\n")
    name = f"member_{agent_id}.md"
    # 防护：如果算出来的文件名恰好等于模板文件名（例如 id 被 slug 成 analyst、
    # 目标是 member_analyst.md），直接写过去会把**模板本身**覆盖掉 —— 那会连累
    # 所有引用这个模板的成员。所以这种碰撞必须改道。
    if name == template:
        name = f"member_{agent_id}_custom.md"
    target = ROLES_DIR / name
    if target.exists() and name != f"member_{agent_id}.md":
        name = f"member_{agent_id}_v2.md"
        target = ROLES_DIR / name
    target.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="")
    return name


def _member_block(
    *, agent_id: str, display: str, avatar: str, duty: str, role: str,
    role_file: str, provider: str, model: str, fallback: dict | None,
) -> str:
    keep = max(2, min(6, 2))
    # 块首**不带空行** —— 空行由插入位置决定。
    # 带空行会让「加完再删」多留一行空白，也让删除的向上回溯更难判断边界。
    lines = [
        f"{MEMBER_MARKER}\n",
        f"  - id: {agent_id}\n",
        f"    display_name: {display}\n",
        f"    avatar: {avatar}\n",
        f"    duty: {duty}\n",
        f"    role: {role}\n",
        f"    role_file: {role_file}\n",
        f"    provider: {provider}\n",
        f"    model: {model}\n",
    ]
    if fallback:
        lines.append("    fallback:\n")
        lines.append(f"      provider: {fallback['provider']}\n")
        lines.append(f"      model: {fallback['model']}\n")
    lines += [
        "    temperature: 0.7\n",
        # max_tokens 是上限不是预留：调大不花钱，但调太小会被「思考」吃光、正文为空
        "    max_tokens: 3000\n",
        f"    max_turns_per_run: {keep}\n",
        "    can_dispatch: true\n",
        f"    tags: [{agent_id}]\n",   # 标签全局唯一：直接用 id
        "    enabled: true\n",
    ]
    return "".join(lines)


def _default_fallback(provider: str) -> dict | None:
    """给新成员配一条**跨厂商** fallback（自检不变量：fallback 不得与主路由同厂）。"""
    from .config import load_config

    cfg = load_config()
    usable = []
    for pid, prov in cfg.providers.items():
        if pid == provider or prov.is_mock:
            continue
        usable.append(pid)
    # 优先挑已配好密钥的
    from .config import resolve_credential

    for pid in usable:
        key, _ = resolve_credential(cfg.providers[pid].credentials)
        if key:
            models = list(cfg.providers[pid].models)
            if models:
                return {"provider": pid, "model": models[0]}
    if "mock" in cfg.providers:
        return {"provider": "mock", "model": "mock-fast"}
    return None


def add_member(
    *,
    name: str,
    provider: str,
    model: str,
    base_url: str = "",
    api_key: str = "",
    role: str = "member",
    duty: str = "",
    avatar: str = "",
    template: str = "",
    role_prompt: str = "",
) -> tuple[GroupConfig, dict]:
    """新增一位成员，返回 ``(新配置, 新增成员的信息)``。

    ``role_prompt`` 非空时直接用它当系统提示词（界面里写的），
    否则按 ``template`` 从模板生成一份。
    """
    name = (name or "").strip()
    model = (model or "").strip()
    provider = (provider or "").strip()
    if not name:
        raise MemberError("请填写成员名字")
    # 显示名里有空白会让 @提及 失效（抽取时按空白切词），必须挡住
    if re.search(r"\s", name):
        raise MemberError("成员名字里不能有空格（否则 @提及 会失效）")
    if not model:
        raise MemberError("请填写模型 id")
    if role not in ("member", "captain"):
        raise MemberError("role 只能是 member 或 captain")

    from .config import load_config

    cfg = load_config()
    taken = _taken_ids()
    if name.lower() in {a.display_name.lower() for a in cfg.agents}:
        raise MemberError(f"已经有一个叫「{name}」的成员了，换个名字")

    # 1) 提供商
    if provider == "custom":
        if not base_url.strip():
            raise MemberError("自定义端点必须填接口地址（base_url）")
        provider_id = re.sub(r"[^a-z0-9_-]+", "", (name.lower() or "custom")) or "custom"
        if provider_id in cfg.providers:
            provider_id = f"{provider_id}-{len(cfg.providers)}"
        _append_custom_provider(provider_id, name, base_url.strip(), model)
        env_name = f"{provider_id.upper().replace('-', '_')}_API_KEY"
    else:
        preset = PRESET_BY_ID.get(provider)
        if preset is None:
            raise MemberError(f"未知的提供商：{provider}")
        provider_id = provider
        env_name = preset.env
        if provider_id not in cfg.providers:
            _append_provider(preset)

    # 2) 密钥 → 只进 .env
    if api_key.strip():
        _write_env(env_name, api_key.strip())

    # 3) 角色提示词：一人一份
    agent_id = _slug_id(name, taken)
    template_file = template if template in {t[0] for t in ROLE_TEMPLATES} else "member_analyst.md"
    # 只有「用模板生成」时才套模板的默认职责。
    # 用户自己写了提示词，还套一个「长文论证 · 深挖依据」上去就是误导 ——
    # 那是模板的职责，不是这位成员的实际职责；留给 _role_brief 从提示词里摘。
    if (role_prompt or "").strip():
        duty = (duty or "").strip()
    else:
        duty = (duty or "").strip() or next(
            (t[2] for t in ROLE_TEMPLATES if t[0] == template_file), "组员"
        )
    role_file = _write_role_file(agent_id, template_file, name, duty, role_prompt)

    # 4) 成员条目
    avatar = (avatar or "").strip() or "🙂"
    block = _member_block(
        agent_id=agent_id, display=name, avatar=avatar, duty=duty, role=role,
        role_file=role_file, provider=provider_id, model=model,
        fallback=_default_fallback(provider_id),
    )
    # 注意：不能直接 append 到文件末尾 —— 末尾可能有 limits: 段（见 _member_insert_index）
    _append_member_block(block)

    if role == "captain":
        # 保证全局只有一个组长
        from .config import update_member_role

        for other in cfg.agents:
            if other.enabled and other.role == "captain" and other.id != agent_id:
                update_member_role(other.id, "member")

    new_cfg = reload_config()
    created = new_cfg.by_id(agent_id)
    if created is None:
        raise RuntimeError("成员已写入 models.yaml，但重新装载后没有生效")
    return new_cfg, {
        "id": agent_id,
        "name": created.display_name,
        "route": created.route,
        "role": created.role,
        "hasKey": bool(api_key.strip()) or _provider_has_key(new_cfg, created.provider),
    }


def _block_bounds(lines: list[str], agent_id: str) -> tuple[int, int] | None:
    """返回该成员块的 ``[start, end)``，找不到返回 None。

    与删除共用同一套边界算法（含那个「回溯后 search_from 要固定」的坑），
    所以单独抽出来，避免两处各写一遍又各踩一次。
    """
    id_line = None
    for index, line in enumerate(lines):
        matched = _ID_LINE_RE.match(line)
        if matched and matched.group(1).strip("\"'") == agent_id:
            id_line = index
            break
    if id_line is None:
        return None
    scan_from = id_line + 1
    start = id_line
    while start > 0 and lines[start - 1].lstrip().startswith("#"):
        start -= 1
    end = len(lines)
    for index in range(scan_from, len(lines)):
        if _ID_LINE_RE.match(lines[index]) or re.match(r"^\S", lines[index]):
            end = index
            break
    return start, end


_FIELD_RE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$")


def _set_block_field(lines: list[str], agent_id: str, key: str, value: str) -> bool:
    """把某成员块里的 ``key:`` 一行改成 ``value``，返回是否命中。"""
    bounds = _block_bounds(lines, agent_id)
    if bounds is None:
        return False
    start, end = bounds
    for index in range(start, end):
        matched = _FIELD_RE.match(lines[index])
        if matched and matched.group(2) == key and len(matched.group(1)) == 4:
            lines[index] = f"    {key}: {value}\n"
            return True
    return False


def read_role_prompt(agent_id: str) -> str:
    """读出某位成员当前的系统提示词正文（编辑界面预填用）。"""
    from .config import load_config

    cfg = load_config()
    agent = cfg.by_id(agent_id)
    if agent is None:
        raise KeyError(agent_id)
    path = ROLES_DIR / agent.role_file
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def write_role_prompt(agent_id: str, text: str) -> str:
    """把某位成员的系统提示词整篇覆盖掉，返回写入了哪个文件。"""
    from .config import load_config

    cfg = load_config()
    agent = cfg.by_id(agent_id)
    if agent is None:
        raise KeyError(agent_id)
    body = (text or "").strip()
    if not body:
        raise MemberError("提示词不能为空")
    if not body.endswith("\n"):
        body += "\n"
    path = ROLES_DIR / agent.role_file
    path.write_text(body, encoding="utf-8", newline="")
    return agent.role_file


def update_member(
    agent_id: str,
    *,
    name: str = "",
    model: str = "",
    api_key: str = "",
    base_url: str = "",
    duty: str = "",
    avatar: str = "",
    max_turns_per_run: int | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    role_prompt: str = "",
) -> tuple[GroupConfig, dict]:
    """修改一个已存在成员的信息。**只改传进来的字段**，其余保持不动。

    用户最常用的场景是**换 API Key**（原来那个欠费了/想换账号）——
    此时只传 api_key 即可，其余一律不碰。
    """
    from .config import load_config

    cfg = load_config()
    target = cfg.by_id(agent_id)
    if target is None:
        raise KeyError(agent_id)

    name = (name or "").strip()
    if name:
        if re.search(r"\s", name):
            raise MemberError("成员名字里不能有空格（否则 @提及 会失效）")
        clash = [
            a for a in cfg.agents
            if a.id != agent_id and a.display_name.lower() == name.lower()
        ]
        if clash:
            raise MemberError(f"已经有一个叫「{name}」的成员了，换个名字")

    lines = MEMBERS_FILE.read_text(encoding="utf-8").splitlines(keepends=True)
    if _block_bounds(lines, agent_id) is None:
        raise MemberError(f"models.yaml 里找不到成员 {agent_id}")

    changed: list[str] = []

    def apply(key: str, value: str) -> None:
        if _set_block_field(lines, agent_id, key, value):
            changed.append(key)

    if name:
        apply("display_name", name)
    if model.strip():
        apply("model", model.strip())
    if duty.strip():
        apply("duty", duty.strip())
    if avatar.strip():
        apply("avatar", avatar.strip())
    if max_turns_per_run is not None:
        if not 1 <= int(max_turns_per_run) <= 20:
            raise MemberError("每轮发言次数要在 1~20 之间")
        apply("max_turns_per_run", str(int(max_turns_per_run)))
    if temperature is not None:
        if not 0 <= float(temperature) <= 2:
            raise MemberError("temperature 要在 0~2 之间")
        apply("temperature", str(float(temperature)))
    if max_tokens is not None:
        if not 256 <= int(max_tokens) <= 200000:
            raise MemberError("max_tokens 要在 256~200000 之间")
        apply("max_tokens", str(int(max_tokens)))

    # 换 Key：写进 .env（并立刻注入当前进程），不动 YAML
    if api_key.strip():
        provider_id = target.provider
        preset = PRESET_BY_ID.get(provider_id)
        if preset:
            env_name = preset.env
        else:
            env_name = f"{provider_id.upper().replace('-', '_')}_API_KEY"
        _write_env(env_name, api_key.strip())
        changed.append(f"key({env_name})")

    if base_url.strip():
        # 改提供商地址：providers.yaml 里那一行
        text = PROVIDERS_FILE.read_text(encoding="utf-8")
        pattern = re.compile(
            rf"(^  {re.escape(target.provider)}:\s*\n(?:.*\n)*?    base_url:\s*).*$",
            re.MULTILINE,
        )
        new_text, n = pattern.subn(lambda m: m.group(1) + base_url.strip(), text, count=1)
        if n:
            PROVIDERS_FILE.write_text(new_text, encoding="utf-8", newline="")
            changed.append("base_url")

    # 提示词整篇覆盖：不走 YAML，直接改角色文件
    if role_prompt.strip():
        write_role_prompt(agent_id, role_prompt)
        changed.append("role_prompt")

    if not changed:
        raise MemberError("没有要修改的内容")

    with MEMBERS_FILE.open("w", encoding="utf-8", newline="") as handle:
        handle.write("".join(lines))

    new_cfg = reload_config()
    updated = new_cfg.by_id(agent_id)
    if updated is None:
        raise RuntimeError("成员已写入 models.yaml，但重新装载后没有生效")
    return new_cfg, {
        "id": agent_id,
        "name": updated.display_name,
        "route": updated.route,
        "changed": changed,
        "hasKey": _provider_has_key(new_cfg, updated.provider),
    }


# --------------------------------------------------------------------------- #
# 额度护栏（单轮发言数 / @ 深度 / 上下文上限）—— 可调并持久化
# --------------------------------------------------------------------------- #
LIMIT_KEYS = ("max_messages", "max_depth", "max_context_chars",
              "max_accept_rounds", "max_parallel")

LIMIT_RANGE = {
    "max_messages": (2, 60),
    "max_depth": (1, 8),
    # 下限不能低于「角色提示词 + 名单 + 输出纪律」的长度：system 本身就占预算，
    # 设得比它还小在物理上不可能满足（实测 captain 的 system ≈ 2830 字）。
    # 4000 能保证 system 之外还给聊天记录留出余量，不会一开场就顶死。
    "max_context_chars": (4000, 60000),
    # 0 = 关闭验收。上限给 5：再多就不是"验收"而是"反复返工"了。
    "max_accept_rounds": (0, 5),
    # 1 = 串行。上限 5：再多也不是"并行"而是"一群人抢麦"了。
    "max_parallel": (1, 5),
}

LIMIT_LABEL = {
    "max_messages": "单轮最多发言",
    "max_depth": "@ 触发深度",
    "max_context_chars": "上下文上限（字）",
    "max_accept_rounds": "组长验收轮数（0 = 关闭）",
    "max_parallel": "同层并发数（1 = 串行）",
}


def limits_view() -> dict:
    """当前生效的护栏值 + 取值范围，给界面的设置面板用。"""
    from .config import load_config

    cfg = load_config()
    out = {}
    for key in LIMIT_KEYS:
        out[key] = {
            "value": int(getattr(cfg.limits, key)),
            "min": LIMIT_RANGE[key][0],
            "max": LIMIT_RANGE[key][1],
            "label": LIMIT_LABEL[key],
        }
    return out


def update_limits(values: dict) -> tuple[GroupConfig, dict]:
    """改写 models.yaml 末尾 ``limits:`` 段里的数值。"""
    from .config import load_config

    clean: dict[str, int] = {}
    for key, raw in (values or {}).items():
        if key not in LIMIT_KEYS:
            continue
        try:
            number = int(raw)
        except (TypeError, ValueError):
            raise MemberError(f"{LIMIT_LABEL[key]} 必须是整数") from None
        low, high = LIMIT_RANGE[key]
        if not low <= number <= high:
            raise MemberError(f"{LIMIT_LABEL[key]} 要在 {low}~{high} 之间")
        clean[key] = number
    if not clean:
        raise MemberError("没有要修改的护栏项")

    cfg = load_config()
    lines = MEMBERS_FILE.read_text(encoding="utf-8").splitlines(keepends=True)
    start = None
    for index, line in enumerate(lines):
        if re.match(r"^limits:\s*$", line):
            start = index
            break

    if start is None:
        # 文件里还没有 limits: 段 → 追加一段
        while lines and not lines[-1].strip():
            lines.pop()
        lines.append("\n# 单轮额度护栏（界面「设置」可改；改这里也行）\n")
        lines.append("limits:\n")
        for key in LIMIT_KEYS:
            value = clean.get(key, int(getattr(cfg.limits, key)))
            lines.append(f"  {key}: {value}\n")
    else:
        end = len(lines)
        for index in range(start + 1, len(lines)):
            if re.match(r"^\S", lines[index]):
                end = index
                break
        present = set()
        for index in range(start + 1, end):
            matched = _FIELD_RE.match(lines[index])
            if matched and matched.group(2) in clean:
                lines[index] = f"  {matched.group(2)}: {clean[matched.group(2)]}\n"
                present.add(matched.group(2))
        missing = [k for k in clean if k not in present]
        if missing:
            lines.insert(end, "".join(f"  {k}: {clean[k]}\n" for k in missing))

    with MEMBERS_FILE.open("w", encoding="utf-8", newline="") as handle:
        handle.write("".join(lines))

    new_cfg = reload_config()
    return new_cfg, limits_view()


# --------------------------------------------------------------------------- #
# models.yaml 的安全写入
# --------------------------------------------------------------------------- #
# 事故复盘（2026-10-14）：models.yaml 末尾有 `limits:` 段之后，「往文件末尾追加成员」
# 会把成员块追加进 limits 里面，YAML 结构被打断，配置整体读不出来。
# 所以追加成员必须插在** agents 列表的最后**，也就是下一个顶层键（limits: 等）之前。
_TOP_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*:")


def _member_insert_index(lines: list[str]) -> int:
    """返回「新成员块应该插在第几行」——agents 列表末尾、下一个顶层段之前。

    做法：找到 ``agents:`` 之后第一个**顶格键**（例如 ``limits:``），
    再往上跳过**顶格注释**（那是给那个段写的说明，不能插到它和键之间），
    插在那个注释之前。这样：

      * 新成员落在 agents 列表内部（不会跑进 limits 段里）；
      * 不会插进别人注释块的中间；
      * 删除时能原样还原（见 remove_member）。
    """
    seen_agents = False
    for index, line in enumerate(lines):
        if re.match(r"^agents\s*:", line):
            seen_agents = True
            continue
        if not seen_agents:
            continue
        if _TOP_KEY_RE.match(line):
            cut = index
            # 只跳过**顶格**注释（^#），不跳缩进注释 —— 那些属于上一个成员的块
            while cut > 0 and lines[cut - 1].startswith("#"):
                cut -= 1
            return cut
    return len(lines)


def _append_member_block(block: str) -> None:
    """把成员块插到 agents 列表末尾，并在写盘后校验 YAML；坏了就回滚。"""
    original = MEMBERS_FILE.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    index = _member_insert_index(lines)
    lines.insert(index, block)
    text = "".join(lines)

    # 写盘前先在内存里验一次，避免把坏文件落盘
    try:
        import yaml as _yaml

        _yaml.safe_load(text)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"新增成员会破坏 models.yaml 的结构，已中止：{exc}") from exc

    MEMBERS_FILE.write_text(text, encoding="utf-8", newline="")

    # 再读回来确认（防止写盘过程本身出问题）
    try:
        import yaml as _yaml

        _yaml.safe_load(MEMBERS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        MEMBERS_FILE.write_text(original, encoding="utf-8", newline="")
        raise RuntimeError(f"models.yaml 写入后校验失败，已回滚：{exc}") from exc


# --------------------------------------------------------------------------- #
# 自定义头像（文件存储，与角色提示词同一套路）
# --------------------------------------------------------------------------- #
AVATAR_DIR = CONFIG_DIR / "avatars"
# 单个头像 dataURL 的上限。4MB 够放一张 1080p 的 PNG，
# 再大就该让用户自己压一下了 —— 反正它现在是文件，不再受 localStorage 5MB 配额限制。
AVATAR_MAX_CHARS = 4_000_000

# mime → 扩展名。只认常见的几种图片格式。
_AVATAR_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/bmp": "bmp",
    "image/svg+xml": "svg",
    "image/x-icon": "ico",
    "image/vnd.microsoft.icon": "ico",
}


def _avatar_stem(target: str) -> str:
    """文件名主干。用户自己用 user，成员用其 id。"""
    t = (target or "").strip()
    return "user" if t in ("", "user", "我") else t


def _avatar_files(target: str) -> list[Path]:
    """某个目标可能存在的头像文件（扩展名不定，所以按主干列出）。"""
    stem = _avatar_stem(target)
    if not AVATAR_DIR.is_dir():
        return []
    return sorted(AVATAR_DIR.glob(f"custom_{stem}.*"))


def _split_data_url(data_url: str) -> tuple[str, str]:
    """把 dataURL 拆成 (mime, base64)。不是 dataURL 就抛 MemberError。"""
    text = (data_url or "").strip()
    if not text.startswith("data:"):
        raise MemberError("头像格式不对：需要 data:image/... 开头的 dataURL")
    if len(text) > AVATAR_MAX_CHARS:
        raise MemberError(
            f"图片太大了（{len(text) // 1024} KB）。请换一张小一点的，或先压缩一下。"
        )
    head, _, payload = text.partition(",")
    if not payload:
        raise MemberError("头像数据是空的")
    mime = head[5:].split(";")[0].strip().lower() or "image/png"
    if mime not in _AVATAR_EXT:
        raise MemberError(f"不支持的图片格式：{mime}")
    return mime, payload


def save_avatar(target: str, data_url: str) -> tuple[str, str]:
    """存一张自定义头像，返回 ``(相对路径, 文件名)``。

    成员的头像路径会同时写进 models.yaml 的 ``avatar:`` 字段 ——
    这样配置里一眼能看出谁用的是什么头像，拷贝 config/ 也能带走。
    """
    import base64

    from .config import load_config

    stem = _avatar_stem(target)
    is_member = stem != "user"
    if is_member and load_config().by_id(stem) is None:
        raise KeyError(stem)

    mime, payload = _split_data_url(data_url)
    ext = _AVATAR_EXT[mime]
    try:
        raw = base64.b64decode(payload, validate=False)
    except Exception as exc:  # noqa: BLE001
        raise MemberError(f"图片数据解不开：{exc}") from exc
    if not raw:
        raise MemberError("图片数据是空的")

    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    # 换格式时把旧文件清掉，免得 .png/.jpg 两份同时留着，读的时候挑中旧的那张
    for old in _avatar_files(stem):
        if old.suffix.lstrip(".") != ext:
            try:
                old.unlink()
            except OSError:
                pass
    name = f"custom_{stem}.{ext}"
    (AVATAR_DIR / name).write_bytes(raw)
    rel = f"avatars/{name}"

    if is_member:
        lines = MEMBERS_FILE.read_text(encoding="utf-8").splitlines(keepends=True)
        if _set_block_field(lines, stem, "avatar", rel):
            with MEMBERS_FILE.open("w", encoding="utf-8", newline="") as handle:
                handle.write("".join(lines))
            reload_config()
    return rel, name


def clear_avatar(target: str) -> bool:
    """删掉自定义头像。成员的话，把 models.yaml 里的 avatar 清空（回落到文字/缩写）。"""
    from .config import load_config

    stem = _avatar_stem(target)
    is_member = stem != "user"
    if is_member and load_config().by_id(stem) is None:
        raise KeyError(stem)

    removed = False
    for path in _avatar_files(stem):
        try:
            path.unlink()
            removed = True
        except OSError:
            pass

    if is_member:
        lines = MEMBERS_FILE.read_text(encoding="utf-8").splitlines(keepends=True)
        if _set_block_field(lines, stem, "avatar", '""'):
            with MEMBERS_FILE.open("w", encoding="utf-8", newline="") as handle:
                handle.write("".join(lines))
            reload_config()
    return removed


def list_avatars() -> dict:
    """列出所有自定义头像，值是可以直接塞进 <img src> 的 dataURL。

    一次性全返回：张数很少（就几个成员 + 用户自己），
    比让前端逐个请求简单，也省得页面出现"头像一张张蹦出来"的观感。
    """
    import base64

    from .config import load_config

    out: dict[str, str] = {}
    if not AVATAR_DIR.is_dir():
        return out
    known = {a.id for a in load_config().agents}
    for path in sorted(AVATAR_DIR.glob("custom_*.*")):
        stem = path.stem[len("custom_"):]
        if stem != "user" and stem not in known:
            # 成员已删除，但头像文件还留着 —— 不返回，也顺手清掉
            try:
                path.unlink()
            except OSError:
                pass
            continue
        ext = path.suffix.lstrip(".").lower()
        mime = next((k for k, v in _AVATAR_EXT.items() if v == ext), "image/png")
        try:
            data = path.read_bytes()
        except OSError:
            continue
        out[stem] = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    return out


def _provider_has_key(cfg: GroupConfig, provider_id: str) -> bool:
    from .config import resolve_credential

    prov = cfg.providers.get(provider_id)
    if prov is None:
        return False
    if prov.is_mock:
        return True
    key, _ = resolve_credential(prov.credentials)
    return bool(key)


def remove_member(agent_id: str) -> tuple[GroupConfig, str]:
    """从 models.yaml 里删掉一个成员的整块（连同它的角色提示词文件）。"""
    from .config import load_config

    cfg = load_config()
    target = cfg.by_id(agent_id)
    if target is None:
        raise KeyError(agent_id)
    if target.is_recorder:
        raise MemberError("记录员不能删除")
    if target.role == "captain":
        others = [a for a in cfg.agents if a.enabled and a.role == "captain" and a.id != agent_id]
        if not others:
            raise MemberError("群里必须保留一个组长：请先把另一位成员设为组长，再删除当前组长。")

    lines = MEMBERS_FILE.read_text(encoding="utf-8").splitlines(keepends=True)
    start = None
    for index, line in enumerate(lines):
        matched = _ID_LINE_RE.match(line)
        if matched and matched.group(1).strip("\"'") == agent_id:
            start = index
            break
    if start is None:
        raise MemberError(f"models.yaml 里找不到成员 {agent_id}")

    # 往上吃掉紧邻的注释行（那是这个成员的说明），往下吃到下一个成员之前。
    #
    # 注意：向下的搜索必须从**原始 id 行的下一行**开始，不能被上面的回溯影响。
    # 踩过的坑：回溯把 start 从 id 行挪到它上面那行注释后，若仍用 start+1 当起点，
    # 起点正好又落回 id 行本身 —— 它立刻匹配 `- id:` 让 end 停在自己身上，
    # 结果只删掉一行注释、成员纹丝不动（表现为「删了但还在」）。
    scan_from = start + 1
    # 只认领**紧贴在自己上面、且是本功能写下的那行标记注释**。
    # 早先的写法是「向上吃掉所有注释行」，结果把 limits 段上方的说明注释
    # 也一起吞了 —— 删一个成员却改坏了别的段落。
    if start > 0 and lines[start - 1].strip() == MEMBER_MARKER.strip():
        start -= 1
    end = len(lines)
    for index in range(scan_from, len(lines)):
        if _ID_LINE_RE.match(lines[index]) or re.match(r"^\S", lines[index]):
            end = index
            break
    # 写盘前自检：算出的区间必须真的覆盖那一行 id，否则宁可报错也不写坏文件
    covered = any(
        _ID_LINE_RE.match(line) and _ID_LINE_RE.match(line).group(1).strip("\"'") == agent_id
        for line in lines[start:end]
    )
    if not covered:
        raise RuntimeError(
            f"删除区间算错了（start={start} end={end}），已中止以免写坏 models.yaml"
        )
    del lines[start:end]

    # 追加成员时块首带了一个空行（可读性），删除后它会留在文件末尾。
    # 这里把尾部空行收干净并保证以单个换行结尾 —— 这样「加完再删」能回到
    # 与操作前**逐字节相同**的文件，而不是留一行空白慢慢积累。
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"

    with MEMBERS_FILE.open("w", encoding="utf-8", newline="") as handle:
        handle.write("".join(lines))

    role_file = target.role_file
    new_cfg = reload_config()
    if new_cfg.by_id(agent_id) is not None:
        raise RuntimeError("成员已从 models.yaml 删除，但重新装载后仍然存在")

    # 角色文件如果没人再引用就一起删掉，避免 roles/ 越积越多
    # 只清理「这个成员专属」的角色文件：
    #   · 文件名必须正好是 member_<id>.md（或它的 _custom/_v2 变体）；
    #   · 且没有任何还在的成员引用它。
    # 这样绝不会误删 captain.md / member_analyst.md 这种被共享或被别人引用的模板。
    expected = {f"member_{agent_id}.md", f"member_{agent_id}_custom.md", f"member_{agent_id}_v2.md"}
    if role_file in expected:
        still_used = {a.role_file for a in new_cfg.agents}
        path = ROLES_DIR / role_file
        if role_file not in still_used and path.exists():
            try:
                path.unlink()
            except OSError:
                pass
    return new_cfg, target.display_name
