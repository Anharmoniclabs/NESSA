"""Bounded Python-source proposals; parsing never executes generated code."""
from __future__ import annotations

import ast
import re

from .workspace import ToolError

MAX_SOURCE_CHARS = 12000
MAX_PROPOSAL_CHARS = 24000
_FENCE = re.compile(r"\A```(?:python|py)?[ \t]*\r?\n(?P<code>[\s\S]*?)```\Z", re.I)


def parse_source(source: str, filename: str) -> ast.Module:
    try:
        tree = ast.parse(source, filename=filename)
        compile(tree, filename, "exec")
        return tree
    except (SyntaxError, ValueError, TypeError, RecursionError) as exc:
        raise ToolError(f"Replacement is not valid Python: {exc}") from exc


def extract_source(content: str, filename: str) -> str:
    if not isinstance(content, str) or not content.strip():
        raise ToolError("Return the complete corrected Python file; the response was empty.")
    if len(content) > MAX_PROPOSAL_CHARS:
        raise ToolError("Replacement exceeds the bounded source-proposal size.")
    stripped = content.strip()
    if stripped.startswith("```"):
        match = _FENCE.fullmatch(stripped)
        if not match:
            raise ToolError("Only a single outer Python fence is allowed; no surrounding prose or extra blocks.")
        source = match.group("code")
    else:
        source = content
    if len(source) > MAX_SOURCE_CHARS:
        raise ToolError("Replacement source exceeds the 12000-character retry-input bound; no edit was applied.")
    if source.lstrip().startswith(("{", "[")):
        raise ToolError("Return Python source, not JSON, a tool call, or an edit description.")
    parse_source(source, filename)
    return source


def _dump(value):
    if isinstance(value, list):
        return tuple(_dump(v) for v in value)
    return None if value is None else ast.dump(value, include_attributes=False)


def interface(tree: ast.Module) -> tuple[dict, set[str]]:
    """Record declarations/signatures and module bindings that must remain.

    This guards accidental omission or renaming, not semantic equivalence.
    Added helpers are permitted. Duplicate qualified declarations are unsupported.
    """
    declarations = {}
    stack = []

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            key = ".".join((*stack, node.name))
            if key in declarations:
                raise ToolError(f"Code-only mode does not support duplicate declaration {key!r}.")
            declarations[key] = (type(node).__name__, _dump(node.args), _dump(node.returns),
                                 _dump(node.decorator_list), _dump(getattr(node, "type_params", [])))
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node):
            key = ".".join((*stack, node.name))
            if key in declarations:
                raise ToolError(f"Code-only mode does not support duplicate declaration {key!r}.")
            declarations[key] = ("ClassDef", _dump(node.bases), _dump(node.keywords),
                                 _dump(node.decorator_list), _dump(getattr(node, "type_params", [])))
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

    Visitor().visit(tree)
    bindings = _module_bindings(tree)
    return declarations, bindings


def _module_bindings(tree: ast.Module) -> set[str]:
    """Collect declared module names through control flow, excluding local scopes."""
    bindings = {name for node in ast.walk(tree) if isinstance(node, ast.Global) for name in node.names}

    class Bindings(ast.NodeVisitor):
        def visit_Name(self, node):
            if isinstance(node.ctx, ast.Store):
                bindings.add(node.id)

        def visit_FunctionDef(self, node):
            bindings.add(node.name)
            # Defaults, decorators and annotations are enclosing-scope expressions.
            for value in (*node.decorator_list, *node.args.defaults, *node.args.kw_defaults, node.returns):
                if value is not None:
                    self.visit(value)
            for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs,
                        node.args.vararg, node.args.kwarg):
                if arg is not None and arg.annotation is not None:
                    self.visit(arg.annotation)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node):
            bindings.add(node.name)
            for value in (*node.decorator_list, *node.bases, *node.keywords):
                self.visit(value)

        def visit_Lambda(self, node):
            for value in (*node.args.defaults, *node.args.kw_defaults):
                if value is not None:
                    self.visit(value)

        def visit_Import(self, node):
            bindings.update(n.asname or n.name.split(".")[0] for n in node.names)

        def visit_ImportFrom(self, node):
            if any(n.name == "*" for n in node.names):
                raise ToolError("Code-only mode cannot establish bindings from wildcard imports; use tools mode.")
            self.visit_Import(node)

        def visit_ExceptHandler(self, node):
            if node.name:
                bindings.add(node.name)
            self.generic_visit(node)

        def visit_MatchAs(self, node):
            if node.name:
                bindings.add(node.name)
            self.generic_visit(node)

        visit_MatchStar = visit_MatchAs

        def visit_MatchMapping(self, node):
            if node.rest:
                bindings.add(node.rest)
            self.generic_visit(node)

        def visit_ListComp(self, node):
            # Comprehension targets are local; walrus expressions can bind outside.
            for generator in node.generators:
                self.visit(generator.iter)
                for condition in generator.ifs:
                    self.visit(condition)
            self.visit(node.elt)

        visit_SetComp = visit_ListComp
        visit_GeneratorExp = visit_ListComp

        def visit_DictComp(self, node):
            for generator in node.generators:
                self.visit(generator.iter)
                for condition in generator.ifs:
                    self.visit(condition)
            self.visit(node.key)
            self.visit(node.value)

    Bindings().visit(tree)
    return bindings


def validate_interface(source: str, filename: str, original: tuple[dict, set[str]]) -> None:
    actual, bindings = interface(parse_source(source, filename))
    expected, original_bindings = original
    missing = sorted(original_bindings - bindings)
    altered = sorted(name for name, signature in expected.items() if actual.get(name) != signature)
    if missing or altered:
        raise ToolError("Preserve existing declarations and signatures. Missing bindings: " +
                        (", ".join(missing) or "none") + "; changed/missing signatures: " +
                        (", ".join(altered) or "none"))
