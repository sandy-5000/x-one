#!/usr/bin/env python3
"""Build the static note pages.

Sources live in _scripts/sources/<group>/<slug>.txt or .md. A source is either
a chat export ("question --- answer") or a plain Markdown note starting with a
"# Title". Existing pages without a source keep their content; only their
sidebar is refreshed.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
SOURCES = SCRIPTS / "sources"
SITE = SCRIPTS.parent / "public"

# Pages listed here keep this label and order; other pages are labelled by
# their "# Title" and sorted alphabetically after them.
LABELS = {
    "docker/start": "Start",
    "docker/compose": "Compose",
    "docker/swarm": "Swarm",
    "java/part-1": "Part 1",
    "java/part-2": "Part 2",
    "java/part-3": "Part 3",
    "java/part-4": "Part 4",
    "java/part-5": "Part 5",
    "os/cpu": "CPU",
    "os/user-threads": "User threads",
}

NAV: list[tuple[str, list[tuple[str, str]]]] = []

FONT_HEAD = """<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:ital,wght@0,100..800;1,100..800&display=swap" rel="stylesheet">"""


def parse_chat(text: str) -> tuple[str | None, str]:
    t = text.strip()
    user = None
    body = t
    if t.startswith("YOU SAID:"):
        rest = t[len("YOU SAID:") :].lstrip("\n")
        if "\n---\n" in rest:
            user, body = rest.split("\n---\n", 1)
            user = user.strip()
            body = body.strip()
        elif "---" in rest:
            user, body = rest.split("---", 1)
            user = user.strip()
            body = body.strip()
    elif "\n---\n" in t:
        user, body = t.split("\n---\n", 1)
        user = user.strip() or None
        body = body.strip()
    return user, body


BOX_CHARS = "─│┌┐└┘├┤┬┴┼═║╔╗╚╝╠╣╦╩╬▼▲►◄"
HR_RE = re.compile(r"^(-{3,}|\*{3,}|_{3,})$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")
UL_RE = re.compile(r"^[-*+]\s+(.*)$")
OL_RE = re.compile(r"^(\d+)[.)]\s+(.*)$")
ARROW_ONLY_RE = re.compile(r"^[|v^<>/\\=\-.:\s▼▲►◄]+$")


def is_art_line(line: str) -> bool:
    s = line.strip()
    if not s or HR_RE.match(s):
        return False
    if any(c in BOX_CHARS for c in s):
        return True
    if s[0] in "+|" and len(s) > 1:
        return True
    if s[0] == "+" and s.count("-") >= 2:
        return True
    return bool(ARROW_ONLY_RE.match(s)) and line.startswith(" ")


BORDER_RE = re.compile(r"^\s*\+[-=+\s]*\+\s*$")


def is_border(line: str) -> bool:
    return bool(BORDER_RE.match(line.rstrip())) and "-" in line


def is_row(line: str) -> bool:
    s = line.rstrip()
    return s.lstrip().startswith("|") and s.endswith("|") and len(s.strip()) > 1


def positions(line: str, ch: str) -> list[int]:
    return [i for i, c in enumerate(line.rstrip()) if c == ch]


