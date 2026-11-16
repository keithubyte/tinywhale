"""Re-flow a conversation into a fixed-width page and encode it as JPEG.

Text is measured and drawn with Cairo.  This keeps the configured font family
(including CJK families) and uses the same block structure as the native body.
No GTK widget screenshot is involved.
"""
from __future__ import annotations

import html
import io
import os
import re
import subprocess

import cairo
import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, Gio, GLib  # noqa: E402

PAGE_WIDTH = 1080
MARGIN = 56
CONTENT_WIDTH = PAGE_WIDTH - MARGIN * 2
BODY_SIZE = 36
CODE_SIZE = 27

_code_words = {
    "def", "class", "return", "if", "elif", "else", "for", "while", "in",
    "not", "and", "or", "is", "None", "True", "False", "import", "from",
    "as", "try", "except", "finally", "with", "lambda", "yield", "async",
    "await", "pass", "break", "continue", "raise", "global", "nonlocal",
    "del", "assert", "const", "let", "var", "function", "new", "this",
    "typeof", "instanceof", "null", "undefined", "extends", "super",
    "switch", "case", "default", "do", "public", "private", "protected",
    "static", "void", "int", "float", "double", "char", "long", "struct",
    "enum", "typedef", "include", "namespace", "using", "echo", "sudo",
}

def _detect_emoji_font() -> str:
    for candidate in ("Noto Color Emoji", "Noto Emoji", "Apple Color Emoji",
                      "Segoe UI Emoji"):
        try:
            result = subprocess.run(
                ["fc-match", "-f", "%{family}", candidate],
                capture_output=True, text=True, timeout=2)
            if result.returncode == 0 and candidate.lower() in result.stdout.lower():
                return candidate
        except Exception:
            pass
    return "Noto Color Emoji"


