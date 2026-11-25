"""Meta message templates: what they need filled, what they look like, and the
exact `components` payload a send carries.

A template is stored as Meta returns it (`components`, `parameter_format`), so
this module is the one place that knows Meta's shape. Everything else talks in
SLOT KEYS:

    header.media        the document/image/video in a media header
    header.<var>        a variable inside a TEXT header
    body.<var>          a body variable — a name ({{greeting}}) or a number ({{1}})
    button.<index>      the variable suffix of a dynamic URL button

A playbook maps each slot key to a placeholder expression; a send stores the
rendered value per slot key; build_components() turns that into Meta's JSON.
"""

from __future__ import annotations

import json
import re
from typing import Any

_VAR_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
MEDIA_FORMATS = ("DOCUMENT", "IMAGE", "VIDEO")


def _components(tpl: dict) -> list[dict]:
    comps = tpl.get("components")
    if isinstance(comps, str):
        try:
            comps = json.loads(comps or "[]")
        except ValueError:
            comps = []
    return comps if isinstance(comps, list) else []


def _vars(text: str) -> list[str]:
    seen: list[str] = []
    for m in _VAR_RE.finditer(text or ""):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def _examples(comp: dict) -> dict[str, str]:
    """Meta's example values per variable, used as the slot's hint."""
    ex = comp.get("example") or {}
    out: dict[str, str] = {}
    for item in ex.get("body_text_named_params") or ex.get("header_text_named_params") or []:
        if isinstance(item, dict) and item.get("param_name"):
            out[str(item["param_name"])] = str(item.get("example") or "")
    for key in ("body_text", "header_text"):
        vals = ex.get(key)
        if vals and isinstance(vals, list):
            row = vals[0] if isinstance(vals[0], list) else vals
            for i, v in enumerate(row, start=1):
                out.setdefault(str(i), str(v))
    return out


def slots(tpl: dict) -> list[dict[str, Any]]:
    """Everything a send must supply for this template, in display order."""
    out: list[dict[str, Any]] = []
    for comp in _components(tpl):
        ctype = (comp.get("type") or "").upper()
        if ctype == "HEADER":
            fmt = (comp.get("format") or "TEXT").upper()
            if fmt in MEDIA_FORMATS:
                out.append({"key": "header.media", "kind": fmt.lower(),
                            "label": f"Header {fmt.lower()}"})
            elif fmt == "TEXT":
                ex = _examples(comp)
                for v in _vars(comp.get("text") or ""):
                    out.append({"key": f"header.{v}", "kind": "text",
                                "label": f"Header {{{{{v}}}}}", "example": ex.get(v, "")})
        elif ctype == "BODY":
            ex = _examples(comp)
            for v in _vars(comp.get("text") or ""):
                out.append({"key": f"body.{v}", "kind": "text",
                            "label": f"{{{{{v}}}}}", "example": ex.get(v, "")})
        elif ctype == "BUTTONS":
            for i, b in enumerate(comp.get("buttons") or []):
                if (b.get("type") or "").upper() == "URL" and _vars(b.get("url") or ""):
                    out.append({"key": f"button.{i}", "kind": "url_suffix",
                                "label": f"Button “{b.get('text') or 'link'}” link",
                                "base_url": _VAR_RE.sub("", b.get("url") or ""),
                                "example": ""})
    return out


def text_slot_keys(tpl: dict) -> list[str]:
    return [s["key"] for s in slots(tpl) if s["kind"] in ("text", "url_suffix")]


def media_format(tpl: dict) -> str:
    """'document' / 'image' / 'video' when the header carries media, else ''."""
    for s in slots(tpl):
        if s["key"] == "header.media":
            return s["kind"]
    return ""


def link_button_is_ours(tpl: dict, public_base: str) -> dict[str, bool]:
    """button.<i> → True when that button's base URL is our /r/ redirector,
    i.e. its variable is a tracked-link TOKEN rather than a free suffix."""
    out = {}
    for s in slots(tpl):
        if s["kind"] == "url_suffix":
            out[s["key"]] = s.get("base_url", "").rstrip("/").endswith(
                public_base.rstrip("/") + "/r")
    return out