def repair_art(block: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    n = len(block)
    while i < n:
        line = block[i]
        if (
            is_border(line)
            and i + 2 < n
            and is_row(block[i + 1])
            and is_border(block[i + 2])
            and block[i].rstrip() == block[i + 2].rstrip()
            and not set(positions(block[i + 1], "|")) <= set(positions(line, "+"))
        ):
            row = block[i + 1].rstrip()
            first = row.index("|")
            out.append((row[:first] + " " + row[first + 1 : -1]).rstrip())
            i += 3
            continue
        out.append(line)
        if (
            is_row(line)
            and out[:-1]
            and is_border(out[-2])
            and positions(line, "|") == positions(out[-2], "+")
            and not (
                i + 1 < n
                and (
                    is_row(block[i + 1])
                    or positions(block[i + 1], "+") == positions(line, "|")
                )
            )
        ):
            out.append(out[-2])
        i += 1
    return out


def next_nonblank(lines: list[str], i: int) -> int:
    while i < len(lines) and not lines[i].strip():
        i += 1
    return i


def md_to_html(src: str) -> str:
    lines = src.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        s = line.strip()

        if not s:
            i += 1
            continue

        if s.startswith("```"):
            lang = s[3:].strip()
            i += 1
            buf: list[str] = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            if lang in ("text", "diagram"):
                out.append(f'<pre class="diagram"><code>{html.escape(chr(10).join(buf))}</code></pre>')
                continue
            cls = f' class="lang-{html.escape(lang)}"' if lang else ""
            out.append(
                '<div class="code-block">'
                '<button type="button" class="copy-btn" aria-label="Copy code">Copy</button>'
                f"<pre><code{cls}>{html.escape(chr(10).join(buf))}</code></pre></div>"
            )
            continue

        if s.startswith("|") and i + 1 < n and TABLE_SEP_RE.match(lines[i + 1].strip()):
            header = split_table_row(s)
            aligns = [table_align(c) for c in split_table_row(lines[i + 1].strip())]
            i += 2
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append(split_table_row(lines[i].strip()))
                i += 1
            out.append(render_table(header, aligns, rows))
            continue

        if is_art_line(line):
            buf = []
            while i < n:
                if is_art_line(lines[i]):
                    buf.append(lines[i])
                    i += 1
                    continue
                j = next_nonblank(lines, i)
                if j < n and j - i == 1 and is_art_line(lines[j]):
                    buf.extend(lines[i:j])
                    i = j
                    continue
                break
            buf = repair_art(buf)
            out.append(f'<pre class="diagram"><code>{html.escape(chr(10).join(buf))}</code></pre>')
            continue

        if HR_RE.match(s):
            out.append("<hr>")
            i += 1
            continue

        hm = HEADING_RE.match(s)
        if hm:
            level = len(hm.group(1))
            out.append(f"<h{level}>{inline_md(hm.group(2))}</h{level}>")
            i += 1
            continue

        if s.startswith(">"):
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip()[1:].strip())
                i += 1
            paras = [p for p in "\n".join(buf).split("\n\n") if p.strip()]
            inner = "".join(f"<p>{inline_md(' '.join(p.split()))}</p>" for p in paras)
            out.append(f"<blockquote>{inner}</blockquote>")
            continue

        if UL_RE.match(s) or OL_RE.match(s):
            ordered = bool(OL_RE.match(s))
            item_re = OL_RE if ordered else UL_RE
            items: list[str] = []
            start_num = int(OL_RE.match(s).group(1)) if ordered else 1
            while i < n:
                cur = lines[i]
                cs = cur.strip()
                m = item_re.match(cs) if cs else None
                if m:
                    items.append(m.group(2) if ordered else m.group(1))
                    i += 1
                    continue
                if not cs:
                    j = next_nonblank(lines, i)
                    if j < n and item_re.match(lines[j].strip()) and not is_art_line(lines[j]):
                        i = j
                        continue
                    break
                if cur.startswith((" ", "\t")) and items and not is_art_line(cur) and not cs.startswith("```"):
                    items[-1] += " " + cs
                    i += 1
                    continue
                break
            tag = "ol" if ordered else "ul"
            start_attr = f' start="{start_num}"' if ordered and start_num != 1 else ""
            lis = "".join(f"<li>{inline_md(it)}</li>" for it in items)
            out.append(f"<{tag}{start_attr}>{lis}</{tag}>")
            continue

        buf = [s]
        i += 1
        while i < n:
            ns = lines[i].strip()
            if (
                not ns
                or ns.startswith(("#", "```", ">"))
                or UL_RE.match(ns)
                or OL_RE.match(ns)
                or HR_RE.match(ns)
                or is_art_line(lines[i])
            ):
                break
            buf.append(ns)
            i += 1
        out.append(f"<p>{inline_md(' '.join(buf))}</p>")

    return "\n".join(out)


TABLE_SEP_RE = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?$")


