"""Static gate: no SQL may be built from a value this repo does not control.

WHY A STATIC TEST AND NOT A RUNTIME GUARD
-----------------------------------------
SQL injection is not a bug you can catch at the point it fires — by then the
statement has already run. It is a property of how a query is CONSTRUCTED, and
that property is visible in the source, so it is checkable before anything
executes.

The audit behind this file (2026-09-12) found no injectable query: every
user-controlled value in this codebase rides a bind parameter, and the handful
of queries that interpolate an IDENTIFIER (`ORDER BY {sort_col}`,
`SET {field}=?`, `ci.{field}`) pick it from a fixed whitelist declared in the
same module. That is a good state and an unstable one — the next `ORDER BY
{whatever_the_user_typed}` looks exactly like the safe ones at a glance, and
nothing would have caught it. This test is what makes the good state stick.

HOW IT WORKS
------------
Every `.execute(...)` / `.executemany(...)` call whose SQL argument is built by
an f-string, `+`, `%` or `.format()` is flagged, and each interpolated
expression is checked against `_SAFE_EXPRESSIONS` — the shapes that provably
cannot carry a payload (a lookup in a module-level whitelist dict, a join over
a literal tuple, a constant). Anything else must be listed in `_REVIEWED` with
a reason, or the test fails.

ADDING A QUERY THAT FAILS THIS TEST
-----------------------------------
In order of preference:

  1. Use a bind parameter. `WHERE x = ?` with the value in the params tuple is
     correct in every case where the interpolated thing is DATA. This is
     almost always the answer.
  2. If it is an IDENTIFIER (a column, a sort direction, a table), map the
     caller's input through a module-level dict or frozenset of literals and
     interpolate the LOOKUP RESULT, never the input. `_SORTABLE.get(sort,
     "c.started_at")` in dashboard/intel_api.py is the pattern.
  3. Only if neither applies, add an entry to `_REVIEWED` explaining why the
     value cannot be attacker-influenced. An entry is a claim you are making
     about reachability; it should name where the value comes from.

Never widen `_SAFE_EXPRESSIONS` to make a specific call pass.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from conftest import REPO_ROOT

# Directories with no bearing on what the platform serves.
_SKIP_DIRS = {
    ".git", "__pycache__", "venv", ".venv", "node_modules", "site-packages",
    "backups", "graphify", "graphify-out", "recordings", "exports", "data",
}

_EXECUTE_NAMES = {"execute", "executemany", "executescript"}


# ---------------------------------------------------------------------------
# reviewed exceptions
#
# Key: (repo-relative path, the interpolated expression as written).
# Value: why that expression cannot carry an attacker-controlled value.
#
# Every one of these was read in full during the 2026-09-12 audit. The common
# shape is `", ".join(f"{k}=?" for k in fields)` where `fields` was built by
# filtering a request body against a literal tuple of column names two lines
# above — the VALUES ride bind parameters, only the column names are
# interpolated, and those come from the tuple, not the body.
# ---------------------------------------------------------------------------
_REVIEWED: dict[tuple[str, str], str] = {
    # ---------------------------------------------------------------------
    # REQUEST-REACHABLE. Each verified to interpolate an IDENTIFIER or a
    # fixed clause; in every one the user's value rides a bind parameter.
    # ---------------------------------------------------------------------
    ("dashboard/intel_api.py", "clause"):
        "_search_clause() returns a fixed OR-chain of `col ILIKE ? ESCAPE` "
        "over a literal `cols` list; the user's term is bound, and "
        "_like_escape() neutralises its LIKE wildcards first",
    ("dashboard/intel_api.py", "' AND '.join(where)"):
        "_call_filters()/the handler append LITERAL clause strings; every "
        "value is bound. The list is built in-function, never from the body",
    ("dashboard/intel_api.py", "sort_col"):
        "_SORTABLE.get(sort, 'c.started_at') — whitelist lookup. The raw "
        "`sort` parameter can only select a key, never supply a column",
    ("dashboard/intel_api.py", "', '.join(sets)"):
        "`sets` entries are literal 'col=?' strings, the column names coming "
        "from the literal _FU_PATCHABLE tuple; values are bound",
    ("dashboard/intel_api.py", "field"):
        "loop variable over a literal tuple of jsonb column names "
        "(topics / objections / product_interest)",
    ("dashboard/intel_api.py", "scoped"):
        "literal owner/agent scope clause assembled in-function; the owner "
        "comes from the session dependency and is bound",
    ("dashboard/intel_api.py", "agent_sql"):
        "either '' or the literal ' AND c.agent_slug = ANY(?)'; slugs bound",
    ("dashboard/public_api.py", "' AND '.join(where)"):
        "literal clauses only; every value is bound. Untrusted text is also "
        "scrubbed at the /v1 boundary by shared/public_api/sanitize.py",
    ("dashboard/telephony_api.py", "cols"):
        "','.join over the keys of the `fields` dict, every key a literal "
        "written two lines above; the body's VALUES ride bind parameters",
    ("dashboard/telephony_api.py", "sets"):
        "'k=?' per key of the literal `editable` dict; values are bound",
    ("dashboard/telephony_api.py", "', '.join(updates)"):
        "`updates` entries are literal 'col=?' strings appended in-function",
    ("shared/telephony_admin.py", "', '.join(updates)"):
        "same shape — literal 'col=?' strings appended in-function",
    ("shared/contacts.py", "', '.join(sets)"):
        "literal 'col=?' strings; the contact's name/notes/gender are bound",
    ("shared/contacts.py", "table"):
        "loop variable over the literal tuple of tables holding contact_id",
    ("dashboard/server.py", "cols"):
        "'k=?' per key of the `fields` dict; every call site passes a dict "
        "whose keys are literals in the source. Values are bound",
    ("shared/intelligence/search.py", "join"):
        "_agent_clause() returns '' or one literal JOIN; slugs are bound",

    # ---------------------------------------------------------------------
    # BACKGROUND / WEBHOOK. No request path reaches these with a column name.
    # ---------------------------------------------------------------------
    ("shared/billing/webhooks.py", "sets"):
        "'k=?' per key of a dict built from literals in-function; the "
        "gateway's payload supplies VALUES only, and they are bound",
    ("shared/billing/service.py", "cols"):
        "'k=?' per key of a dict built from literals in-function",
    ("shared/billing/entitlements.py", "cols"):
        "keys of the `values` dict assembled literally in resolve_entitlements",
    ("shared/billing/entitlements.py", "updates"):
        "'c=excluded.c' over those same literal keys",
    ("shared/intelligence/pipeline.py", "', '.join(cols)"):
        "`cols` is the key list of payload_cols, built entirely from the "
        "module-level _SCALAR_COLS / _JSON_COLS literals. The LLM-authored "
        "record supplies only the VALUES, which are bound (and json.dumps'd)",
    ("shared/intelligence/pipeline.py", "', '.join((f'{c}=EXCLUDED.{c}' for c in cols))"):
        "same `cols` as above — the ON CONFLICT clause mirrors the insert",

    # ---------------------------------------------------------------------
    # OPERATOR-ONLY. Admin console (token-gated) and hand-run CLI tools;
    # no tenant request reaches any of these.
    # ---------------------------------------------------------------------
    ("dashboard/admin_ops.py", "clause"):
        "'WHERE ' + ' AND '.join of literal clauses; the operator's search "
        "text is bound. Admin console, behind require_token",
    ("shared/billing/plans_bootstrap.py", "column"):
        "_id_column(mode) returns one of two literal column names "
        "(razorpay_plan_id_test / _live). CLI, run by hand with --apply",
    ("shared/db/seed.py", "table"):
        "loop variable over the module-level SEEDED_TABLES literal list",
    ("shared/db/import_json.py", "table"):
        "table name passed by the JSON->DB import CLI from its own literal "
        "table map; not reachable from any request",
    ("shared/db/migrate_sqlite_to_pg.py", "table"):
        "loop variable over the module-level TABLES literal list (one-time "
        "sqlite->postgres migration tool)",
    ("shared/db/migrate_sqlite_to_pg.py", "','.join(cols)"):
        "column names read back from the SOURCE sqlite schema during that "
        "same one-time migration; values are bound",
}


# ---------------------------------------------------------------------------
# scanner
# ---------------------------------------------------------------------------

def _bound_names(target: ast.AST) -> set[str]:
    """Names a comprehension's `for` target binds."""
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        out: set[str] = set()
        for elt in target.elts:
            out |= _bound_names(elt)
        return out
    return set()


