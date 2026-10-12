"""検索語のハイライト(<mark>)。表示用HTMLにだけ施し、保存してある原文は変えない。

- プロンプト・メモ(プレーンテキスト): エスケープしながら、一致箇所を <mark> で囲む
- 応答(markdown変換済みのHTML): タグの外側の「文字」だけを対象にする
    * タグの属性・&amp; のような文字参照・<button> の中身には触れない
"""
from __future__ import annotations

import re

from markupsafe import Markup, escape

_TAG = re.compile(r"(<[^>]*>)")
_ENTITY = re.compile(r"(&#?\w+;)")


def _regex(terms: list[str], *, html_escaped: bool) -> re.Pattern | None:
    parts = []
    for t in terms:
        if t:
            parts.append(re.escape(_html_escape(t) if html_escaped else t))
    if not parts:
        return None
    parts.sort(key=len, reverse=True)  # 長い語を優先
    return re.compile("|".join(parts), re.IGNORECASE)


def _html_escape(s: str) -> str:
    # markdown-it の escapeHtml と同じ(& < > " だけ)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def highlight_text(text: str, terms: list[str]) -> Markup:
    """プレーンテキストをエスケープして返す。terms があれば一致箇所を <mark> で囲む。"""
    rx = _regex(terms, html_escaped=False)
    if rx is None:
        return Markup(escape(text))
    out: list[Markup] = []
    last = 0
    for m in rx.finditer(text):
        out.append(escape(text[last:m.start()]))
        out.append(Markup("<mark>") + escape(m.group(0)) + Markup("</mark>"))
        last = m.end()
    out.append(escape(text[last:]))
    return Markup("").join(out)


def highlight_html(html: str, terms: list[str]) -> str:
    rx = _regex(terms, html_escaped=True)
    if rx is None:
        return html
    out: list[str] = []
    in_button = 0
    for part in _TAG.split(html):
        if part.startswith("<"):
            low = part[:9].lower()
            if low.startswith("<button"):
                in_button += 1
            elif low.startswith("</button"):
                in_button = max(0, in_button - 1)
            out.append(part)
        elif in_button or not part:
            out.append(part)
        else:
            for chunk in _ENTITY.split(part):
                if _ENTITY.fullmatch(chunk):
                    out.append(chunk)  # &amp; などの途中は壊さない
                else:
                    out.append(rx.sub(lambda m: f"<mark>{m.group(0)}</mark>", chunk))
    return "".join(out)
