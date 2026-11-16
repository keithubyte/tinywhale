#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小鲸鱼（TinyWhale）—— 用 GTK4 原生控件显示 DeepSeek 的对话。

和网页外壳的区别：
* 登录只借用一次 WebKit（拿 token / cookie），之后不再加载聊天页面；
* 会话列表、消息、流式增量全部走 HTTP + SSE，渲染由 GTK 控件完成；
* 同一份内容可以切换「气泡 / 文档 / 极简」三种排版。

非官方协议，可能随 DeepSeek 改版失效；请只在自己的账号上使用。
"""

from __future__ import annotations

import base64
import html
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from datetime import datetime

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("GdkPixbuf", "2.0")
    gi.require_version("Gsk", "4.0")
    gi.require_version("Soup", "3.0")
    gi.require_version("WebKit", "6.0")

    from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango, Soup, WebKit
except (ImportError, ValueError) as exc:  # pragma: no cover
    sys.stderr.write(
        f"缺少 GTK4 / WebKitGTK 6.0 绑定：{exc}\n"
        "请用 ./run.sh 启动，或安装 gir1.2-webkit-6.0。\n"
    )
    sys.exit(1)

from api import (  # noqa: E402
    ApiError, AuthError, DeepSeekAPI, Session, USER_AGENT,
    parse_sse, pow_header, solve_pow_python,
)
from mathrender import math_widget  # noqa: E402
from mathjax_render import math_widget as mathjax_math_widget  # noqa: E402
import share_image  # noqa: E402

APP_NAME = "TinyWhale"
APP_NAME_CN = "小鲸鱼"
APP_VERSION = "1.0.0"
BASE_URL = "https://chat.deepseek.com"
CONFIG_DIR = os.environ.get("DSD_CONFIG_DIR") or os.path.join(
    GLib.get_user_config_dir(), "deepseek-native")
DATA_DIR = os.environ.get("DSD_DATA_DIR") or os.path.join(
    GLib.get_user_data_dir(), "deepseek-native")
SESSION_FILE = os.path.join(CONFIG_DIR, "session.json")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")

FONT_CATEGORIES = ("ui", "text", "mono")
DEFAULT_FONTS = {
    "ui": {"family": "", "size": 0},
    "text": {"family": "", "size": 0, "line_spacing": 1.55},
    "mono": {"family": "Monospace", "size": 0},
}
FONT_CONFIG = {key: dict(value) for key, value in DEFAULT_FONTS.items()}
FONT_PROVIDER = None
VENDOR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
POW_WORKER = os.path.join(VENDOR_DIR, "pow_worker.js")

DEFAULT_MODEL_TYPE = "default"
THEME_MODES = ("system", "light", "dark")

APP_CSS = """
@define-color chat_bg mix(@theme_bg_color, #5B5BD6, 0.02);
window {
    background: @chat_bg;
    color: @theme_fg_color;
    border-radius: 18px;
}
window.csd { border-radius: 18px; }
headerbar { background: transparent; border: none; box-shadow: none; min-height: 38px; }
headerbar .title { font-size: 15px; font-weight: 700; }
.header-subtitle { font-size: 10px; color: alpha(@theme_fg_color, 0.50); }
button.header-action {
    background: alpha(@theme_fg_color, 0.04);
    border: 1px solid alpha(@theme_fg_color, 0.08);
    border-radius: 10px;
    box-shadow: none;
}
button.header-action:hover { background: alpha(@theme_fg_color, 0.09); }
button.titlebar-icon {
    background: transparent;
    border: none;
    border-radius: 9px;
    min-width: 32px;
    min-height: 30px;
    padding: 0;
    box-shadow: none;
    outline: none;
}
button.titlebar-icon:hover { background: alpha(@theme_fg_color, 0.07); }
button.primary-button {
    background-color: #5B5BD6;
    background-image: none;
    color: #ffffff;
    border: none;
    border-radius: 10px;
    font-weight: 600;
    box-shadow: none;
    outline: none;
    text-shadow: none;
}
button.primary-button:hover { background-color: #4E4EC9; }
button.primary-button:active { background-color: #4545BA; }
button.suggested-action { background: #5B5BD6; color: #ffffff; }
button.icon-button {
    background: alpha(@theme_fg_color, 0.04);
    border: 1px solid alpha(@theme_fg_color, 0.08);
    border-radius: 10px;
    box-shadow: none;
}
button.icon-button:hover { background: alpha(@theme_fg_color, 0.09); }
.sidebar-card {
    background: @theme_base_color;
    border-radius: 18px;
    box-shadow: 0 8px 26px alpha(black, 0.07);
}
.sidebar-card scrolledwindow,
.sidebar-card list,
.sidebar-card viewport { background: transparent; }
.chat-list row {
    padding: 8px 10px;
    border-radius: 12px;
    margin: 1px 6px;
}
.chat-list row:hover { background: alpha(@theme_fg_color, 0.04); }
.chat-list row:selected {
    background: alpha(#5B5BD6, 0.12);
    box-shadow: inset 3px 0 0 #5B5BD6;
    color: @theme_fg_color;
}
.chat-list row:selected label { color: @theme_fg_color; }
.chat-title { font-weight: 600; font-size: 0.95em; }
.chat-preview { font-size: 0.8em; color: alpha(@theme_fg_color, 0.50); }
.search-pill {
    background: alpha(@theme_fg_color, 0.05);
    border: none;
    border-radius: 10px;
    min-height: 30px;
}
.toolbar { background: transparent; }
.pill {
    background: alpha(@theme_fg_color, 0.04);
    border: 1px solid alpha(@theme_fg_color, 0.07);
    border-radius: 10px;
    box-shadow: none;
}
.pill-toggle {
    background: alpha(@theme_fg_color, 0.04);
    border: 1px solid alpha(@theme_fg_color, 0.07);
    border-radius: 10px;
    box-shadow: none;
}
.pill-toggle:checked {
    background: alpha(#5B5BD6, 0.14);
    color: #5B5BD6;
    border-color: alpha(#5B5BD6, 0.25);
}
.msg-role { font-size: 0.78em; font-weight: 700; color: alpha(@theme_fg_color, 0.50); }
.msg-body { color: @theme_fg_color; }
.bubble-assistant {
    background: transparent;
    border-radius: 0;
    padding: 0;
    box-shadow: none;
}
.bubble-user {
    background: alpha(#5B5BD6, 0.12);
    border-radius: 18px;
    padding: 10px 14px;
    box-shadow: none;
}
.bubble-user label, .bubble-user .msg-body { color: @theme_fg_color; }
.bubble-user button { background: transparent; color: alpha(@theme_fg_color, 0.55); border: none; }
.source-card {
    background: alpha(@theme_fg_color, 0.04);
    border: 1px solid alpha(@theme_fg_color, 0.06);
    border-radius: 12px;
    padding: 9px 11px;
}
.source-card:hover { background: alpha(@theme_fg_color, 0.07); }
.source-index { font-weight: 700; color: #5B5BD6; min-width: 20px; }
.source-title { font-weight: 600; }
.source-meta { font-size: 0.8em; color: alpha(@theme_fg_color, 0.55); }
.source-snippet { font-size: 0.85em; color: alpha(@theme_fg_color, 0.78); }
.attachment-chip {
    background: alpha(#5B5BD6, 0.12);
    color: #5B5BD6;
    border-radius: 999px;
    padding: 4px 10px;
}
.attachment-error { color: #e01b24; }
.attachment-name { font-size: 0.9em; }
.input-frame {
    background: @theme_base_color;
    border-radius: 18px;
    border: 1px solid alpha(@theme_fg_color, 0.08);
    box-shadow: 0 10px 30px alpha(black, 0.07);
}
.input-frame textview,
.input-frame text,
.input-frame scrolledwindow { background: transparent; color: @theme_fg_color; }
scrolledwindow { background: transparent; }
.msg-actions { margin-top: 2px; }
.edit-frame {
    background: @theme_base_color;
    border: 1.5px solid #5B5BD6;
    border-radius: 16px;
    padding: 6px 12px 5px 12px;
}
.edit-frame textview,
.edit-frame scrolledwindow {
    background: transparent;
    color: @theme_fg_color;
    min-height: 0;
    padding: 0;
}
.edit-cancel {
    background: transparent;
    border: 1px solid alpha(@theme_fg_color, 0.16);
    border-radius: 999px;
    padding: 2px 14px;
    min-height: 26px;
    box-shadow: none;
}
.edit-cancel:hover { background: alpha(@theme_fg_color, 0.05); }
.edit-send {
    background: #5B5BD6;
    color: #ffffff;
    border: none;
    border-radius: 999px;
    padding: 2px 16px;
    min-height: 26px;
    box-shadow: none;
    font-weight: 600;
}
.edit-send:hover { background: #4E4EC9; }
.edit-send:disabled { opacity: 0.45; }
.msg-action {
    background: transparent;
    border: none;
    box-shadow: none;
    color: alpha(@theme_fg_color, 0.45);
    font-size: 0.8em;
    padding: 2px 4px;
    min-height: 24px;
}
.msg-action:hover { color: #5B5BD6; background: alpha(@theme_fg_color, 0.05); }
.msg-action.copied { color: #2E9E5B; }
.share-brand { margin-top: 6px; margin-bottom: 18px; }
.share-brand-title {
    font-size: 22px;
    font-weight: 700;
    color: #4D6BFE;
}
.chat-heading { font-size: 16px; font-weight: 700; }
.group-header {
    font-size: 0.78em;
    font-weight: 600;
    color: alpha(@theme_fg_color, 0.45);
    padding: 10px 12px 4px;
}
.brand-title { font-size: 18px; font-weight: 700; color: #4D6BFE; }
.titlebar-title {
    font-size: 0.95em;
    font-weight: 600;
    color: alpha(@theme_fg_color, 0.88);
}
.date-label { font-size: 0.82em; color: alpha(@theme_fg_color, 0.55); }
.avatar {
    min-width: 40px;
    min-height: 40px;
    border-radius: 999px;
    background: transparent;
    color: #5B5BD6;
    font-weight: 700;
    padding: 0;
    -gtk-icon-size: 40px;
}
.profile-name { font-weight: 600; font-size: 0.92em; }
.profile-sub { font-size: 0.75em; color: alpha(@theme_fg_color, 0.5); }
.footer-note { font-size: 0.75em; color: alpha(@theme_fg_color, 0.45); }
.settings-section {
    background: alpha(@theme_fg_color, 0.035);
    border: 1px solid alpha(@theme_fg_color, 0.06);
    border-radius: 12px;
    padding: 14px;
}
.settings-section-title { font-weight: 700; font-size: 1.04em; }
.settings-hint { font-size: 0.82em; color: alpha(@theme_fg_color, 0.50); }
.settings-row { min-height: 38px; }
.settings-unit { font-size: 0.82em; color: alpha(@theme_fg_color, 0.55); }
.about-name { font-size: 28px; font-weight: 700; }
.about-english { font-size: 15px; color: alpha(@theme_fg_color, 0.55); }
.about-comment {
    font-size: 0.93em;
    color: alpha(@theme_fg_color, 0.72);
}
.about-version {
    background: alpha(#5B5BD6, 0.10);
    color: #5B5BD6;
    border-radius: 999px;
    padding: 3px 12px;
    font-size: 0.78em;
}
.modal-shade { background: alpha(black, 0.32); }
button.settings-about {
    background: alpha(@theme_fg_color, 0.035);
    border: 1px solid alpha(@theme_fg_color, 0.06);
    border-radius: 12px;
    padding: 10px 14px;
    box-shadow: none;
}
button.settings-about:hover { background: alpha(@theme_fg_color, 0.065); }
.input-pill {
    background: alpha(#5B5BD6, 0.04);
    border: 1px solid alpha(#5B5BD6, 0.28);
    border-radius: 999px;
    color: #5B5BD6;
    padding: 2px 12px;
    font-size: 0.9em;
    box-shadow: none;
}
.input-pill:checked { background: alpha(#5B5BD6, 0.16); }
.input-pill image { color: #5B5BD6; }
.send-circle {
    min-width: 34px;
    min-height: 34px;
    border-radius: 999px;
    background: alpha(#5B5BD6, 0.18);
    color: #5B5BD6;
    padding: 0;
    border: 0;
    outline: none;
    box-shadow: none;
}
.send-circle:disabled { opacity: 1; }
.send-circle.send-active {
    background: #5B5BD6;
    color: #ffffff;
    box-shadow: none;
}
.send-circle.send-idle { background: alpha(#5B5BD6, 0.18); color: #5B5BD6; }
.send-circle.send-idle:hover { background: alpha(#5B5BD6, 0.26); }
.stop-circle { background: alpha(@theme_fg_color, 0.14); color: @theme_fg_color; box-shadow: none; }
.stop-circle:disabled { opacity: 1; }
.list-fade {
    background-image: linear-gradient(
        to bottom,
        alpha(@theme_base_color, 0.0),
        @theme_base_color
    );
}
.messages-fade-top {
    background-image: linear-gradient(
        to bottom,
        @chat_bg 0%,
        alpha(@chat_bg, 0.0) 100%
    );
}
.messages-fade-bottom {
    background-image: linear-gradient(
        to top,
        @chat_bg 0%,
        alpha(@chat_bg, 0.0) 100%
    );
}
.input-placeholder { color: alpha(@theme_fg_color, 0.45); font-size: 1.02em; }
button.attach-flat {
    background: transparent;
    border: 0;
    box-shadow: none;
    padding: 0;
    min-width: 24px;
    min-height: 24px;
    outline: none;
    color: alpha(@theme_fg_color, 0.65);
}
button.attach-flat:hover { background: alpha(@theme_fg_color, 0.06); border-radius: 6px; }
button.flat { background: transparent; border: 0; box-shadow: none; outline: none; }
menubutton.attach-flat,
menubutton.attach-flat > button {
    background: transparent;
    border: 0;
    box-shadow: none;
    outline: none;
    padding: 0;
    min-width: 24px;
    min-height: 24px;
}
button.attach-flat image { -gtk-icon-size: 20px; }
paned > separator { background: transparent; min-width: 0; min-height: 0; box-shadow: none; border: none; }
.empty-title { font-size: 25px; font-weight: 700; }
.empty-subtitle { font-size: 0.9em; color: alpha(@theme_fg_color, 0.55); }
.doc .msg-body { font-size: 1.03em; }
.minimal .msg-role { opacity: 0; min-height: 0; margin: 0; padding: 0; }
.h1 { font-size: 1.45em; font-weight: 700; }
.h2 { font-size: 1.25em; font-weight: 700; }
.h3 { font-size: 1.1em; font-weight: 700; }
.h4 { font-size: 1.02em; font-weight: 700; }
.h5 { font-size: 0.96em; font-weight: 700; }
.h6 { font-size: 0.9em; font-weight: 700; color: alpha(@theme_fg_color, 0.72); }
.quote {
    background: alpha(#5B5BD6, 0.06);
    border-left: 3px solid alpha(#5B5BD6, 0.45);
    border-radius: 0 10px 10px 0;
    padding: 8px 12px;
    color: alpha(@theme_fg_color, 0.82);
}
.msg-reasoning {
    background: transparent;
    padding: 1px 0;
    color: alpha(@theme_fg_color, 0.60);
    font-size: 0.86em;
}
.md-p { margin: 1px 0; line-height: 1.55; }
.h1 { margin-top: 12px; margin-bottom: 3px; }
.h2 { margin-top: 10px; margin-bottom: 2px; }
.h3 { margin-top: 8px; margin-bottom: 2px; }
.md-bullet { color: #5B5BD6; font-weight: 600; min-width: 20px; }
.md-hr { margin: 8px 0; background: alpha(@theme_fg_color, 0.10); min-height: 1px; }
.md-math-block {
    background: alpha(@theme_fg_color, 0.03);
    border: 1px solid alpha(@theme_fg_color, 0.08);
    border-radius: 12px;
    padding: 12px 16px;
    font-family: "DejaVu Serif", "Noto Serif CJK SC", serif;
    font-size: 1.12em;
    color: @theme_fg_color;
}
.md-math-mono { font-family: monospace; font-size: 0.95em; }
.md-footnote {
    font-size: 0.86em;
    color: alpha(@theme_fg_color, 0.68);
    margin-top: 3px;
}
.md-footnote-title { font-size: 0.82em; font-weight: 700; color: alpha(@theme_fg_color, 0.55); margin-top: 6px; }
.md-image { border-radius: 10px; }
.md-image-caption { font-size: 0.78em; color: alpha(@theme_fg_color, 0.5); }
.task-done { opacity: 0.55; }
.md-table-card {
    background: @theme_base_color;
    border: 1px solid alpha(@theme_fg_color, 0.10);
    border-radius: 12px;
}
.md-table { background: alpha(@theme_fg_color, 0.09); }
.md-cell {
    background: @theme_base_color;
    padding: 8px 10px;
    color: @theme_fg_color;
}
.md-head {
    background: alpha(#5B5BD6, 0.08);
    font-weight: 700;
}
.md-row-alt { background: alpha(@theme_fg_color, 0.025); }
.md-table-scroll { background: transparent; }
.code-block {
    font-family: monospace;
    background: alpha(@theme_fg_color, 0.06);
    padding: 10px;
    border-radius: 10px;
}
.input-frame .code-block { background: alpha(@theme_fg_color, 0.06); }
.code-card {
    background: alpha(@theme_fg_color, 0.035);
    border: 1px solid alpha(@theme_fg_color, 0.09);
    border-radius: 12px;
    box-shadow: none;
}
.code-head {
    background: transparent;
    border-bottom: 1px solid alpha(@theme_fg_color, 0.07);
    border-radius: 12px 12px 0 0;
    padding: 4px 8px 4px 12px;
}
.code-lang {
    color: alpha(@theme_fg_color, 0.55);
    font-family: monospace;
    font-size: 0.78em;
    font-weight: 700;
}
button.code-action {
    background: transparent;
    border: 0;
    box-shadow: none;
    color: alpha(@theme_fg_color, 0.55);
    padding: 2px 5px;
    min-height: 24px;
    font-size: 0.8em;
}
button.code-action:hover { background: alpha(@theme_fg_color, 0.06); border-radius: 6px; }
.code-scroll { background: transparent; }
.code-text {
    background: transparent;
    color: @theme_fg_color;
    font-family: monospace;
    font-size: 0.9em;
    padding: 0;
}
.code-plain {
    background: transparent;
    color: @theme_fg_color;
    font-family: monospace;
    font-size: 0.9em;
    padding: 10px 12px 12px 12px;
}
"""


# ---------------------------------------------------------------------------
# 基础工具 / Markdown
# ---------------------------------------------------------------------------

def ensure_dirs() -> None:
    for path in (CONFIG_DIR, DATA_DIR):
        os.makedirs(path, exist_ok=True)


def load_session() -> Session | None:
    try:
        with open(SESSION_FILE, "r", encoding="utf-8") as fh:
            return Session.from_dict(json.load(fh))
    except (OSError, ValueError):
        return None


def save_session(session: Session) -> None:
    ensure_dirs()
    tmp = SESSION_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(session.to_dict(), fh, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, SESSION_FILE)


def clear_session() -> None:
    try:
        os.remove(SESSION_FILE)
    except OSError:
        pass


def normalize_fonts(raw) -> dict:
    fonts = {key: dict(value) for key, value in DEFAULT_FONTS.items()}
    if not isinstance(raw, dict):
        return fonts
    for key in FONT_CATEGORIES:
        value = raw.get(key)
        if not isinstance(value, dict):
            continue
        family = value.get("family")
        if isinstance(family, str) and key != "math":
            fonts[key]["family"] = family.strip()[:120]
        try:
            size = int(value.get("size", fonts[key]["size"]))
        except (TypeError, ValueError):
            size = fonts[key]["size"]
        fonts[key]["size"] = max(0, min(64, size))
        if key == "text":
            try:
                spacing = float(value.get("line_spacing", 1.55))
            except (TypeError, ValueError):
                spacing = 1.55
            fonts[key]["line_spacing"] = max(1.0, min(2.8, spacing))
    return fonts


def system_font_size() -> int:
    try:
        settings = Gtk.Settings.get_default()
        name = settings.get_property("gtk-font-name") if settings else "Sans 11"
        desc = Pango.FontDescription.from_string(name or "Sans 11")
        size = desc.get_size() / Pango.SCALE
        if size > 0:
            return max(7, min(32, round(size)))
    except Exception:
        pass
    return 11


def system_font_family() -> str:
    try:
        settings = Gtk.Settings.get_default()
        name = settings.get_property("gtk-font-name") if settings else "Sans 11"
        desc = Pango.FontDescription.from_string(name or "Sans 11")
        return desc.get_family() or "Sans"
    except Exception:
        return "Sans"


def effective_text_family(fonts=None) -> str:
    fonts = fonts or FONT_CONFIG
    return fonts["text"]["family"] or system_font_family()


def effective_text_size(fonts=None) -> int:
    fonts = fonts or FONT_CONFIG
    return int(fonts["text"]["size"] or system_font_size())


def math_em_size(fonts=None) -> int:
    return max(12, round(effective_text_size(fonts) * 1.6))


def set_font_config(fonts: dict) -> None:
    global FONT_CONFIG
    FONT_CONFIG = normalize_fonts(fonts)


def _css_font_name(family: str) -> str:
    family = (family or "").strip()
    if not family:
        return ""
    return '"%s"' % family.replace("\\", "\\\\").replace('"', '\\"')


def _css_font_family(family: str) -> str:
    name = _css_font_name(family)
    return "%s, sans-serif" % name if name else ""


def build_font_css(fonts: dict) -> str:
    fonts = normalize_fonts(fonts)
    rules = []

    def add(selector, category):
        family = _css_font_family(fonts[category]["family"])
        size = fonts[category]["size"]
        props = []
        if family:
            props.append("font-family: %s;" % family)
        if size:
            props.append("font-size: %dpt;" % size)
        if props:
            rules.append("%s { %s }" % (selector, " ".join(props)))

    add("window, headerbar, popover, menu, button, entry, spinbutton, dropdown", "ui")
    body_family = _css_font_family(effective_text_family(fonts)) or "sans-serif"
    body_size = effective_text_size(fonts)
    rules.append(
        ".msg-body, .md-p, .quote, .msg-reasoning, .input-frame textview, "
        ".edit-frame textview, .md-cell, .md-footnote "
        "{ font-family: %s; font-size: %dpt; }" % (body_family, body_size))
    spacing = fonts["text"].get("line_spacing", 1.55)
    rules.append(
        ".msg-body, .md-p, .quote, .msg-reasoning, .md-cell, .md-footnote "
        "{ line-height: %.2f; }" % spacing)
    rules.append(
        ".code-text, .code-plain { line-height: 1.55; }")
    mono_names = []
    code_fallback = effective_text_family(fonts)
    for name in (fonts["mono"].get("family"), code_fallback):
        family = _css_font_name(name)
        if family and family not in mono_names:
            mono_names.append(family)
    mono_names.append("monospace")
    mono_family = ", ".join(mono_names)
    mono_size = fonts["mono"]["size"]
    mono_props = []
    if mono_family:
        mono_props.append("font-family: %s;" % mono_family)
    if mono_props:
        rules.append(".code-text, .code-lang, .code-plain { %s }" % " ".join(mono_props))
    if mono_size:
        rules.append(".code-text, .code-plain { font-size: %dpt; }" % mono_size)
        rules.append(".code-lang { font-size: %dpt; }" % max(8, round(mono_size * 0.8)))
    return "\n".join(rules)


def install_font_css(css: str) -> None:
    global FONT_PROVIDER
    display = Gdk.Display.get_default()
    if display is None:
        return
    if FONT_PROVIDER is None:
        FONT_PROVIDER = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(
            display, FONT_PROVIDER,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
    FONT_PROVIDER.load_from_data(css.encode("utf-8"), -1)


def system_prefers_dark() -> bool:
    try:
        interface = Gio.Settings.new("org.gnome.desktop.interface")
        scheme = interface.get_string("color-scheme")
        if scheme == "prefer-dark":
            return True
        if scheme == "prefer-light":
            return False
    except Exception:
        pass
    try:
        settings = Gtk.Settings.get_default()
        name = (settings.get_property("gtk-theme-name") or "").lower() if settings else ""
        return "dark" in name
    except Exception:
        return False


def apply_theme_mode(mode: str) -> bool:
    mode = mode if mode in THEME_MODES else "system"
    dark = mode == "dark" or (mode == "system" and system_prefers_dark())
    try:
        settings = Gtk.Settings.get_default()
        if settings is not None:
            settings.set_property("gtk-application-prefer-dark-theme", dark)
    except Exception:
        pass
    return dark


def load_settings() -> dict:
    settings = {"theme": "system", "fonts": normalize_fonts(None)}
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        if isinstance(saved, dict):
            if saved.get("theme") in THEME_MODES:
                settings["theme"] = saved["theme"]
            settings["fonts"] = normalize_fonts(saved.get("fonts"))
    except (OSError, ValueError):
        pass
    return settings


def save_settings(settings: dict) -> None:
    ensure_dirs()
    tmp = SETTINGS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(settings, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS_FILE)
    except OSError:
        pass


def _is_dark_theme() -> bool:
    try:
        settings = Gtk.Settings.get_default()
        if settings is None:
            return False
        if settings.get_property("gtk-application-prefer-dark-theme"):
            return True
        name = (settings.get_property("gtk-theme-name") or "").lower()
        return "dark" in name
    except Exception:
        return False


def _code_span(match):
    return match.group(1)


_INLINE_CODE = re.compile(r"`([^`\n]+)`")


_MATH_SYMBOLS = {
    r"\int": "∫", r"\iint": "∬", r"\iiint": "∭", r"\sum": "∑", r"\prod": "∏",
    r"\infty": "∞", r"\pi": "π", r"\alpha": "α", r"\beta": "β", r"\gamma": "γ",
    r"\delta": "δ", r"\epsilon": "ε", r"\theta": "θ", r"\lambda": "λ",
    r"\mu": "μ", r"\sigma": "σ", r"\omega": "ω", r"\phi": "φ", r"\psi": "ψ",
    r"\cdot": "·", r"\times": "×", r"\div": "÷", r"\pm": "±", r"\mp": "∓",
    r"\le": "≤", r"\leq": "≤", r"\ge": "≥", r"\geq": "≥", r"\neq": "≠",
    r"\ne": "≠", r"\approx": "≈", r"\equiv": "≡", r"\in": "∈", r"\notin": "∉",
    r"\subset": "⊂", r"\supset": "⊃", r"\cup": "∪", r"\cap": "∩",
    r"\to": "→", r"\rightarrow": "→", r"\leftarrow": "←", r"\Rightarrow": "⇒",
    r"\forall": "∀", r"\exists": "∃", r"\partial": "∂", r"\nabla": "∇",
    r"\ldots": "…", r"\cdots": "⋯", r"\dots": "…",
}
_SUPER = dict(zip("0123456789+-=()ni", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ"))
_SUB = dict(zip("0123456789+-=()aeihklmnopstuvx",
                "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑᵢₕₖₗₘₙₒₚₛₜᵤᵥₓ"))
_MATH_FUNCTIONS = ("sin", "cos", "tan", "cot", "sec", "csc", "log", "ln", "exp",
                   "lim", "max", "min", "det", "gcd")


def _map_script(content: str, mapping: dict, marker: str) -> str:
    content = content.strip()
    if content and all(ch in mapping for ch in content):
        return "".join(mapping[ch] for ch in content)
    return f"{marker}({content})"


def _frac(numerator: str, denominator: str) -> str:
    return f"{numerator.strip()}/{denominator.strip()}"


def _latex_basic(source: str) -> str:
    src = source
    src = src.replace(r"\left", "").replace(r"\right", "")
    src = re.sub(r"\\begin\{[^}]+\}|\\end\{[^}]+\}", "", src)
    src = src.replace(r"\\", "\n").replace("&", "")
    src = re.sub(r"\\(?:quad|qquad)", "    ", src)
    src = re.sub(r"\\[,;! ]", " ", src)
    src = re.sub(r"\\text\{([^{}]*)\}", r"\1", src)
    for _ in range(4):
        src = re.sub(r"\\frac\{([^{}]*)\}\{([^{}]*)\}",
                     lambda m: _frac(m.group(1), m.group(2)), src)
    src = re.sub(r"\\sqrt\{([^{}]*)\}", r"√(\1)", src)
    for command in sorted(_MATH_SYMBOLS, key=len, reverse=True):
        src = src.replace(command, _MATH_SYMBOLS[command])
    for name in _MATH_FUNCTIONS:
        src = re.sub(r"\\%s\b" % name, name, src)
    src = re.sub(r"\^\{([^{}]*)\}", lambda m: _map_script(m.group(1), _SUPER, "^"), src)
    src = re.sub(r"\^([0-9A-Za-z+\-=()])", lambda m: _map_script(m.group(1), _SUPER, "^"), src)
    src = re.sub(r"_\{([^{}]*)\}", lambda m: _map_script(m.group(1), _SUB, "_"), src)
    src = re.sub(r"_([0-9A-Za-z+\-=()])", lambda m: _map_script(m.group(1), _SUB, "_"), src)
    src = src.replace("{", "").replace("}", "")
    src = re.sub(r"\\([A-Za-z]+)", r"\1", src)
    src = src.replace("\\", "")
    src = re.sub(r"[ \t]+\n", "\n", src)
    src = re.sub(r"\n{3,}", "\n\n", src)
    return src.strip()


def latex_to_unicode(source: str) -> str:
    matrix = re.search(r"\\begin\{pmatrix\}(.*?)\\end\{pmatrix\}", source, re.S)
    if matrix:
        rows = [row for row in re.split(r"\\\\", matrix.group(1)) if row.strip()]
        parsed = []
        for row in rows:
            cells = [_latex_basic(cell.strip()) for cell in row.split("&")]
            parsed.append(cells)
        if parsed:
            columns = max(len(row) for row in parsed)
            widths = []
            for col in range(columns):
                widths.append(max(len(row[col]) if col < len(row) else 0 for row in parsed))
            lines = []
            for index, row in enumerate(parsed):
                padded = []
                for col in range(columns):
                    cell = row[col] if col < len(row) else ""
                    padded.append(cell + " " * (widths[col] - len(cell)))
                line = "  ".join(padded).rstrip()
                lines.append("( " + line + " )")
            return "\n".join(lines)
    aligned = re.search(r"\\begin\{aligned\}(.*?)\\end\{aligned\}", source, re.S)
    if aligned:
        rows = [row.strip() for row in re.split(r"\\\\", aligned.group(1)) if row.strip()]
        return "\n".join(_latex_basic(row) for row in rows)
    return _latex_basic(source)


def latex_to_pango(source: str) -> str:
    scripts = []
    text = source
    text = text.replace(r"\left", "").replace(r"\right", "")
    text = re.sub(r"\\begin\{[^}]+\}|\\end\{[^}]+\}", "", text)
    text = text.replace(r"\\", " ").replace("&", "")
    text = re.sub(r"\\(?:quad|qquad)", "  ", text)
    text = re.sub(r"\\[,;! ]", " ", text)
    text = re.sub(r"\\text\{([^{}]*)\}", r"\1", text)
    for _ in range(3):
        text = re.sub(r"\\frac\{([^{}]*)\}\{([^{}]*)\}",
                      lambda m: "%s/%s" % (m.group(1), m.group(2)), text)
    text = re.sub(r"\\sqrt\{([^{}]*)\}", r"√(\1)", text)
    for command in sorted(_MATH_SYMBOLS, key=len, reverse=True):
        text = text.replace(command, _MATH_SYMBOLS[command])
    for name in _MATH_FUNCTIONS:
        text = re.sub(r"\\%s\b" % name, name, text)
    text = re.sub(r"\\([A-Za-z]+)", r"\1", text)
    text = text.replace("\\", "")
    text = re.sub(r"\s*=\s*", " = ", text)
    text = re.sub(r"\s*\+\s*", " + ", text)

    def protect_script(marker, content):
        scripts.append((marker, latex_to_pango(content)))
        return f"\u0006{len(scripts) - 1}\u0006"

    text = re.sub(r"\^\{([^{}]*)\}", lambda m: protect_script("^", m.group(1)), text)
    text = re.sub(r"_\{([^{}]*)\}", lambda m: protect_script("_", m.group(1)), text)
    text = re.sub(r"\^([A-Za-z0-9+\-=])", lambda m: protect_script("^", m.group(1)), text)
    text = re.sub(r"_([A-Za-z0-9+\-=])", lambda m: protect_script("_", m.group(1)), text)
    text = text.replace("{", "").replace("}", "")
    text = html.escape(text)
    for index, (marker, content) in enumerate(scripts):
        rise = "6000" if marker == "^" else "-4200"
        text = text.replace(
            f"\u0006{index}\u0006",
            '<span rise="%s" size="smaller">%s</span>' % (rise, content))
    return text


def math_block_widget(source: str) -> Gtk.Widget:
    size = math_em_size()
    rendered = mathjax_math_widget(source, size, _is_dark_theme())
    if rendered is not None:
        return rendered
    return math_widget(source, size)


def inline_markup(text: str, source_urls: dict[int, str] | None = None,
                  refs: dict | None = None,
                  footnotes: dict | None = None) -> str:
    codes: list[str] = []
    escapes: list[str] = []
    maths: list[str] = []
    autolinks: list[str] = []
    refs = refs or {}
    footnotes = footnotes or {}

    def protect_escape(match):
        escapes.append(match.group(1))
        return f"\u0002{len(escapes) - 1}\u0002"

    text = re.sub(r"\\([\\`*_{}#+.!~>|-])", protect_escape, text)

    def code_repl(match):
        codes.append(match.group(1))
        return f"\u0001{len(codes) - 1}\u0001"

    text = re.sub(r"``(.+?)``", code_repl, text, flags=re.S)
    text = _INLINE_CODE.sub(code_repl, text)
    text = text.replace("  \n", "\n")

    def math_repl(match):
        maths.append(match.group(1))
        return f"\u0004{len(maths) - 1}\u0004"

    text = re.sub(r"\$([^$\n]+?)\$", math_repl, text)
    text = re.sub(r"\\\((.+?)\\\)", math_repl, text, flags=re.S)

    def auto_repl(match):
        autolinks.append(match.group(1))
        return f"\u0005{len(autolinks) - 1}\u0005"

    text = re.sub(r"<(https?://[^>\s]+|mailto:[^>\s]+)>", auto_repl, text)

    text = html.escape(html.unescape(text), quote=False)

    def cite(match):
        number = int(match.group(1))
        url = (source_urls or {}).get(number, "")
        if url:
            return '<sup><a href="%s">[%d]</a></sup>' % (
                html.escape(url, quote=True), number)
        return "<sup>[%d]</sup>" % number

    text = re.sub(r"\[citation:(\d+)\]", cite, text)
    text = re.sub(r"\[reference:(\d+)\]", cite, text)

    def nested_image(match):
        alt = match.group(1).strip() or "图片"
        outer = match.group(3)
        return '<a href="%s">🖼 %s</a>' % (
            html.escape(outer, quote=True), html.escape(alt))

    text = re.sub(
        r"\[!\[([^\]]*)\]\(([^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\)\]\(([^)\s]+)\)",
        nested_image, text)

    def image_ref(match):
        alt = match.group(1).strip() or "图片"
        key = (match.group(2) or match.group(1)).strip().lower()
        info = refs.get(key)
        if not info:
            return match.group(0)
        url = info[0] if isinstance(info, (tuple, list)) else info
        return '<a href="%s">🖼 %s</a>' % (
            html.escape(url, quote=True), html.escape(alt))

    text = re.sub(r"!\[([^\]]*)\]\[([^\]]*)\]", image_ref, text)

    def image(match):
        alt = match.group(1).strip() or "图片"
        url = match.group(2)
        return '<a href="%s">🖼 %s</a>' % (
            html.escape(url, quote=True), html.escape(alt))

    text = re.sub(
        r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\)",
        image, text)

    def footnote(match):
        key = match.group(1)
        info = footnotes.get(key)
        if not info:
            return match.group(0)
        number = info[0] if isinstance(info, (tuple, list)) else info
        return "<sup>[%s]</sup>" % number

    text = re.sub(r"\[\^([^\]]+)\]", footnote, text)

    def ref_link(match):
        label = match.group(1)
        key = (match.group(2) or label).strip().lower()
        info = refs.get(key)
        if not info:
            return match.group(0)
        url = info[0] if isinstance(info, (tuple, list)) else info
        return '<a href="%s">%s</a>' % (html.escape(url, quote=True), label)

    text = re.sub(r"\[([^\]]+)\]\[([^\]]*)\]", ref_link, text)

    text = re.sub(r"~~(.+?)~~", r'<span strikethrough="true">\1</span>',
                  text, flags=re.S)
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"<b><i>\1</i></b>", text, flags=re.S)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", text)

    def link(match):
        label = match.group(1)
        url = match.group(2)
        return '<a href="%s">%s</a>' % (html.escape(url, quote=True), label)

    text = re.sub(
        r"\[([^\]]+)\]\(((?:https?://|mailto:)[^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\)",
        link, text)

    dark = _is_dark_theme()
    code_bg = "#333A4A" if dark else "#E4E7EE"
    code_fg = "#DCE4F7" if dark else "#2F3442"
    mono_family = html.escape(FONT_CONFIG["mono"]["family"] or "Monospace", quote=True)
    mono_size = FONT_CONFIG["mono"]["size"]
    mono_size_attr = ' font_size="%dpt"' % mono_size if mono_size else ""
    for index, code in enumerate(codes):
        span = ('<span background="%s" foreground="%s" font_family="%s"%s>'
                '\u2009%s\u2009</span>') % (
                    code_bg, code_fg, mono_family, mono_size_attr, html.escape(code))
        text = text.replace(f"\u0001{index}\u0001", span)
    math_family = "DejaVu Serif"
    math_size = math_em_size()
    inline_math_size = max(10, round(math_size * 0.62))
    for index, formula in enumerate(maths):
        rendered = latex_to_pango(formula)
        span = ('<span font_family="%s" font_size="%dpt" font_style="italic">'
                '%s</span>') % (math_family, inline_math_size, rendered)
        text = text.replace(f"\u0004{index}\u0004", span)
    for index, url in enumerate(autolinks):
        label = url[7:] if url.lower().startswith("mailto:") else url
        anchor = '<a href="%s">%s</a>' % (
            html.escape(url, quote=True), html.escape(label))
        text = text.replace(f"\u0005{index}\u0005", anchor)
    for index, char in enumerate(escapes):
        text = text.replace(f"\u0002{index}\u0002", html.escape(char))
    return text
def parse_blocks(text: str) -> list[tuple[str, str]]:
    lines = text.split("\n")
    blocks: list[tuple[str, str]] = []
    i = 0

    def is_block_start(value: str) -> bool:
        return bool(re.match(
            r"^\s*(```|#{1,6}\s|>|\$\$|\\\[|[-*+]\s|\d+\.\s)", value))

    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        stripped = line.strip()

        if stripped.startswith("$$"):
            if stripped.endswith("$$") and len(stripped) > 4:
                blocks.append(("math", stripped[2:-2]))
                i += 1
            else:
                i += 1
                body = []
                while i < len(lines) and "$$" not in lines[i]:
                    body.append(lines[i])
                    i += 1
                if i < len(lines):
                    i += 1
                blocks.append(("math", "\n".join(body)))
            continue

        if stripped.startswith("\\["):
            i += 1
            body = []
            while i < len(lines) and "\\]" not in lines[i]:
                body.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1
            blocks.append(("math", "\n".join(body)))
            continue

        fence = re.match(r"^(\s*)(`{3,}|~{3,})(.*)$", line)
        if fence and (fence.group(2)[0] != "`" or "`" not in fence.group(3)):
            indent = len(fence.group(1))
            fence_char = fence.group(2)[0]
            fence_len = len(fence.group(2))
            lang = fence.group(3).strip()
            closing = re.compile(
                r"^\s*%s{%d,}\s*$" % (re.escape(fence_char), fence_len))
            i += 1
            body = []
            while i < len(lines) and not closing.match(lines[i]):
                body_line = lines[i]
                if body_line.startswith(" " * indent):
                    body_line = body_line[indent:]
                body.append(body_line)
                i += 1
            i += 1
            blocks.append((f"code:{lang}", "\n".join(body)))
            continue

        if line.startswith("    "):
            body = []
            while i < len(lines):
                if lines[i].startswith("    "):
                    body.append(lines[i][4:])
                    i += 1
                elif not lines[i].strip():
                    body.append("")
                    i += 1
                else:
                    break
            blocks.append(("code:text", "\n".join(body).rstrip()))
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            level = len(heading.group(1))
            blocks.append((f"h{level}", heading.group(2).strip()))
            i += 1
            continue

        if i + 1 < len(lines) and line.strip() and not stripped.startswith("|"):
            underline = lines[i + 1].strip()
            if re.match(r"^=+\s*$", underline):
                blocks.append(("h1", stripped))
                i += 2
                continue
            if re.match(r"^-{2,}\s*$", underline):
                blocks.append(("h2", stripped))
                i += 2
                continue

        if re.match(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$", line):
            blocks.append(("hr", ""))
            i += 1
            continue

        if line.startswith(">"):
            body = []
            while i < len(lines) and lines[i].startswith(">"):
                content = lines[i][1:]
                if content.startswith(" "):
                    content = content[1:]
                body.append(content)
                i += 1
            blocks.append(("quote", "\n".join(body)))
            continue

        list_start = re.match(r"^(\s*)([-*+]|\d+\.)\s+", line)
        if list_start:
            base_indent = len(list_start.group(1).expandtabs(4))
            items = []
            while i < len(lines):
                raw = lines[i]
                if not raw.strip():
                    items.append(raw)
                    i += 1
                    continue
                item = re.match(r"^(\s*)([-*+]|\d+\.)\s+", raw)
                indent = len(raw) - len(raw.lstrip(" "))
                if item or indent > base_indent:
                    items.append(raw)
                    i += 1
                    continue
                break
            blocks.append(("list", "\n".join(items)))
            continue

        if line.strip().startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i].strip())
                i += 1
            if rows:
                blocks.append(("table", "\n".join(rows)))
                continue

        para = []
        while i < len(lines) and lines[i].strip() and not is_block_start(lines[i]):
            para.append(lines[i])
            i += 1
        blocks.append(("p", "\n".join(para)))

    return blocks

def open_link(_label, uri: str) -> bool:
    try:
        from gi.repository import Gio
        Gio.AppInfo.launch_default_for_uri(uri, None)
    except Exception:
        pass
    return True


_CODE_KEYWORDS = {
    "python": ("def", "class", "return", "if", "elif", "else", "for", "while",
               "in", "not", "and", "or", "is", "None", "True", "False", "import",
               "from", "as", "try", "except", "finally", "with", "lambda", "yield",
               "async", "await", "pass", "break", "continue", "raise", "global",
               "nonlocal", "del", "assert"),
    "javascript": ("const", "let", "var", "function", "return", "if", "else",
                   "for", "while", "class", "import", "export", "from", "new",
                   "this", "await", "async", "try", "catch", "finally", "throw",
                   "typeof", "instanceof", "in", "of", "null", "undefined", "true",
                   "false", "extends", "super", "switch", "case", "break", "continue"),
    "bash": ("if", "then", "else", "elif", "fi", "for", "in", "do", "done",
             "while", "case", "esac", "function", "return", "exit", "export",
             "local", "echo", "cd", "sudo", "source", "alias"),
    "c": ("int", "char", "float", "double", "void", "long", "short", "unsigned",
          "signed", "struct", "union", "enum", "typedef", "static", "const",
          "return", "if", "else", "for", "while", "do", "switch", "case", "break",
          "continue", "sizeof", "include", "define", "class", "public", "private",
          "protected", "new", "delete", "namespace", "using", "template"),
    "go": ("func", "package", "import", "return", "if", "else", "for", "range",
           "var", "const", "type", "struct", "interface", "map", "chan", "go",
           "defer", "select", "switch", "case", "default", "break", "continue",
           "nil", "true", "false"),
    "rust": ("fn", "let", "mut", "pub", "use", "mod", "struct", "enum", "impl",
             "trait", "return", "if", "else", "match", "for", "while", "loop",
             "in", "as", "where", "const", "static", "unsafe", "async", "await",
             "move", "ref", "self", "Self", "true", "false", "None", "Some", "Ok", "Err"),
    "java": ("class", "interface", "enum", "public", "private", "protected",
             "static", "final", "void", "int", "long", "double", "float", "boolean",
             "char", "String", "return", "if", "else", "for", "while", "do",
             "switch", "case", "break", "continue", "new", "try", "catch",
             "finally", "throw", "throws", "extends", "implements", "package",
             "import", "null", "true", "false"),
    "sql": ("select", "from", "where", "and", "or", "not", "insert", "into",
            "values", "update", "set", "delete", "create", "table", "drop",
            "alter", "join", "left", "right", "inner", "outer", "on", "group",
            "by", "order", "having", "limit", "offset", "as", "distinct",
            "count", "sum", "avg", "min", "max", "null", "is", "like", "in"),
}
_CODE_KEYWORDS["generic"] = tuple(sorted(set().union(*_CODE_KEYWORDS.values())))


def _language_key(lang: str) -> str:
    text = (lang or "").strip().lower()
    name = text.split()[0] if text else ""
    if name.startswith("py"):
        return "python"
    if name.startswith(("js", "ts", "javascript", "typescript", "jsx", "tsx")):
        return "javascript"
    if name.startswith(("sh", "bash", "shell", "zsh", "console")):
        return "bash"
    if name.startswith(("c", "cpp", "c++", "h", "hpp", "objc")):
        return "c"
    if name.startswith(("go", "golang")):
        return "go"
    if name.startswith(("rs", "rust")):
        return "rust"
    if name.startswith(("java", "kotlin", "kt")):
        return "java"
    if name.startswith("sql"):
        return "sql"
    return "generic"


def _code_tag(buffer, tag_table, name: str, color: str):
    tag = Gtk.TextTag.new(name)
    tag.set_property("foreground", color)
    tag_table.add(tag)
    return tag


def _highlight_code(buffer, code: str, lang: str) -> None:
    table = buffer.get_tag_table()
    if _is_dark_theme():
        palette = ("#FF7AB2", "#82AAFF", "#F6C177", "#A6E3A1", "#8A93A8")
    else:
        palette = ("#A23A8A", "#2F5BD0", "#9A5B00", "#2E7D32", "#7A8496")
    function = _code_tag(buffer, table, "fn", palette[1])
    number = _code_tag(buffer, table, "num", palette[2])
    keyword = _code_tag(buffer, table, "kw", palette[0])
    string = _code_tag(buffer, table, "str", palette[3])
    comment = _code_tag(buffer, table, "comment", palette[4])

    def paint(pattern, tag, flags=0):
        for match in re.finditer(pattern, code, flags):
            if match.start() == match.end():
                continue
            start = buffer.get_iter_at_offset(match.start())
            end = buffer.get_iter_at_offset(match.end())
            buffer.apply_tag(tag, start, end)

    key = _language_key(lang)
    words = _CODE_KEYWORDS.get(key, _CODE_KEYWORDS["generic"])
    if words:
        paint(r"\b(?:%s)\b" % "|".join(re.escape(w) for w in words),
              keyword, re.IGNORECASE if key == "sql" else 0)
    paint(r"\b[A-Za-z_]\w*(?=\s*\()", function)
    paint(r"\b(?:0x[0-9A-Fa-f]+|\d+(?:\.\d+)?)\b", number)
    paint(r'"""(?:.|\n)*?"""', string)
    paint(r"'''(?:.|\n)*?'''", string)
    paint(r'"(?:\\.|[^"\\])*"', string)
    paint(r"'(?:\\.|[^'\\])*'", string)
    paint(r"`[^`]*`", string)
    if key in ("python", "bash"):
        paint(r"#.*$", comment, re.MULTILINE)
    elif key == "sql":
        paint(r"--.*$", comment, re.MULTILINE)
    elif key == "generic":
        paint(r"#.*$|//.*$", comment, re.MULTILINE)
        paint(r"/\*.*?\*/", comment, re.DOTALL)
    else:
        paint(r"//.*$", comment, re.MULTILINE)
        paint(r"/\*.*?\*/", comment, re.DOTALL)


def _code_extension(lang: str) -> str:
    key = _language_key(lang)
    return {
        "python": "py", "javascript": "js", "bash": "sh", "c": "c",
        "go": "go", "rust": "rs", "java": "java", "sql": "sql",
    }.get(key, "txt")


def _code_line_height(size: int) -> int:
    try:
        probe = Gtk.Label()
        context = probe.get_pango_context()
        layout = Pango.Layout.new(context)
        desc = Pango.FontDescription()
        desc.set_family(FONT_CONFIG["mono"]["family"] or "Monospace")
        desc.set_absolute_size(max(1, size) * Pango.SCALE)
        layout.set_font_description(desc)
        layout.set_text("Xg", -1)
        _width, height = layout.get_pixel_size()
        return max(14, height + 4)
    except Exception:
        return max(14, round(size * 1.8))


def code_block_widget(code: str, lang: str = "") -> Gtk.Widget:
    raw = code.rstrip("\n") or " "
    card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    card.add_css_class("code-card")
    card.set_hexpand(True)

    head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
    head.add_css_class("code-head")
    label = Gtk.Label(label=(lang.strip() or "code"), xalign=0, hexpand=True)
    label.add_css_class("code-lang")
    head.append(label)

    def action_button(icon_name: str, tooltip: str):
        button = Gtk.Button()
        button.add_css_class("code-action")
        image = Gtk.Image.new_from_icon_name(icon_name)
        image.set_pixel_size(16)
        button.set_child(image)
        button.set_tooltip_text(tooltip)
        return button, image

    copy, copy_image = action_button("edit-copy-symbolic", "复制代码")
    head.append(copy)

    download, download_image = action_button(
        "document-save-symbolic", "保存为文件")
    head.append(download)

    if lang.strip().lower() in ("markdown", "md"):
        body = Gtk.Label(label=raw)
        body.add_css_class("code-plain")
        body.set_wrap(True)
        body.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        body.set_xalign(0)
        body.set_selectable(True)
        body.set_hexpand(True)
    else:
        view = Gtk.TextView()
        view.add_css_class("code-text")
        view.set_editable(False)
        view.set_cursor_visible(False)
        view.set_wrap_mode(Gtk.WrapMode.NONE)
        view.set_top_margin(8)
        view.set_bottom_margin(8)
        view.set_left_margin(10)
        view.set_right_margin(10)
        buffer = view.get_buffer()
        buffer.set_text(raw)
        _highlight_code(buffer, raw, lang)

        scroll = Gtk.ScrolledWindow()
        scroll.add_css_class("code-scroll")
        scroll.set_child(view)
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        try:
            scroll.set_propagate_natural_width(False)
            scroll.set_propagate_natural_height(False)
        except Exception:
            pass
        lines = raw.count("\n") + 1
        text_size = effective_text_size()
        mono_size = FONT_CONFIG["mono"]["size"] or max(8, round(text_size * 0.9))
        line_height = _code_line_height(mono_size)
        code_height = lines * line_height + 16
        scroll.set_min_content_height(code_height)
        scroll.set_size_request(-1, code_height)
        body = scroll
    card.append(head)
    card.append(body)

    def restore_copy():
        copy_image.set_from_icon_name("edit-copy-symbolic")
        copy.set_tooltip_text("复制代码")
        return False

    def do_copy(_button):
        try:
            provider = Gdk.ContentProvider.new_for_bytes(
                "text/plain;charset=utf-8", GLib.Bytes.new(raw.encode("utf-8")))
            clipboard = copy.get_clipboard()
            clipboard.set_content(provider)
            if not clipboard.is_local():
                raise RuntimeError("clipboard not owned")
        except Exception:
            copy.set_tooltip_text("复制失败")
            return
        copy_image.set_from_icon_name("object-select-symbolic")
        copy.set_tooltip_text("已复制")
        GLib.timeout_add(1200, restore_copy)

    def do_download(_button):
        dialog = Gtk.FileDialog()
        dialog.set_title("保存代码")
        dialog.set_initial_name("code." + _code_extension(lang))

        def on_save(dlg, result):
            try:
                file = dlg.save_finish(result)
                file.replace_contents(
                    raw.encode("utf-8"), None, False,
                    Gio.FileCreateFlags.REPLACE_DESTINATION, None)
            except GLib.Error:
                pass

        try:
            dialog.save(None, None, on_save)
        except Exception:
            pass

    copy.connect("clicked", do_copy)
    download.connect("clicked", do_download)
    return card
def _split_table_row(row: str) -> list[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    cells = re.split(r"(?<!\\)\|", row)
    return [cell.replace("\\|", "|").strip() for cell in cells]


def table_widget(body: str, source_urls, refs=None, footnotes=None) -> Gtk.Widget:
    rows = [line.strip() for line in body.split("\n") if line.strip()]
    if not rows:
        rows = ["| |"]
    header = _split_table_row(rows[0])
    alignments = ["left"] * len(header)
    data_rows = []
    if len(rows) > 1 and "---" in rows[1]:
        separator = _split_table_row(rows[1])
        for index, cell in enumerate(separator):
            if index >= len(alignments):
                break
            if cell.startswith(":") and cell.endswith(":"):
                alignments[index] = "center"
            elif cell.endswith(":"):
                alignments[index] = "right"
        data_rows = [_split_table_row(row) for row in rows[2:]]
    else:
        data_rows = [_split_table_row(row) for row in rows[1:]]
    columns = max([len(header)] + [len(row) for row in data_rows] or [1])

    grid = Gtk.Grid()
    grid.add_css_class("md-table")
    grid.set_row_spacing(1)
    grid.set_column_spacing(1)

    def attach_cell(text, row_index, column, is_header=False):
        label = Gtk.Label()
        label.set_markup(inline_markup(text, source_urls, refs, footnotes) or " ")
        align = alignments[column] if column < len(alignments) else "left"
        label.set_xalign({"left": 0.0, "center": 0.5, "right": 1.0}[align])
        label.set_wrap(True)
        label.set_max_width_chars(40)
        label.set_selectable(True)
        label.set_hexpand(True)
        label.add_css_class("md-cell")
        if is_header:
            label.add_css_class("md-head")
        elif row_index % 2 == 0:
            label.add_css_class("md-row-alt")
        label.connect("activate-link", open_link)
        grid.attach(label, column, row_index, 1, 1)

    for column in range(columns):
        text = header[column] if column < len(header) else ""
        attach_cell(text, 0, column, True)
    for index, row in enumerate(data_rows, start=1):
        for column in range(columns):
            text = row[column] if column < len(row) else ""
            attach_cell(text, index, column)

    card = Gtk.Frame()
    card.add_css_class("md-table-card")
    card.set_child(grid)
    scroll = Gtk.ScrolledWindow()
    scroll.add_css_class("md-table-scroll")
    scroll.set_child(card)
    scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    try:
        scroll.set_propagate_natural_width(False)
        scroll.set_propagate_natural_height(True)
    except Exception:
        pass
    row_count = len(data_rows) + 1
    scroll.set_min_content_height(min(360, max(48, row_count * 44 + 6)))
    scroll.set_max_content_height(360)
    return scroll
_IMAGE_SESSION = None


def remote_image_widget(url: str, alt: str) -> Gtk.Widget:
    global _IMAGE_SESSION
    if _IMAGE_SESSION is None:
        _IMAGE_SESSION = Soup.Session()
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    picture = Gtk.Picture()
    picture.add_css_class("md-image")
    picture.set_can_shrink(True)
    picture.set_size_request(260, 120)
    box.append(picture)
    caption = Gtk.Label(label="图片加载中…" if alt else "图片加载中…", xalign=0)
    caption.add_css_class("md-image-caption")
    box.append(caption)

    message = Soup.Message.new("GET", url)
    if message is None:
        caption.set_text("🖼 " + (alt or "图片") + "（链接无效）")
        return box

    def finished(session, result):
        try:
            data = session.send_and_read_finish(result)
            stream = Gio.MemoryInputStream.new_from_bytes(data)
            pixbuf = GdkPixbuf.Pixbuf.new_from_stream(stream, None)
            width, height = pixbuf.get_width(), pixbuf.get_height()
            if width > 420:
                height = max(1, int(height * 420 / width))
                width = 420
                pixbuf = pixbuf.scale_simple(width, height, GdkPixbuf.InterpType.BILINEAR)
            texture = Gdk.Texture.new_for_pixbuf(pixbuf)
            picture.set_paintable(texture)
            picture.set_size_request(width, height)
            caption.set_text(alt or "图片")
        except Exception:
            caption.set_text("🖼 " + (alt or "图片") + "（图片加载失败）")
        return False

    _IMAGE_SESSION.send_and_read_async(message, GLib.PRIORITY_DEFAULT, None, finished)
    return box


def extract_definitions(text: str):
    refs = {}
    footnotes = {}
    footnote_order = []
    kept = []
    fence_char = ""
    fence_len = 0
    for line in text.split("\n"):
        if fence_char:
            kept.append(line)
            if re.match(r"^\s*%s{%d,}\s*$" % (re.escape(fence_char), fence_len), line):
                fence_char = ""
                fence_len = 0
            continue
        opening = re.match(r"^(\s*)(`{3,}|~{3,})(.*)$", line)
        if opening and (opening.group(2)[0] != "`" or "`" not in opening.group(3)):
            fence_char = opening.group(2)[0]
            fence_len = len(opening.group(2))
            kept.append(line)
            continue
        if line.startswith("    "):
            kept.append(line)
            continue
        footnote_def = re.match(r"^\[\^([^\]]+)\]:\s*(.*)$", line)
        if footnote_def:
            key = footnote_def.group(1)
            footnotes[key] = (len(footnote_order) + 1, footnote_def.group(2).strip())
            footnote_order.append(key)
            continue
        link_def = re.match(
            r"^\[([^\]]+)\]:\s*(\S+?)(?:\s+[\"'(](.*?)[\"')])?\s*$", line)
        if link_def:
            refs[link_def.group(1).strip().lower()] = (
                link_def.group(2), link_def.group(3) or "")
            continue
        kept.append(line)
    return "\n".join(kept), refs, footnotes, footnote_order


def markdown_widgets(text: str, sources=None, refs=None, footnotes=None,
                     footnote_order=None, _nested: bool = False) -> list[Gtk.Widget]:
    if refs is None and footnotes is None:
        text, refs, footnotes, footnote_order = extract_definitions(text)
    refs = refs or {}
    footnotes = footnotes or {}
    footnote_order = footnote_order or []
    widgets: list[Gtk.Widget] = []
    source_urls = {s["index"]: s["url"] for s in (sources or []) if s.get("url")}

    for kind, body in parse_blocks(text):
        if kind.startswith("code:"):
            lang = kind.split(":", 1)[1]
            widgets.append(code_block_widget(body, lang))
        elif kind == "math":
            widgets.append(math_block_widget(body))
        elif kind in ("h1", "h2", "h3", "h4", "h5", "h6"):
            label = Gtk.Label()
            label.set_markup(inline_markup(body, source_urls, refs, footnotes))
            label.set_xalign(0)
            label.set_wrap(True)
            label.set_selectable(True)
            label.add_css_class(kind)
            widgets.append(label)
        elif kind == "quote":
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            box.add_css_class("quote")
            children = markdown_widgets(
                body, sources, refs, footnotes, footnote_order, _nested=True)
            for child in children:
                box.append(child)
            widgets.append(box)
        elif kind == "list":
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            items = []
            current = None
            for raw in body.split("\n"):
                match = re.match(r"^(\s*)([-*+]|\d+\.)\s+(.*)$", raw)
                if match:
                    if current is not None:
                        items.append(current)
                    current = {
                        "indent": len(match.group(1).expandtabs(4)),
                        "marker": match.group(2),
                        "content": match.group(3).strip(),
                        "rest": [],
                    }
                elif current is not None:
                    current["rest"].append(raw)
            if current is not None:
                items.append(current)

            for item in items:
                level = item["indent"] // 2
                marker = item["marker"]
                content = item["content"]
                task = None
                if content.startswith("[x]") or content.startswith("[X]"):
                    task = True
                    content = content[3:].strip()
                elif content.startswith("[ ]"):
                    task = False
                    content = content[3:].strip()
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                row.set_margin_start(level * 18)
                if task is True:
                    bullet_text = "☑"
                elif task is False:
                    bullet_text = "☐"
                elif marker in ("-", "*", "+"):
                    bullet_text = "•"
                else:
                    bullet_text = marker
                bullet = Gtk.Label(label=bullet_text, xalign=0, valign=Gtk.Align.START)
                bullet.add_css_class("md-bullet")
                bullet.set_size_request(16, -1)
                row.append(bullet)

                content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
                content_box.set_hexpand(True)
                label = Gtk.Label()
                label.set_markup(inline_markup(content, source_urls, refs, footnotes))
                label.set_xalign(0)
                label.set_wrap(True)
                label.set_selectable(True)
                label.set_hexpand(True)
                label.connect("activate-link", open_link)
                if task is True:
                    label.add_css_class("task-done")
                content_box.append(label)

                rest = item["rest"]
                while rest and not rest[0].strip():
                    rest.pop(0)
                while rest and not rest[-1].strip():
                    rest.pop()
                if rest:
                    indents = [len(line) - len(line.lstrip(" "))
                               for line in rest if line.strip()]
                    cut = min(indents) if indents else 0
                    continuation = "\n".join(
                        line[cut:] if line.strip() else "" for line in rest)
                    for child in markdown_widgets(
                            continuation, sources, refs, footnotes,
                            footnote_order, _nested=True):
                        content_box.append(child)
                row.append(content_box)
                box.append(row)
            widgets.append(box)
        elif kind == "table":
            widgets.append(table_widget(body, source_urls, refs, footnotes))
        elif kind == "hr":
            separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            separator.add_css_class("md-hr")
            widgets.append(separator)
        else:
            image_url = ""
            image_alt = ""
            image_match = re.fullmatch(
                r"!\[([^\]]*)\]\((https?://[^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\)",
                body.strip())
            if image_match:
                image_alt = image_match.group(1).strip()
                image_url = image_match.group(2)
            else:
                ref_match = re.fullmatch(r"!\[([^\]]*)\]\[([^\]]*)\]", body.strip())
                if ref_match:
                    ref_key = (ref_match.group(2) or ref_match.group(1)).strip().lower()
                    ref_info = refs.get(ref_key)
                    if ref_info:
                        image_alt = ref_match.group(1).strip()
                        image_url = ref_info[0] if isinstance(ref_info, (tuple, list)) else ref_info
            if image_url:
                widgets.append(remote_image_widget(image_url, image_alt))
                continue
            label = Gtk.Label()
            label.set_markup(inline_markup(body, source_urls, refs, footnotes))
            label.set_xalign(0)
            label.set_wrap(True)
            label.set_selectable(True)
            label.add_css_class("md-p")
            label.connect("activate-link", open_link)
            widgets.append(label)

    if not _nested and footnote_order:
        separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        separator.add_css_class("md-hr")
        widgets.append(separator)
        title = Gtk.Label(label="脚注", xalign=0)
        title.add_css_class("md-footnote-title")
        widgets.append(title)
        for key in footnote_order:
            number, content = footnotes[key]
            label = Gtk.Label()
            label.set_markup("<sup>[%s]</sup> %s" % (
                number, inline_markup(content, source_urls, refs, footnotes)))
            label.set_xalign(0)
            label.set_wrap(True)
            label.set_selectable(True)
            label.add_css_class("md-footnote")
            label.connect("activate-link", open_link)
            widgets.append(label)
    return widgets

# ---------------------------------------------------------------------------
# 消息控件
# ---------------------------------------------------------------------------

def source_card(source: dict) -> Gtk.Widget:
    button = Gtk.Button()
    button.add_css_class("source-card")
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    index = Gtk.Label(label=str(source.get("index", "")))
    index.add_css_class("source-index")
    index.set_valign(Gtk.Align.START)
    box.append(index)

    texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    title = Gtk.Label(xalign=0)
    title.set_markup("<b>%s</b>" % html.escape(source.get("title") or source.get("url") or ""))
    title.set_ellipsize(Pango.EllipsizeMode.END)
    title.set_max_width_chars(64)
    title.add_css_class("source-title")
    texts.append(title)

    meta_bits = [b for b in (source.get("site"), source.get("published")) if b]
    if meta_bits:
        meta = Gtk.Label(label=" · ".join(str(b) for b in meta_bits), xalign=0)
        meta.add_css_class("source-meta")
        meta.set_ellipsize(Pango.EllipsizeMode.END)
        texts.append(meta)

    snippet = source.get("snippet") or ""
    if len(snippet) > 220:
        snippet = snippet[:217] + "…"
    if snippet:
        snippet_label = Gtk.Label(label=snippet, xalign=0, wrap=True)
        snippet_label.set_max_width_chars(80)
        snippet_label.add_css_class("source-snippet")
        texts.append(snippet_label)
    box.append(texts)
    button.set_child(box)
    button.set_halign(Gtk.Align.START)
    url = source.get("url") or ""
    if url:
        button.connect("clicked", lambda *_: open_link(None, url))
        button.set_tooltip_text(url)
    else:
        button.set_sensitive(False)
    return button


class MessageView(Gtk.Box):
    def __init__(self, role: str, on_regenerate=None, on_edit=None, on_copy=None,
                 on_share=None, on_share_image=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.role = role
        self.add_css_class("msg")
        self.add_css_class(f"msg-{role}")

        self.bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.append(self.bubble)

        self.role_label = Gtk.Label(
            label="你" if role == "user" else "DeepSeek",
            xalign=0,
        )
        self.role_label.add_css_class("msg-role")
        self.role_label.set_visible(False)
        self.bubble.append(self.role_label)

        self.reasoning = Gtk.Expander(label="已思考")
        self.reasoning.add_css_class("msg-reasoning")
        self.reasoning.set_visible(False)
        self.reasoning_body = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.reasoning_body.add_css_class("msg-body")
        self.reasoning.set_child(self.reasoning_body)
        self.bubble.append(self.reasoning)

        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.body.add_css_class("msg-body")
        self.bubble.append(self.body)

        self.sources: list[dict] = []
        self.sources_expander = Gtk.Expander(label="参考来源")
        self.sources_expander.set_visible(False)
        self.sources_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.sources_box.set_halign(Gtk.Align.START)
        self.sources_expander.set_child(self.sources_box)
        self.bubble.append(self.sources_expander)

        self._text = ""
        self._reasoning = ""
        self.meta: dict = {}

        self.actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        self.actions.add_css_class("msg-actions")
        if role == "user":
            if on_copy is not None:
                copy = Gtk.Button.new_from_icon_name("edit-copy-symbolic")
                copy.add_css_class("msg-action")
                copy.set_tooltip_text("复制")
                copy.connect("clicked", lambda *_: on_copy(self))
                self.copy_button = copy
                self.actions.append(copy)
            if on_edit is not None:
                edit = Gtk.Button.new_from_icon_name("document-edit-symbolic")
                edit.add_css_class("msg-action")
                edit.set_tooltip_text("编辑")
                edit.connect("clicked", lambda *_: on_edit(self))
                self.actions.append(edit)
        elif role == "assistant":
            if on_copy is not None:
                copy = Gtk.Button.new_from_icon_name("edit-copy-symbolic")
                copy.add_css_class("msg-action")
                copy.set_tooltip_text("复制回答")
                copy.connect("clicked", lambda *_: on_copy(self))
                self.copy_button = copy
                self.actions.append(copy)
            if on_regenerate is not None:
                regenerate = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
                regenerate.add_css_class("msg-action")
                regenerate.set_tooltip_text("重新生成")
                regenerate.connect("clicked", lambda *_: on_regenerate(self))
                self.actions.append(regenerate)
            if on_share is not None:
                share = Gtk.Button.new_from_icon_name("share-symbolic")
                share.add_css_class("msg-action")
                share.set_tooltip_text("分享对话")
                share.connect("clicked", lambda *_: on_share(self))
                self.actions.append(share)
            if on_share_image is not None:
                share_image = Gtk.Button.new_from_icon_name("image-x-generic-symbolic")
                share_image.add_css_class("msg-action")
                share_image.set_tooltip_text("保存对话长图")
                share_image.connect("clicked", lambda *_: on_share_image(self))
                self.actions.append(share_image)
        if self.actions.get_first_child() is not None:
            self.append(self.actions)

    def set_content(self, text: str, reasoning: str | None = None,
                    sources: list[dict] | None = None) -> None:
        self._text = text or ""
        if reasoning is not None:
            self._reasoning = reasoning or ""
        if sources is not None:
            self.sources = sources
            self._render_sources()
        while self.body.get_first_child() is not None:
            self.body.remove(self.body.get_first_child())
        for widget in markdown_widgets(self._text, self.sources):
            self.body.append(widget)
        if self._reasoning.strip():
            self.reasoning_body.set_text(self._reasoning)
            self.reasoning.set_visible(True)
        else:
            self.reasoning.set_visible(False)

    def refresh_content(self) -> None:
        self.set_content(self.meta.get("text", ""),
                         self.meta.get("reasoning", ""),
                         self.sources)

    def _render_sources(self) -> None:
        while (child := self.sources_box.get_first_child()) is not None:
            self.sources_box.remove(child)
        if not self.sources:
            self.sources_expander.set_visible(False)
            return
        self.sources_expander.set_label(f"参考来源 · {len(self.sources)}")
        self.sources_expander.set_visible(True)
        self.sources_expander.set_expanded(len(self.sources) <= 5)
        for source in self.sources:
            self.sources_box.append(source_card(source))

    def apply_mode(self, mode: str) -> None:
        for name in ("bubble-user", "bubble-assistant", "doc", "minimal"):
            self.bubble.remove_css_class(name)
        if mode == "bubble":
            if self.role == "user":
                self.bubble.add_css_class("bubble-user")
                self.set_halign(Gtk.Align.END)
                self.set_hexpand(False)
            else:
                self.bubble.add_css_class("bubble-assistant")
                self.set_halign(Gtk.Align.FILL)
                self.set_hexpand(True)
        elif mode == "document":
            self.bubble.add_css_class("doc")
            self.set_halign(Gtk.Align.FILL)
            self.set_hexpand(True)
        else:
            self.bubble.add_css_class("minimal")
            self.set_halign(Gtk.Align.FILL)
            self.set_hexpand(True)
        self.actions.set_halign(
            Gtk.Align.END if self.role == "user" else Gtk.Align.START)


# ---------------------------------------------------------------------------
# 登录：只在这里用一次 WebKit 拿 token / cookie
# ---------------------------------------------------------------------------

class LoginWindow(Gtk.ApplicationWindow):
    def __init__(self, app, on_success):
        super().__init__(application=app, title="登录 DeepSeek")
        self.set_icon_name("io.github.deepseeknative.App")
        self.on_success = on_success
        self.set_default_size(980, 760)

        header = Gtk.HeaderBar()
        header.set_show_title_buttons(True)
        self.set_titlebar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(box)

        self.network = WebKit.NetworkSession(
            data_directory=os.path.join(DATA_DIR, "login-webkit"),
            cache_directory=os.path.join(DATA_DIR, "login-cache"),
        )
        try:
            self.network.get_cookie_manager().set_persistent_storage(
                os.path.join(DATA_DIR, "login-cookies.sqlite"),
                WebKit.CookiePersistentStorage.SQLITE,
            )
        except Exception:
            pass

        self.view = WebKit.WebView(network_session=self.network)
        settings = self.view.get_settings()
        settings.set_property("user-agent", USER_AGENT)
        self.view.set_hexpand(True)
        self.view.set_vexpand(True)
        box.append(self.view)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10,
                      margin_start=12, margin_end=12, margin_top=8, margin_bottom=8)
        self.status = Gtk.Label(
            label="登录完成后点右侧按钮；登录状态只保存在本机。",
            xalign=0, hexpand=True, wrap=True)
        bar.append(self.status)
        manual = Gtk.Button(label="手动输入 Token")
        manual.connect("clicked", lambda *_: self._manual_token())
        bar.append(manual)
        done = Gtk.Button(label="我已完成登录")
        done.add_css_class("suggested-action")
        done.connect("clicked", lambda *_: self._capture())
        bar.append(done)
        box.append(bar)

        self.view.connect("load-changed", self._on_load)
        self.view.load_uri(BASE_URL + "/sign_in")
        if os.environ.get("DSD_LOGIN_SCREENSHOT"):
            GLib.timeout_add(4000, self._screenshot)
        if os.environ.get("DSD_FAKE_TOKEN"):
            GLib.timeout_add(2500, self._fake_login)

    def _on_load(self, _view, event):
        if event == WebKit.LoadEvent.FINISHED:
            uri = self.view.get_uri() or ""
            if "sign_in" not in uri:
                self.status.set_text("检测到已登录，正在读取 token…")
                GLib.timeout_add(800, self._capture)

    def _screenshot(self):
        path = os.environ.get("DSD_LOGIN_SCREENSHOT")
        try:
            from gi.repository import Gsk
            paintable = Gtk.WidgetPaintable.new(self)
            snapshot = Gtk.Snapshot.new()
            paintable.snapshot(snapshot, self.get_width(), self.get_height())
            texture = self.get_renderer().render_texture(snapshot.to_node(), None)
            texture.save_to_png(path)
            print(f"LOGIN_SCREENSHOT {path}", flush=True)
        except Exception as exc:
            print(f"LOGIN_SCREENSHOT_ERROR {exc}", flush=True)
        self.get_application().quit()
        return False

    def _fake_login(self):
        token = os.environ.get("DSD_FAKE_TOKEN") or "test-token"
        self.view.evaluate_javascript(
            "localStorage.setItem('userToken', %s); 'ok'" % json.dumps(token),
            -1, None, None, None, None, None)
        GLib.timeout_add(600, self._capture)
        return False

    def _capture(self):
        def token_done(view, result, _data):
            raw = None
            try:
                value = view.evaluate_javascript_finish(result)
                raw = value.to_string() if value is not None else None
            except Exception:
                raw = None
            if not raw or raw == "null":
                self.status.set_text("还没有读到 token，请确认已经登录成功。")
                return
            token = raw
            if token[:1] in "{[":
                try:
                    parsed = json.loads(token)
                    token = parsed.get("value") or parsed.get("token") or token
                except ValueError:
                    pass
            if not token:
                self.status.set_text("token 为空，请重试。")
                return
            self.status.set_text("正在读取 Cookie…")
            self._read_cookies(token)

        self.view.evaluate_javascript(
            "localStorage.getItem('userToken')",
            -1, None, None, None, token_done, None,
        )

    def _read_cookies(self, token):
        def done(manager, result, _data):
            cookies = {}
            try:
                for cookie in manager.get_cookies_finish(result):
                    if isinstance(cookie, Soup.Cookie):
                        cookies[cookie.get_name()] = cookie.get_value()
            except Exception:
                pass
            session = Session(token=token, cookies=cookies)
            save_session(session)
            self.on_success(session)
            self.close()

        try:
            self.network.get_cookie_manager().get_cookies(BASE_URL, None, done, None)
        except Exception:
            done(None, None, None)

    def _manual_token(self):
        dialog = Gtk.Window(transient_for=self, modal=True, title="手动输入")
        dialog.set_default_size(560, 260)
        header = Gtk.HeaderBar()
        header.set_show_title_buttons(True)
        dialog.set_titlebar(header)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                      margin_start=16, margin_end=16, margin_top=14, margin_bottom=14)
        box.append(Gtk.Label(
            label="从浏览器 DevTools 的 localStorage 里复制 userToken（可只填 Token）。",
            xalign=0, wrap=True))
        token_entry = Gtk.Entry()
        token_entry.set_placeholder_text("userToken")
        box.append(token_entry)
        cookie_entry = Gtk.Entry()
        cookie_entry.set_placeholder_text("Cookie（可选，形如 a=1; b=2）")
        box.append(cookie_entry)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                          halign=Gtk.Align.END)
        cancel = Gtk.Button(label="取消")
        cancel.connect("clicked", lambda *_: dialog.close())
        ok = Gtk.Button(label="保存")
        ok.add_css_class("suggested-action")

        def confirm(_btn):
            token = token_entry.get_text().strip()
            if not token:
                return
            cookies = {}
            for part in cookie_entry.get_text().split(";"):
                if "=" in part:
                    key, value = part.split("=", 1)
                    cookies[key.strip()] = value.strip()
            session = Session(token=token, cookies=cookies)
            save_session(session)
            dialog.close()
            self.on_success(session)
            self.close()

        ok.connect("clicked", confirm)
        buttons.append(cancel)
        buttons.append(ok)
        box.append(buttons)
        dialog.set_child(box)
        dialog.present()


# ---------------------------------------------------------------------------
# PoW：隐藏的 WebKit 页面里跑网页版自带的 JS solver
# ---------------------------------------------------------------------------

class PowBridge:
    def __init__(self):
        self.ready = threading.Event()
        self.waiting: dict[str, tuple[threading.Event, dict]] = {}
        self.lock = threading.Lock()

        self.window = Gtk.Window()
        self.window.set_default_size(1, 1)
        self.window.set_decorated(False)
        self.ucm = WebKit.UserContentManager()
        try:
            self.ucm.register_script_message_handler("pow")
            self.ucm.connect("script-message-received::pow", self._on_message)
        except Exception as exc:
            print(f"[pow] 消息通道不可用: {exc}", file=sys.stderr)

        self.view = WebKit.WebView(user_content_manager=self.ucm)
        self.window.set_child(self.view)
        self.view.connect("load-changed", self._on_load)
        self.view.load_html("<html><body>pow</body></html>", "about:blank")
        try:
            self.window.set_opacity(0.0)
        except Exception:
            pass
        self.window.present()
        GLib.timeout_add(700, self.window.hide)

    def _on_load(self, _view, event):
        if event != WebKit.LoadEvent.FINISHED:
            return
        try:
            with open(POW_WORKER, "r", encoding="utf-8") as fh:
                worker = fh.read()
        except OSError as exc:
            print(f"[pow] 读取 solver 失败: {exc}", file=sys.stderr)
            return
        script = """
        window.__powCode = %s;
        window.__powSolve = function(id, chal) {
            try {
                const blob = new Blob([window.__powCode], {type: 'text/javascript'});
                const worker = new Worker(URL.createObjectURL(blob));
                worker.onmessage = function(ev) {
                    try {
                        window.webkit.messageHandlers.pow.postMessage(
                            JSON.stringify({id: id, data: ev.data}));
                    } catch (e) {}
                    worker.terminate();
                };
                worker.onerror = function(ev) {
                    try {
                        window.webkit.messageHandlers.pow.postMessage(
                            JSON.stringify({id: id, error: String(ev.message || ev)}));
                    } catch (e) {}
                    worker.terminate();
                };
                worker.postMessage({type: 'pow-challenge', challenge: chal});
            } catch (err) {
                try {
                    window.webkit.messageHandlers.pow.postMessage(
                        JSON.stringify({id: id, error: String(err)}));
                } catch (e) {}
            }
        };
        'ok';
        """ % json.dumps(worker)
        self.view.evaluate_javascript(script, -1, None, None, None,
                                      lambda *a: self.ready.set(), None)

    def _on_message(self, _ucm, value, *_args):
        try:
            raw = value.to_string() if hasattr(value, "to_string") else str(value)
            payload = json.loads(raw)
        except Exception:
            return
        ident = payload.get("id")
        with self.lock:
            entry = self.waiting.pop(ident, None)
        if entry is None:
            return
        event, box = entry
        box.update(payload)
        event.set()

    def solve(self, challenge: dict, target_path: str, timeout: float = 90.0) -> str:
        if not self.ready.wait(10):
            raise ApiError("PoW 引擎没有就绪")
        ident = uuid.uuid4().hex
        event = threading.Event()
        box: dict = {}
        with self.lock:
            self.waiting[ident] = (event, box)
        GLib.idle_add(self._start, ident, challenge)
        if not event.wait(timeout):
            with self.lock:
                self.waiting.pop(ident, None)
            raise ApiError("PoW 求解超时")
        if "error" in box:
            raise ApiError(f"PoW 失败: {box['error']}")
        data = box.get("data") or {}
        if data.get("type") != "pow-answer":
            raise ApiError(f"PoW 返回异常: {data}")
        answer = data.get("answer") or {}
        return pow_header(challenge, int(answer["answer"]), target_path)

    def _start(self, ident: str, challenge: dict):
        chal = dict(challenge)
        if "expireAt" not in chal:
            chal["expireAt"] = chal.get("expire_at")
        if "expireAfter" not in chal:
            chal["expireAfter"] = chal.get("expire_after", 3600)
        script = "window.__powSolve(%s, %s); 'ok';" % (
            json.dumps(ident), json.dumps(chal))
        self.view.evaluate_javascript(script, -1, None, None, None, None, None)
        return False


# ---------------------------------------------------------------------------
# 主窗口：原生消息列表
# ---------------------------------------------------------------------------

DEMO_CONVERSATIONS = [
    {
        "id": "demo-1",
        "title": "原生渲染演示",
        "messages": [
            {"role": "user", "text": "用两三句话解释一下，为什么把网页壳换成原生控件会更好？"},
            {"role": "assistant", "reasoning": "用户想要的是可定制性，而不是再包一层浏览器。",
             "text": (
                 "因为数据一旦从网页里拿出来，**怎么显示就由本地决定**：\n\n"
                 "- 字体、行距、宽度可以按阅读习惯调\n"
                 "- 同一条回答可以切「气泡 / 文档 / 极简」\n"
                 "- 代码块、引用、表格都能用本地控件重新排版\n\n"
                 "> 网页壳只能改 CSS，原生客户端可以改结构。"
             )},
        ],
    },
    {
        "id": "demo-2",
        "title": "代码与表格",
        "messages": [
            {"role": "user", "text": "给我一个 Python 的快速排序，再列一个复杂度对比表。"},
            {"role": "assistant", "text": (
                "### 快速排序\n\n"
                "```python\n"
                "def quicksort(items):\n"
                "    if len(items) <= 1:\n"
                "        return items\n"
                "    pivot = items[len(items) // 2]\n"
                "    left = [x for x in items if x < pivot]\n"
                "    mid = [x for x in items if x == pivot]\n"
                "    right = [x for x in items if x > pivot]\n"
                "    return quicksort(left) + mid + quicksort(right)\n"
                "```\n\n"
                "| 算法 | 平均 | 最坏 | 稳定 |\n"
                "| --- | --- | --- | --- |\n"
                "| 快速排序 | O(n log n) | O(n²) | 否 |\n"
                "| 归并排序 | O(n log n) | O(n log n) | 是 |\n"
            )},
        ],
    },
    {
        "id": "demo-3",
        "title": "这套客户端怎么工作",
        "messages": [
            {"role": "user", "text": "它是怎么拿到 DeepSeek 数据的？"},
            {"role": "assistant", "text": (
                "登录只借用一次系统 WebKit 拿到 `userToken` 和 Cookie；之后：\n\n"
                "1. 直接请求 `chat_session/fetch_page` 拿会话列表\n"
                "2. 请求 `history_messages` 拿历史\n"
                "3. 发送时解一次 PoW，再读 `chat/completion` 的 SSE 流\n\n"
                "所有文字都由 GTK 的 `Gtk.Label`、`Gtk.Box` 重新排版，不再渲染网页。"
            )},
        ],
    },
    {
        "id": "demo-4",
        "title": "列表与行内代码",
        "messages": [
            {"role": "user", "text": "dconf-editor 怎么装？给我一个步骤。"},
            {"role": "assistant", "text": (
                "### 🔧 高级技巧：用 `dconf-editor` 突破 UI 限制\n\n"
                "这是一个普通段落，包含 **粗体**、*斜体*、***粗斜体***、~~删除线~~、"
                "`行内代码`、转义字符 \\*不是斜体\\*、链接 [示例链接](https://example.com)、"
                "图片 ![图片](https://via.placeholder.com/120x40?text=Demo)、emoji 😊，"
                "以及 HTML 实体：&copy; &amp;。\n\n"
                "1. 安装 `dconf-editor`：\n\n"
                "   ```bash\n"
                "   sudo apt install dconf-editor\n"
                "   ```\n\n"
                "2. 打开后导航至路径：`/org/gnome/shell/extensions/burn-my-windows/`\n"
            )},
        ],
    },
]


def save_widget_png(widget, path: str) -> bool:
    try:
        paintable = Gtk.WidgetPaintable.new(widget)
        snapshot = Gtk.Snapshot.new()
        paintable.snapshot(snapshot, widget.get_width(), widget.get_height())
        texture = widget.get_renderer().render_texture(snapshot.to_node(), None)
        texture.save_to_png(path)
        return True
    except Exception:
        return False


def make_pill_toggle(label: str, icon_name: str) -> Gtk.ToggleButton:
    button = Gtk.ToggleButton()
    button.add_css_class("input-pill")
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                  halign=Gtk.Align.CENTER)
    box.append(Gtk.Image.new_from_icon_name(icon_name))
    box.append(Gtk.Label(label=label))
    button.set_child(box)
    return button


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app, api: DeepSeekAPI | None, session: Session | None,
                 demo: bool = False):
        super().__init__(application=app, title=f"{APP_NAME_CN} · {APP_NAME}")
        self.set_icon_name("io.github.deepseeknative.App")
        self.api = api
        self.demo = demo
        self.chat_id: str | None = None
        self.last_message_id: int | None = None
        self.message_views: list[MessageView] = []
        self.current_assistant: MessageView | None = None
        self.stream_text = ""
        self.stream_reasoning = ""
        self.stream_sources: list[dict] = []
        self.streaming = False
        self.cancel_event = threading.Event()
        self.render_scheduled = False
        self.stream_error = False
        self._test_regenerated = False
        self._test_edited = False
        self.settings = load_settings()
        self.fonts = normalize_fonts(self.settings.get("fonts"))
        set_font_config(self.fonts)
        self.theme = self.settings.get("theme", "system")
        self._theme_dark = apply_theme_mode(self.theme)
        self._theme_dropdown = None
        self._theme_sync = False
        self._interface_settings = None
        self._sharing = False
        self._sharing_image = False
        self._share_header = None
        self.mode = "bubble"
        self._settings_window = None
        self._fonts_dirty = False
        self._font_spins = {}
        self._font_sync = False
        self.attachments: list[dict] = []
        self._edit_editor = None
        self._edit_view = None
        self._edit_message_id = None
        self.demo_conversations = DEMO_CONVERSATIONS
        if demo and os.environ.get("DSD_DEMO_FILE"):
            try:
                with open(os.environ["DSD_DEMO_FILE"], "r", encoding="utf-8") as fh:
                    demo_text = fh.read()
                self.demo_conversations = [{
                    "id": "demo-file",
                    "title": os.path.basename(os.environ["DSD_DEMO_FILE"]),
                    "messages": [{"role": "assistant", "text": demo_text}],
                }]
            except OSError:
                pass
        self.chat_cursor = None
        self.chat_has_more = False
        self.loaded_chat_ids: set[str] = set()
        self.loading_chats = False
        self.search_mode = False
        self.search_generation = 0
        self.search_source_id = 0
        self._last_group = None

        self.set_default_size(1280, 860)
        if os.environ.get("DSD_WINDOW_SIZE"):
            try:
                w, h = os.environ["DSD_WINDOW_SIZE"].lower().split("x")
                self.set_default_size(int(w), int(h))
            except ValueError:
                pass
        self.set_size_request(900, 600)
        self._build_ui()
        try:
            self._interface_settings = Gio.Settings.new("org.gnome.desktop.interface")
            self._interface_settings.connect(
                "changed::color-scheme", self._on_system_theme_changed)
        except Exception:
            self._interface_settings = None
        if self.demo:
            self.search_entry.set_visible(False)
        if os.environ.get("DSD_SEARCH"):
            self.search_toggle.set_active(True)

        if os.environ.get("DSD_OPEN_SETTINGS"):
            GLib.timeout_add(1200, self.open_settings)
        if os.environ.get("DSD_SCROLL_FRAC"):
            def _scroll_frac():
                try:
                    frac = float(os.environ["DSD_SCROLL_FRAC"])
                    adj = self.messages_scroll.get_vadjustment()
                    adj.set_value(max(0.0, min(1.0, frac)) * max(0.0, adj.get_upper() - adj.get_page_size()))
                except (TypeError, ValueError):
                    pass
                return False
            GLib.timeout_add(2500, _scroll_frac)
        if os.environ.get("DSD_INPUT_TEXT"):
            def fill_input():
                self.input_view.get_buffer().set_text(os.environ["DSD_INPUT_TEXT"])
                self.input_view.grab_focus()
                return False
            GLib.timeout_add(1200, fill_input)
        if os.environ.get("DSD_SCROLL_BOTTOM"):
            GLib.timeout_add(1500, self._scroll_chat_bottom)
        if os.environ.get("DSD_EMPTY"):
            GLib.timeout_add(1200, self.new_chat)
        if os.environ.get("DSD_SEARCH_QUERY"):
            query = os.environ["DSD_SEARCH_QUERY"]
            self.search_entry.set_visible(True)
            GLib.timeout_add(1500, lambda: self.search_entry.set_text(query))

        if demo:
            self.status_label.set_text("演示模式（本地样例数据）")
            self._populate_demo_chats()
        else:
            self.status_label.set_text("")
            if self.api is not None:
                if self.api.session.user:
                    self._apply_user_info(self.api.session.user)
                threading.Thread(target=self._fetch_user_info, daemon=True).start()
                self.load_chats()

        if os.environ.get("DSD_AUTO_ATTACH"):
            GLib.timeout_add(1200, self._auto_attach)
        if os.environ.get("DSD_AUTO_SEND"):
            GLib.timeout_add(2000, self._auto_send)
        if os.environ.get("DSD_SCREENSHOT"):
            delay = int(os.environ.get("DSD_SHOT_DELAY", "1600"))
            GLib.timeout_add(delay, self._screenshot)

    # -- 布局 ---------------------------------------------------------------
    def _build_ui(self):
        header = Gtk.HeaderBar()
        header.set_show_title_buttons(True)
        header.set_title_widget(Gtk.Box())
        def titlebar_icon(icon_name, tooltip, callback):
            button = Gtk.Button()
            button.add_css_class("titlebar-icon")
            image = Gtk.Image.new_from_icon_name(icon_name)
            image.set_pixel_size(18)
            button.set_child(image)
            button.set_tooltip_text(tooltip)
            button.connect("clicked", lambda *_: callback())
            return button

        self.theme_button = Gtk.Button()
        self.theme_button.add_css_class("titlebar-icon")
        self.theme_button.connect("clicked", self._toggle_theme)

        titlebar_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        titlebar_actions.append(titlebar_icon(
            "system-log-out-symbolic", "重新登录", self._relogin))
        titlebar_actions.append(titlebar_icon(
            "emblem-system-symbolic", "设置", self.open_settings))
        titlebar_actions.append(self.theme_button)
        header.pack_end(titlebar_actions)
        self.set_titlebar(header)
        self._update_theme_icon()

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.main_overlay = Gtk.Overlay()
        self.main_overlay.set_child(root)
        self.set_child(self.main_overlay)
        self.modal_shade = Gtk.Box()
        self.modal_shade.add_css_class("modal-shade")
        self.modal_shade.set_can_target(True)
        self.modal_shade.set_visible(False)
        self.main_overlay.add_overlay(self.modal_shade)

        paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        self.paned = paned
        paned.set_position(290)
        paned.set_vexpand(True)
        paned.set_resize_start_child(False)
        paned.set_shrink_start_child(False)
        root.append(paned)

        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sidebar.add_css_class("sidebar")
        sidebar.add_css_class("sidebar-card")
        sidebar.set_size_request(282, -1)
        sidebar.set_margin_start(20)
        sidebar.set_margin_end(4)
        sidebar.set_margin_top(6)
        sidebar.set_margin_bottom(20)
        self.sidebar_widget = sidebar

        brand = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                        margin_start=12, margin_end=8, margin_top=10, margin_bottom=4)
        self.date_label = Gtk.Label(label=self._today_label(), xalign=0)
        self.date_label.add_css_class("date-label")
        brand.append(self.date_label)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        brand.append(spacer)
        self.search_button = Gtk.Button.new_from_icon_name("edit-find-symbolic")
        self.search_button.add_css_class("attach-flat")
        self.search_button.set_tooltip_text("搜索会话")
        self.search_button.connect("clicked", lambda *_: self._toggle_search())
        brand.append(self.search_button)
        self.refresh_button = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self.refresh_button.add_css_class("attach-flat")
        self.refresh_button.set_tooltip_text("刷新会话列表")
        self.refresh_button.connect("clicked", lambda *_: self._refresh_chats())
        brand.append(self.refresh_button)
        self.collapse_button = Gtk.Button.new_from_icon_name("sidebar-show-right-symbolic")
        self.collapse_button.add_css_class("attach-flat")
        self.collapse_button.set_tooltip_text("收起侧栏")
        self.collapse_button.connect("clicked", lambda *_: self._toggle_sidebar())
        brand.append(self.collapse_button)
        sidebar.append(brand)

        new_btn = Gtk.Button(label="＋ 开启新对话")
        new_btn.add_css_class("primary-button")
        new_btn.set_margin_start(10)
        new_btn.set_margin_end(10)
        new_btn.set_margin_top(4)
        new_btn.set_margin_bottom(6)
        new_btn.connect("clicked", lambda *_: self.new_chat())
        sidebar.append(new_btn)

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.add_css_class("search-pill")
        self.search_entry.set_placeholder_text("搜索会话…")
        self.search_entry.set_margin_start(10)
        self.search_entry.set_margin_end(10)
        self.search_entry.set_margin_bottom(6)
        self.search_entry.set_visible(False)
        self.search_entry.connect("search-changed", self._on_search_changed)
        sidebar.append(self.search_entry)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.chat_list = Gtk.ListBox()
        self.chat_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.chat_list.add_css_class("chat-list")
        self.chat_list.connect("row-activated", self._on_chat_activated)
        scrolled.set_child(self.chat_list)
        self.chat_scroll = scrolled.get_vadjustment()
        self.chat_scroll.connect("value-changed", self._on_chat_scroll)

        list_overlay = Gtk.Overlay()
        list_overlay.set_vexpand(True)
        list_overlay.set_child(scrolled)
        list_fade = Gtk.Box()
        list_fade.add_css_class("list-fade")
        list_fade.set_valign(Gtk.Align.END)
        list_fade.set_can_target(False)
        list_fade.set_size_request(-1, 42)
        list_overlay.add_overlay(list_fade)
        sidebar.append(list_overlay)

        profile = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                          margin_start=10, margin_end=6, margin_top=6, margin_bottom=8)
        self.profile_row = profile
        try:
            badge = GdkPixbuf.Pixbuf.new_from_file(
                os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "assets", "avatar-user-badge-40.png"))
            self.profile_avatar = Gtk.Image.new_from_pixbuf(badge)
            self.profile_avatar.set_size_request(40, 40)
        except Exception:
            self.profile_avatar = Gtk.Label(label="D")
            self.profile_avatar.set_valign(Gtk.Align.CENTER)
        self.profile_avatar.add_css_class("avatar")
        profile.append(self.profile_avatar)
        profile_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0,
                               valign=Gtk.Align.CENTER)
        self.profile_name = Gtk.Label(label="", xalign=0)
        self.profile_name.add_css_class("profile-name")
        self.profile_name.set_ellipsize(Pango.EllipsizeMode.END)
        self.profile_name.set_max_width_chars(18)
        self.profile_sub = Gtk.Label(label="", xalign=0)
        self.profile_sub.add_css_class("profile-sub")
        self.profile_sub.set_ellipsize(Pango.EllipsizeMode.END)
        self.profile_sub.set_max_width_chars(22)
        profile_text.append(self.profile_name)
        profile_text.append(self.profile_sub)
        profile.append(profile_text)
        profile.set_visible(False)
        sidebar.append(profile)

        self.sidebar_visible = True
        paned.set_start_child(sidebar)

        chat_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        paned.set_end_child(chat_area)

        chat_head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                            margin_start=24, margin_end=24,
                            margin_top=8, margin_bottom=2)
        self.panel_button = Gtk.Button.new_from_icon_name("sidebar-show-right-symbolic")
        self.panel_button.add_css_class("attach-flat")
        self.panel_button.set_tooltip_text("展开侧栏")
        self.panel_button.set_visible(False)
        self.panel_button.connect("clicked", lambda *_: self._toggle_sidebar())
        chat_head.append(self.panel_button)
        title_spacer = Gtk.Box()
        title_spacer.set_hexpand(True)
        chat_head.append(title_spacer)
        self.status_label = Gtk.Label(label="", xalign=1)
        self.status_label.add_css_class("chat-preview")
        self.status_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.status_label.set_max_width_chars(36)
        chat_head.append(self.status_label)
        chat_area.append(chat_head)

        self.chat_stack = Gtk.Stack()
        self.chat_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.chat_stack.set_vexpand(True)
        chat_area.append(self.chat_stack)

        messages_scroll = Gtk.ScrolledWindow()
        messages_scroll.set_vexpand(True)
        messages_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.messages_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                                    margin_start=32, margin_end=32,
                                    margin_top=20, margin_bottom=20)
        messages_scroll.set_child(self.messages_box)
        self.messages_scroll = messages_scroll

        messages_overlay = Gtk.Overlay()
        messages_overlay.set_vexpand(True)
        messages_overlay.set_child(messages_scroll)
        self.messages_fade_top = Gtk.Box()
        self.messages_fade_top.add_css_class("messages-fade-top")
        self.messages_fade_top.set_valign(Gtk.Align.START)
        self.messages_fade_top.set_can_target(False)
        self.messages_fade_top.set_size_request(-1, 44)
        self.messages_fade_top.set_opacity(0)
        messages_overlay.add_overlay(self.messages_fade_top)
        self.messages_fade_bottom = Gtk.Box()
        self.messages_fade_bottom.add_css_class("messages-fade-bottom")
        self.messages_fade_bottom.set_valign(Gtk.Align.END)
        self.messages_fade_bottom.set_can_target(False)
        self.messages_fade_bottom.set_size_request(-1, 44)
        self.messages_fade_bottom.set_opacity(0)
        messages_overlay.add_overlay(self.messages_fade_bottom)
        self.messages_overlay = messages_overlay
        self.chat_stack.add_named(messages_overlay, "chat")
        self.chat_stack.add_named(self._build_empty_page(), "empty")
        self.chat_stack.set_visible_child_name("empty")
        adjustment = self.messages_scroll.get_vadjustment()
        if adjustment is not None:
            adjustment.connect("value-changed", lambda *_: self._update_message_fades())
            adjustment.connect("changed", lambda *_: self._update_message_fades())
        GLib.idle_add(self._update_message_fades)

        input_frame = Gtk.Frame()
        input_frame.add_css_class("input-frame")
        input_frame.set_margin_start(32)
        input_frame.set_margin_end(32)
        input_frame.set_margin_bottom(20)
        input_frame.set_margin_top(6)
        input_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                            margin_start=14, margin_end=14,
                            margin_top=12, margin_bottom=10)
        input_frame.set_child(input_box)

        self.attachments_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                                       spacing=6, margin_start=2, margin_end=2)
        self.attachments_box.set_visible(False)
        input_box.append(self.attachments_box)

        self.input_view = Gtk.TextView()
        self.input_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.input_view.set_top_margin(4)
        self.input_view.set_bottom_margin(4)
        self.input_view.set_left_margin(2)
        self.input_view.set_right_margin(2)
        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_key)
        self.input_view.add_controller(key)

        input_scroll = Gtk.ScrolledWindow()
        input_scroll.set_child(self.input_view)
        input_scroll.set_min_content_height(52)
        input_scroll.set_max_content_height(170)
        input_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        overlay = Gtk.Overlay()
        overlay.set_child(input_scroll)
        self.input_placeholder = Gtk.Label(label="给 DeepSeek 发送消息", xalign=0)
        self.input_placeholder.add_css_class("input-placeholder")
        self.input_placeholder.set_halign(Gtk.Align.START)
        self.input_placeholder.set_valign(Gtk.Align.START)
        self.input_placeholder.set_margin_start(4)
        self.input_placeholder.set_margin_top(5)
        self.input_placeholder.set_can_target(False)
        overlay.add_overlay(self.input_placeholder)
        input_box.append(overlay)
        self.input_view.get_buffer().connect("changed", self._on_input_changed)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                           margin_start=2, margin_end=2)
        self.thinking_toggle = make_pill_toggle("深度思考", "applications-science-symbolic")
        controls.append(self.thinking_toggle)
        self.search_toggle = make_pill_toggle("智能搜索", "web-browser-symbolic")
        controls.append(self.search_toggle)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        controls.append(spacer)

        self.attach_button = Gtk.Button()
        attach_image = Gtk.Image.new_from_icon_name("mail-attachment-symbolic")
        attach_image.set_pixel_size(20)
        self.attach_button.set_child(attach_image)
        self.attach_button.add_css_class("attach-flat")
        self.attach_button.set_tooltip_text("添加文件或图片（也可拖拽 / Ctrl+V 粘贴图片）")
        self.attach_button.connect("clicked", lambda *_: self._choose_files())
        if self.demo:
            self.attach_button.set_sensitive(False)
            self.attach_button.set_tooltip_text("演示模式不支持上传")
        controls.append(self.attach_button)

        self.stop_button = Gtk.Button()
        stop_image = Gtk.Image.new_from_icon_name("media-playback-stop-symbolic")
        stop_image.set_pixel_size(16)
        self.stop_button.set_child(stop_image)
        self.stop_button.add_css_class("send-circle")
        self.stop_button.add_css_class("stop-circle")
        self.stop_button.set_tooltip_text("停止生成")
        self.stop_button.set_visible(False)
        self.stop_button.connect("clicked", lambda *_: self.stop_stream())
        controls.append(self.stop_button)

        self.send_button = Gtk.Button()
        send_image = Gtk.Image.new_from_icon_name("go-up-symbolic")
        send_image.set_pixel_size(20)
        self.send_button.set_child(send_image)
        self.send_button.add_css_class("send-circle")
        self.send_button.add_css_class("send-idle")
        self.send_button.set_tooltip_text("发送（Enter）")
        self.send_button.set_sensitive(False)
        self.send_button.connect("clicked", lambda *_: self.send_current())
        controls.append(self.send_button)

        input_box.append(controls)
        chat_area.append(input_frame)

        install_font_css(build_font_css(self.fonts))
        GLib.timeout_add_seconds(3600, self._refresh_date)
        self._install_actions()
        self._install_drop_target()

    def _build_empty_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                       halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                       hexpand=True, vexpand=True)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                      halign=Gtk.Align.CENTER)
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "assets", "icon.svg")
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file(icon_path)
            picture = Gtk.Picture.new_for_paintable(
                Gdk.Texture.new_for_pixbuf(pixbuf))
            picture.set_size_request(52, 52)
            row.append(picture)
        except Exception:
            pass
        title = Gtk.Label(label="你好，有什么我能帮你的吗？")
        title.add_css_class("empty-title")
        row.append(title)
        page.append(row)
        subtitle = Gtk.Label(label="从一句话开始，或者拖一个文件进来")
        subtitle.add_css_class("empty-subtitle")
        page.append(subtitle)
        return page

    def _on_input_changed(self, buffer):
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
        self.input_placeholder.set_visible(not text.strip())
        self._update_send_sensitive()
        return False

    def _update_send_sensitive(self):
        if not hasattr(self, "send_button"):
            return
        buf = self.input_view.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True).strip()
        ready = any(a["status"] == "ready" for a in self.attachments)
        enabled = bool(text or ready) and not self.streaming
        self.send_button.set_sensitive(enabled)
        if enabled:
            self.send_button.add_css_class("send-active")
            self.send_button.remove_css_class("send-idle")
        else:
            self.send_button.remove_css_class("send-active")
            self.send_button.add_css_class("send-idle")
        self.send_button.set_tooltip_text("发送（Enter）" if enabled else "请输入你的问题")

    @staticmethod
    def _today_label() -> str:
        now = datetime.now()
        weekdays = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
        return f"{now.year}年{now.month}月{now.day}日 · {weekdays[now.weekday()]}"

    def _refresh_date(self):
        self.date_label.set_text(self._today_label())
        return True

    def _scroll_chat_bottom(self):
        if self.chat_scroll is not None:
            self.chat_scroll.set_value(
                self.chat_scroll.get_upper() - self.chat_scroll.get_page_size())
        return False

    def _toggle_search(self):
        visible = not self.search_entry.get_visible()
        self.search_entry.set_visible(visible)
        if visible:
            self.search_entry.grab_focus()
        elif self.search_entry.get_text():
            self.search_entry.set_text("")

    def _toggle_sidebar(self):
        visible = not self.sidebar_widget.get_visible()
        self.sidebar_widget.set_visible(visible)
        self.sidebar_visible = visible
        self.panel_button.set_visible(not visible)

    def _install_drop_target(self):
        try:
            target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
            target.connect("drop", self._on_drop_files)
            self.add_controller(target)
        except Exception:
            pass

    def _on_drop_files(self, _target, value, _x, _y):
        try:
            paths = [f.get_path() for f in value.get_files() if f.get_path()]
        except Exception:
            paths = []
        if paths:
            self._attach_paths(paths)
            return True
        return False

    def _install_actions(self):
        def simple(name, handler):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)

        simple("preferences", lambda *_: self.open_settings())
        simple("relogin", lambda *_: self._relogin())
        simple("about", lambda *_: self._about())
        app = self.get_application()

    def apply_fonts(self, fonts: dict) -> None:
        self.fonts = normalize_fonts(fonts)
        self.settings["fonts"] = self.fonts
        save_settings(self.settings)
        set_font_config(self.fonts)
        install_font_css(build_font_css(self.fonts))
        if self.streaming:
            self._fonts_dirty = True
            return
        for view in list(self.message_views):
            try:
                view.refresh_content()
            except Exception:
                pass
        self._sync_font_spins()

    def _change_font(self, category: str, family=None, size=None) -> None:
        fonts = normalize_fonts(self.fonts)
        if family is not None:
            fonts[category]["family"] = family.strip()
        if size is not None:
            fonts[category]["size"] = int(size)
        self.apply_fonts(fonts)

    def _effective_font_sizes(self, fonts=None) -> dict:
        fonts = fonts or self.fonts
        base = system_font_size()
        ui = int(fonts["ui"]["size"] or base)
        text = int(fonts["text"]["size"] or base)
        mono = int(fonts["mono"]["size"] or max(8, round(text * 0.9)))
        return {"ui": ui, "text": text, "mono": mono}

    def _sync_font_spins(self) -> None:
        if not self._font_spins:
            return
        sizes = self._effective_font_sizes()
        self._font_sync = True
        try:
            for category, spin in self._font_spins.items():
                if not self.fonts[category]["size"]:
                    spin.set_value(sizes[category])
        finally:
            self._font_sync = False

    def _font_row(self, category: str, label: str,
                  show_family: bool = True) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.add_css_class("settings-row")
        name = Gtk.Label(label=label, xalign=0, valign=Gtk.Align.CENTER)
        name.set_size_request(118, -1)
        row.append(name)

        if not show_family:
            fixed = Gtk.Label(label="MathJax", xalign=0)
            fixed.add_css_class("settings-hint")
            fixed.set_hexpand(True)
            row.append(fixed)
            return self._font_size_controls(row, category)

        dialog = Gtk.FontDialog()
        try:
            dialog.set_title("选择字体")
        except Exception:
            pass
        button = Gtk.FontDialogButton.new(dialog)
        button.set_level(Gtk.FontLevel.FAMILY)
        button.set_use_font(True)
        button.set_hexpand(True)
        current = self.fonts[category]
        try:
            defaults = Gtk.Settings.get_default()
            default_name = defaults.get_property("gtk-font-name") if defaults else "Sans 11"
            desc = Pango.FontDescription.from_string(default_name or "Sans 11")
        except Exception:
            desc = Pango.FontDescription.from_string("Sans 11")
        if current.get("family"):
            desc.set_family(current["family"])
        button.set_font_desc(desc)
        button.set_tooltip_text("选择字体家族；字号在右侧设置")

        def on_font(widget, _pspec):
            chosen = widget.get_font_desc()
            if chosen is not None:
                self._change_font(category, family=chosen.get_family() or "")

        button.connect("notify::font-desc", on_font)
        row.append(button)

        return self._font_size_controls(row, category)

    def _line_spacing_row(self) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.add_css_class("settings-row")
        name = Gtk.Label(label="行距", xalign=0, valign=Gtk.Align.CENTER)
        name.set_size_request(118, -1)
        row.append(name)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        row.append(spacer)
        spin = Gtk.SpinButton.new_with_range(1.0, 2.8, 0.05)
        spin.set_digits(2)
        spin.set_value(float(self.fonts["text"].get("line_spacing", 1.55)))
        spin.set_width_chars(5)
        spin.set_tooltip_text("正文行距倍数；不影响代码框")
        spin.connect("value-changed", self._on_line_spacing_changed)
        row.append(spin)
        unit = Gtk.Label(label="×")
        unit.add_css_class("settings-unit")
        row.append(unit)
        return row

    def _on_line_spacing_changed(self, spin) -> None:
        fonts = normalize_fonts(self.fonts)
        fonts["text"]["line_spacing"] = round(spin.get_value(), 2)
        self.apply_fonts(fonts)

    def _font_size_controls(self, row: Gtk.Box, category: str) -> Gtk.Widget:
        current = self.fonts[category]
        size = Gtk.SpinButton.new_with_range(0, 48, 1)
        display_size = int(current.get("size") or
                           self._effective_font_sizes().get(category, 11))
        size.set_value(max(0, min(48, display_size)))
        size.set_width_chars(3)
        size.set_tooltip_text("字号；0 表示跟随默认")
        self._font_spins[category] = size

        def on_size(widget):
            if self._font_sync:
                return
            self._change_font(category, size=int(widget.get_value()))

        size.connect("value-changed", on_size)
        row.append(size)
        unit = Gtk.Label(label="pt")
        unit.add_css_class("settings-unit")
        row.append(unit)
        return row

    def open_settings(self):
        if self._settings_window is not None:
            try:
                if self._settings_window.get_realized():
                    if hasattr(self, "modal_shade"):
                        self.modal_shade.set_visible(True)
                    self._settings_window.present()
                    return
            except Exception:
                pass
            self._settings_window = None
        self._font_spins = {}
        self._theme_dropdown = None
        window = Gtk.Window(transient_for=self, modal=True, title="设置")
        header = Gtk.HeaderBar()
        header.set_show_title_buttons(True)
        window.set_titlebar(header)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                       margin_start=22, margin_end=22, margin_top=16, margin_bottom=20)

        theme_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        theme_section.add_css_class("settings-section")
        theme_title = Gtk.Label(label="主题", xalign=0)
        theme_title.add_css_class("settings-section-title")
        theme_section.append(theme_title)
        theme_dropdown = Gtk.DropDown.new_from_strings(["跟随系统", "浅色", "深色"])
        self._theme_sync = True
        try:
            theme_dropdown.set_selected(THEME_MODES.index(self.theme))
        except ValueError:
            theme_dropdown.set_selected(0)
        self._theme_sync = False
        self._theme_dropdown = theme_dropdown

        def on_theme_changed(widget, _pspec):
            if self._theme_sync:
                return
            index = widget.get_selected()
            if 0 <= index < len(THEME_MODES):
                self.set_theme(THEME_MODES[index])

        theme_dropdown.connect("notify::selected", on_theme_changed)
        theme_section.append(theme_dropdown)
        page.append(theme_section)

        font_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        font_section.add_css_class("settings-section")
        font_title = Gtk.Label(label="字体", xalign=0)
        font_title.add_css_class("settings-section-title")
        font_section.append(font_title)
        for category, label in (
                ("ui", "界面"),
                ("text", "正文"),
                ("mono", "等宽")):
            font_section.append(self._font_row(category, label))
        page.append(font_section)

        other_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        other_section.add_css_class("settings-section")
        other_title = Gtk.Label(label="其它", xalign=0)
        other_title.add_css_class("settings-section-title")
        other_section.append(other_title)
        other_section.append(self._line_spacing_row())
        page.append(other_section)

        about_content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        about_label = Gtk.Label(label="关于 小鲸鱼", xalign=0)
        about_label.set_hexpand(True)
        about_content.append(about_label)
        about_button = Gtk.Button()
        about_button.add_css_class("settings-about")
        about_button.set_child(about_content)
        about_button.connect("clicked", lambda *_: self._about())
        page.append(about_button)

        window.set_child(page)
        self._sync_font_spins()
        min_w, natural_w, _, _ = page.measure(Gtk.Orientation.HORIZONTAL, -1)
        min_h, natural_h, _, _ = page.measure(Gtk.Orientation.VERTICAL, natural_w)
        header_min, header_natural, _, _ = header.measure(Gtk.Orientation.VERTICAL, natural_w)
        width = max(min_w, natural_w)
        height = max(min_h, natural_h) + max(header_min, header_natural)
        window.set_size_request(width, height)
        window.set_default_size(width, height)
        window.connect("close-request", self._on_settings_close)
        window.connect("destroy", self._on_settings_destroyed)
        self._settings_window = window
        if hasattr(self, "modal_shade"):
            self.modal_shade.set_visible(True)
        window.present()
        if os.environ.get("DSD_SETTINGS_SCREENSHOT"):
            path = os.environ["DSD_SETTINGS_SCREENSHOT"]
            GLib.timeout_add(900, lambda: (
                save_widget_png(window, path),
                self.get_application().quit(),
                False,
            )[2])

    def _on_settings_close(self, window):
        if self._settings_window is window:
            self._settings_window = None
            self._theme_dropdown = None
            if hasattr(self, "modal_shade"):
                self.modal_shade.set_visible(False)
        return False

    def _on_settings_destroyed(self, window):
        if self._settings_window is window:
            self._settings_window = None
            self._font_spins = {}
            self._theme_dropdown = None
            if hasattr(self, "modal_shade"):
                self.modal_shade.set_visible(False)

    # -- 侧边栏 -------------------------------------------------------------
    def _clear_chat_list(self):
        while (child := self.chat_list.get_first_child()) is not None:
            self.chat_list.remove(child)
        self._last_group = None

    @staticmethod
    def _chat_group(updated_at) -> str:
        try:
            stamp = updated_at / 1000 if updated_at > 10 ** 12 else updated_at
            date = datetime.fromtimestamp(stamp).date()
        except (ValueError, OSError, OverflowError):
            return "更早"
        today = datetime.now().date()
        if date == today:
            return "今天"
        if (today - date).days == 1:
            return "昨天"
        if (today - date).days < 7:
            return "7 天内"
        return "更早"

    def _add_group_header(self, title: str):
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.set_activatable(False)
        label = Gtk.Label(label=title, xalign=0)
        label.add_css_class("group-header")
        row.set_child(label)
        self.chat_list.append(row)

    def _add_chat_row(self, chat: dict):
        group = self._chat_group(chat.get("updated_at") or 0)
        if group != self._last_group:
            self._add_group_header(group)
            self._last_group = group
        row = Gtk.ListBoxRow()
        row.chat_id = chat.get("id")
        row.chat_title = chat.get("title") or "未命名对话"
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label(label=chat.get("title") or "未命名对话", xalign=0)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.add_css_class("chat-title")
        box.append(title)
        updated = chat.get("updated_at") or 0
        try:
            # API 有的版本返回秒，有的返回毫秒
            seconds = updated / 1000 if updated > 10**12 else updated
            stamp = datetime.fromtimestamp(seconds).strftime("%m-%d %H:%M")
        except (ValueError, OSError):
            stamp = ""
        subtitle = Gtk.Label(label=stamp, xalign=0)
        subtitle.add_css_class("chat-preview")
        box.append(subtitle)
        row.set_child(box)
        self.chat_list.append(row)

    def _populate_demo_chats(self):
        self._clear_chat_list()
        for chat in self.demo_conversations:
            self._add_chat_row(chat)
        index = int(os.environ.get("DSD_DEMO_INDEX", "0") or 0)
        target_id = self.demo_conversations[min(index, len(self.demo_conversations) - 1)]["id"]
        row = self.chat_list.get_first_child()
        while row is not None:
            if getattr(row, "chat_id", None) == target_id:
                self.chat_list.select_row(row)
                self._render_demo(target_id)
                break
            row = row.get_next_sibling()

    def load_chats(self, reset: bool = True):
        if self.demo or self.api is None or self.loading_chats:
            return
        if self.search_mode:
            return
        if reset:
            self.chat_cursor = None
            self.chat_has_more = False
            self.loaded_chat_ids.clear()
            self._clear_chat_list()
        self.loading_chats = True
        self.status_label.set_text("正在加载会话列表…")
        cursor = self.chat_cursor

        def work():
            try:
                chats, has_more, next_cursor = self.api.list_chats_page(30, cursor)
            except Exception as exc:
                GLib.idle_add(self._chat_load_failed, str(exc))
                return
            GLib.idle_add(self._append_chat_page, chats, has_more, next_cursor)

        threading.Thread(target=work, daemon=True).start()

    def _chat_load_failed(self, message):
        self.loading_chats = False
        self.status_label.set_text(f"会话列表失败：{message}")
        return False

    def _append_chat_page(self, chats, has_more, next_cursor):
        self.loading_chats = False
        added = 0
        for chat in chats:
            chat_id = chat.get("id")
            if not chat_id or chat_id in self.loaded_chat_ids:
                continue
            self.loaded_chat_ids.add(chat_id)
            self._add_chat_row(chat)
            added += 1
        self.chat_cursor = next_cursor
        self.chat_has_more = bool(has_more and next_cursor and added)
        if not self.search_mode:
            self.status_label.set_text("")
        if self.chat_has_more:
            GLib.timeout_add(120, self._maybe_autoload)
        return False

    def _on_chat_scroll(self, adjustment):
        if self.search_mode or self.loading_chats or not self.chat_has_more:
            return
        if (adjustment.get_value() + adjustment.get_page_size()
                >= adjustment.get_upper() - 40):
            self.load_chats(reset=False)

    def _maybe_autoload(self):
        if (not self.chat_has_more or self.loading_chats or self.search_mode
                or self.chat_scroll is None):
            return False
        if (self.chat_scroll.get_value() + self.chat_scroll.get_page_size()
                >= self.chat_scroll.get_upper() - 40):
            self.load_chats(reset=False)
        return False

    def _refresh_chats(self):
        if self.search_entry.get_text().strip():
            self.search_entry.set_text("")
        else:
            self.load_chats(reset=True)

    # -- 会话全文搜索 -------------------------------------------------------
    def _on_search_changed(self, entry):
        query = entry.get_text().strip()
        if self.search_source_id:
            GLib.source_remove(self.search_source_id)
            self.search_source_id = 0
        self._last_group = None
        if not query:
            if self.search_mode:
                self.search_mode = False
                self.load_chats(reset=True)
            return
        self.search_mode = True
        self.search_generation += 1
        generation = self.search_generation
        self._clear_chat_list()
        self.search_seen: set[str] = set()
        self.status_label.set_text("搜索中…")
        self.search_source_id = GLib.timeout_add(
            350, self._start_search, query, generation)

    def _start_search(self, query, generation):
        self.search_source_id = 0
        self._last_group = None

        def work():
            count = 0
            try:
                for item in self.api.search_chats(query):
                    if generation != self.search_generation:
                        return
                    if item.get("type") in ("message", "title"):
                        count += 1
                        GLib.idle_add(self._add_search_row, item, generation)
            except Exception as exc:
                GLib.idle_add(self._search_failed, str(exc), generation)
                return
            GLib.idle_add(self._search_done, count, generation)

        threading.Thread(target=work, daemon=True).start()
        return False

    def _search_done(self, count, generation):
        if generation == self.search_generation:
            self.status_label.set_text(f"{count} 条搜索结果")
        return False

    def _search_failed(self, message, generation):
        if generation == self.search_generation:
            self.status_label.set_text(f"搜索失败：{message}")
        return False

    def _add_search_row(self, item, generation):
        if generation != self.search_generation:
            return False
        key = str(item.get("id"))
        if key in self.search_seen:
            return False
        self.search_seen.add(key)
        row = Gtk.ListBoxRow()
        row.session_id = item.get("session_id")
        row.message_id = item.get("message_id")
        row.chat_title = item.get("title") or "未命名对话"
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label(label=item.get("title") or "未命名对话", xalign=0)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.add_css_class("chat-title")
        box.append(title)
        snippet = item.get("snippet") or ""
        if snippet:
            if len(snippet) > 110:
                snippet = snippet[:107] + "…"
            snippet_label = Gtk.Label(label=snippet, xalign=0)
            snippet_label.set_ellipsize(Pango.EllipsizeMode.END)
            snippet_label.add_css_class("chat-preview")
            box.append(snippet_label)
        row.set_child(box)
        self.chat_list.append(row)
        return False

    def _on_chat_activated(self, _listbox, row):
        if row is None:
            return
        session_id = getattr(row, "session_id", None) or getattr(row, "chat_id", None)
        if not session_id:
            return
        if self.demo:
            self._render_demo(session_id)
        else:
            self.open_chat(session_id, getattr(row, "chat_title", None))

    def _render_demo(self, chat_id):
        chat = next((c for c in self.demo_conversations if c["id"] == chat_id), None)
        if chat is None:
            return
        self.chat_id = None
        self.last_message_id = None
        self._clear_messages()
        for message in chat["messages"]:
            self._append_message(message["role"], message["text"],
                                 message.get("reasoning", ""))

    # -- 消息区 -------------------------------------------------------------
    def _clear_messages(self):
        if self._edit_editor is not None:
            self._close_inline_edit(restore=False)
        self._edit_editor = None
        self._edit_view = None
        self._edit_message_id = None
        while (child := self.messages_box.get_first_child()) is not None:
            self.messages_box.remove(child)
        self.message_views.clear()
        self.current_assistant = None
        if hasattr(self, "chat_stack"):
            self.chat_stack.set_visible_child_name("empty")

    def _append_message(self, role, text="", reasoning="", message_id=None,
                        parent_id=None, sources=None) -> MessageView:
        callbacks = {"on_copy": self._copy_message}
        if role == "assistant":
            callbacks["on_share"] = self._share_chat
            callbacks["on_share_image"] = self._share_long_image
        if not self.demo:
            if role == "assistant":
                callbacks["on_regenerate"] = self._regenerate_message
            elif role == "user":
                callbacks["on_edit"] = self._edit_message
        view = MessageView(role, **callbacks)
        view.meta = {"id": message_id, "parent_id": parent_id,
                     "text": text or "", "reasoning": reasoning or ""}
        view.set_content(text, reasoning, sources)
        view.apply_mode(self.mode)
        self.messages_box.append(view)
        self.message_views.append(view)
        if hasattr(self, "chat_stack"):
            self.chat_stack.set_visible_child_name("chat")
        GLib.idle_add(self._scroll_bottom)
        return view

    def _scroll_bottom(self):
        adj = self.messages_scroll.get_vadjustment()
        if adj is not None:
            adj.set_value(adj.get_upper() - adj.get_page_size())
        GLib.idle_add(self._update_message_fades)
        return False

    def _update_message_fades(self):
        try:
            adj = self.messages_scroll.get_vadjustment()
            if adj is None:
                return False
            value = adj.get_value()
            maximum = max(0.0, adj.get_upper() - adj.get_page_size())
            top = min(1.0, max(0.0, value / 28.0))
            bottom = min(1.0, max(0.0, (maximum - value) / 28.0))
            self.messages_fade_top.set_opacity(top)
            self.messages_fade_bottom.set_opacity(bottom)
        except Exception:
            pass
        return False

    def _update_theme_icon(self) -> None:
        icon_name = ("weather-clear-symbolic" if self._theme_dark
                     else "weather-clear-night-symbolic")
        image = Gtk.Image.new_from_icon_name(icon_name)
        image.set_pixel_size(18)
        self.theme_button.set_child(image)
        self.theme_button.set_tooltip_text(
            "切换到浅色主题" if self._theme_dark else "切换到深色主题")

    def _toggle_theme(self, *_args) -> None:
        self.set_theme("light" if self._theme_dark else "dark")

    def _sync_theme_dropdown(self) -> None:
        dropdown = self._theme_dropdown
        if dropdown is None:
            return
        try:
            self._theme_sync = True
            dropdown.set_selected(THEME_MODES.index(self.theme))
        except Exception:
            pass
        finally:
            self._theme_sync = False

    def _on_system_theme_changed(self, *_args) -> None:
        if self.theme == "system":
            self.set_theme("system")

    def set_theme(self, mode: str) -> None:
        if mode not in THEME_MODES:
            return
        self.theme = mode
        self.settings["theme"] = mode
        save_settings(self.settings)
        self._theme_dark = apply_theme_mode(mode)
        self._update_theme_icon()
        self._sync_theme_dropdown()
        if self.streaming:
            self._fonts_dirty = True
            return
        for view in list(self.message_views):
            try:
                view.refresh_content()
            except Exception:
                pass
        GLib.idle_add(self._update_message_fades)

    def new_chat(self):
        self.chat_id = None
        self.last_message_id = None
        self._clear_attachments()
        self._clear_messages()
        self.status_label.set_text("")
        self.chat_list.unselect_all()
        self.input_view.grab_focus()

    def _set_clipboard_text(self, text: str) -> bool:
        try:
            provider = Gdk.ContentProvider.new_for_bytes(
                "text/plain;charset=utf-8", GLib.Bytes.new(text.encode("utf-8")))
            clipboard = self.get_clipboard()
            clipboard.set_content(provider)
            return clipboard.is_local()
        except Exception as exc:
            print(f"复制到剪贴板失败: {exc}", file=sys.stderr, flush=True)
            return False

    def _copy_message(self, view):
        text = view.meta.get("text") or ""
        if not text:
            return
        if self._set_clipboard_text(text):
            self.status_label.set_text("已复制")
            self._flash_copy_success(view)
        else:
            self.status_label.set_text("复制失败")
        return False

    def _flash_copy_success(self, view) -> None:
        button = getattr(view, "copy_button", None)
        if button is None:
            return
        old_source = getattr(view, "_copy_restore_source", 0)
        if old_source:
            try:
                GLib.source_remove(old_source)
            except Exception:
                pass

        image = Gtk.Image.new_from_icon_name("object-select-symbolic")
        image.set_pixel_size(16)
        button.set_child(image)
        button.add_css_class("copied")

        def restore():
            try:
                if button.get_root() is not None:
                    old = Gtk.Image.new_from_icon_name("edit-copy-symbolic")
                    old.set_pixel_size(16)
                    button.set_child(old)
                    button.remove_css_class("copied")
            except Exception:
                pass
            view._copy_restore_source = 0
            return False

        view._copy_restore_source = GLib.timeout_add(1200, restore)

    def _share_long_image(self, _view=None):
        if self._sharing_image:
            return
        if not self.message_views:
            self.status_label.set_text("没有可分享的对话")
            return
        self._sharing_image = True
        self.status_label.set_text("正在排版长图…")
        try:
            messages = []
            for view in self.message_views:
                text = view.meta.get("text") or ""
                clean_text, _refs, _notes, _order = extract_definitions(text)
                messages.append({
                    "role": view.role,
                    "text": clean_text,
                    "blocks": parse_blocks(clean_text),
                })
            base_size = max(1, system_font_size())
            text_pt = effective_text_size()
            if FONT_CONFIG["text"]["size"]:
                body_px = max(26, min(48, round(36 * text_pt / base_size)))
            else:
                body_px = 36
            mono_pt = FONT_CONFIG["mono"]["size"]
            if mono_pt:
                default_mono = max(8, round(text_pt * 0.9))
                code_px = max(20, min(40, round(27 * mono_pt / default_mono)))
            else:
                code_px = max(20, min(40, round(body_px * 0.75)))
            data, error = share_image.render_jpeg_bytes(
                messages,
                self._theme_dark,
                effective_text_family(),
                FONT_CONFIG["mono"].get("family") or "monospace",
                os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "assets", "app-icon.png"),
                ui_font=(FONT_CONFIG["ui"].get("family") or system_font_family()),
                body_size=body_px,
                code_size=code_px,
                line_spacing=FONT_CONFIG["text"].get("line_spacing", 1.55),
            )
            if data is None:
                raise ValueError(error or "长图渲染失败")
            self._sharing_image = False
            self.status_label.set_text("长图已生成")
            self._share_image_data = data
            self._open_long_image_save(data)
        except Exception as exc:
            print(f"生成长图失败: {exc}", file=sys.stderr, flush=True)
            self._sharing_image = False
            self.status_label.set_text("生成长图失败：" + str(exc)[:80])
            self.status_label.set_tooltip_text(str(exc))

    def _open_long_image_save(self, data):
        dialog = Gtk.FileDialog()
        dialog.set_title("保存对话长图")
        dialog.set_initial_name("deepseek-conversation.jpg")

        def save_done(dlg, result):
            try:
                file = dlg.save_finish(result)
            except GLib.Error:
                self.status_label.set_text("已取消保存长图")
                return
            try:
                file.replace_contents(
                    data, None, False,
                    Gio.FileCreateFlags.REPLACE_DESTINATION, None)
                self.status_label.set_text("长图已保存")
            except Exception as exc:
                print(f"保存长图失败: {exc}", file=sys.stderr, flush=True)
                self.status_label.set_text("保存长图失败：" + str(exc)[:80])
                self.status_label.set_tooltip_text(str(exc))

        try:
            dialog.save(self, None, save_done)
        except Exception as exc:
            self.status_label.set_text("保存长图失败：" + str(exc)[:80])


    def _share_chat(self, _view=None):
        if self._sharing:
            return
        if self.demo:
            self.status_label.set_text("演示模式不支持分享")
            return
        if self.streaming:
            self.status_label.set_text("请等待回答完成后再分享")
            return
        if not self.chat_id or self.api is None:
            self.status_label.set_text("还没有可分享的会话")
            return
        self._sharing = True
        self.status_label.set_text("正在创建分享链接…")
        chat_id = self.chat_id

        def work():
            try:
                url = self.api.share_chat(chat_id)
            except Exception as exc:
                GLib.idle_add(self._share_failed, str(exc))
            else:
                GLib.idle_add(self._share_ready, url)

        threading.Thread(target=work, daemon=True).start()

    def _share_ready(self, url: str):
        self._sharing = False
        if self._set_clipboard_text(url):
            self.status_label.set_text("分享链接已复制")
        else:
            self.status_label.set_text("分享链接已创建，但复制失败")
        self.status_label.set_tooltip_text(url)
        return False

    def _share_failed(self, message: str):
        self._sharing = False
        print(f"分享失败: {message}", file=sys.stderr, flush=True)
        self.status_label.set_text("分享失败：" + message[:80])
        self.status_label.set_tooltip_text(message)
        return False

    def _relogin(self):
        clear_session()
        if self.api:
            self.api.session.token = ""
        app = self.get_application()
        if hasattr(app, "show_login"):
            app.show_login()

    def _fetch_user_info(self):
        try:
            user = self.api.current_user()
        except Exception:
            if not self.profile_row.get_visible():
                GLib.idle_add(self.profile_row.set_visible, False)
            return
        if not user:
            if not self.profile_row.get_visible():
                GLib.idle_add(self.profile_row.set_visible, False)
            return
        GLib.idle_add(self._apply_user_info, user)

    def _apply_user_info(self, user):
        profile = user.get("id_profile") or {}
        email = user.get("email") or profile.get("email") or ""
        mobile = (user.get("mobile") or user.get("mobile_number") or "")
        name = profile.get("name") or user.get("name") or ""
        if not (email or mobile or name):
            self.profile_row.set_visible(False)
            return False
        primary = name or email or mobile
        sub = email or mobile or "DeepSeek 用户"
        self.profile_name.set_text(primary)
        self.profile_name.set_tooltip_text(primary)
        self.profile_sub.set_text(sub)
        self.profile_sub.set_tooltip_text(sub)
        self.profile_row.set_visible(True)
        if self.api is not None:
            self.api.session.user = user
            save_session(self.api.session)
        return False

    def _doctor(self):
        if self.api is None:
            self.status_label.set_text("演示模式没有网络连接")
            return

        def work():
            try:
                count = self.api.check()
                message = f"连接正常，可见会话 {count} 个"
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
            GLib.idle_add(self._show_doctor, message)

        threading.Thread(target=work, daemon=True).start()

    def _show_doctor(self, message):
        dialog = Gtk.Window(transient_for=self, modal=False, title="诊断")
        dialog.set_default_size(520, 180)
        header = Gtk.HeaderBar()
        header.set_show_title_buttons(True)
        dialog.set_titlebar(header)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                      margin_start=18, margin_end=18, margin_top=16, margin_bottom=16)
        label = Gtk.Label(label=message, xalign=0, wrap=True, selectable=True)
        box.append(label)
        detail = Gtk.Label(
            label=f"token: {len(self.api.session.token)} 字符 · cookie: {len(self.api.session.cookies)} 项",
            xalign=0, wrap=True)
        detail.add_css_class("chat-preview")
        box.append(detail)
        close = Gtk.Button(label="关闭")
        close.set_halign(Gtk.Align.END)
        close.connect("clicked", lambda *_: dialog.close())
        box.append(close)
        dialog.set_child(box)
        dialog.present()
        return False

    def _about(self):
        parent = self._settings_window or self
        dialog = Gtk.Window(transient_for=parent, modal=True,
                            title="关于 小鲸鱼")
        dialog.set_resizable(False)
        header = Gtk.HeaderBar()
        header.set_show_title_buttons(True)
        header.set_title_widget(Gtk.Label(label="关于 小鲸鱼"))
        dialog.set_titlebar(header)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                      margin_start=32, margin_end=32,
                      margin_top=22, margin_bottom=28,
                      halign=Gtk.Align.CENTER)
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file(os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "assets", "app-icon.png"))
            pixbuf = pixbuf.scale_simple(120, 120, GdkPixbuf.InterpType.BILINEAR)
            icon = Gtk.Image.new_from_pixbuf(pixbuf)
            icon.set_pixel_size(120)
            icon.set_size_request(120, 120)
            box.append(icon)
        except Exception:
            pass

        name = Gtk.Label(label="小鲸鱼", xalign=0.5)
        name.add_css_class("about-name")
        name.set_selectable(False)
        box.append(name)

        english = Gtk.Label(label="TinyWhale", xalign=0.5)
        english.add_css_class("about-english")
        english.set_selectable(False)
        box.append(english)

        version = Gtk.Label(label=f"v{APP_VERSION}", xalign=0.5)
        version.add_css_class("about-version")
        version.set_halign(Gtk.Align.CENTER)
        version.set_selectable(False)
        box.append(version)

        comment = Gtk.Label(
            label="原生GTK4 控件的 DeepSeek 客户端",
            xalign=0.5, justify=Gtk.Justification.CENTER, wrap=True)
        comment.add_css_class("about-comment")
        comment.set_max_width_chars(30)
        comment.set_selectable(False)
        box.append(comment)

        dialog.set_child(box)
        dialog.present()

    # -- 输入 ---------------------------------------------------------------
    def _on_key(self, _controller, keyval, _keycode, state):
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if not (state & Gdk.ModifierType.SHIFT_MASK):
                self.send_current()
                return True
        if (keyval in (Gdk.KEY_v, Gdk.KEY_V)
                and (state & Gdk.ModifierType.CONTROL_MASK)
                and not (state & Gdk.ModifierType.SHIFT_MASK)):
            if self._try_paste_attachment():
                return True
        return False

    # -- 附件 ---------------------------------------------------------------
    def _choose_files(self):
        if self.demo:
            self.status_label.set_text("演示模式不支持上传")
            return
        dialog = Gtk.FileDialog()
        dialog.set_title("选择文件")
        dialog.open_multiple(self, None, self._on_files_chosen)

    def _on_files_chosen(self, dialog, result):
        try:
            files = dialog.open_multiple_finish(result)
        except GLib.Error:
            return
        paths = []
        for index in range(files.get_n_items()):
            item = files.get_item(index)
            path = item.get_path() if hasattr(item, "get_path") else None
            if path:
                paths.append(path)
        if paths:
            self._attach_paths(paths)

    def _attach_paths(self, paths):
        if self.demo or self.api is None:
            self.status_label.set_text("演示模式不支持上传")
            return
        for path in paths:
            if len(self.attachments) >= 50:
                self.status_label.set_text("最多 50 个文件")
                break
            if not os.path.isfile(path):
                continue
            if any(a["path"] == path for a in self.attachments):
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if size > 100 * 1024 * 1024:
                self.status_label.set_text(f"{os.path.basename(path)} 超过 100MB")
                continue
            att = {
                "path": path,
                "name": os.path.basename(path),
                "size": size,
                "id": None,
                "status": "uploading",
                "error": "",
            }
            self.attachments.append(att)
            self._refresh_attachments()
            thinking = self.thinking_toggle.get_active()
            threading.Thread(target=self._upload_attachment,
                             args=(att, DEFAULT_MODEL_TYPE, thinking),
                             daemon=True).start()

    def _upload_attachment(self, att, model="default", thinking=False):
        try:
            fileobj = self.api.upload_file(att["path"], model_type=model,
                                           thinking=thinking)
            att["id"] = str(fileobj.get("id") or "")
            if fileobj.get("file_name"):
                att["name"] = fileobj["file_name"]
            if not att["id"]:
                raise ApiError("上传响应里没有 file id")
            att["status"] = "parsing"
            GLib.idle_add(self._refresh_attachments)
            ready = self.api.wait_file_ready(att["id"])
            att["status"] = "ready"
            att["server"] = ready
        except Exception as exc:
            att["status"] = "error"
            att["error"] = str(exc)
        GLib.idle_add(self._refresh_attachments)

    def _refresh_attachments(self):
        while (child := self.attachments_box.get_first_child()) is not None:
            self.attachments_box.remove(child)
        self.attachments_box.set_visible(bool(self.attachments))
        for att in self.attachments:
            chip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            chip.add_css_class("attachment-chip")
            icon = Gtk.Label(label="📎")
            chip.append(icon)
            name = Gtk.Label(label=att["name"], xalign=0)
            name.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            name.set_max_width_chars(24)
            name.add_css_class("attachment-name")
            chip.append(name)
            size = att["size"]
            if size < 1024:
                meta = f"{size} B"
            elif size < 1024 * 1024:
                meta = f"{size / 1024:.0f} KB"
            else:
                meta = f"{size / 1024 / 1024:.1f} MB"
            meta_label = Gtk.Label(label=meta)
            meta_label.add_css_class("chat-preview")
            chip.append(meta_label)
            if att["status"] in ("uploading", "parsing"):
                spinner = Gtk.Spinner()
                spinner.start()
                chip.append(spinner)
                state = Gtk.Label(label="上传中" if att["status"] == "uploading" else "解析中")
                state.add_css_class("chat-preview")
                chip.append(state)
            elif att["status"] == "error":
                state = Gtk.Label(label="失败")
                state.add_css_class("attachment-error")
                state.set_tooltip_text(att["error"])
                chip.append(state)
            else:
                chip.append(Gtk.Label(label="✓"))
            remove = Gtk.Button(label="×")
            remove.set_tooltip_text("移除")
            remove.add_css_class("flat")
            remove.connect("clicked", lambda _b, a=att: self._remove_attachment(a))
            chip.append(remove)
            self.attachments_box.append(chip)
        self._update_send_sensitive()
        return False

    def _remove_attachment(self, att):
        if att in self.attachments:
            self.attachments.remove(att)
        self._refresh_attachments()

    def _clear_attachments(self):
        self.attachments.clear()
        self._refresh_attachments()

    def _try_paste_attachment(self):
        if self.demo or self.api is None:
            return False
        try:
            clipboard = self.input_view.get_clipboard()
            formats = clipboard.get_formats()
            if not any(formats.contain_mime_type(mime) for mime in
                       ("image/png", "image/jpeg", "image/webp", "image/gif")):
                return False
            clipboard.read_texture_async(None, self._on_paste_texture, None)
            return True
        except Exception:
            return False

    def _on_paste_texture(self, clipboard, result, _data):
        try:
            texture = clipboard.read_texture_finish(result)
        except Exception:
            return
        fd, path = tempfile.mkstemp(prefix="deepseek-paste-", suffix=".png")
        os.close(fd)
        try:
            texture.save_to_png(path)
        except Exception:
            return
        self._attach_paths([path])

    def _auto_attach(self):
        paths = [p for p in (os.environ.get("DSD_AUTO_ATTACH") or "").split(",") if p]
        if paths:
            self._attach_paths(paths)
        return False

    def send_current(self):
        if self.streaming:
            return
        if any(a["status"] in ("uploading", "parsing") for a in self.attachments):
            self.status_label.set_text("文件还在上传/解析，请稍候")
            return
        if any(a["status"] == "error" for a in self.attachments):
            self.status_label.set_text("有文件上传失败，请先移除")
            return
        buf = self.input_view.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True).strip()
        file_ids = [a["id"] for a in self.attachments
                    if a["status"] == "ready" and a.get("id")]
        file_names = [a["name"] for a in self.attachments
                      if a["status"] == "ready"]
        if not text and not file_ids:
            return
        buf.set_text("")
        display = text
        if file_names:
            display = (display + "\n\n📎 " + "、".join(file_names)).strip()
        self._append_message("user", display)
        self._clear_attachments()
        self.current_assistant = self._append_message("assistant", "")
        self.stream_text = ""
        self.stream_reasoning = ""
        self.stream_sources = []
        self.streaming = True
        self.stream_error = False
        self.send_button.set_visible(False)
        self.stop_button.set_visible(True)
        self.cancel_event.clear()

        if self.demo:
            self._demo_stream(text)
        else:
            self._live_stream(text, self.current_assistant, file_ids)

    def _demo_stream(self, prompt):
        reply = ("这是演示模式下的本地回复，没有联网。\n\n"
                 "切到「文档」或「极简」看看同一段内容的排版变化；"
                 "真正接入账号后，增量会从 DeepSeek 的 SSE 流里来。\n\n"
                 "- 气泡模式适合聊天\n"
                 "- 文档模式适合长回答\n"
                 "- 极简模式只留文字")
        state = {"text": reply}

        def tick():
            if self.cancel_event.is_set() or not state["text"]:
                self._finish_stream()
                return False
            chunk = state["text"][:5]
            state["text"] = state["text"][5:]
            self.stream_text += chunk
            self._queue_render()
            if not state["text"]:
                self._finish_stream()
                return False
            return True

        GLib.timeout_add(28, tick)

    def _live_stream(self, prompt, assistant_view, file_ids=None):
        cancel = self.cancel_event
        thinking = self.thinking_toggle.get_active()
        search = self.search_toggle.get_active()
        chat_id = self.chat_id
        parent = self.last_message_id

        def work():
            try:
                if not chat_id:
                    sid = self.api.create_chat_session()
                    GLib.idle_add(self._set_chat_id, sid)
                else:
                    sid = chat_id
            except AuthError as exc:
                GLib.idle_add(self._on_stream_error, str(exc), True)
                GLib.idle_add(self._finish_stream)
                return
            except Exception as exc:
                GLib.idle_add(self._on_stream_error, str(exc), False)
                GLib.idle_add(self._finish_stream)
                return
            self._consume_stream(
                lambda: self.api.stream_completion(
                    prompt, sid, parent,
                    model_type=DEFAULT_MODEL_TYPE if not chat_id else None,
                    thinking=thinking, search=search,
                    file_ids=file_ids, cancel=cancel),
                assistant_view,
            )

        threading.Thread(target=work, daemon=True).start()

    def _consume_stream(self, factory, assistant_view):
        try:
            for kind, text in factory():
                if kind == "__meta__":
                    try:
                        meta = json.loads(text)
                    except ValueError:
                        continue
                    GLib.idle_add(self._on_meta, meta)
                elif kind == "THINK":
                    GLib.idle_add(self._on_delta, text, True)
                else:
                    GLib.idle_add(self._on_delta, text, False)
        except AuthError as exc:
            GLib.idle_add(self._on_stream_error, str(exc), True)
        except Exception as exc:
            GLib.idle_add(self._on_stream_error, str(exc), False)
        finally:
            GLib.idle_add(self._finish_stream)

    def _on_meta(self, meta):
        if meta.get("search_results"):
            self.stream_sources = meta["search_results"]
        message_id = meta.get("message_id")
        parent_id = meta.get("parent_id")
        if isinstance(message_id, int):
            self.last_message_id = message_id
            if self.current_assistant is not None:
                self.current_assistant.meta["id"] = message_id
        if isinstance(parent_id, int):
            if self.current_assistant is not None:
                self.current_assistant.meta["parent_id"] = parent_id
            for view in reversed(self.message_views):
                if view.role == "user" and view.meta.get("id") is None:
                    view.meta["id"] = parent_id
                    break
        return False

    def _begin_stream(self, assistant_view):
        self.current_assistant = assistant_view
        self.stream_text = ""
        self.stream_reasoning = ""
        self.stream_sources = []
        self.streaming = True
        self.stream_error = False
        self.send_button.set_visible(False)
        self.stop_button.set_visible(True)
        self.cancel_event.clear()

    def _remove_view(self, view):
        if view in self.message_views:
            self.message_views.remove(view)
        try:
            self.messages_box.remove(view)
        except Exception:
            pass

    def _regenerate_message(self, view):
        if self.demo or self.streaming:
            return
        if not self.chat_id:
            self.status_label.set_text("还没有会话，无法重新生成")
            return
        if not self.message_views or view is not self.message_views[-1]:
            self.status_label.set_text("只能重新生成最后一条回答")
            return
        message_id = view.meta.get("id")
        if not isinstance(message_id, int):
            self.status_label.set_text("缺少消息 ID，请重新打开会话后再试")
            return
        thinking = self.thinking_toggle.get_active()
        search = self.search_toggle.get_active()
        self._remove_view(view)
        assistant = self._append_message("assistant", "")
        self._begin_stream(assistant)
        chat_id = self.chat_id

        def factory():
            return self.api.regenerate(chat_id, message_id, thinking=thinking,
                                       search=search, cancel=self.cancel_event)

        threading.Thread(target=lambda: self._consume_stream(factory, assistant),
                         daemon=True).start()

    def _edit_message(self, view):
        if self.demo or self.streaming:
            return
        if not self.chat_id:
            self.status_label.set_text("还没有会话，无法编辑")
            return
        if view not in self.message_views:
            return
        index = self.message_views.index(view)
        trailing = self.message_views[index + 1:]
        if trailing and not (len(trailing) == 1 and trailing[0].role == "assistant"):
            self.status_label.set_text("只能编辑最后一条提问")
            return
        message_id = view.meta.get("id")
        if not isinstance(message_id, int):
            self.status_label.set_text("缺少消息 ID，请重新打开会话后再试")
            return
        self._open_inline_edit(view, message_id)

    def _close_inline_edit(self, restore: bool = True):
        editor = self._edit_editor
        view = self._edit_view
        if editor is not None:
            parent = editor.get_parent()
            if parent is not None:
                parent.remove(editor)
        if restore and view is not None:
            try:
                view.set_visible(True)
            except Exception:
                pass
        self._edit_editor = None
        self._edit_view = None
        self._edit_message_id = None
        try:
            self.input_view.set_sensitive(True)
            self.attach_button.set_sensitive(not self.demo)
            self._update_send_sensitive()
        except Exception:
            pass

    def _open_inline_edit(self, view, message_id):
        self._close_inline_edit()
        self._edit_view = view
        self._edit_message_id = message_id

        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        frame.add_css_class("edit-frame")
        frame.set_valign(Gtk.Align.START)
        frame.set_vexpand(False)
        frame.set_size_request(-1, 76)

        textview = Gtk.TextView()
        textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        textview.set_top_margin(0)
        textview.set_bottom_margin(0)
        textview.set_left_margin(2)
        textview.set_right_margin(2)
        textview.set_size_request(-1, 30)
        buf = textview.get_buffer()
        buf.set_text(view.meta.get("text") or "")
        buf.place_cursor(buf.get_end_iter())

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_child(textview)
        scrolled.set_propagate_natural_height(False)
        scrolled.set_vexpand(False)
        scrolled.set_size_request(-1, 30)
        scrolled.set_min_content_height(30)
        scrolled.set_max_content_height(80)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        frame.append(scrolled)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        controls.append(spacer)

        cancel = Gtk.Button(label="取消")
        cancel.add_css_class("edit-cancel")
        cancel.connect("clicked", lambda *_: self._close_inline_edit())
        controls.append(cancel)

        send = Gtk.Button(label="发送")
        send.add_css_class("edit-send")
        send.set_sensitive(bool((view.meta.get("text") or "").strip()))
        controls.append(send)
        frame.append(controls)

        def update_send():
            text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True).strip()
            send.set_sensitive(bool(text))
            return False

        def do_send(_widget=None):
            text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True).strip()
            if not text:
                return
            edit_view = self._edit_view
            edit_id = self._edit_message_id
            self._close_inline_edit(restore=False)
            self._apply_edit(edit_view, edit_id, text)

        send.connect("clicked", do_send)
        buf.connect("changed", lambda _b: update_send())

        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_edit_key, buf, do_send)
        textview.add_controller(key)
        frame.connect("map", lambda *_: textview.grab_focus())

        self._edit_editor = frame
        view.set_visible(False)
        self.messages_box.insert_child_after(frame, view)
        self.input_view.set_sensitive(False)
        self.attach_button.set_sensitive(False)
        self.send_button.set_sensitive(False)
        textview.grab_focus()

    def _on_edit_key(self, _controller, keyval, _keycode, state, buf, do_send):
        if keyval == Gdk.KEY_Escape:
            self._close_inline_edit()
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if state & Gdk.ModifierType.SHIFT_MASK:
                return False
            do_send()
            return True
        return False

    def _apply_edit(self, view, message_id, text):
        index = self.message_views.index(view)
        for trailing in self.message_views[index + 1:]:
            self._remove_view(trailing)
        self._remove_view(view)
        self._append_message("user", text)
        assistant = self._append_message("assistant", "")
        self._begin_stream(assistant)
        thinking = self.thinking_toggle.get_active()
        search = self.search_toggle.get_active()
        chat_id = self.chat_id

        def factory():
            return self.api.edit_message(chat_id, message_id, text,
                                         thinking=thinking, search=search,
                                         cancel=self.cancel_event)

        threading.Thread(target=lambda: self._consume_stream(factory, assistant),
                         daemon=True).start()

    def _set_chat_id(self, sid):
        self.chat_id = sid
        return False

    def _set_last_message_id(self, mid):
        self.last_message_id = mid
        return False

    def _on_delta(self, text, is_think):
        if is_think:
            self.stream_reasoning += text
        else:
            self.stream_text += text
        self._queue_render()
        return False

    def _queue_render(self):
        if self.render_scheduled:
            return
        self.render_scheduled = True
        GLib.timeout_add(80, self._flush_render)

    def _flush_render(self):
        self.render_scheduled = False
        if self.current_assistant is not None:
            self.current_assistant.set_content(self.stream_text, self.stream_reasoning,
                                                self.stream_sources)
            self.current_assistant.meta["text"] = self.stream_text
            self.current_assistant.meta["reasoning"] = self.stream_reasoning
            self._scroll_bottom()
        return False

    def _on_stream_error(self, message, auth):
        self.stream_error = True
        self.status_label.set_text(("登录失效：" if auth else "请求失败：") + message)
        if auth:
            self._relogin()
        return False

    def _finish_stream(self):
        if not self.streaming:
            return False
        self.streaming = False
        self.send_button.set_visible(True)
        self.stop_button.set_visible(False)
        self._update_send_sensitive()
        self._flush_render()
        if self._fonts_dirty:
            self._fonts_dirty = False
            for view in list(self.message_views):
                try:
                    view.refresh_content()
                except Exception:
                    pass
        if not self.stream_error:
            self.status_label.set_text("完成")
        if (self.chat_id and not self.demo
                and any(v.role == "user" and not isinstance(v.meta.get("id"), int)
                        for v in self.message_views)):
            threading.Thread(target=self._reload_ids, daemon=True).start()
        if os.environ.get("DSD_AUTO_REGENERATE") and not self._test_regenerated:
            self._test_regenerated = True
            GLib.timeout_add(700, self._auto_regenerate)
        if os.environ.get("DSD_AUTO_EDIT_TEXT") and not self._test_edited:
            self._test_edited = True
            GLib.timeout_add(700, self._auto_edit)
        return False

    def _reload_ids(self):
        try:
            messages = self.api.history(self.chat_id)
        except Exception:
            return
        GLib.idle_add(self._apply_history_ids, messages)

    def _apply_history_ids(self, messages):
        if not messages or not self.message_views:
            return False
        tail = messages[-len(self.message_views):]
        if len(tail) != len(self.message_views):
            return False
        for view, message in zip(self.message_views, tail):
            if view.role != message["role"]:
                continue
            if isinstance(message.get("id"), int):
                view.meta["id"] = message["id"]
            if isinstance(message.get("parent_id"), int):
                view.meta["parent_id"] = message["parent_id"]
        return False

    def _auto_regenerate(self):
        if self.message_views and self.message_views[-1].role == "assistant":
            self._regenerate_message(self.message_views[-1])
        return False

    def _auto_edit(self):
        text = os.environ.get("DSD_AUTO_EDIT_TEXT") or ""
        for view in reversed(self.message_views):
            if view.role == "user" and isinstance(view.meta.get("id"), int):
                self._apply_edit(view, view.meta["id"], text)
                break
        return False

    def stop_stream(self):
        self.cancel_event.set()
        self.status_label.set_text("正在停止…")

    # -- 历史 ---------------------------------------------------------------
    def open_chat(self, chat_id: str, title: str | None = None):
        self.chat_id = chat_id
        self.status_label.set_text("正在加载历史…")

        def work():
            try:
                messages = self.api.history(chat_id)
            except Exception as exc:
                GLib.idle_add(self.status_label.set_text, f"历史加载失败：{exc}")
                return
            GLib.idle_add(self._render_history, messages)

        threading.Thread(target=work, daemon=True).start()

    def _render_history(self, messages):
        self._clear_messages()
        self.last_message_id = None
        for message in messages:
            self._append_message(message["role"], message.get("text", ""),
                                 message.get("reasoning", ""),
                                 message_id=message.get("id"),
                                 parent_id=message.get("parent_id"),
                                 sources=message.get("sources"))
            if message["role"] == "assistant" and isinstance(message.get("id"), int):
                self.last_message_id = message["id"]
        self.status_label.set_text(f"{len(messages)} 条消息")

    def _auto_send(self):
        if any(a["status"] in ("uploading", "parsing") for a in self.attachments):
            GLib.timeout_add(400, self._auto_send)
            return False
        prompt = os.environ.get("DSD_AUTO_SEND") or ""
        if prompt:
            self.input_view.get_buffer().set_text(prompt)
            self.send_current()
        return False

    # -- 截图（调试） -------------------------------------------------------
    def _screenshot(self):
        path = os.environ.get("DSD_SCREENSHOT")
        if path:
            try:
                from gi.repository import Gdk, Gsk
                paintable = Gtk.WidgetPaintable.new(self)
                snapshot = Gtk.Snapshot.new()
                paintable.snapshot(snapshot, self.get_width(), self.get_height())
                texture = self.get_renderer().render_texture(snapshot.to_node(), None)
                texture.save_to_png(path)
                print(f"SCREENSHOT {path}", flush=True)
            except Exception as exc:
                print(f"SCREENSHOT_ERROR {exc}", flush=True)
        self.get_application().quit()
        return False


# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------

class DeepSeekApp(Gtk.Application):
    def __init__(self, demo: bool = False, token: str | None = None,
                 api_base: str = BASE_URL, pow_mode: str = "js"):
        app_id = os.environ.get("DSD_APP_ID") or "io.github.deepseeknative.App"
        super().__init__(application_id=app_id,
                         flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.demo = demo
        self.cli_token = token
        self.api_base = api_base
        self.pow_mode = pow_mode
        self.window = None
        self.pow_bridge = None
        self.api = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        action = Gio.SimpleAction.new("quit", None)
        action.connect("activate", lambda *_: self.quit())
        self.add_action(action)

        saved_settings = load_settings()
        apply_theme_mode(saved_settings.get("theme", "system"))
        provider = Gtk.CssProvider()
        try:
            provider.load_from_string(APP_CSS)
        except Exception:
            provider.load_from_data(APP_CSS.encode("utf-8"), -1)
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            try:
                icon_theme = Gtk.IconTheme.get_for_display(display)
                icon_theme.add_search_path(os.path.join(
                    os.path.dirname(os.path.abspath(__file__)), "assets", "icons"))
                Gtk.Window.set_default_icon_name("io.github.deepseeknative.App")
            except Exception:
                pass
        fonts = normalize_fonts(saved_settings.get("fonts"))
        set_font_config(fonts)
        install_font_css(build_font_css(fonts))

    def do_activate(self):
        if self.window is not None:
            self.window.present()
            return
        ensure_dirs()
        if self.demo:
            self._start_main(None, None, demo=True)
            return
        if self.cli_token:
            session = Session(token=self.cli_token)
            self._start_main(session, self._make_api(session))
            return
        session = load_session()
        if session and session.token:
            self._start_main(session, self._make_api(session))
        else:
            self.show_login()

    def _make_api(self, session: Session) -> DeepSeekAPI:
        if self.pow_mode == "python":
            def solver(challenge, target_path):
                prefix = f"{challenge['salt']}_{challenge['expire_at']}_"
                answer = solve_pow_python(challenge["challenge"], prefix,
                                          int(challenge["difficulty"]))
                if answer is None:
                    raise ApiError("Python PoW 未找到答案")
                return pow_header(challenge, answer, target_path)
            return DeepSeekAPI(session, pow_solver=solver, base=self.api_base)
        if self.pow_bridge is None:
            self.pow_bridge = PowBridge()
        return DeepSeekAPI(session, pow_solver=self.pow_bridge.solve, base=self.api_base)

    def _start_main(self, session, api, demo: bool = False):
        window = MainWindow(self, api, session, demo=demo)
        window.present()
        self.window = window

    def show_login(self):
        login = LoginWindow(self, self._on_login)
        login.present()
        if self.window is not None and self.window is not login:
            self.window.close()
        self.window = login

    def _on_login(self, session: Session):
        self._start_main(session, self._make_api(session))
        if self.window is not None and not isinstance(self.window, MainWindow):
            self.window.close()
        # LoginWindow 自己会在回调后关闭；这里只保证主窗口已经建好


def doctor(api_base: str) -> int:
    ensure_dirs()
    print("config:", CONFIG_DIR)
    print("data:  ", DATA_DIR)
    session = load_session()
    if not session or not session.token:
        print("session: 未登录（先运行 ./run.sh 登录）")
        return 1
    print("token:  %d 字符" % len(session.token))
    print("cookie: %s" % (", ".join(session.cookies) or "(空)"))
    api = DeepSeekAPI(session, base=api_base)
    try:
        count = api.check()
        print("check:  OK，可见会话 %d 个" % count)
        return 0
    except AuthError as exc:
        print("check:  登录失效：%s" % exc)
        return 2
    except Exception as exc:
        print("check:  失败 %s: %s" % (type(exc).__name__, exc))
        return 3


def main(argv=None) -> int:
    argv = list(sys.argv if argv is None else argv)
    if "--doctor" in argv:
        base = BASE_URL
        for index, arg in enumerate(argv):
            if arg == "--api-base" and index + 1 < len(argv):
                base = argv[index + 1]
            elif arg.startswith("--api-base="):
                base = arg.split("=", 1)[1]
        return doctor(base)
    demo = "--demo" in argv
    token = None
    api_base = BASE_URL
    pow_mode = "js"
    for index, arg in enumerate(argv):
        if arg == "--token" and index + 1 < len(argv):
            token = argv[index + 1]
        elif arg.startswith("--token="):
            token = arg.split("=", 1)[1]
        elif arg == "--api-base" and index + 1 < len(argv):
            api_base = argv[index + 1]
        elif arg.startswith("--api-base="):
            api_base = arg.split("=", 1)[1]
        elif arg == "--pow" and index + 1 < len(argv):
            pow_mode = argv[index + 1]
        elif arg.startswith("--pow="):
            pow_mode = arg.split("=", 1)[1]

    ensure_dirs()
    app = DeepSeekApp(demo=demo, token=token, api_base=api_base, pow_mode=pow_mode)
    return app.run([argv[0]])


if __name__ == "__main__":
    sys.exit(main())
