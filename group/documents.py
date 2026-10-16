"""把上传的文件转成纯文本，好塞进提示词让模型真的读到。

为什么需要它：成员是**纯 LLM**，没有工具、读不到文件系统，
它们只能看到后端拼进提示词的那段文字。而 .docx/.xlsx/.pptx 本质是 **ZIP 包**
（里面装着 XML），按文本读只会得到一团二进制 —— 所以早先上传 docx 时，
模型收到的正文是空的，只能回一句"我看不到内容"。

这里**只用标准库**（zipfile + xml.etree + zlib）：
目标机器上没有任何 Office 解析库，装依赖对 PyInstaller 打包也不友好。

支持情况（诚实列一下）：
    .docx / .xlsx / .pptx   完整支持（它们就是 ZIP+XML）
    .txt/.md/.csv/.json/…   按文本解码
    .pdf                    **尽力而为**：能处理未压缩和 FlateDecode 的文本流，
                            扫描件、加密 PDF、复杂编码会读不出来 —— 那时如实说明
    .doc/.xls/.ppt 老格式   二进制且无公开结构，读不了 —— 如实说明
"""
from __future__ import annotations

import io
import re
import zipfile

# 单个文件抽出来的文本上限。塞进提示词的东西，太长会把上下文挤爆。
EXTRACT_LIMIT = 12000

# 这些后缀直接按文本读
TEXT_EXTS = {
    "txt", "md", "markdown", "csv", "tsv", "json", "xml", "yaml", "yml", "log",
    "ini", "cfg", "conf", "toml", "env", "py", "js", "ts", "tsx", "jsx", "mjs",
    "html", "htm", "css", "scss", "less", "vue", "java", "kt", "go", "rs", "rb",
    "php", "c", "h", "cc", "cpp", "hpp", "cs", "swift", "sql", "sh", "bash",
    "ps1", "bat", "cmd", "tex", "rst", "srt", "vtt",
}

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _ext(name: str) -> str:
    return (name or "").rsplit(".", 1)[-1].lower() if "." in (name or "") else ""


def extract_text(name: str, mime: str, raw: bytes) -> tuple[str, str]:
    """把文件转成文本，返回 ``(文本, 说明)``。

    说明是给用户/模型看的：成功时写"已提取 N 字"，失败时写清**为什么读不了**，
    这样模型能如实说"这个格式我读不了"，而不是误以为文件是空的。
    """
    ext = _ext(name)
    mime = (mime or "").lower()

    try:
        if ext in TEXT_EXTS or mime.startswith("text/"):
            return _from_text(raw)
        if ext == "docx":
            return _from_docx(raw)
        if ext == "xlsx":
            return _from_xlsx(raw)
        if ext == "pptx":
            return _from_pptx(raw)
        if ext == "pdf" or mime == "application/pdf":
            return _from_pdf(raw)
        if ext in ("doc", "xls", "ppt"):
            return "", f"{ext} 是 Office 2003 的老二进制格式，程序读不了。请另存为新格式（.docx/.xlsx/.pptx）再传。"
        if ext == "odt" or ext == "ods" or ext == "odp":
            return _from_odf(raw, ext)
        # 未知格式：试一试当文本读（很多文件其实没有正确后缀）
        text, note = _from_text(raw)
        if text.strip():
            return text, note
        return "", f"不认识的格式（.{ext or '无后缀'}），读不出文字。"
    except zipfile.BadZipFile:
        return "", "文件已损坏或不是有效的 Office 文档（解压失败）。"
    except Exception as exc:  # noqa: BLE001 —— 解析失败不该中断整轮对话
        return "", f"解析失败（{type(exc).__name__}）：{exc}"


def _clip(text: str) -> tuple[str, str]:
    text = text.strip()
    if len(text) > EXTRACT_LIMIT:
        return text[:EXTRACT_LIMIT], f"已提取 {EXTRACT_LIMIT} 字（原文更长，已截断）"
    return text, f"已提取 {len(text)} 字"


def _from_text(raw: bytes) -> tuple[str, str]:
    for enc in ("utf-8", "utf-8-sig", "gbk", "gb18030", "utf-16", "latin-1"):
        try:
            return _clip(raw.decode(enc))
        except (UnicodeDecodeError, LookupError):
            continue
    return _clip(raw.decode("utf-8", "replace"))


def _from_docx(raw: bytes) -> tuple[str, str]:
    """Word：按段落抽 w:p 里的 w:t 文本。"""
    import xml.etree.ElementTree as ET

    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        if "word/document.xml" not in z.namelist():
            return "", "这个 .docx 里没有正文（word/document.xml 缺失）。"
        xml = z.read("word/document.xml")
    root = ET.fromstring(xml)
    paras: list[str] = []
    for para in root.iter(_W + "p"):
        line = "".join(t.text or "" for t in para.iter(_W + "t")).strip()
        if line:
            paras.append(line)
    # 表格里的文字也在 w:t 里，但不在 w:p 之外 —— 所以上面已经覆盖到了
    if not paras:
        return "", "文档正文是空的（或者只有图片/表格线条，没有文字）。"
    return _clip("\n".join(paras))