def split_table_row(row: str) -> list[str]:
    row = row.strip().removeprefix("|").removesuffix("|").replace("\\|", "\x01")
    cells, cur, in_code = [], "", False
    for c in row:
        if c == "`":
            in_code = not in_code
        if c == "|" and not in_code:
            cells.append(cur)
            cur = ""
        else:
            cur += c
    cells.append(cur)
    return [cell.strip().replace("\x01", "|") for cell in cells]


def table_align(sep: str) -> str:
    if sep.startswith(":") and sep.endswith(":"):
        return "center"
    if sep.endswith(":"):
        return "right"
    return ""


def render_table(header: list[str], aligns: list[str], rows: list[list[str]]) -> str:
    def cell(tag: str, text: str, col: int) -> str:
        align = aligns[col] if col < len(aligns) else ""
        style = f' style="text-align:{align}"' if align else ""
        return f"<{tag}{style}>{inline_md(text)}</{tag}>"

    head = "".join(cell("th", h, c) for c, h in enumerate(header))
    body = "".join(
        "<tr>" + "".join(cell("td", r[c] if c < len(r) else "", c) for c in range(len(header))) + "</tr>"
        for r in rows
    )
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def inline_md(text: str) -> str:
    codes: list[str] = []

    def stash(m: re.Match) -> str:
        codes.append(f"<code>{html.escape(m.group(1))}</code>")
        return f"\x00{len(codes) - 1}\x00"

    s = re.sub(r"`([^`]+)`", stash, text)
    s = html.escape(s)
    s = re.sub(r"\*\*(\S(?:.*?\S)??)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![\w*])\*(\S(?:[^*]*?\S)??)\*(?![\w*])", r"<em>\1</em>", s)
    s = re.sub(
        r"\[([^\]]+)\]\(([^)\s]+)\)",
        lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>',
        s,
    )
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], s)


def css_href(depth: int) -> str:
    return "../" * depth + "assets/style.css"


def page_href(from_depth: int, group: str, slug: str) -> str:
    prefix = "../" * from_depth
    return f"{prefix}{group}/{slug}.html"


def index_href(from_depth: int) -> str:
    return "../" * from_depth + "index.html"


def render_sidebar(active: str | None, depth: int) -> str:
    active_group = active.split("/")[0] if active else None
    parts = [
        '<aside class="sidebar">',
        '<input type="checkbox" id="nav-toggle" class="nav-toggle">',
        '<div class="sidebar-brand">',
        f'<a href="{index_href(depth)}">x-one notes</a>',
        '<label for="nav-toggle" class="menu-btn" aria-label="Toggle navigation">'
        '<span class="menu-open">Menu</span><span class="menu-close">Close</span></label>',
        "</div><nav>",
    ]
    for group, items in NAV:
        open_attr = " open" if group == active_group else ""
        parts.append(
            f'<details class="nav-group"{open_attr}><summary class="nav-group-label">'
            f'{html.escape(group)}<span class="nav-count">{len(items)}</span></summary><ul>'
        )
        for slug, label in items:
            href = page_href(depth, group, slug)
            key = f"{group}/{slug}"
            if active == key:
                parts.append(
                    f'<li><a href="{href}" class="is-active" aria-current="page">{html.escape(label)}</a></li>'
                )
            else:
                parts.append(f'<li><a href="{href}">{html.escape(label)}</a></li>')
        parts.append("</ul></details>")
    parts.append("</nav></aside>")
    return "".join(parts)


def render_page(
    *,
    title: str,
    depth: int,
    active: str | None,
    crumb: str | None,
    user_html: str | None,
    assistant_html: str,
    placeholder: bool = False,
) -> str:
    assistant_class = "message assistant placeholder" if placeholder else "message assistant"
    user_block = ""
    if user_html is not None:
        user_block = f"""
    <article class="message user">
      <div class="message-label">You</div>
      <div class="message-body">{user_html}</div>
    </article>"""
    head_block = ""
    if crumb:
        head_block = f'<header class="page-head"><span class="crumb">{html.escape(crumb)}</span></header>'
    assistant_label = '<div class="message-label">Assistant</div>' if user_html is not None else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} · x-one</title>
  {FONT_HEAD}
  <link rel="icon" href="{'../' * depth}assets/favicon.svg" type="image/svg+xml">
  <link rel="stylesheet" href="{css_href(depth)}">
  <script src="{'../' * depth}assets/copy.js" defer></script>
