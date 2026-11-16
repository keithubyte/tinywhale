"""Small, dependency-free LaTeX-ish display renderer for the chat block formulas."""
from __future__ import annotations

import re

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
gi.require_version("Graphene", "1.0")

from gi.repository import Graphene, Gtk, Pango  # noqa: E402


SYMBOLS = {
    "int": "∫", "iint": "∬", "sum": "∑", "prod": "∏", "infty": "∞",
    "pi": "π", "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ",
    "epsilon": "ε", "theta": "θ", "lambda": "λ", "mu": "μ", "sigma": "σ",
    "omega": "ω", "phi": "φ", "psi": "ψ", "cdot": "·", "times": "×",
    "div": "÷", "pm": "±", "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥",
    "neq": "≠", "ne": "≠", "approx": "≈", "equiv": "≡", "in": "∈",
    "notin": "∉", "subset": "⊂", "supset": "⊃", "cup": "∪", "cap": "∩",
    "to": "→", "rightarrow": "→", "leftarrow": "←", "Rightarrow": "⇒",
    "forall": "∀", "exists": "∃", "partial": "∂", "nabla": "∇",
    "ldots": "…", "cdots": "⋯", "dots": "…", "quad": "\u2003", "qquad": "\u2003\u2003",
}
FUNCS = {"sin", "cos", "tan", "cot", "sec", "csc", "log", "ln", "exp",
         "lim", "max", "min", "det", "gcd"}
OPERATORS = {"=", "+", "-", "<", ">", "≤", "≥", "≈", "≠", "×", "÷", "·", "→"}

MATH_FAMILY = "DejaVu Serif"


def set_math_family(family: str) -> None:
    global MATH_FAMILY
    MATH_FAMILY = family or "DejaVu Serif"


class Pen:
    """Tiny drawing context that turns our horizontal rules into GtkSnapshot rects."""

    def __init__(self, snapshot, color):
        self.snapshot = snapshot
        self.color = color
        self.line_width = 1.0
        self._path = []

    def draw_layout(self, layout, x, y):
        self.snapshot.save()
        point = Graphene.Point().init(float(x), float(y))
        self.snapshot.translate(point)
        self.snapshot.append_layout(layout, self.color)
        self.snapshot.restore()

    def set_line_width(self, width):
        self.line_width = float(width)

    def move_to(self, x, y):
        self._path = [(float(x), float(y))]

    def line_to(self, x, y):
        self._path.append((float(x), float(y)))

    def stroke(self):
        points = self._path
        self._path = []
        for (x1, y1), (x2, y2) in zip(points, points[1:]):
            if abs(y2 - y1) < 0.75:
                rect = Graphene.Rect().init(
                    min(x1, x2), y1 - self.line_width / 2,
                    abs(x2 - x1), max(1.0, self.line_width))
                self.snapshot.append_color(self.color, rect)
            elif abs(x2 - x1) < 0.75:
                rect = Graphene.Rect().init(
                    x1 - self.line_width / 2, min(y1, y2),
                    max(1.0, self.line_width), abs(y2 - y1))
                self.snapshot.append_color(self.color, rect)


class Box:
    def __init__(self, width, ascent, descent, draw, spaced=False):
        self.width = width
        self.ascent = ascent
        self.descent = descent
        self.draw = draw
        self.spaced = spaced

    @property
    def height(self):
        return self.ascent + self.descent


def _font(ctx, size, italic=False, family=None):
    layout = Pango.Layout.new(ctx)
    desc = Pango.FontDescription()
    desc.set_family(family or MATH_FAMILY)
    desc.set_absolute_size(size * Pango.SCALE)
    desc.set_style(Pango.Style.ITALIC if italic else Pango.Style.NORMAL)
    layout.set_font_description(desc)
    return layout


