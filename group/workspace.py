"""共享工作区 —— 让群成员有"一张共用的桌子"。

为什么需要它
------------
成员是**纯 LLM**，伸不出手：既看不见彼此的文件，也写不出文件。
没有共享工作面时，"7 个模型合成一个人" 落不了地 ——
豆包说"前端我建议用 Vue"、Kimi 说"后端我用 FastAPI"，
那些都是**话**，不是**东西**，谁也接不上谁的活。

这里给它们一块真目录，让产物**真的落盘、彼此可见**。

为什么用文本约定而不是原生 function calling
--------------------------------------------
    ```file:frontend/App.vue
    <template>…</template>
    ```

内核解析这个块 → 真的写到 ``workspace/frontend/App.vue``。

理由：
  · **跨厂商一致** —— 各家 function calling 实现有差异（有的支持 builtin、
    有的只认 function，参数格式还不一样），文本协议到哪都一样；
  · **聊天里看得见** —— 用户能直接看到"它要产出一个文件"，而不是黑箱；
  · **契合最终架构** —— 用户设想的是「一个 Agent 持有工作区和工具，
    模型只负责产出内容」。那种架构下**工具本来就属于那个 Agent**，
    模型只需要按约定输出，不需要自己有工具。
  · 零依赖，不用改模型调用层。

代价要说清楚：不如原生 tool_calls 可靠，模型可能不按格式写。
所以解析器要宽容（容忍大小写、多余空格），而且**成功和失败都要在群里可见**。

安全
----
一旦模型能写文件就必须沙箱：
  · 只能写在 workspace/ 里，绝不允许 ``..`` 跳出；
  · 不允许绝对路径 / 盘符 / UNC；
  · 只允许文本类扩展名（不给它写 .exe/.dll 的机会）；
  · 单文件与总文件数都有上限。
"""
from __future__ import annotations

import re
from pathlib import Path

# 单文件上限 256KB：够放源码和文档，又不至于把上下文撑爆。
MAX_FILE_BYTES = 256 * 1024
# 工作区文件总数上限，防止模型刷爆磁盘。
MAX_FILES = 300
# 给组长看清单时，每个文件展示多少字的开头。
DIGEST_PER_FILE = 120
DIGEST_MAX_FILES = 40
# 派发给某个成员时，单个文件最多塞多少字进提示词。
INJECT_PER_FILE = 4000
INJECT_MAX_FILES = 3

# 只允许文本类产物 —— 不给模型写可执行文件的机会。
ALLOWED_EXTS = {
    "md", "txt", "rst", "csv", "tsv", "json", "yaml", "yml", "toml", "ini", "cfg",
    "html", "htm", "css", "scss", "less", "js", "mjs", "cjs", "ts", "tsx", "jsx",
    "vue", "svelte", "py", "java", "kt", "go", "rs", "rb", "php", "c", "h", "cc",
    "cpp", "hpp", "cs", "swift", "sql", "sh", "bash", "ps1", "bat", "xml", "svg",
    "env", "tex", "log", "conf",
}

# ```file:路径  /  ```文件:路径  /  ~~~file:路径
_WRITE_RE = re.compile(
    r"^[ \t]*(?:```|~~~)[ \t]*(?:file|文件|FILE)[ \t]*[:：][ \t]*(\S+)[ \t]*\r?\n"
    r"(.*?)"
    r"^[ \t]*(?:```|~~~)[ \t]*$",
    re.MULTILINE | re.DOTALL,
)