</head>
<body>
  {render_sidebar(active, depth)}
  <main class="chat">
    {head_block}{user_block}
    <article class="{assistant_class}">
      {assistant_label}
      <div class="message-body prose">{assistant_html}</div>
    </article>
  </main>
</body>
</html>
"""


def render_index() -> str:
    sidebar = render_sidebar(None, 0)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>x-one notes</title>
  {FONT_HEAD}
  <link rel="icon" href="assets/favicon.svg" type="image/svg+xml">
  <link rel="stylesheet" href="assets/style.css">
  <script src="assets/copy.js" defer></script>
</head>
<body>
  {sidebar}
  <main class="chat">
    <header class="page-head"><span class="crumb">home</span></header>
    <div class="landing-intro">
      <p>Local static notes. Pick a topic in the sidebar.</p>
    </div>
  </main>
</body>
</html>
"""


def find_source(group: str, slug: str) -> Path | None:
    for ext in (".txt", ".md"):
        path = SOURCES / group / f"{slug}{ext}"
        if path.is_file():
            return path
    return None


def is_note(source: Path, text: str) -> bool:
    return source.suffix == ".md" and text.lstrip().startswith("# ")


def page_label(group: str, slug: str, source: Path | None) -> str:
    if f"{group}/{slug}" in LABELS:
        return LABELS[f"{group}/{slug}"]
    if source is not None:
        title = re.search(r"^#\s+(.+?)\s*$", source.read_text(encoding="utf-8"), re.M)
        if title:
            return title.group(1)
    return slug.replace("-", " ").capitalize()


def build_nav() -> list[tuple[str, list[tuple[str, str]]]]:
    slugs: dict[str, set[str]] = {}
    for path in list(SOURCES.glob("*/*")) + list(SITE.glob("*/*.html")):
        if path.suffix in (".txt", ".md", ".html"):
            slugs.setdefault(path.parent.name, set()).add(path.stem)

    order = list(LABELS)
    nav = []
    for group in sorted(slugs):
        items = [(slug, page_label(group, slug, find_source(group, slug))) for slug in slugs[group]]
        items.sort(
            key=lambda item: (0, order.index(f"{group}/{item[0]}"), "")
            if f"{group}/{item[0]}" in LABELS
            else (1, 0, item[1].casefold())
        )
        nav.append((group, items))
    return nav


SIDEBAR_RE = re.compile(r'<aside class="sidebar">.*?</aside>', re.S)


def main():
    NAV[:] = build_nav()
    (SITE / "index.html").write_text(render_index(), encoding="utf-8")

    for group, items in NAV:
        out_dir = SITE / group
        out_dir.mkdir(parents=True, exist_ok=True)
        for slug, label in items:
            out_path = out_dir / f"{slug}.html"
            source = find_source(group, slug)
            if source is None:
                sidebar = render_sidebar(f"{group}/{slug}", 1)
                existing = out_path.read_text(encoding="utf-8")
                page = SIDEBAR_RE.sub(lambda _: sidebar, existing, count=1)
                if 'rel="icon"' not in page:
                    page = page.replace(
                        '<link rel="stylesheet"',
                        '<link rel="icon" href="../assets/favicon.svg" type="image/svg+xml">\n  <link rel="stylesheet"',
                        1,
                    )
                out_path.write_text(page, encoding="utf-8")
                print("sidebar refreshed", out_path.relative_to(SITE))
                continue
            text = source.read_text(encoding="utf-8")
            user, body = (None, text) if is_note(source, text) else parse_chat(text)
            page = render_page(
                title=label,
                depth=1,
                active=f"{group}/{slug}",
                crumb=f"{group} / {slug}",
                user_html=html.escape(user) if user else None,
                assistant_html=md_to_html(body),
            )
            out_path.write_text(page, encoding="utf-8")
            print("wrote", out_path.relative_to(SITE))


if __name__ == "__main__":
    main()
