"""Small helpers for working with Python ASTs."""

from __future__ import annotations

import ast
from typing import Iterator


def dotted(node: ast.AST | None) -> str | None:
    """'a.b.c' for Name/Attribute chains, else None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def last_name(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        return last_name(node.func)
    return None


def const_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def iter_strings(node: ast.AST | None) -> Iterator[tuple[str, ast.AST]]:
    """String constants in a literal: 'a', ['a', 'b'], (('a', 'b'),), {'a', ...}."""
    if node is None:
        return
    s = const_str(node)
    if s is not None:
        yield s, node
        return
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        for elt in node.elts:
            yield from iter_strings(elt)
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        yield from iter_strings(node.left)
        yield from iter_strings(node.right)


def dict_items(node: ast.AST | None) -> Iterator[tuple[ast.AST | None, ast.AST]]:
    if isinstance(node, ast.Dict):
        for k, v in zip(node.keys, node.values):
            yield k, v
    elif isinstance(node, ast.Call) and last_name(node.func) == "dict":
        for kw in node.keywords:
            if kw.arg:
                yield ast.Constant(kw.arg, lineno=kw.value.lineno, col_offset=kw.value.col_offset), kw.value


def kwarg(call: ast.Call, name: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def chain_root(node: ast.AST) -> str | None:
    """Leftmost Name of a call/attribute chain: Order.objects.filter(...).all -> 'Order'."""
    while True:
        if isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.Attribute):
            node = node.value
        elif isinstance(node, ast.Subscript):
            node = node.value
        elif isinstance(node, ast.Name):
            return node.id
        else:
            return None


def chain_calls(node: ast.AST) -> Iterator[ast.Call]:
    """All calls along a method chain, outermost first."""
    while True:
        if isinstance(node, ast.Call):
            yield node
            node = node.func
        elif isinstance(node, ast.Attribute):
            node = node.value
        elif isinstance(node, ast.Subscript):
            node = node.value
        else:
            return


def chain_attrs(node: ast.AST) -> list[str]:
    """Attribute names along a chain, outermost first."""
    out = []
    while True:
        if isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.Attribute):
            out.append(node.attr)
            node = node.value
        elif isinstance(node, ast.Subscript):
            node = node.value
        else:
            return out


def class_assigns(cls: ast.ClassDef) -> Iterator[tuple[str, ast.expr, ast.stmt]]:
    """Class-level `name = value` and `name: T = value` statements."""
    for stmt in cls.body:
        if isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Name):
                    yield t.id, stmt.value, stmt
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
            yield stmt.target.id, stmt.value, stmt


def inner_class(cls: ast.ClassDef, name: str) -> ast.ClassDef | None:
    for stmt in cls.body:
        if isinstance(stmt, ast.ClassDef) and stmt.name == name:
            return stmt
    return None


def span(node: ast.AST) -> tuple[int, int, int, int]:
    return (node.lineno, node.col_offset, node.end_lineno or node.lineno, node.end_col_offset or 0)
