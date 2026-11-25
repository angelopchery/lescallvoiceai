"""
diff — path-level diff and surgical ops over agent definition dicts (plan §5.2).

Paths use dotted keys with list indices: `call_flow.stages[3].goal`.
Pure functions, no I/O.
"""

from __future__ import annotations

import copy
import re
from typing import Any


def _walk_diff(old: Any, new: Any, path: str, out: list[dict]) -> None:
    if isinstance(old, dict) and isinstance(new, dict):
        for k in sorted(set(old) | set(new)):
            sub = f"{path}.{k}" if path else str(k)
            if k not in old:
                out.append({"path": sub, "op": "add", "old": None, "new": new[k]})
            elif k not in new:
                out.append({"path": sub, "op": "remove", "old": old[k], "new": None})
            else:
                _walk_diff(old[k], new[k], sub, out)
        return
    if isinstance(old, (list, tuple)) and isinstance(new, (list, tuple)):
        old_l, new_l = list(old), list(new)
        for i in range(max(len(old_l), len(new_l))):
            sub = f"{path}[{i}]"
            if i >= len(old_l):
                out.append({"path": sub, "op": "add", "old": None, "new": new_l[i]})
            elif i >= len(new_l):
                out.append({"path": sub, "op": "remove", "old": old_l[i], "new": None})
            else:
                _walk_diff(old_l[i], new_l[i], sub, out)
        return
    if old != new:
        out.append({"path": path, "op": "set", "old": old, "new": new})


def compute_diff(old: dict, new: dict) -> list[dict]:
    """[{path, op, old, new}] — leaf-level differences old → new."""
    out: list[dict] = []
    _walk_diff(old or {}, new or {}, "", out)
    return out


def _short(v: Any, limit: int = 90) -> str:
    s = repr(v)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def render_human(diff: list[dict]) -> str:
    """Human-readable diff for chat display / diff cards."""
    if not diff:
        return "No changes."
    lines = []
    for d in diff:
        if d["op"] == "add":
            lines.append(f"+ {d['path']} = {_short(d['new'])}")
        elif d["op"] == "remove":
            lines.append(f"- {d['path']} (was {_short(d['old'])})")
        else:
            lines.append(f"~ {d['path']}: {_short(d['old'])} → {_short(d['new'])}")
    return "\n".join(lines)


_PATH_TOKEN_RE = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def parse_path(path: str) -> list:
    """'call_flow.stages[3].goal' → ['call_flow', 'stages', 3, 'goal']."""
    tokens: list = []
    for m in _PATH_TOKEN_RE.finditer(path or ""):
        if m.group(1) is not None:
            tokens.append(m.group(1))
        else:
            tokens.append(int(m.group(2)))
    if not tokens:
        raise ValueError(f"empty path {path!r}")
    return tokens


def _resolve_parent(root: Any, tokens: list):
    node = root
    for t in tokens[:-1]:
        if isinstance(t, int):
            if not isinstance(node, list) or not (0 <= t < len(node)):
                raise ValueError(f"path index [{t}] out of range")
            node = node[t]
        else:
            if not isinstance(node, dict) or t not in node:
                raise ValueError(f"path key {t!r} not found")
            node = node[t]
    return node


def apply_ops(definition: dict, ops: list[dict]) -> dict:
    """Apply [{op: set|append|remove_at|move, path, value?, to?}] server-side.
    Returns a NEW dict; the input is never mutated. Raises ValueError on any
    unknown op or unresolvable path — ops are all-or-nothing."""
    doc = copy.deepcopy(definition or {})
    for op_row in ops or []:
        op = str(op_row.get("op") or "")
        tokens = parse_path(str(op_row.get("path") or ""))
        last = tokens[-1]
        if op == "set":
            parent = _resolve_parent(doc, tokens)
            if isinstance(last, int):
                if not isinstance(parent, list) or not (0 <= last < len(parent)):
                    raise ValueError(f"set index [{last}] out of range")
                parent[last] = copy.deepcopy(op_row.get("value"))
            else:
                if not isinstance(parent, dict):
                    raise ValueError(f"set target for {last!r} is not an object")
                parent[last] = copy.deepcopy(op_row.get("value"))
        elif op == "append":
            parent = _resolve_parent(doc, tokens)
            target = parent[last] if not isinstance(last, int) else parent
            if isinstance(last, int):
                raise ValueError("append path must name a list key, not an index")
            if target is None:
                parent[last] = target = []
            if not isinstance(target, list):
                raise ValueError(f"append target {op_row.get('path')!r} is not a list")
            target.append(copy.deepcopy(op_row.get("value")))
        elif op == "remove_at":
            parent = _resolve_parent(doc, tokens)
            if isinstance(last, int):
                if not isinstance(parent, list) or not (0 <= last < len(parent)):
                    raise ValueError(f"remove_at index [{last}] out of range")
                parent.pop(last)
            else:
                if not isinstance(parent, dict) or last not in parent:
                    raise ValueError(f"remove_at key {last!r} not found")
                parent.pop(last)
        elif op == "move":
            if not isinstance(last, int):
                raise ValueError("move path must end in a list index")
            parent = _resolve_parent(doc, tokens)
            to = int(op_row.get("to", 0))
            if not isinstance(parent, list) or not (0 <= last < len(parent)):
                raise ValueError(f"move index [{last}] out of range")
            if not (0 <= to < len(parent)):
                raise ValueError(f"move destination [{to}] out of range")
            item = parent.pop(last)
            parent.insert(to, item)
        else:
            raise ValueError(f"unknown op {op!r}")
    return doc


def changed_top_paths(diff: list[dict]) -> set[str]:
    """Coarse top-of-subtree paths from a leaf diff (used for scoping checks):
    'call_flow.stages[3].goal' stays as-is; callers compare with is_within()."""
    return {d["path"] for d in diff}


def is_within(leaf_path: str, allowed_prefixes: set[str]) -> bool:
    """True when `leaf_path` is equal to, nested under, or an ANCESTOR of any
    allowed prefix.

    The ancestor direction matters for adds: a proposal allowing
    `response_bank.localized_responses.hi.opening_greeting` on an agent with
    no `localized_responses` yet makes compute_diff emit op=add at the new
    subtree ROOT (`response_bank.localized_responses.hi`) — a PREFIX of the
    allowed leaf. Without the ancestor rule that legitimate add tripped the
    scope check and 500'd every diff-card Apply that introduced a new nested
    section (2026-07-23 copilot audit).
    """
    for p in allowed_prefixes:
        if leaf_path == p or leaf_path.startswith(p + ".") or leaf_path.startswith(p + "["):
            return True
        # leaf_path is an ancestor of an allowed leaf → the write cannot touch
        # anything outside that leaf's own subtree.
        if p.startswith(leaf_path + ".") or p.startswith(leaf_path + "["):
            return True
    return False