def preview(tpl: dict, params: dict[str, str]) -> dict[str, Any]:
    """The message as the customer will see it (text parts only)."""
    def fill(text: str, prefix: str) -> str:
        return _VAR_RE.sub(
            lambda m: params.get(f"{prefix}.{m.group(1)}") or f"{{{{{m.group(1)}}}}}",
            text or "")

    out: dict[str, Any] = {"header": None, "header_media": None, "body": "",
                           "footer": "", "buttons": []}
    for comp in _components(tpl):
        ctype = (comp.get("type") or "").upper()
        if ctype == "HEADER":
            fmt = (comp.get("format") or "TEXT").upper()
            if fmt in MEDIA_FORMATS:
                out["header_media"] = fmt.lower()
            elif fmt == "TEXT":
                out["header"] = fill(comp.get("text") or "", "header")
        elif ctype == "BODY":
            out["body"] = fill(comp.get("text") or "", "body")
        elif ctype == "FOOTER":
            out["footer"] = comp.get("text") or ""
        elif ctype == "BUTTONS":
            for i, b in enumerate(comp.get("buttons") or []):
                btype = (b.get("type") or "").upper()
                url = b.get("url") or ""
                if btype == "URL" and _vars(url):
                    url = _VAR_RE.sub(params.get(f"button.{i}") or "", url)
                out["buttons"].append({"type": btype, "text": b.get("text") or "",
                                       "url": url or None})
    return out


def build_components(tpl: dict, params: dict[str, str],
                     media: dict | None = None) -> list[dict]:
    """Meta's `template.components` for one send.

    `media` is {"id": <meta media id>, "filename": ...} (or {"link": ...});
    required when the template has a media header."""
    named = (tpl.get("parameter_format") or "").upper() == "NAMED"

    def text_param(name: str, value: str) -> dict:
        p = {"type": "text", "text": value}
        if named and not name.isdigit():
            p["parameter_name"] = name
        return p

    out: list[dict] = []
    for comp in _components(tpl):
        ctype = (comp.get("type") or "").upper()
        if ctype == "HEADER":
            fmt = (comp.get("format") or "TEXT").upper()
            if fmt in MEDIA_FORMATS:
                if not media:
                    raise ValueError("this template needs an attachment and the "
                                     "playbook has none")
                kind = fmt.lower()
                obj = {k: v for k, v in media.items()
                       if k in ("id", "link") and v}
                if kind == "document" and media.get("filename"):
                    obj["filename"] = media["filename"]
                out.append({"type": "header",
                            "parameters": [{"type": kind, kind: obj}]})
            elif fmt == "TEXT":
                names = _vars(comp.get("text") or "")
                if names:
                    out.append({"type": "header", "parameters": [
                        text_param(n, params.get(f"header.{n}", "")) for n in names]})
        elif ctype == "BODY":
            names = _vars(comp.get("text") or "")
            if names:
                if not named:
                    names = sorted(names, key=lambda n: int(n) if n.isdigit() else 0)
                out.append({"type": "body", "parameters": [
                    text_param(n, params.get(f"body.{n}", "")) for n in names]})
        elif ctype == "BUTTONS":
            for i, b in enumerate(comp.get("buttons") or []):
                if (b.get("type") or "").upper() == "URL" and _vars(b.get("url") or ""):
                    out.append({"type": "button", "sub_type": "url", "index": str(i),
                                "parameters": [{"type": "text",
                                                "text": params.get(f"button.{i}", "")}]})
    return out


# ---------------------------------------------------------------------------
# Authoring: a template written in the dashboard → Meta's create payload
# ---------------------------------------------------------------------------

_AUTHOR_TOKEN_RE = re.compile(r"\{\s*([a-z][a-z0-9_]{0,30})\s*\}")
_NAME_RE = re.compile(r"^[a-z0-9_]{1,512}$")


def author_body_to_meta(body: str) -> tuple[str, list[str]]:
    """"Hi {greeting}, the {project_name} brochure" →
    ("Hi {{greeting}}, the {{project_name}} brochure", ["greeting", "project_name"]).

    The author writes the same single-brace placeholders a playbook uses; Meta
    wants double-brace NAMED parameters. The variable keeps the placeholder's
    name, so a new playbook can map `body.greeting` to `{greeting}` by itself.
    """
    names: list[str] = []

    def sub(m: re.Match) -> str:
        n = m.group(1)
        if n not in names:
            names.append(n)
        return "{{" + n + "}}"

    return _AUTHOR_TOKEN_RE.sub(sub, body or ""), names