def _text_box(ctx, text, size, italic=False, family=None, spaced=False):
    if not text:
        return Box(0, 0, 0, lambda cr, x, y: None, spaced)
    layout = _font(ctx, size, italic, family)
    layout.set_text(text, -1)
    width, height = layout.get_pixel_size()
    baseline = layout.get_baseline()
    if baseline > height:
        baseline /= Pango.SCALE
    ascent = baseline if baseline else height * 0.78
    descent = max(0.0, height - ascent)

    def draw(cr, x, y):
        cr.draw_layout(layout, x, y - ascent)

    return Box(width, ascent, descent, draw, spaced)


def _sequence(ctx, children, size):
    boxes = []
    for child in children:
        boxes.append(child)
    gap = size * 0.14
    width = 0.0
    ascent = 0.0
    descent = 0.0
    for index, box in enumerate(boxes):
        if index:
            width += gap if (box.spaced or boxes[index - 1].spaced) else size * 0.02
        width += box.width
        ascent = max(ascent, box.ascent)
        descent = max(descent, box.descent)

    def draw(cr, x, y):
        cursor = x
        for index, box in enumerate(boxes):
            if index:
                cursor += gap if (box.spaced or boxes[index - 1].spaced) else size * 0.02
            box.draw(cr, cursor, y)
            cursor += box.width

    return Box(width, ascent, descent, draw)


def _fraction(ctx, numerator, denominator, size):
    num = _layout(ctx, numerator, size * 0.88)
    den = _layout(ctx, denominator, size * 0.88)
    pad = size * 0.10
    width = max(num.width, den.width) + pad * 2
    gap = size * 0.10
    ascent = num.height + gap
    descent = den.height + gap

    def draw(cr, x, y):
        bar_y = y - descent + den.height + gap * 0.45
        num.draw(cr, x + (width - num.width) / 2, bar_y - gap * 0.3)
        cr.set_line_width(max(1.0, size * 0.045))
        cr.move_to(x + pad * 0.25, bar_y)
        cr.line_to(x + width - pad * 0.25, bar_y)
        cr.stroke()
        den.draw(cr, x + (width - den.width) / 2, bar_y + gap * 0.8 + den.ascent)

    return Box(width, ascent, descent, draw)


def _scripts(ctx, base, sup, sub, size, limits=False):
    sup_box = _layout(ctx, sup, size * 0.62) if sup else None
    sub_box = _layout(ctx, sub, size * 0.62) if sub else None
    if limits and base.width > 0 and (base.height > size * 1.2 or True):
        # display-style limits for \sum and \prod
        sup_w = sup_box.width if sup_box else 0
        sub_w = sub_box.width if sub_box else 0
        width = max(base.width, sup_w, sub_w)
        ascent = (sup_box.height if sup_box else 0) + base.ascent * 0.72
        descent = (sub_box.height if sub_box else 0) + base.descent * 0.6

        def draw(cr, x, y):
            base.draw(cr, x + (width - base.width) / 2, y)
            if sup_box:
                sup_box.draw(cr, x + (width - sup_box.width) / 2,
                             y - base.ascent * 0.72 - sup_box.height * 0.2)
            if sub_box:
                sub_box.draw(cr, x + (width - sub_box.width) / 2,
                             y + base.descent + sub_box.ascent + size * 0.08)

        return Box(width, ascent + base.ascent * 0.28,
                   descent + base.descent * 0.4, draw)

    right = max(sup_box.width if sup_box else 0, sub_box.width if sub_box else 0)
    width = base.width + right + size * 0.03
    ascent = max(base.ascent, (sup_box.height + size * 0.28) if sup_box else 0)
    descent = max(base.descent, (sub_box.height + size * 0.16) if sub_box else 0)

    def draw(cr, x, y):
        base.draw(cr, x, y)
        if sup_box:
            sup_box.draw(cr, x + base.width + size * 0.03,
                         y - size * 0.28)
        if sub_box:
            sub_box.draw(cr, x + base.width + size * 0.03,
                         y + size * 0.16 + sub_box.ascent)

    return Box(width, ascent, descent, draw)


