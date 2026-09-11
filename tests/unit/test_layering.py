"""The layer boundaries, checked mechanically.

Business rules do not depend on technology. Written down in prose that is a
wish. Written here it fails the build.

Two levels of check, on purpose:

  * A substring scan. Crude, but it catches a forbidden name anywhere in a file,
    including inside a dynamically built import or a field named after a driver.
  * An import analysis over the parsed syntax tree. Precise: it reads what a
    module actually imports rather than what its text happens to contain.

Neither subsumes the other. The scan has false positives — a comment naming a
driver turns the build red — and the analysis has a blind spot, importlib with a
computed name. What actually happens is accidental drift, and that does not get
past both.
"""

import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
DOMAIN = SRC / "cua" / "domain"
PORTS = SRC / "cua" / "ports"
ADAPTERS = SRC / "cua" / "adapters"

FORBIDDEN_SUBSTRINGS = ("adapters", "playwright", "anthropic")

STDLIB = set(sys.stdlib_module_names)


def python_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def imported_modules(path: Path) -> set[str]:
    """Dotted module names imported by one file.

    Relative imports are ignored: they cannot escape their own package, so they
    cannot cross a layer boundary.
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
    return found


def is_allowed(module: str, allowed_prefixes: tuple[str, ...]) -> bool:
    root = module.split(".")[0]
    if root in STDLIB:
        return True
    return any(module == p or module.startswith(p + ".") for p in allowed_prefixes)


def test_domain_is_not_empty() -> None:
    """Every assertion below is trivially true over an empty directory.

    Without this the suite goes green on a domain that has been deleted.
    """
    assert python_files(DOMAIN), f"no python files under {DOMAIN}"


def test_domain_mentions_no_infrastructure() -> None:
    """No forbidden name appears anywhere in domain text, imported or not."""
    for path in python_files(DOMAIN):
        text = path.read_text(encoding="utf-8")
        for word in FORBIDDEN_SUBSTRINGS:
            assert word not in text, f"{path.relative_to(SRC)} mentions {word!r}"


def test_domain_imports_only_the_standard_library() -> None:
    """The domain holds business rules expressed in plain Python.

    It has no third party dependency at all, which is why its tests need no
    browser, no model and no network.
    """
    for path in python_files(DOMAIN):
        for module in imported_modules(path):
            assert is_allowed(module, ("cua.domain",)), (
                f"{path.relative_to(SRC)} imports {module!r}; "
                "domain may import the standard library and cua.domain only"
            )


def test_ports_import_only_domain() -> None:
    """Ports are phrased in domain types.

    A driver or SDK type in a port signature makes every caller of that port
    depend on that technology, whatever the layering diagram says.
    """
    for path in python_files(PORTS):
        for module in imported_modules(path):
            assert is_allowed(module, ("cua.domain", "cua.ports")), (
                f"{path.relative_to(SRC)} imports {module!r}; "
                "ports may import the standard library, cua.domain and cua.ports only"
            )


def test_only_the_composition_root_imports_adapters() -> None:
    """Every module is written against ports except one, which does the wiring.

    A second module importing adapters means the wiring has leaked, and swapping
    a technology stops being a one file change.
    """
    offenders = []
    for path in python_files(SRC):
        # The composition root chooses implementations, and an adapter may lean
        # on another adapter: a store raising a configuration error is still one
        # piece of technology talking to its neighbour.
        if path.name == "composition.py" or ADAPTERS in path.parents:
            continue
        if any(m.startswith("cua.adapters") for m in imported_modules(path)):
            offenders.append(str(path.relative_to(SRC)))
    assert not offenders, f"only composition.py may import adapters; found {offenders}"
