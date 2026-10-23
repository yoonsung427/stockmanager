"""AST-enforced import boundaries (AS §1.8). Walks ``src/stockmanager``; nothing is imported or executed."""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
PKG = "stockmanager"
THIRD_PARTY_NETWORK_AND_LLM = {"httpx", "discord", "anthropic"}


def _modules(package: str) -> list[tuple[str, Path]]:
    """``(dotted module name, path)`` for every ``.py`` file in ``stockmanager.<package>`` (dir or single file)."""
    base = SRC / PKG / package
    files = sorted(base.rglob("*.py")) if base.is_dir() else [base.with_suffix(".py")]
    return [(".".join(f.relative_to(SRC).with_suffix("").parts), f) for f in files]


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _imports(path: Path, module: str, *, runtime_only: bool = False) -> Iterator[tuple[str, int]]:
    """Absolute dotted names imported by ``path`` (relative imports resolved), with line numbers.

    ``runtime_only`` skips imports under ``if TYPE_CHECKING:`` (they never execute).
    """
    is_pkg = path.name == "__init__.py"
    package_parts = module.split(".") if is_pkg else module.split(".")[:-1]

    def walk(nodes: list[ast.stmt]) -> Iterator[tuple[str, int]]:
        for node in nodes:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    yield alias.name, node.lineno
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    anchor = package_parts[: len(package_parts) - (node.level - 1)]
                    base = ".".join([*anchor, *([node.module] if node.module else [])])
                else:
                    base = node.module or ""
                yield base, node.lineno
                for alias in node.names:  # ``from pkg import sub`` may import the submodule ``pkg.sub``
                    yield f"{base}.{alias.name}", node.lineno
            elif isinstance(node, ast.If) and runtime_only and _is_type_checking(node.test):
                yield from walk(node.orelse)
            else:
                for field in ("body", "orelse", "finalbody", "handlers"):
                    children = getattr(node, field, None)
                    if isinstance(children, list):
                        yield from walk([c for c in children if isinstance(c, (ast.stmt, ast.ExceptHandler))])  # type: ignore[misc]

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    yield from walk(tree.body)


def _violations(package: str, forbidden: Callable[[str], bool], *, runtime_only: bool = False) -> list[str]:
    """``"<file>:<line> imports <name>"`` for every import in ``package`` that ``forbidden`` rejects."""
    found = []
    for module, path in _modules(package):
        for name, line in _imports(path, module, runtime_only=runtime_only):
            if forbidden(name):
                found.append(f"{path.relative_to(SRC.parent)}:{line} imports {name}")
    return found


def _under(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


def test_news_fetchers_do_http_only_through_adapters_http_and_never_touch_store() -> None:
    def forbidden(name: str) -> bool:
        if _under(name, f"{PKG}.store"):
            return True
        # the only adapters module a fetcher may use is the shared HTTP transport
        return _under(name, f"{PKG}.adapters") and not _under(name, f"{PKG}.adapters.http") and name != f"{PKG}.adapters"

    assert _violations("news/fetchers", forbidden) == []


# Known breach of AS §1.8 contract (1): ``domain.docs`` takes ``MixedSourceError`` from ``pricing.errors``. Fixing it
# means moving that error into ``domain``; until then it is pinned here so any *new* breach still fails the test.
DOMAIN_KNOWN_BREACHES = {f"{PKG}.domain.docs": f"{PKG}.pricing.errors"}


def test_domain_imports_nothing_from_the_project_except_itself_at_runtime() -> None:
    # Type-only imports (ports.py names NewsQuery/RawItem in annotations under TYPE_CHECKING) never execute.
    unexpected = []
    for module, path in _modules("domain"):
        allowed = DOMAIN_KNOWN_BREACHES.get(module)
        for name, line in _imports(path, module, runtime_only=True):
            outside = _under(name, PKG) and not _under(name, f"{PKG}.domain") and name != PKG
            if outside and not (allowed and _under(name, allowed)):
                unexpected.append(f"{module}:{line} imports {name}")
    assert unexpected == []


@pytest.mark.parametrize("package", ["evaluation", "execution", "commands"])
def test_engines_and_commands_do_not_import_http_or_llm_sdks(package: str) -> None:
    assert _violations(package, lambda n: n.split(".")[0] in THIRD_PARTY_NETWORK_AND_LLM) == []


@pytest.mark.parametrize("package", ["evaluation/reasoner_io", "evaluation/reasoner"])
def test_reasoner_cannot_import_raw_news(package: str) -> None:
    """The injection boundary (AS §7.3): nothing that feeds or wraps the Reasoner may see raw headlines."""
    assert _violations(package, lambda n: _under(n, f"{PKG}.news.raw")) == []


def test_import_walker_resolves_relative_imports_and_type_checking(tmp_path: Path) -> None:
    sample = tmp_path / "pkg" / "mod.py"
    sample.parent.mkdir()
    sample.write_text(
        "from __future__ import annotations\n"
        "from typing import TYPE_CHECKING\n"
        "from . import sibling\n"
        "from ..other import thing\n"
        "if TYPE_CHECKING:\n"
        "    import only_for_types\n"
    )
    runtime = {n for n, _ in _imports(sample, "pkg.mod", runtime_only=True)}
    everything = {n for n, _ in _imports(sample, "pkg.mod")}
    assert "pkg.sibling" in runtime and "other.thing" in runtime
    assert "only_for_types" not in runtime and "only_for_types" in everything