EMOJI_FONT = _detect_emoji_font()
_measure_surface = cairo.ImageSurface(cairo.FORMAT_RGB24, 1, 1)
_measure_ctx = cairo.Context(_measure_surface)
_advance_cache: dict[tuple[str, str, int, bool], float] = {}


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _hex(value: str):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _strip_inline(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"\\\((.+?)\\\)",
                  lambda m: "$" + m.group(1) + "$", text)
    text = re.sub(r"\\([\\`*{}\[\]()#+\-.!_>])", r"\1", text)
    text = re.sub(r"<https?://[^>]+>", lambda m: m.group(0)[1:-1], text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"[图片:\1]", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = re.sub(r"_([^_]+)_", r"\1", text)
    text = re.sub(r"~~([^~]+)~~", r"\1", text)
    return text


def _native_list(body: str):
    items = []
    for line in body.split("\n"):
        match = re.match(r"^\s*([-*+]|\d+\.)\s+(.*)$", line)
        if match:
            items.append((match.group(1), match.group(2)))
    return items


def _native_table(body: str):
    rows = []
    for line in body.split("\n"):
        if not line.strip():
            continue
        row = line.strip().strip("|")
        cells = [cell.replace("\\|", "|").strip()
                 for cell in re.split(r"(?<!\\)\|", row)]
        if not all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            rows.append(cells)
    return rows


def _blocks(text: str):
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        fence = re.match(r"^(\s*)(`{3,}|~{3,})(.*)$", line)
        if fence and (fence.group(2)[0] != "`" or "`" not in fence.group(3)):
            char, count = fence.group(2)[0], len(fence.group(2))
            closing = re.compile(r"^\s*%s{%d,}\s*$" % (re.escape(char), count))
            lang = fence.group(3).strip()
            i += 1
            body = []
            while i < len(lines) and not closing.match(lines[i]):
                body.append(lines[i])
                i += 1
            i += 1
            yield ("code", lang, "\n".join(body).rstrip())
            continue
        if stripped.startswith("$$") and stripped.endswith("$$") and len(stripped) > 4:
            yield ("math", "", stripped[2:-2].strip())
            i += 1
            continue
        if stripped.startswith("\\["):
            i += 1
            body = []
            while i < len(lines) and "\\]" not in lines[i]:
                body.append(lines[i])
                i += 1
            i += 1
            yield ("math", "", "\n".join(body).strip())
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            yield ("heading", heading.group(1), heading.group(2))
            i += 1
            continue
        if re.match(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$", line):
            yield ("hr", "", "")
            i += 1
            continue
        if re.match(r"^\s*([-*+]|\d+\.)\s+", line):
            items = []
            while i < len(lines) and re.match(r"^\s*([-*+]|\d+\.)\s+", lines[i]):
                m = re.match(r"^\s*([-*+]|\d+\.)\s+(.*)$", lines[i])
                items.append((m.group(1), m.group(2)))
                i += 1
            yield ("list", "", items)
            continue
        if stripped.startswith("|") and i + 1 < len(lines) and "---" in lines[i + 1]:
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                row = lines[i].strip().strip("|")
                cells = [c.strip() for c in re.split(r"(?<!\\)\|", row)]
                if not all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
                    rows.append(cells)
                i += 1
            yield ("table", "", rows)
            continue
        if stripped.startswith(">"):
            body = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                body.append(lines[i].lstrip()[1:].lstrip())
                i += 1
            yield ("quote", "", "\n".join(body))
            continue
        para = []
        while i < len(lines) and lines[i].strip():
            if re.match(r"^\s*(`{3,}|~{3,}|#{1,6}\s|>|[-*+]\s|\d+\.\s)", lines[i]):
                break
            para.append(_strip_inline(lines[i]))
            i += 1
        yield ("p", "", "\n".join(para))


def _code_spans(line, lang, dark):
    if lang.strip().lower() in ("markdown", "md", "text", "plaintext"):
        return [(line, "#E7EBF5" if dark else "#202126")]
    palette = (("#FF7AB2", "#82AAFF", "#F6C177", "#A6E3A1", "#8A93A8")
               if dark else ("#A23A8A", "#2F5BD0", "#9A5B00", "#2E7D32", "#7A8496"))
    keyword, function, number, string, comment = palette
    pattern = re.compile(
        r'(?P<comment>#.*|//.*|<!--.*?-->)'
        r'|(?P<string>"(?:\\\\.|[^"\\\\])*"|\'(?:\\\\.|[^\'\\\\])*\')'
        r'|(?P<number>\b(?:0x[0-9A-Fa-f]+|\d+(?:\.\d+)?)\b)'
        r'|(?P<word>\b[A-Za-z_]\w*\b)'
        r'|(?P<plain>.)', re.S)
    spans = []
    for match in pattern.finditer(line):
        group = match.lastgroup
        text = match.group(0)
        color = None
        if group == "comment":
            color = comment
        elif group == "string":
            color = string
        elif group == "number":
            color = number
        elif group == "word":
            if text in _code_words:
                color = keyword
            elif re.match(r"\s*\(", line[match.end():]):
                color = function
        if color is None:
            color = palette[-1]
        if spans and spans[-1][1] == color:
            spans[-1] = (spans[-1][0] + text, color)
        else:
            spans.append((text, color))
    return spans


def _font(ctx, family, size, bold=False):
    ctx.select_font_face(
        family or "Sans", cairo.FONT_SLANT_NORMAL,
        cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL)
    ctx.set_font_size(max(6, size))


def _advance(text, family, size, bold=False):
    key = (text, family, int(size), bool(bold))
    cached = _advance_cache.get(key)
    if cached is not None:
        return cached
    _font(_measure_ctx, family, size, bold)
    width = _measure_ctx.text_extents(text)[4]
    _advance_cache[key] = width
    return width


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (0x2E80 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF
            or 0xF900 <= code <= 0xFAFF or 0xFF00 <= code <= 0xFFEF)


def _is_emoji(ch: str) -> bool:
    code = ord(ch)
    return (0x1F000 <= code <= 0x1FAFF or 0x2600 <= code <= 0x27BF
            or 0x2B00 <= code <= 0x2BFF or 0xFE0F == code
            or 0x1F1E6 <= code <= 0x1F1FF)


def _font_runs(text, primary, fallback):
    runs = []
    current = ""
    current_family = None
    for ch in text:
        if _is_emoji(ch):
            family = EMOJI_FONT
        elif _is_cjk(ch):
            family = fallback
        else:
            family = primary
        if current and family != current_family:
            runs.append((current, current_family))
            current = ""
        current += ch
        current_family = family
    if current:
        runs.append((current, current_family))
    return runs


def _advance_mixed(text, family, fallback, size, bold=False):
    total = 0.0
    for run, run_family in _font_runs(text, family, fallback):
        total += _advance(run, run_family, size, bold)
    return total


def _wrap_cairo(text, width, family, size, bold=False,
                preserve_indent=False, fallback_family=None):
    lines = []
    for paragraph in text.split("\n"):
        if paragraph == "":
            lines.append("")
            continue
        current = ""
        current_w = 0.0
        for token in re.findall(r"\s+|[^\s]+", paragraph):
            if token.isspace():
                if current:
                    current += " "
                    current_w += _advance(" ", family, size, bold)
                elif preserve_indent:
                    current = token
                    current_w = _advance(token, family, size, bold)
                continue
            token_w = (_advance_mixed(token, family, fallback_family, size, bold)
                       if fallback_family else _advance(token, family, size, bold))
            if token_w > width:
                if current:
                    lines.append(current.rstrip())
                    current, current_w = "", 0.0
                piece = ""
                piece_w = 0.0
                for ch in token:
                    cw = (_advance_mixed(ch, family, fallback_family, size, bold)
                          if fallback_family else _advance(ch, family, size, bold))
                    if piece and piece_w + cw > width:
                        lines.append(piece)
                        piece, piece_w = ch, cw
                    else:
                        piece += ch
                        piece_w += cw
                current, current_w = piece, piece_w
                continue
            if current and current_w + token_w > width:
                lines.append(current.rstrip())
                current, current_w = token, token_w
            else:
                current += token
                current_w += token_w
        lines.append(current.rstrip())
    return lines or [""]


def _image_surface(pixbuf):
    if pixbuf is None:
        return None
    try:
        ok, data = pixbuf.save_to_bufferv("png", [], [])
        if not ok:
            return None
        return cairo.ImageSurface.create_from_png(io.BytesIO(bytes(data)))
    except Exception:
        return None



_math_cache = {}


def _inline_math_surface(formula, size, dark):
    key = (formula, size, dark)
    if key in _math_cache:
        return _math_cache[key]
    try:
        import mathjax_render
        em = max(16, round(size * 0.82))
        svg = mathjax_render._render_svg(formula, em)
        pix = mathjax_render._svg_to_pixbuf(
            svg, em, "#E7EBF5" if dark else "#202126") if svg else None
        if pix is not None:
            surf = _image_surface(pix)
            if surf is not None:
                result = (surf, pix.get_width(), pix.get_height())
                _math_cache[key] = result
                return result
    except Exception:
        pass
    _math_cache[key] = None
    return None


class Page:
    def __init__(self, dark, body_font, code_font, ui_font=None,
                 body_size=BODY_SIZE, code_size=CODE_SIZE,
                 line_spacing=1.55):
        self.dark = dark
        self.body_font = body_font or "Sans"
        self.code_font = code_font or "Monospace"
        self.ui_font = ui_font or self.body_font
        self.body_size = max(24, min(48, int(body_size)))
        self.code_size = max(18, min(42, int(code_size)))
        self.bg = _hex("#17171F" if dark else "#FAFAFC")
        self.fg = _hex("#E7EBF5" if dark else "#202126")
        self.muted = _hex("#9AA3B5" if dark else "#667085")
        self.card = _hex("#22232C" if dark else "#EDEFF5")
        self.bubble = _hex("#2D2B45" if dark else "#E7E5FA")
        self.accent = _hex("#7C8CFF" if dark else "#4D6BFE")
        self.ops = []
        self.y = MARGIN

    def _text(self, text, x, y, size, family, color, bold=False, width=None):
        if width is not None:
            rows = _wrap_cairo(text, width, family, size, bold)
        else:
            rows = text.split("\n")
        line_h = round(size * 1.55)
        for row in rows:
            self.ops.append(("text", x, y + size * 1.15, row, family,
                             size, color, bold))
            y += line_h
        return len(rows) * line_h

    def rect(self, x, y, w, h, color, radius=0):
        self.ops.append(("rect", x, y, w, h, color, radius))

    def image(self, surface, x, y, w, h):
        self.ops.append(("image", surface, x, y, w, h))

    def add_header(self, icon_path):
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file(icon_path)
            surf = _image_surface(pb)
            if surf is not None:
                self.image(surf, MARGIN, self.y, 88, 88)
        except Exception:
            pass
        self.y += 88 + 24

    def _rich_text(self, text, x, y, width, size=None, color=None,
                   family=None, bold=False, dry=False):
        size = self.body_size if size is None else size
        color = color or self.fg
        family = family or self.body_font
        line_h = round(size * 1.55)
        parts = re.split(r"(\$[^$\n]+\$|\\\([^\n]*?\\\))", text)
        tokens = []
        for part in parts:
            if not part:
                continue
            if ((part.startswith("$") and part.endswith("$"))
                    or (part.startswith("\\(") and part.endswith("\\)"))):
                formula = part[1:-1] if part.startswith("$") else part[2:-2]
                tokens.append(("math", formula.strip()))
            else:
                for token in re.findall(r"\s+|[^\s]+", part):
                    tokens.append(("text", token))

        cursor_x = x
        cursor_y = y
        used_h = line_h
        started = False

        def flush():
            nonlocal cursor_x, cursor_y, used_h, started
            cursor_y += used_h
            cursor_x = x
            used_h = line_h
            started = False

        for kind, value in tokens:
            if kind == "math":
                math = _inline_math_surface(value, size, self.dark)
                if math is None:
                    value = "$" + value + "$"
                    kind = "text"
                else:
                    surf, mw, mh = math
                    if mw > width:
                        scale = width / mw
                        mw, mh = width, round(mh * scale)
                    if started and cursor_x + mw > x + width:
                        flush()
                    top = cursor_y + max(0, (line_h - mh) / 2)
                    if not dry:
                        self.ops.append(("image", surf, cursor_x, top, mw, mh))
                    cursor_x += mw + 2
                    used_h = max(used_h, mh + 6)
                    started = True
                    continue
            if value.isspace():
                if started:
                    space_w = _advance(" ", family, size, bold)
                    if cursor_x + space_w <= x + width:
                        cursor_x += space_w
                continue
            token_w = _advance(value, family, size, bold)
            if token_w > width:
                if started:
                    flush()
                for ch in value:
                    cw = _advance(ch, family, size, bold)
                    if cursor_x + cw > x + width and cursor_x > x:
                        flush()
                    if not dry:
                        self.ops.append(("text", cursor_x,
                                         cursor_y + size * 1.15,
                                         ch, family, size, color, bold))
                    cursor_x += cw
                    started = True
                continue
            if started and cursor_x + token_w > x + width:
                flush()
            if not dry:
                self.ops.append(("text", cursor_x, cursor_y + size * 1.15,
                                 value, family, size, color, bold))
            cursor_x += token_w
            started = True
        return cursor_y + used_h - y

    def add_message(self, role, text, blocks=None):
        if role == "user":
            plain = _strip_inline(text)
            layout_width = CONTENT_WIDTH - 48
            rows = _wrap_cairo(plain, layout_width, self.body_font,
                               self.body_size)
            line_h = round(self.body_size * 1.55)
            widest = max((_advance(row, self.body_font, self.body_size)
                          for row in rows), default=0)
            bubble_w = min(CONTENT_WIDTH, max(280, widest + 48))
            bubble_h = len(rows) * line_h + 34
            bubble_x = PAGE_WIDTH - MARGIN - bubble_w
            self.rect(bubble_x, self.y, bubble_w, bubble_h, self.bubble, 24)
            rows = _wrap_cairo(plain, bubble_w - 48, self.body_font,
                               self.body_size)
            yy = self.y + 17
            for row in rows:
                self.ops.append(("text", bubble_x + 24, yy + self.body_size * 1.15,
                                 row, self.body_font, self.body_size, self.fg,
                                 False))
                yy += line_h
            self.y += bubble_h + 22
            return

        parsed = []
        if blocks is not None:
            for kind, body in blocks:
                if kind.startswith("code:"):
                    parsed.append(("code", kind.split(":", 1)[1], body))
                elif kind == "math":
                    parsed.append(("math", "", body))
                elif re.fullmatch(r"h[1-6]", kind):
                    parsed.append(("heading", kind[1], body))
                elif kind == "list":
                    parsed.append(("list", "", _native_list(body)))
                elif kind == "table":
                    parsed.append(("table", "", _native_table(body)))
                elif kind == "quote":
                    parsed.append(("quote", "", body))
                elif kind == "hr":
                    parsed.append(("hr", "", ""))
                else:
                    parsed.append(("p", "", body))
        else:
            parsed = list(_blocks(text))

        for kind, meta, body in parsed:
            if kind == "heading":
                size = int(self.body_size * {1: 1.28, 2: 1.17, 3: 1.08}.get(
                    int(meta), 1.0))
                h = self._text(_strip_inline(body), MARGIN, self.y, size,
                               self.body_font, self.fg, True, CONTENT_WIDTH)
                self.y += h + 12
            elif kind == "p":
                h = self._rich_text(_strip_inline(body), MARGIN, self.y,
                                    CONTENT_WIDTH, self.body_size,
                                    self.fg, self.body_font)
                self.y += h + 12
            elif kind == "quote":
                inner = _strip_inline(body)
                h = self._rich_text(inner, MARGIN + 22, self.y + 13,
                                    CONTENT_WIDTH - 44, self.body_size,
                                    self.fg, self.body_font, dry=True)
                self.rect(MARGIN, self.y, CONTENT_WIDTH, h + 26,
                          self.card, 10)
                self.rect(MARGIN, self.y, 4, h + 26, self.accent, 0)
                self._rich_text(inner, MARGIN + 22, self.y + 13,
                                CONTENT_WIDTH - 44, self.body_size,
                                self.fg, self.body_font)
                self.y += h + 26 + 14
            elif kind == "list":
                for marker, item in body:
                    bullet = "•" if marker in "-*+" else marker
                    prefix = bullet + " "
                    text_line = _strip_inline(item)
                    self.y += self._rich_text(
                        prefix + text_line, MARGIN + 12, self.y,
                        CONTENT_WIDTH - 30, self.body_size,
                        self.fg, self.body_font) + 4
                self.y += 8
            elif kind == "code":
                self.add_code(meta or "code", body)
            elif kind == "table":
                self.add_table(body)
            elif kind == "math":
                self.add_math(body)
            elif kind == "hr":
                self.rect(MARGIN, self.y + 12, CONTENT_WIDTH, 1, self.muted, 0)
                self.y += 30
        self.y += 20

    def add_code(self, lang, code):
        line_h = round(self.code_size * 1.48)
        rows = []
        for line in code.split("\n"):
            rows.extend(_wrap_cairo(
                line or " ", CONTENT_WIDTH - 40,
                self.code_font, self.code_size, preserve_indent=True,
                fallback_family=self.body_font))
        card_h = 54 + len(rows) * line_h + 16
        self.rect(MARGIN, self.y, CONTENT_WIDTH, card_h, self.card, 14)
        self._text(lang or "code", MARGIN + 18, self.y + 8,
                   self.body_size * 0.61, self.code_font, self.muted, True)
        yy = self.y + 54
        for row in rows:
            indent = len(row) - len(row.lstrip(" "))
            x = MARGIN + 20 + indent * self.code_size * 0.56
            tx = x
            for text, color in _code_spans(row, lang, self.dark):
                for run, run_family in _font_runs(
                        text, self.code_font, self.body_font):
                    self.ops.append(("text", tx, yy + self.code_size * 1.1,
                                     run, run_family, self.code_size,
                                     _hex(color), False))
                    tx += _advance(run, run_family, self.code_size)
            yy += line_h
        self.y += card_h + 16

    def add_table(self, rows):
        if not rows:
            return
        cols = max(len(r) for r in rows)
        col_w = CONTENT_WIDTH / max(1, cols)
        cell_size = int(self.body_size * 0.78)
        line_h = round(cell_size * 1.55)
        all_rows = []
        row_heights = []
        for ri, row in enumerate(rows):
            cells = []
            max_h = line_h
            for ci in range(cols):
                cell = _strip_inline(row[ci]) if ci < len(row) else ""
                cell_h = self._rich_text(
                    cell, 0, 0, col_w - 20, cell_size, self.fg,
                    self.body_font, bold=(ri == 0), dry=True)
                cells.append(cell)
                max_h = max(max_h, cell_h)
            all_rows.append(cells)
            row_heights.append(max_h + 20)
        card_h = sum(row_heights)
        self.rect(MARGIN, self.y, CONTENT_WIDTH, card_h, self.card, 10)
        yy = self.y
        for ri, cells in enumerate(all_rows):
            for ci, cell in enumerate(cells):
                self._rich_text(
                    cell, MARGIN + 10 + ci * col_w, yy + 10,
                    col_w - 20, cell_size, self.fg, self.body_font,
                    bold=(ri == 0))
            yy += row_heights[ri]
        self.y += card_h + 18

    def add_math(self, formula):
        try:
            import mathjax_render
            svg = mathjax_render._render_svg(formula, self.body_size)
            pix = mathjax_render._svg_to_pixbuf(
                svg, self.body_size,
                "#E7EBF5" if self.dark else "#202126") if svg else None
            if pix is not None:
                surf = _image_surface(pix)
                if surf is not None:
                    w, h = pix.get_width(), pix.get_height()
                    if w > CONTENT_WIDTH:
                        h = round(h * CONTENT_WIDTH / w)
                        w = CONTENT_WIDTH
                    self.image(surf, (PAGE_WIDTH - w) // 2, self.y, w, h)
                    self.y += h + 36
                    return
        except Exception:
            pass
        h = self._text(formula, MARGIN, self.y, int(self.body_size * 0.85),
                       self.body_font, self.muted, False, CONTENT_WIDTH)
        self.y += h + 20

    def finish(self):
        height = int(self.y + MARGIN)
        surface = cairo.ImageSurface(cairo.FORMAT_RGB24, PAGE_WIDTH, height)
        ctx = cairo.Context(surface)
        ctx.set_source_rgb(*self.bg)
        ctx.paint()
        for op in self.ops:
            if op[0] == "text":
                _kind, x, y, text, family, size, color, bold = op
                tx = x
                for run, run_family in _font_runs(text, family, self.body_font):
                    _font(ctx, run_family, size, bold)
                    ctx.set_source_rgb(*color)
                    ctx.move_to(tx, y)
                    ctx.show_text(run)
                    tx += _advance(run, run_family, size, bold)
            elif op[0] == "rect":
                _kind, x, y, w, h, color, radius = op
                ctx.set_source_rgb(*color)
                if radius:
                    r = min(radius, h / 2, w / 2)
                    ctx.new_sub_path()
                    ctx.arc(x + w - r, y + r, r, -1.5708, 0)
                    ctx.arc(x + w - r, y + h - r, r, 0, 1.5708)
                    ctx.arc(x + r, y + h - r, r, 1.5708, 3.1416)
                    ctx.arc(x + r, y + r, r, 3.1416, 4.7124)
                    ctx.close_path()
                else:
                    ctx.rectangle(x, y, w, h)
                ctx.fill()
            elif op[0] == "image":
                _kind, surf, x, y, w, h = op
                if surf is None:
                    continue
                iw, ih = surf.get_width(), surf.get_height()
                ctx.save()
                ctx.translate(x, y)
                if iw and ih:
                    ctx.scale(w / iw, h / ih)
                ctx.set_source_surface(surf, 0, 0)
                ctx.paint()
                ctx.restore()
        buf = io.BytesIO()
        surface.write_to_png(buf)
        return buf.getvalue(), height


def render_jpeg_bytes(messages, dark, body_font, code_font, icon_path,
                      ui_font=None, body_size=BODY_SIZE,
                      code_size=CODE_SIZE, line_spacing=1.55):
    page = Page(dark, body_font, code_font, ui_font, body_size, code_size,
                line_spacing)
    page.add_header(icon_path)
    for message in messages:
        page.add_message(
            message.get("role", "assistant"),
            message.get("text") or "",
            message.get("blocks"))
    png, _height = page.finish()
    try:
        stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(png))
        pix = GdkPixbuf.Pixbuf.new_from_stream(stream, None)
        ok, data = pix.save_to_bufferv("jpeg", ["quality"], ["90"])
        if ok:
            return bytes(data), ""
        return None, "JPEG 编码失败"
    except Exception as exc:
        return None, str(exc)