def _is_safe_expression(node: ast.AST, scope: list[ast.AST],
                        depth: int = 0, safe_names: frozenset = frozenset()) -> bool:
    """Shapes whose VALUE cannot depend on any input.

    The rule throughout is provability, not plausibility: an expression is safe
    only when every value it can possibly take is fixed in the source. That
    admits a literal, a constant, arithmetic over those, a ternary whose
    branches are all safe, a `",".join(...)` over safe elements — and nothing
    that reads a variable the scanner cannot trace to a literal.

    Deliberately narrow. Anything that even might be a request value has to go
    through _REVIEWED, where a human has to write down why it cannot be.
    """
    if depth > 6:      # a cycle, or an expression too tangled to certify
        return False

    def sub(n, extra=frozenset()):
        return _is_safe_expression(n, scope, depth + 1, safe_names | extra)

    # A literal.
    if isinstance(node, ast.Constant):
        return True

    # An f-string is safe when every expression spliced into it is. This is
    # the same rule the scanner applies to SQL itself, one level down.
    if isinstance(node, ast.JoinedStr):
        return all(sub(v.value) for v in node.values
                   if isinstance(v, ast.FormattedValue))

    # Arithmetic / concatenation over safe operands stays safe.
    if isinstance(node, ast.BinOp) and not isinstance(node.op, ast.Mod):
        return sub(node.left) and sub(node.right)

    # A ternary is safe when EVERY branch is. `'ASC' if asc else 'DESC'` cannot
    # produce anything but those two strings however `asc` was derived, so the
    # input genuinely cannot reach the SQL text.
    if isinstance(node, ast.IfExp):
        return sub(node.body) and sub(node.orelse)

    if isinstance(node, ast.Call):
        func = node.func
        # int(x) / len(x) — cannot produce SQL syntax whatever x is.
        if isinstance(func, ast.Name) and func.id in {"int", "len"}:
            return True
        # "<sep>".join(<safe elements>) — the classic placeholder builder,
        # `",".join("?" for _ in ids)`. Safe when the separator is a literal
        # and every element the comprehension can yield is safe; the VALUES
        # still ride bind parameters. Note this is about the produced STRING:
        # `", ".join(f"{c}=?" for c in cols)` has an unsafe element (it splices
        # a loop variable) and is correctly rejected.
        if (isinstance(func, ast.Attribute) and func.attr == "join"
                and isinstance(func.value, ast.Constant)
                and len(node.args) == 1):
            return _joined_elements_safe(node.args[0], scope, depth + 1,
                                         safe_names)

    if isinstance(node, ast.Name):
        # Module-level UPPER_CASE constants are literals by convention here.
        if node.id.isupper():
            return True
        # A comprehension variable bound by iterating something already proven
        # constant: every value it can take comes out of that constant.
        if node.id in safe_names:
            return True
        # Otherwise trace it: a local built entirely from safe pieces is safe.
        # This is what lets `marks = ",".join("?" for _ in ids)` pass on its
        # own merits rather than needing a hand-written exception.
        assignments = _resolve_name(node.id, scope)
        return bool(assignments) and all(sub(a) for a in assignments)

    if isinstance(node, ast.Attribute) and node.attr.isupper():
        return True

    return False