def validate_new_template(name: str, body: str) -> list[str]:
    """Meta's own review rules, checked before we spend a review cycle."""
    problems = []
    if not _NAME_RE.match(name or ""):
        problems.append("Name may use only lowercase letters, digits and underscores.")
    text = (body or "").strip()
    if not text:
        problems.append("The message body is empty.")
    if len(text) > 1024:
        problems.append("The body is longer than Meta's 1,024-character limit.")
    if re.match(r"^\s*\{", text) or re.search(r"\}\s*[.!?]?\s*$", text):
        problems.append("Meta rejects a body that starts or ends with a placeholder — "
                        "put words before the first and after the last.")
    if re.search(r"\}\s*\{", text):
        problems.append("Two placeholders side by side are rejected by Meta; "
                        "put a word between them.")
    return problems


def validate_registered_template(name: str, body: str) -> list[str]:
    """A template registered by hand must at least be sendable: a legal code
    name and a body whose variables Interakt can fill."""
    problems = []
    if not _NAME_RE.match(name or ""):
        problems.append("Name may use only lowercase letters, digits and underscores — "
                        "copy the template's code name from Interakt.")
    text = (body or "").strip()
    if not text:
        problems.append("The message body is empty.")
    if len(text) > 1024:
        problems.append("The body is longer than WhatsApp's 1,024-character limit.")
    return problems


def manual_components(*, header: str = "", header_text: str = "", body: str,
                      footer: str = "", button_text: str = "",
                      button_url: str = "") -> tuple[list[dict], str]:
    """Meta-shaped `components` for a template the tenant registered by hand —
    one created and approved in their BSP's dashboard (Interakt has no API
    that lists them). The text is written exactly as approved there, with
    {{1}} / {{2}} variables; our single-brace placeholders ({greeting}) are
    accepted too and become named variables. Returns (components,
    parameter_format) so slots(), preview() and the senders all work on it
    like a synced template."""
    text = (body or "").strip()
    if "{{" not in text:
        text, _ = author_body_to_meta(text)
    comps: list[dict[str, Any]] = []
    fmt = (header or "").upper()
    if fmt in MEDIA_FORMATS:
        comps.append({"type": "HEADER", "format": fmt})
    elif fmt == "TEXT" and header_text.strip():
        htext = header_text.strip()
        if "{{" not in htext:
            htext, _ = author_body_to_meta(htext)
        comps.append({"type": "HEADER", "format": "TEXT", "text": htext[:60]})
    comps.append({"type": "BODY", "text": text})
    if footer.strip():
        comps.append({"type": "FOOTER", "text": footer.strip()[:60]})
    if button_text.strip() and button_url.strip():
        url = button_url.strip()
        if "{{" not in url and "{" in url:
            url, _ = author_body_to_meta(url)
        comps.append({"type": "BUTTONS", "buttons": [
            {"type": "URL", "text": button_text.strip()[:25], "url": url}]})
    names = []
    for comp in comps:
        names += _vars(comp.get("text") or "")
        for b in comp.get("buttons") or []:
            names += _vars(b.get("url") or "")
    fmt_param = "POSITIONAL" if all(n.isdigit() for n in names) else "NAMED"
    return comps, fmt_param


def create_payload(*, name: str, language: str, category: str, body: str,
                   examples: dict[str, str], footer: str = "",
                   header_format: str = "", header_handle: str = "",
                   button_text: str = "", button_url_base: str = "") -> dict:
    """The POST /{waba}/message_templates body for a dashboard-authored template."""
    meta_body, names = author_body_to_meta(body)
    comps: list[dict] = []
    if header_format in MEDIA_FORMATS:
        comps.append({"type": "HEADER", "format": header_format,
                      "example": {"header_handle": [header_handle]}})
    body_comp: dict[str, Any] = {"type": "BODY", "text": meta_body}
    if names:
        body_comp["example"] = {"body_text_named_params": [
            {"param_name": n, "example": examples.get(n) or n.replace("_", " ").title()}
            for n in names]}
    comps.append(body_comp)
    if footer.strip():
        comps.append({"type": "FOOTER", "text": footer.strip()[:60]})
    if button_text.strip() and button_url_base:
        base = button_url_base.rstrip("/") + "/{{1}}"
        comps.append({"type": "BUTTONS", "buttons": [{
            "type": "URL", "text": button_text.strip()[:25], "url": base,
            "example": [button_url_base.rstrip("/") + "/aB3dE5fG7h"]}]})
    return {"name": name, "language": language, "category": category.upper(),
            "parameter_format": "NAMED", "components": comps}