def _sqrt(ctx, child_node, size):
    child = _layout(ctx, child_node, size)
    radical = _text_box(ctx, "√", size * 1.25)
    width = radical.width * 0.7 + child.width + size * 0.08
    ascent = child.ascent + size * 0.16
    descent = child.descent

    def draw(cr, x, y):
        radical.draw(cr, x, y + size * 0.04)
        cr.set_line_width(max(1.0, size * 0.04))
        cr.move_to(x + radical.width * 0.62, y - ascent)
        cr.line_to(x + width, y - ascent)
        child.draw(cr, x + radical.width * 0.7, y)

    return Box(width, ascent, descent, draw)


def _matrix(ctx, rows, size):
    cell_boxes = []
    for row in rows:
        cell_boxes.append([_layout(ctx, cell, size * 0.92) for cell in row])
    columns = max((len(row) for row in cell_boxes), default=1)
    col_widths = []
    for column in range(columns):
        widths = [row[column].width for row in cell_boxes if column < len(row)]
        col_widths.append(max(widths or [0]))
    row_heights = [max((box.height for box in row), default=size) for row in cell_boxes]
    gap_x = size * 0.35
    gap_y = size * 0.12
    inner_w = sum(col_widths) + gap_x * max(0, columns - 1)
    inner_h = sum(row_heights) + gap_y * max(0, len(row_heights) - 1)
    bracket = _text_box(ctx, "(", max(size * 1.4, inner_h * 0.9))
    bracket_r = _text_box(ctx, ")", max(size * 1.4, inner_h * 0.9))
    width = bracket.width + inner_w + bracket_r.width + size * 0.10
    ascent = inner_h / 2 + size * 0.35
    descent = inner_h / 2 + size * 0.25

    def draw(cr, x, y):
        bracket.draw(cr, x, y + inner_h / 2 - bracket.ascent * 0.05)
        cx = x + bracket.width + size * 0.05
        row_y = y - inner_h / 2
        for row_index, row in enumerate(cell_boxes):
            baseline = row_y + row_heights[row_index] / 2
            cursor = cx
            for column in range(columns):
                box = row[column] if column < len(row) else None
                if box:
                    box.draw(cr, cursor + (col_widths[column] - box.width) / 2, baseline)
                cursor += col_widths[column] + gap_x
            row_y += row_heights[row_index] + gap_y
        bracket_r.draw(cr, x + width - bracket_r.width, y + inner_h / 2 - bracket_r.ascent * 0.05)

    return Box(width, ascent, descent, draw)


def _aligned(ctx, rows, size):
    parsed = []
    for row in rows:
        parsed.append([_layout(ctx, cell, size) for cell in row])
    columns = max((len(row) for row in parsed), default=1)
    col_widths = []
    for column in range(columns):
        widths = [row[column].width for row in parsed if column < len(row)]
        col_widths.append(max(widths or [0]))
    gap = size * 0.18
    row_heights = [max((box.height for box in row), default=size) for row in parsed]
    width = sum(col_widths) + gap * max(0, columns - 1)
    total_h = sum(row_heights) + gap * max(0, len(row_heights) - 1)
    ascent = total_h / 2 + size * 0.2
    descent = total_h / 2 + size * 0.1

    def draw(cr, x, y):
        row_y = y - total_h / 2
        for index, row in enumerate(parsed):
            baseline = row_y + row_heights[index] / 2
            cursor = x
            for column in range(columns):
                box = row[column] if column < len(row) else None
                if box:
                    offset = 0 if column == 0 and len(row) == 1 else 0
                    box.draw(cr, cursor + offset, baseline)
                cursor += col_widths[column] + gap
            row_y += row_heights[index] + gap

    return Box(width, ascent, descent, draw)