def _joined_elements_safe(arg: ast.AST, scope: list[ast.AST], depth: int,
                          safe_names: frozenset = frozenset()) -> bool:
    """Is every string `"x".join(arg)` can produce fixed in the source?"""
    if isinstance(arg, (ast.GeneratorExp, ast.ListComp, ast.SetComp)):
        # First, the common case: the element ignores the loop variable, as in
        # `",".join("?" for _ in ids)`. What is iterated only decides HOW MANY
        # placeholders come out, never which characters — so the iterable does
        # not have to be proven at all.
        if _is_safe_expression(arg.elt, scope, depth, safe_names):
            return True
        # Otherwise the element does splice the loop variable, and now the
        # iterable matters: iterating a provably-constant collection binds
        # provably-constant names, so `f"...{lo}...{hi}" for name, lo, hi in
        # _LEAD_BANDS` is as fixed as _LEAD_BANDS itself. One unproven `for`
        # clause and the whole comprehension stays unproven.
        bound: set[str] = set()
        for gen in arg.generators:
            if not _is_safe_expression(gen.iter, scope, depth, safe_names):
                return False
            bound |= _bound_names(gen.target)
        return _is_safe_expression(arg.elt, scope, depth,
                                   safe_names | frozenset(bound))
    if isinstance(arg, (ast.List, ast.Tuple)):
        return all(_is_safe_expression(e, scope, depth, safe_names)
                   for e in arg.elts)
    # `"?" * len(x)` and friends.
    return _is_safe_expression(arg, scope, depth, safe_names)