def workspace_dir() -> Path:
    from .config import user_data_dir

    path = user_data_dir() / "workspace"
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_rel(rel: str) -> str | None:
    """把模型给的路径清洗成工作区内的相对 POSIX 路径；不安全返回 None。

    这是**唯一**的入口校验 —— 所有写入都必须先过这里。
    """
    if not rel:
        return None
    raw = str(rel).strip().strip('"').strip("'").replace("\\", "/")
    if not raw:
        return None
    # 绝对路径 / 家目录 / 盘符 / UNC —— 一律拒绝
    if raw.startswith("/") or raw.startswith("~") or raw.startswith("//"):
        return None
    if re.match(r"^[A-Za-z]:", raw):
        return None
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    cleaned: list[str] = []
    for p in parts:
        p = re.sub(r'[<>:"|?*\x00-\x1f]', "_", p).strip(". ")
        if not p:
            return None
        cleaned.append(p)
    out = "/".join(cleaned)
    return out if len(out) <= 200 else None


def _ext_ok(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    if "." not in name:
        return True          # 没后缀的（Makefile、LICENSE）按文本处理
    return name.rsplit(".", 1)[-1].lower() in ALLOWED_EXTS


def all_files() -> list[dict]:
    root = workspace_dir()
    out: list[dict] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if any(seg.startswith(".") for seg in rel.split("/")):
            continue          # 跳过隐藏文件/目录
        try:
            stat = path.stat()
        except OSError:
            continue
        out.append({"path": rel, "bytes": stat.st_size, "mtime": stat.st_mtime})
    return out


def write_file(rel: str, content: str) -> dict:
    """写一个文件，返回 ``{path, bytes, created}``。不合法抛 ValueError。"""
    safe = safe_rel(rel)
    if safe is None:
        raise ValueError(f"路径不合法（只能是工作区内的相对路径）：{rel!r}")
    if not _ext_ok(safe):
        raise ValueError(f"不允许写这种类型的文件：{safe}")
    data = content.encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(
            f"文件太大（{len(data) // 1024} KB），上限 {MAX_FILE_BYTES // 1024} KB：{safe}"
        )
    existing = all_files()
    if len(existing) >= MAX_FILES and not any(f["path"] == safe for f in existing):
        raise ValueError(f"工作区文件数已达上限（{MAX_FILES}），先清理一些再加")

    target = workspace_dir() / safe
    created = not target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8", newline="")
    return {"path": safe, "bytes": len(data), "created": created}


def read_file(rel: str) -> str | None:
    safe = safe_rel(rel)
    if safe is None:
        return None
    target = workspace_dir() / safe
    if not target.is_file():
        return None
    try:
        return target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def extract_writes(text: str) -> tuple[str, list[dict], list[dict]]:
    """从模型回复里抽出 ```file:路径 块。

    返回 ``(去掉产物块后的正文, 成功的写入, 失败的写入)``。

    **失败不能静默丢弃** —— 必须让调用方在群里说明，
    否则用户会以为文件写了，其实没写。
    """
    good: list[dict] = []
    bad: list[dict] = []

    def _repl(m: re.Match) -> str:
        rel, content = m.group(1), m.group(2)
        try:
            good.append(write_file(rel, content))
        except ValueError as exc:
            bad.append({"path": rel, "error": str(exc)})
        return f"〔已产出文件：{rel}〕"

    cleaned = _WRITE_RE.sub(_repl, text or "")
    return cleaned, good, bad


def digest(limit_files: int = DIGEST_MAX_FILES) -> str:
    """给组长看的工作区清单（每份带开头一点内容，好判断是什么）。"""
    files = all_files()
    if not files:
        return "（工作区还是空的）"
    lines: list[str] = []
    for item in files[:limit_files]:
        text = read_file(item["path"]) or ""
        head = re.sub(r"\s+", " ", text).strip()[:DIGEST_PER_FILE]
        lines.append(f"- {item['path']}（{item['bytes']} 字节）：{head}")
    if len(files) > limit_files:
        lines.append(f"- …还有 {len(files) - limit_files} 个文件未列出")
    return "\n".join(lines)


def files_mentioned(text: str, limit: int = INJECT_MAX_FILES) -> list[str]:
    """从一段文本里找出它提到的、且工作区真有的文件。

    模型看不到文件系统，**不塞进提示词就等于没传**。
    """
    text = text or ""
    found: list[str] = []
    for item in all_files():
        rel = item["path"]
        name = rel.rsplit("/", 1)[-1]
        if rel in text or (len(name) > 3 and name in text):
            if rel not in found:
                found.append(rel)
        if len(found) >= limit:
            break
    return found


def bundle_for(text: str, limit: int = INJECT_MAX_FILES) -> str:
    """把文本里提到的文件内容打包，供拼进提示词。"""
    picked = files_mentioned(text, limit)
    if not picked:
        return ""
    chunks: list[str] = []
    for rel in picked:
        body = read_file(rel) or ""
        if len(body) > INJECT_PER_FILE:
            body = body[:INJECT_PER_FILE] + f"\n…（已截断，原文 {len(body)} 字）"
        chunks.append(f"### 工作区文件 {rel}\n```\n{body}\n```")
    return "\n\n".join(chunks)


# 拼进 system 提示词的格式说明。
WRITE_PROTOCOL = """## 产出文件（重要 —— 你在这里**确实能**写文件）

先说清楚一件事，免得你误会：

在这个工作群里，你**可以真的把文件写进共享工作区**，其他成员也能读到它。
这**不是**「给用户一段内容让他自己保存」，而是**真的落盘**。

（实测踩过的坑：不加这段说明，DeepSeek 这类模型会回一句
「我无法直接在你的电脑上创建文件，你可以复制保存」然后附上几张
「Windows 记事本 / macOS 终端 / PowerShell」的操作说明 —— 它把普通聊天里的
先验搬了过来。那个先验在普通聊天里是对的，**在这里不对**。）

所以：

- **不要说「我无法直接创建文件」**，也不要说「你复制保存即可」；
- 直接用下面的格式把文件写出来，内核会替你落盘、并在群里公布文件名。

格式（注意是 **file: 路径**）：

```file:frontend/App.vue
<template>...</template>
```

规则：
- 路径是**工作区内的相对路径**（像 `hello.txt`、`src/app.py`），
  不能有 `..`、不能是绝对路径、不能带盘符；
- 只允许文本类文件（.md/.txt/.py/.js/.vue/.html/.json/.csv…），单个不超过 256 KB；
- 写完后内核会把文件内容从你的发言里收走，聊天框只留一行「已产出文件」——
  所以**不要**在文件块外面再重复粘贴一遍内容；
- 不需要产出文件时，正常说话就行。
"""


# --------------------------------------------------------------------------- #
# 真正的 function calling 工具
# --------------------------------------------------------------------------- #
# 实测结论：模型判断"我有没有工具"看的是请求里的 tools 字段，不是提示词。
# 所以这里给出真正的工具 schema，由内核本地执行。
#
# 默认只声明 write_file 一个：
#   · 它是关键缺口（成员能写文件，"7 个模型共用一张桌子"才成立）；
#   · 每个工具都要占请求 token，而且每次调用都要重发一遍，
#     工具声明多了会实打实推高成本 —— 所以先只上最必要的那个。
# 读文件暂时走已有路径（内核把任务里提到的文件内容附进提示词），不上工具。
LOCAL_TOOL_NAMES = {"write_file", "read_file", "list_files", "set_task"}

# 单次读取的字符上限。读回来的内容会进对话，**不受 max_context_chars 约束**
# （那个只管 system+user 的拼装），所以必须在这里自己封顶 ——
# 否则一个 256KB 的文件能把一次请求直接撑爆。
READ_LIMIT = 8000




# --------------------------------------------------------------------------- #
# 工具输出的"保头保尾"压缩
# --------------------------------------------------------------------------- #
# 思路来自 dsh-our-free-model 的 src/token-saver/rtk/（MIT）。
#
# 原来这里是**按字符硬砍**：`body = body[:8000]`。问题很具体 ——
#   · 关键信息常常在**末尾**（函数结尾、结论、最后一个报错），硬砍全丢
#   · 模型**不知道**自己被砍了，它以为那就是全文
#
# 改成"保头保尾 + 说明缺了多少行"，模型就知道自己在看一个**窗口**，
# 需要中间部分时会再来读一次。
READ_HEAD_LINES = 150   # 保留下来的开头行数
READ_TAIL_LINES = 50    # 保留下来的结尾行数
READ_MIN_LINES = 260    # 低于这个行数就别动：head+tail+标记不会比原文更小


def safe_apply(fn, text: str) -> str:
    """跑一个文本压缩器；**它抛错或返回非字符串时，一律退回原文**。

    照 rtk/applyFilter.js。它那段注释说得比我好：

        "A filter that throws must degrade to *no savings* — never to a
         broken request… an exception here would fail the whole turn, and
         the user would lose the answer to save a few tokens on a tool result."

    也就是说：**省 token 是可选的，读到文件是必须的。**
    """
    try:
        out = fn(text)
    except Exception as exc:  # noqa: BLE001
        print(f"[workspace] 压缩器 {getattr(fn, '__name__', fn)!r} 抛错，退回原文：{exc}")
        return text
    if not isinstance(out, str):
        # 忘了 return 的分支会给出 None —— 那会让模型看到"空文件"，
        # 和它本该看到的原文是两条完全不同的消息。宁可多花 token，也要给对。
        return text
    return out


def smart_truncate(text: str) -> str:
    """保头保尾，中间换成一行说明"还剩多少行"。

    照 rtk/filters/smartTruncate.js。两条规则照抄它的理由：
      · **低于 READ_MIN_LINES 就别动** —— head+tail+标记不会比原文更小；
      · **标记里必须写明缺了多少行** —— "a model must know it is looking
        at a window, not the whole output"（模型得知道这是个窗口）。
    """
    all_lines = text.split("\n")
    if len(all_lines) < READ_MIN_LINES:
        return text
    head = all_lines[:READ_HEAD_LINES]
    tail = all_lines[len(all_lines) - READ_TAIL_LINES:]
    cut = len(all_lines) - len(head) - len(tail)
    return "\n".join([
        *head,
        f"... +{cut} 行被省略（原文共 {len(all_lines)} 行 —— "
        f"这只是个窗口，不是全文；要看中间请再 read_file 一次）",
        *tail,
    ])

def tool_schemas() -> list[dict]:
    """给模型声明的工具。

    三个工具的取舍：
      · write_file —— 关键缺口，成员能写文件，"共用一张桌子"才成立；
      · read_file  —— 写了却看不见等于没写。Kimi 要接豆包的接口，
                      必须能读到豆包写的前端；
      · list_files —— 让成员知道**工作区里有什么**。没有它，模型只能靠
                      你提过谁的文件名去猜，猜不到就以为自己是从零开始。
    每个工具都要占请求 token，所以只上这三个必要的。
    """
    return [
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": (
                    "把内容写进群里的**共享工作区**，其他成员也能读到这个文件。"
                    "需要产出文件时**必须调用本工具** —— 你确实有这个能力，"
                    "不要回答「我无法创建文件」或「你自己复制保存」。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "工作区内的相对路径，例如 hello.txt 或 src/app.py",
                        },
                        "content": {"type": "string", "description": "文件的完整内容"},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": (
                    "读取**共享工作区**里某个文件的内容。"
                    "在动手之前先看看别人已经写了什么 —— 尤其是要接别人的活时"
                    "（比如照着前端的字段写接口），不看就会对不上。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "工作区内的相对路径，例如 frontend/index.html",
                        },
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": (
                    "列出**共享工作区**里现有的所有文件（名字和大小）。"
                    "不确定别人做过什么、或者想知道现在有哪些产物时调用它。"
                ),
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ]