def _layout(ctx, node, size):
    if node is None:
        return Box(0, 0, 0, lambda cr, x, y: None)
    kind = node[0]
    if kind == "text":
        _, text, style, spaced = node
        italic = style == "var"
        family = "DejaVu Sans" if style == "text" else "DejaVu Serif"
        return _text_box(ctx, text, size, italic, family, spaced)
    if kind == "space":
        return Box(size * node[1], 0, 0, lambda cr, x, y: None)
    if kind == "seq":
        return _sequence(ctx, [_layout(ctx, child, size) for child in node[1]], size)
    if kind == "frac":
        return _fraction(ctx, node[1], node[2], size)
    if kind == "sqrt":
        return _sqrt(ctx, node[1], size)
    if kind == "script":
        _, base, sup, sub = node
        return _scripts(ctx, _layout(ctx, base, size), sup, sub, size, False)
    if kind == "op":
        _, symbol, sub, sup, display = node
        base = _text_box(ctx, SYMBOLS.get(symbol, symbol), size * 1.45)
        return _scripts(ctx, base, sup, sub, size, display)
    if kind == "matrix":
        return _matrix(ctx, node[1], size)
    if kind == "aligned":
        return _aligned(ctx, node[1], size)
    return Box(0, 0, 0, lambda cr, x, y: None)


class MathParser:
    def __init__(self, text):
        self.text = text
        self.i = 0

    def skip(self):
        while self.i < len(self.text) and self.text[self.i].isspace():
            self.i += 1

    def parse_sequence(self, stop=None):
        children = []
        while self.i < len(self.text):
            self.skip()
            if self.i >= len(self.text):
                break
            char = self.text[self.i]
            if stop and char in stop:
                break
            node = self.parse_atom()
            if node is None:
                continue
            sub = sup = None
            while True:
                self.skip()
                if self.i >= len(self.text) or self.text[self.i] not in "_^":
                    break
                marker = self.text[self.i]
                self.i += 1
                arg = self.parse_group()
                if marker == "_":
                    sub = arg
                else:
                    sup = arg
            if sub is not None or sup is not None:
                if node[0] == "op":
                    node = ("op", node[1], sub, sup, node[3])
                else:
                    node = ("script", node, sup, sub)
            children.append(node)
        return ("seq", children)

    def parse_group(self):
        self.skip()
        if self.i >= len(self.text):
            return None
        if self.text[self.i] == "{":
            self.i += 1
            node = self.parse_sequence("}")
            if self.i < len(self.text) and self.text[self.i] == "}":
                self.i += 1
            return node
        return self.parse_atom()

    def parse_atom(self):
        self.skip()
        if self.i >= len(self.text):
            return None
        char = self.text[self.i]
        if char == "{":
            self.i += 1
            node = self.parse_sequence("}")
            if self.i < len(self.text) and self.text[self.i] == "}":
                self.i += 1
            return node
        if char == "\\":
            return self.parse_command()
        if char in "_^":
            self.i += 1
            return self.parse_group()
        if char in OPERATORS:
            self.i += 1
            return ("text", char, "op", True)
        if char == " ":
            self.i += 1
            return ("space", 0.18)
        if char == "&":
            self.i += 1
            return None
        start = self.i
        while self.i < len(self.text) and (self.text[self.i].isalnum() or self.text[self.i] in "."):
            self.i += 1
        if self.i == start:
            self.i += 1
            return ("text", char, "op", False)
        text = self.text[start:self.i]
        style = "var" if any(ch.isalpha() for ch in text) else "num"
        return ("text", text, style, False)

    def parse_command(self):
        self.i += 1
        start = self.i
        while self.i < len(self.text) and self.text[self.i].isalpha():
            self.i += 1
        name = self.text[start:self.i]
        if not name:
            if self.i < len(self.text) and self.text[self.i] in ",;! ":
                self.i += 1
                return ("space", 0.14)
            if self.i < len(self.text) and self.text[self.i] == "\\":
                self.i += 1
                return ("space", 0.0)
            return None
        if name in ("frac",):
            return ("frac", self.parse_group(), self.parse_group())
        if name == "sqrt":
            return ("sqrt", self.parse_group())
        if name == "text":
            self.skip()
            if self.i < len(self.text) and self.text[self.i] == "{":
                depth = 1
                self.i += 1
                start = self.i
                while self.i < len(self.text) and depth:
                    if self.text[self.i] == "{":
                        depth += 1
                    elif self.text[self.i] == "}":
                        depth -= 1
                    self.i += 1
                content = self.text[start:self.i - 1]
            else:
                content = ""
            return ("text", content, "text", False)
        if name in ("begin",):
            self.skip()
            if self.i < len(self.text) and self.text[self.i] == "{":
                end = self.text.find("}", self.i)
                env = self.text[self.i + 1:end]
                self.i = end + 1
                end_token = "\\end{%s}" % env
                end = self.text.find(end_token, self.i)
                if end == -1:
                    content = self.text[self.i:]
                    self.i = len(self.text)
                else:
                    content = self.text[self.i:end]
                    self.i = end + len(end_token)
            else:
                env = "pmatrix"
                content = ""
            rows = _split_rows(content)
            parsed_rows = [
                [MathParser(cell).parse_sequence() for cell in row]
                for row in rows
            ]
            if env == "aligned":
                return ("aligned", parsed_rows)
            return ("matrix", parsed_rows)
        if name in ("int", "sum", "prod"):
            return ("op", name, None, None, name in ("sum", "prod"))
        if name in ("left", "right"):
            self.skip()
            if self.i < len(self.text):
                delimiter = self.text[self.i]
                self.i += 1
                return ("text", delimiter, "op", False)
            return None
        if name in FUNCS:
            return ("text", name, "text", True)
        symbol = SYMBOLS.get(name)
        if symbol is not None:
            return ("text", symbol, "op", symbol in OPERATORS or name in ("cdot", "times", "pm", "le", "ge", "neq", "to", "approx"))
        return ("text", name, "text", False)


