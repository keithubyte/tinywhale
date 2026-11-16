"""Render block formulas with MathJax (SVG) through a small vendored Node helper."""
from __future__ import annotations

import atexit
import json
import os
import re
import subprocess
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("GdkPixbuf", "2.0")

from gi.repository import GdkPixbuf, Gtk  # noqa: E402


ROOT = os.path.dirname(os.path.abspath(__file__))
NODE = os.path.join(ROOT, "vendor", "node", "node")
HELPER = os.path.join(ROOT, "vendor", "mathjax-node", "render.js")
WORKDIR = os.path.join(ROOT, "vendor", "mathjax-node")

_lock = threading.Lock()
_process = None
_cache = {}


def _stop():
    global _process
    proc = _process
    _process = None
    if proc is not None:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
        except Exception:
            pass


atexit.register(_stop)


def _ensure_process():
    global _process
    if _process is not None and _process.poll() is None:
        return _process
    if not (os.path.exists(NODE) and os.path.exists(HELPER)):
        return None
    try:
        _process = subprocess.Popen(
            [NODE, HELPER],
            cwd=WORKDIR,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        return _process
    except Exception:
        _process = None
        return None


def _render_svg(source: str, em: int) -> str | None:
    with _lock:
        proc = _ensure_process()
        if proc is None:
            return None
        request = json.dumps({"tex": source, "display": True, "em": em},
                             ensure_ascii=False)
        try:
            proc.stdin.write(request + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
            if not line:
                _stop()
                return None
            answer = json.loads(line)
            if not answer.get("ok"):
                return None
            return answer.get("svg")
        except Exception:
            _stop()
            return None


def _svg_to_pixbuf(svg: str, em: int, color: str):
    match = re.search(r"<svg\b.*?</svg>", svg, re.S)
    if not match:
        return None
    inner = match.group(0)
    width_match = re.search(r'width="([\d.]+)ex"', inner)
    height_match = re.search(r'height="([\d.]+)ex"', inner)
    ex = em * 0.5
    if width_match and height_match:
        width = max(1, round(float(width_match.group(1)) * ex))
        height = max(1, round(float(height_match.group(1)) * ex))
        inner = re.sub(r'width="[\d.]+ex"', 'width="%d"' % width, inner, count=1)
        inner = re.sub(r'height="[\d.]+ex"', 'height="%d"' % height, inner, count=1)
    else:
        width = 600
        height = 100
    inner = inner.replace("currentColor", color)
    inner = inner.replace("<svg ", '<svg xmlns="http://www.w3.org/2000/svg" ', 1) \
        if "xmlns=" not in inner.split(">", 1)[0] else inner
    loader = GdkPixbuf.PixbufLoader.new_with_type("svg")
    try:
        loader.write(inner.encode("utf-8"))
        loader.close()
        pixbuf = loader.get_pixbuf()
    except Exception:
        try:
            loader.close()
        except Exception:
            pass
        return None
    if pixbuf is None:
        return None
    width, height = pixbuf.get_width(), pixbuf.get_height()
    if width > 780:
        height = max(1, round(height * 780 / width))
        width = 780
        pixbuf = pixbuf.scale_simple(width, height, GdkPixbuf.InterpType.BILINEAR)
    return pixbuf


def math_widget(source: str, em: int = 26, dark: bool = False) -> Gtk.Widget:
    key = (source, em, dark)
    cached = _cache.get(key)
    if cached is not None:
        pixbuf = cached
    else:
        svg = _render_svg(source, em)
        color = "#E8ECF5" if dark else "#202126"
        pixbuf = _svg_to_pixbuf(svg, em, color) if svg else None
        if pixbuf is not None:
            _cache[key] = pixbuf

    if pixbuf is None:
        return None
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
    box.set_halign(Gtk.Align.CENTER)
    box.set_hexpand(False)
    picture = Gtk.Picture.new_for_pixbuf(pixbuf)
    picture.set_halign(Gtk.Align.CENTER)
    picture.set_valign(Gtk.Align.CENTER)
    picture.set_can_shrink(False)
    picture.set_size_request(pixbuf.get_width(), pixbuf.get_height())
    picture.set_margin_top(5)
    picture.set_margin_bottom(5)
    box.set_size_request(-1, pixbuf.get_height() + 10)
    box.append(picture)
    box.set_tooltip_text(source)
    return box
