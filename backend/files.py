"""下载响应头的小工具。

HTTP 头只能是 latin-1，中文文件名直接塞进 `Content-Disposition` 会让 Starlette 抛
`UnicodeEncodeError: 'latin-1' codec can't encode characters in position ...`（导出的中文文件名
实测踩过）。按 RFC 5987 同时给两个名字：`filename=` 是 ASCII 兜底（老浏览器/下载器用），
`filename*=` 是 UTF-8 真名（现代浏览器优先用）。
"""
from __future__ import annotations

from urllib.parse import quote


def _ascii_letters(value: str) -> bool:
    return any(character.isascii() and character.isalpha() for character in value)


def content_disposition(filename: str, suffix: str = "", *, fallback: str = "download") -> str:
    """`filename` 是不带后缀的名字（可含中文），`fallback` 是 ASCII 兜底用的名字。

    规则只有一条：**名字只要含非 ASCII 字符就整个换成 fallback**。不去「保留能保留下来的
    那几个字符」——`财务分析报告 2097-09 部门A` 按字符过滤会变成 `2097-09 A`，
    这种名字下载下来谁也认不出是什么，还不如明确换成 `financial-report-2097-09-A`。
    """
    ascii_name = filename if filename.isascii() else fallback
    ascii_name = ascii_name.encode("ascii", "ignore").decode("ascii").strip()
    if not _ascii_letters(ascii_name):
        ascii_name = "download"
    full = f"{filename}{suffix}"
    return f"attachment; filename=\"{ascii_name}{suffix}\"; filename*=UTF-8''{quote(full)}"
