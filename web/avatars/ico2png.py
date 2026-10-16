"""把下载到的 .ico 转成 PNG（挑最大的一张），并报告每个图标里有哪些尺寸。

ICO 结构：
  头 6 字节：reserved(2) type(2) count(2)
  每个目录项 16 字节：w h colors reserved planes bitcount bytesInRes imageOffset
    - w/h 为 0 表示 256
  图像数据：要么是内嵌 PNG（Vista+），要么是 DIB（BMP 去头）。

本脚本不依赖 Pillow：内嵌 PNG 直接导出；DIB 则手工拼 PNG（BGRA→RGBA + 行过滤字节）。
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

AV = Path(__file__).resolve().parent


def png_chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def dib_to_png(dib: bytes, w: int, h: int) -> bytes:
    """ICO 里的 DIB 是「BITMAPINFOHEADER + 像素 + AND 掩码」，高是双倍（含掩码）。

    BITMAPINFOHEADER 共 11 个字段：
      biSize(I) biWidth(i) biHeight(i) biPlanes(H) biBitCount(H) biCompression(I)
      biSizeImage(I) biXPelsPerMeter(i) biYPelsPerMeter(i) biClrUsed(I) biClrImportant(I)
    """
    (
        bi_size,
        bw,
        bh,
        _planes,
        bpp,
        _compression,
        _size_image,
        _xppm,
        _yppm,
        clr_used,
        _clr_important,
    ) = struct.unpack_from("<IiiHHIIiiII", dib, 0)
    real_h = max(1, bh // 2)  # 前一半是颜色，后一半是 AND 掩码
    offset = bi_size

    pal = b""
    if bpp <= 8:
        pal_len = clr_used or (1 << bpp)
        pal = dib[offset : offset + pal_len * 4]
        offset += pal_len * 4

    raw = bytearray()
    stride = ((bw * bpp + 31) // 32) * 4
    for y in range(real_h - 1, -1, -1):  # DIB 自下而上存储
        raw.append(0)  # PNG 行过滤器 None
        row = dib[offset + y * stride : offset + y * stride + stride]
        for x in range(bw):
            if bpp == 32:
                b, g, r, a = row[x * 4 : x * 4 + 4]
            elif bpp == 24:
                b, g, r = row[x * 3 : x * 3 + 3]
                a = 255
            elif bpp == 8 and pal:
                b, g, r, a = pal[row[x] * 4 : row[x] * 4 + 4]
            else:
                b = g = r = 0
                a = 255
            raw += bytes((r, g, b, a))
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", bw, real_h, 8, 6, 0, 0, 0))
        + png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + png_chunk(b"IEND", b"")
    )


def convert(ico_path: Path) -> None:
    data = ico_path.read_bytes()
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        # 有些站点把 favicon.ico 直接放了一张 PNG，改名即可
        out = ico_path.with_suffix(".png")
        out.write_bytes(data)
        print(f"  {ico_path.name}: 其实是 PNG，直接改名 → {out.name}  {len(data) // 1024} KB")
        return
    if len(data) < 6:
        print(f"  {ico_path.name}: 文件过小，跳过")
        return
    _, typ, count = struct.unpack_from("<HHH", data, 0)
    if typ != 1 or count == 0:
        head = data[:24]
        print(f"  {ico_path.name}: 不是 ICO（type={typ} count={count}），前 24 字节 {head!r}")
        return

    best = None
    sizes = []
    for i in range(count):
        off = 6 + i * 16
        w, h, _c, _r, _p, bpp, size, data_off = struct.unpack_from("<BBBBHHII", data, off)
        w = w or 256
        h = h or 256
        sizes.append(f"{w}x{h}@{bpp}bpp")
        if best is None or size > best[6]:
            best = (w, h, _c, _r, _p, bpp, size, data_off)

    w, h, _c, _r, _p, bpp, size, data_off = best
    blob = data[data_off : data_off + size]
    is_png = blob[:8] == b"\x89PNG\r\n\x1a\n"

    if is_png:
        png = blob
        kind = "内嵌 PNG"
    else:
        png = dib_to_png(blob, w, h)
        kind = "DIB→PNG"

    out = ico_path.with_suffix(".png")
    out.write_bytes(png)
    print(f"  {ico_path.name}: 含 {len(sizes)} 张 [{', '.join(sizes[:6])}{'…' if len(sizes) > 6 else ''}]")
    print(f"     取最大 {w}x{h}（{kind}）→ {out.name}  {len(png) // 1024} KB")


def main() -> int:
    icons = sorted(AV.glob("*.ico"))
    print(f"发现 {len(icons)} 个 .ico")
    for ico in icons:
        try:
            convert(ico)
        except Exception as exc:  # noqa: BLE001
            print(f"  {ico.name}: 转换失败 {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