def _from_xlsx(raw: bytes) -> tuple[str, str]:
    """Excel：共享字符串表 + 各工作表，按行输出成 TSV。"""
    import xml.etree.ElementTree as ET

    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names = z.namelist()
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            sroot = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in sroot.iter(ns + "si"):
                shared.append("".join(t.text or "" for t in si.iter(ns + "t")))
        sheets = sorted(n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
        if not sheets:
            return "", "这个 .xlsx 里没有工作表。"
        out: list[str] = []
        for idx, sheet in enumerate(sheets, 1):
            out.append(f"=== 工作表 {idx} ===")
            sroot = ET.fromstring(z.read(sheet))
            for row in sroot.iter(ns + "row"):
                cells: list[str] = []
                for c in row.iter(ns + "c"):
                    v = c.find(ns + "v")
                    txt = ""
                    if v is not None and v.text:
                        if c.get("t") == "s":       # 共享字符串，值是下标
                            try:
                                txt = shared[int(v.text)]
                            except (ValueError, IndexError):
                                txt = v.text
                        else:
                            txt = v.text
                    else:
                        is_el = c.find(ns + "is")
                        if is_el is not None:
                            txt = "".join(t.text or "" for t in is_el.iter(ns + "t"))
                    cells.append(txt)
                line = "\t".join(cells).strip()
                if line:
                    out.append(line)
    if len(out) <= len(sheets):
        return "", "表格里没有文字内容。"
    return _clip("\n".join(out))


def _from_pptx(raw: bytes) -> tuple[str, str]:
    """PPT：按幻灯片抽 a:t 文本。"""
    import xml.etree.ElementTree as ET

    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        slides = sorted(
            (n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
            key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[-1]).group(1)),
        )
        if not slides:
            return "", "这个 .pptx 里没有幻灯片。"
        out: list[str] = []
        for idx, name in enumerate(slides, 1):
            root = ET.fromstring(z.read(name))
            texts = [t.text.strip() for t in root.iter(_A + "t") if (t.text or "").strip()]
            if texts:
                out.append(f"=== 第 {idx} 页 ===")
                out.extend(texts)
    if not out:
        return "", "演示文稿里没有文字（可能全是图片）。"
    return _clip("\n".join(out))


def _from_odf(raw: bytes, ext: str) -> tuple[str, str]:
    """OpenDocument（LibreOffice 的 .odt/.ods/.odp），同样是 ZIP+XML。"""
    import xml.etree.ElementTree as ET

    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        if "content.xml" not in z.namelist():
            return "", f"这个 .{ext} 里没有 content.xml。"
        xml = z.read("content.xml")
    root = ET.fromstring(xml)
    texts = [t for t in (el.text or "" for el in root.iter()) if t and t.strip()]
    if not texts:
        return "", "文档里没有文字。"
    return _clip("\n".join(t.strip() for t in texts))


def _from_pdf(raw: bytes) -> tuple[str, str]:
    """PDF：**尽力而为**。

    只处理能直接看到的文本操作符，以及 FlateDecode 压缩的流。
    扫描件（图片型 PDF）、加密 PDF、用 CID 字体的中文 PDF 都读不出来 ——
    那属于要上 OCR 或专业库的范畴，这里不假装能做到。
    """
    import zlib

    chunks: list[bytes] = [raw]
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", raw, re.S):
        blob = m.group(1)
        try:
            chunks.append(zlib.decompress(blob))
        except Exception:  # noqa: BLE001 —— 解不开就跳过这一股流
            continue

    pieces: list[str] = []
    for buf in chunks:
        # (文本) Tj  和  [ (a) -1 (b) ] TJ
        for tm in re.finditer(rb"\(((?:\\.|[^\\()])*)\)\s*Tj", buf):
            pieces.append(_unescape_pdf(tm.group(1)))
        for tm in re.finditer(rb"\[((?:[^\[\]]|\\.)*)\]\s*TJ", buf):
            inner = b" ".join(re.findall(rb"\(((?:\\.|[^\\()])*)\)", tm.group(1)))
            if inner:
                pieces.append(_unescape_pdf(inner))

    text = " ".join(p for p in pieces if p.strip())
    if not text.strip():
        return "", (
            "读不出 PDF 里的文字。常见原因：这是扫描件（整页是图片），"
            "或者用了需要 OCR 的编码。请把内容复制成文本贴过来，或换成 .docx 上传。"
        )
    return _clip(text)


def _unescape_pdf(blob: bytes) -> str:
    out = blob.replace(b"\\(", b"(").replace(b"\\)", b")").replace(b"\\\\", b"\\")
    return out.decode("utf-8", "replace")