def _expr_src(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:      # pragma: no cover - python < 3.9 has no unparse
        return "<unparseable>"


def _interpolations(node: ast.AST) -> list[ast.AST] | None:
    """The expressions spliced into a dynamically built SQL string, or None if
    this node is not dynamically built."""
    if isinstance(node, ast.JoinedStr):
        return [v.value for v in node.values if isinstance(v, ast.FormattedValue)]
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        out: list[ast.AST] = []
        for side in (node.left, node.right):
            nested = _interpolations(side)
            if nested is not None:
                out.extend(nested)
            elif not isinstance(side, ast.Constant):
                out.append(side)
        return out
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "format"):
        return list(node.args) + [kw.value for kw in node.keywords]
    return None


def _sql_arg(call: ast.Call) -> ast.AST | None:
    """The SQL argument of an execute()-shaped call, resolved one level through
    a local variable assignment where possible."""
    return call.args[0] if call.args else None


def _resolve_name(name: str, scope: list[ast.AST]) -> list[ast.AST]:
    """Every assignment to `name` within the enclosing scopes.

    `sql = f"...{sort_col}..."` then `db.execute(sql, params)` is a common and
    entirely legitimate style here, and a scanner that only looked at the
    execute() call would see a bare Name and wave it through — which would make
    the gate useless exactly where the queries are biggest.

    `scope` is the chain from the innermost enclosing function outwards, NOT
    the whole module: a `where` built from literals in one handler has nothing
    to do with a `where` built by appending in another, and conflating them
    made half the findings noise about code that was never involved.

    Anything this cannot see — a tuple-unpack target, an augmented assignment,
    a `for` binding, a parameter — yields no assignment for that name, and a
    name with no traceable assignment is treated as UNSAFE by the caller. The
    unknown direction is the refusing direction, which is the only correct
    default for a security gate.
    """
    found: list[ast.AST] = []
    for fn in scope:
        for stmt in ast.walk(fn):
            if isinstance(stmt, ast.Assign):
                for tgt in stmt.targets:
                    if isinstance(tgt, ast.Name) and tgt.id == name:
                        found.append(stmt.value)
                    elif isinstance(tgt, (ast.Tuple, ast.List)):
                        # `where, params = [], []` — the name IS assigned, and
                        # by something this cannot evaluate. Record it as an
                        # unprovable assignment rather than pretending it does
                        # not exist.
                        for elt in tgt.elts:
                            if isinstance(elt, ast.Name) and elt.id == name:
                                found.append(ast.Name(id="__unprovable__",
                                                      ctx=ast.Load()))
            elif isinstance(stmt, ast.AugAssign):
                if isinstance(stmt.target, ast.Name) and stmt.target.id == name:
                    found.append(stmt.value)
        if found:
            # Innermost binding wins; an outer scope's same-named variable is
            # shadowed and irrelevant.
            break
    return found