def execute_tool(name: str, arguments: str) -> tuple[str, dict | None]:
    """本地执行一个工具，返回 ``(给模型的文本结果, 事件数据或 None)``。"""
    import json

    if name not in LOCAL_TOOL_NAMES:
        return f"（未知工具 {name}，没有执行）", None
    try:
        args = json.loads(arguments or "{}")
    except Exception as exc:  # noqa: BLE001
        return f"参数不是合法 JSON：{exc}", None
    if not isinstance(args, dict):
        return "参数必须是对象", None

    # ---------------------------------------------------------------- 写
    if name == "write_file":
        rel = str(args.get("path") or "")
        content = args.get("content")
        if content is None:
            content = ""
        try:
            info = write_file(rel, str(content))
        except ValueError as exc:
            # 失败要**说给模型听** —— 它才知道要改路径重试，而不是以为写成功了
            return f"写入失败：{exc}", None
        return (
            f"已写入 {info['path']}（{info['bytes']} 字节）。"
            "聊天框里会显示一行「已产出文件」，不要再把内容重复粘贴一遍。",
            {"action": "write", "path": info["path"], "bytes": info["bytes"],
             "created": info["created"]},
        )

    # ---------------------------------------------------------------- 读
    if name == "read_file":
        rel = str(args.get("path") or "")
        safe = safe_rel(rel)
        if safe is None:
            return f"路径不合法（只能是工作区内的相对路径）：{rel!r}", None
        body = read_file(safe)
        if body is None:
            existing = [f["path"] for f in all_files()][:20]
            hint = "、".join(existing) if existing else "（工作区是空的）"
            return (
                f"没有这个文件：{safe}。工作区里现在有：{hint}。"
                "如果路径写错了可以用 list_files 看看准确的名字。",
                {"action": "read_miss", "path": safe},
            )
        # 保头保尾（而不是"砍掉第 8000 字之后"）。
        # 用 safe_apply 包住：**压缩失败必须退回原文** —— 绝不能因为想省
        # 几个 token 而让这一轮读文件失败。顺序是"先保证读得到，再谈省"。
        raw_body = body
        body = safe_apply(smart_truncate, raw_body)
        truncated = body is not raw_body
        total = len(raw_body)
        total_lines = len(raw_body.split("\n"))
        note = (
            f"（原文 {total_lines} 行 / {total} 字；已保留开头 {READ_HEAD_LINES} 行"
            f"与结尾 {READ_TAIL_LINES} 行，中间省略）"
            if truncated else ""
        )
        return (
            f"### {safe}{note}\n```\n{body}\n```",
            {"action": "read", "path": safe, "chars": total, "truncated": truncated},
        )

    # ---------------------------------------------------------------- 列目录
    if name == "list_files":
        files = all_files()
        if not files:
            return "工作区现在是空的，还没有人产出过文件。", {
                "action": "list", "count": 0}
        listing = "\n".join(f"- {f['path']}（{f['bytes']} 字节）" for f in files[:60])
        more = f"\n…还有 {len(files) - 60} 个未列出" if len(files) > 60 else ""
        return (
            f"工作区里现在有 {len(files)} 个文件：\n{listing}{more}",
            {"action": "list", "count": len(files)},
        )

    # ---------------------------------------------------------------- 任务板
    # 只组长会拿到这个工具（见 llm.attach_workspace_tools），
    # 但这里照样做参数校验 —— 别把调用方会守规矩当安全边界。
    if name == "set_task":
        from .taskboard import set_task

        try:
            item = set_task(
                str(args.get("title") or ""),
                owner=str(args.get("owner") or ""),
                status=str(args.get("status") or "todo"),
                note=str(args.get("note") or ""),
            )
        except ValueError as exc:
            return f"登记失败：{exc}", None
        return (
            f"任务已登记/更新：{item['title']}"
            f"（{item['status']}，负责人 {item.get('owner') or '未指派'}）",
            {"action": "task", "title": item["title"], "status": item["status"],
             "owner": item.get("owner", "")},
        )

    return f"（未知工具 {name}，没有执行）", None
