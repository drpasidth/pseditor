"""Render the PSEditor user manuals (English and Thai) as HTML for the Help button."""
import html
import re
from pathlib import Path

from django.conf import settings

_LANG_FILE = {"en": "USER_MANUAL.md", "th": "USER_MANUAL_TH.md"}


def manual_html(lang):
    name = _LANG_FILE.get(lang, _LANG_FILE["en"])
    path = Path(settings.BASE_DIR) / name
    text = path.read_text(encoding="utf-8")
    return render_md(text)


def render_md(text):
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("```"):
            i += 1
            buf = []
            while i < n and not lines[i].startswith("```"):
                buf.append(lines[i])
                i += 1
            if i < n:
                i += 1
            blocks.append("<pre><code>" + html.escape("\n".join(buf)) + "</code></pre>")
            continue
        if re.fullmatch(r"-{3,}", line.strip()):
            blocks.append("<hr>")
            i += 1
            continue
        if line.startswith("|") and i + 1 < n and _is_table_sep(lines[i + 1]):
            html_table, i = _table(lines, i)
            blocks.append(html_table)
            continue
        heading = re.match(r"^(#{1,3})\s+(.*)$", line)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip()
            blocks.append(f'<h{level} id="{_slug(title)}">{_inline(title)}</h{level}>')
            i += 1
            continue
        if re.match(r"^\s*(?:[-*]|\d+\.)\s+", line):
            html_list, i = _list(lines, i)
            blocks.append(html_list)
            continue
        if line.startswith(">"):
            buf = []
            while i < n and lines[i].startswith(">"):
                buf.append(lines[i][1:].strip())
                i += 1
            blocks.append("<blockquote><p>" + _inline(" ".join(buf)) + "</p></blockquote>")
            continue
        if not line.strip():
            i += 1
            continue
        para = [line.strip()]
        i += 1
        while i < n and lines[i].strip() and not _starts_block(lines, i):
            para.append(lines[i].strip())
            i += 1
        blocks.append("<p>" + _inline(" ".join(para)) + "</p>")
    return "\n".join(blocks)


def _starts_block(lines, i):
    line = lines[i]
    if line.startswith(("```", "#", "|", ">")):
        return True
    if re.fullmatch(r"-{3,}", line.strip()):
        return True
    if re.match(r"^\s*(?:[-*]|\d+\.)\s+", line):
        return True
    if line.startswith("|") and i + 1 < len(lines) and _is_table_sep(lines[i + 1]):
        return True
    return False


def _is_table_sep(line):
    return bool(re.match(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$", line))


def _cells(line):
    parts = [c.strip() for c in line.strip().strip("|").split("|")]
    return parts


def _table(lines, i):
    header = _cells(lines[i])
    i += 2
    rows = []
    while i < len(lines) and lines[i].startswith("|"):
        rows.append(_cells(lines[i]))
        i += 1
    head = "".join(f"<th>{_inline(c)}</th>" for c in header)
    body = []
    for row in rows:
        body.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>")
    return "<table><thead><tr>" + head + "</tr></thead><tbody>" + "".join(body) + "</tbody></table>", i


def _list(lines, i):
    ordered = bool(re.match(r"^\s*\d+\.\s+", lines[i]))
    items = []
    while i < len(lines):
        line = lines[i]
        marker = re.match(r"^\s*(?:(\d+)\.|[-*])\s+(.*)$", line)
        is_ordered = bool(marker and marker.group(1))
        if marker and is_ordered == ordered:
            items.append(marker.group(2))
            i += 1
            continue
        if items and re.match(r"^[ \t]+\S", line) and not re.match(r"^\s*(?:[-*]|\d+\.)\s+", line):
            items[-1] += " " + line.strip()
            i += 1
            continue
        break
    tag = "ol" if ordered else "ul"
    body = "".join("<li>" + _inline(item) + "</li>" for item in items)
    return f"<{tag}>{body}</{tag}>", i


def _slug(title):
    plain = re.sub(r"[`*]", "", title).lower()
    plain = re.sub(r"[^\w\s-]", "", plain, flags=re.UNICODE)
    return re.sub(r"\s+", "-", plain.strip())


def _inline(text):
    pieces = re.split(r"(`[^`]+`)", text)
    out = []
    for piece in pieces:
        if len(piece) >= 2 and piece.startswith("`") and piece.endswith("`"):
            out.append("<code>" + html.escape(piece[1:-1]) + "</code>")
        else:
            out.append(_inline_text(html.escape(piece)))
    return "".join(out)


def _inline_text(escaped):
    def link(match):
        label, url = match.group(1), match.group(2)
        if url.startswith(("#", "http://", "https://")):
            return f'<a href="{html.escape(url, quote=True)}">{label}</a>'
        return match.group(0)

    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", link, escaped)
    text = re.sub(
        r"&lt;(https?://[^&\s]+)&gt;",
        lambda m: f'<a href="{html.escape(html.unescape(m.group(1)), quote=True)}">{m.group(1)}</a>',
        text,
    )
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    return text