def _scope_chain(tree: ast.Module) -> dict[ast.AST, list[ast.AST]]:
    """node -> [innermost enclosing function, ..., outermost]."""
    chains: dict[ast.AST, list[ast.AST]] = {}

    def walk(node: ast.AST, stack: list[ast.AST]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                inner = [child] + stack
                chains[child] = inner
                walk(child, inner)
            else:
                chains[child] = stack
                walk(child, stack)

    walk(tree, [])
    return chains


def _python_files() -> list[Path]:
    out = []
    for path in REPO_ROOT.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.name.startswith("test_"):
            # Tests build throwaway SQL against a throwaway database. They are
            # not a production surface and an attacker cannot reach them.
            continue
        out.append(path)
    return sorted(out)


def _interpolated_sites(path: Path) -> list[tuple[int, ast.AST, list[ast.AST]]]:
    """(lineno, interpolated expression, scope chain) for every execute() in
    this file whose SQL is dynamically built."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []
    chains = _scope_chain(tree)
    out: list[tuple[int, ast.AST, list[ast.AST]]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _EXECUTE_NAMES):
            continue
        arg = _sql_arg(node)
        if arg is None:
            continue
        scope = chains.get(node, [])
        candidates = [arg]
        if isinstance(arg, ast.Name):
            # A bare `db.execute(sql, ...)` — follow it to what built `sql`.
            candidates = _resolve_name(arg.id, scope) or []
        for cand in candidates:
            for part in (_interpolations(cand) or []):
                out.append((node.lineno, part, scope))
    return out


def _findings() -> list[tuple[str, int, str]]:
    """(relpath, lineno, interpolated expression) for every unreviewed site."""
    findings: list[tuple[str, int, str]] = []
    for path in _python_files():
        rel = str(path.relative_to(REPO_ROOT))
        for lineno, part, scope in _interpolated_sites(path):
            if _is_safe_expression(part, scope):
                continue
            src = _expr_src(part)
            if (rel, src) in _REVIEWED:
                continue
            findings.append((rel, lineno, src))
    return findings


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------

def test_no_unreviewed_dynamic_sql():
    findings = _findings()
    if findings:
        lines = "\n".join(f"  {rel}:{line} interpolates `{src}`"
                          for rel, line, src in sorted(set(findings)))
        pytest.fail(
            "SQL is being built from an expression this gate cannot prove "
            "safe:\n" + lines + "\n\n"
            "Fix it with a bind parameter, or map the value through a "
            "module-level whitelist and interpolate the lookup RESULT. If it "
            "genuinely cannot be attacker-influenced, add it to _REVIEWED in "
            "this file with the reason. See the module docstring.")


def test_reviewed_list_has_no_dead_entries():
    """A _REVIEWED entry for a query that no longer exists is a claim nobody is
    checking any more. Prune it, so the list stays a description of the code
    rather than a growing pile of past exceptions."""
    live: set[tuple[str, str]] = set()
    for path in _python_files():
        rel = str(path.relative_to(REPO_ROOT))
        for _lineno, part, _scope in _interpolated_sites(path):
            live.add((rel, _expr_src(part)))
    dead = sorted(set(_REVIEWED) - live)
    assert not dead, (
        "These _REVIEWED entries no longer match any query — delete them:\n"
        + "\n".join(f"  {rel}: `{src}`" for rel, src in dead))