def _split_rows(content):
    rows = []
    row = []
    cell = []
    depth = 0
    i = 0
    while i < len(content):
        char = content[i]
        if char == "{":
            depth += 1
            cell.append(char)
        elif char == "}":
            depth -= 1
            cell.append(char)
        elif char == "&" and depth == 0:
            row.append("".join(cell).strip())
            cell = []
        elif char == "\\" and depth == 0:
            if i + 1 < len(content) and content[i + 1] == "\\":
                row.append("".join(cell).strip())
                cell = []
                rows.append(row)
                row = []
                i += 1
            else:
                cell.append(char)
        else:
            cell.append(char)
        i += 1
    if cell or row:
        row.append("".join(cell).strip())
        rows.append(row)
    return [row for row in rows if any(cell for cell in row)] or [[""]]


class MathView(Gtk.DrawingArea):
    def __init__(self, source: str, size: int = 22):
        super().__init__()
        self.source = source
        self.font_size = size
        self._box = None
        self.set_hexpand(True)
        self.set_halign(Gtk.Align.FILL)
        try:
            ctx = self.create_pango_context()
            self._build(ctx)
        except Exception:
            pass

    def _build(self, ctx):
        parser = MathParser(self.source)
        node = parser.parse_sequence()
        box = _layout(ctx, node, self.font_size)
        self._box = box
        self.set_content_width(int(box.width) + 24)
        self.set_content_height(int(box.height) + 18)

    def do_snapshot(self, snapshot):
        if self._box is None:
            try:
                self._build(self.create_pango_context())
            except Exception:
                return
        box = self._box
        width = self.get_width()
        height = self.get_height()
        x = max(10.0, (width - box.width) / 2)
        y = (height - box.height) / 2 + box.ascent
        color = self.get_style_context().get_color()
        pen = Pen(snapshot, color)
        box.draw(pen, x, y)


def math_widget(source: str, size: int = 22) -> Gtk.Widget:
    return MathView(source, size)
