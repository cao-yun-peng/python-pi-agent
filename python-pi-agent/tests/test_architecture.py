"""Offline static dependency contracts, including type-only and local imports."""

from __future__ import annotations

import ast
import unittest
from importlib.util import resolve_name
from pathlib import Path


_PACKAGE = Path(__file__).resolve().parents[1] / "src" / "pi_agent_py"
_DEFAULTS = {
    "pi_agent_py.runtime.components": {
        "pi_agent_py.context.strategies",
        "pi_agent_py.tools.approval",
        "pi_agent_py.tools.gates",
        "pi_agent_py.tools.schema",
    },
    "pi_agent_py.runtime.compaction_orchestration": {
        "pi_agent_py.context.compaction",
    },
}


def _within(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def _dependencies(source: str, module: str, *, package: bool = False) -> list[tuple[int, str]]:
    """Resolve static imports without importing or executing application code."""
    result = []
    parent = module if package else module.rpartition(".")[0]
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            result.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = (
                resolve_name("." * node.level + (node.module or ""), parent)
                if node.level else node.module or ""
            )
            # Include member paths to catch `from package import submodule`.
            package_path = _PACKAGE.parent.joinpath(*base.split("."))
            if not package_path.is_dir():
                result.append((node.lineno, base))
            result.extend((node.lineno, base + "." + alias.name) for alias in node.names)
    return result


def _violations(source: str, module: str, *, package: bool = False) -> list[str]:
    failures = []
    for line, target in _dependencies(source, module, package=package):
        if not _within(target, "pi_agent_py"):
            continue
        allowed = True
        if module in {"pi_agent_py.domain", "pi_agent_py._metadata"}:
            allowed = any(_within(target, name) for name in (
                "pi_agent_py.domain", "pi_agent_py._metadata",
            ))
        elif module == "pi_agent_py.ports":
            allowed = _within(target, "pi_agent_py.domain")
        elif _within(module, "pi_agent_py.runtime"):
            if target == "pi_agent_py" or any(_within(target, name) for name in (
                "pi_agent_py.api", "pi_agent_py.application", "pi_agent_py.testing",
            )):
                allowed = False
            elif _within(target, "pi_agent_py.providers"):
                allowed = _within(target, "pi_agent_py.providers.runtime")
            elif any(_within(target, name) for name in (
                "pi_agent_py.tools", "pi_agent_py.context",
            )):
                allowed = any(_within(target, name) for name in _DEFAULTS.get(module, set()))
            elif target.startswith("pi_agent_py."):
                # Root public objects are not an internal dependency surface.
                allowed = target.split(".")[1] in {
                    "runtime", "domain", "ports", "errors", "_metadata",
                    "plugins", "storage", "observability",
                }
        if not allowed:
            failures.append(f"{module}:{line} -> {target}")
    return failures


class ArchitectureTests(unittest.TestCase):
    def test_core_dependencies_respect_boundaries(self) -> None:
        paths = [_PACKAGE / name for name in ("domain.py", "_metadata.py", "ports.py")]
        paths.extend(sorted((_PACKAGE / "runtime").rglob("*.py")))
        failures = []
        for path in paths:
            parts = path.relative_to(_PACKAGE.parent).with_suffix("").parts
            package = parts[-1] == "__init__"
            module = ".".join(parts[:-1] if package else parts)
            failures.extend(_violations(path.read_text(encoding="utf-8-sig"), module, package=package))
        self.assertEqual(failures, [])

    def test_rejects_absolute_relative_type_only_and_local_imports(self) -> None:
        examples = (
            ("pi_agent_py.domain", "import pi_agent_py.runtime.loop"),
            ("pi_agent_py.ports", "from .providers.runtime import AttemptFailure"),
            ("pi_agent_py.ports", "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n from .runtime.session_spec import SessionSpec"),
            ("pi_agent_py.runtime.loop", "def f():\n from ..application.builder import AgentRuntimeBuilder"),
            ("pi_agent_py.runtime.loop", "from ..providers import deepseek"),
            ("pi_agent_py.runtime.loop", "from pi_agent_py import DeepSeekProvider"),
            ("pi_agent_py.runtime.loop", "import pi_agent_py.api as api"),
            ("pi_agent_py.runtime.loop", "import pi_agent_py"),
            ("pi_agent_py.runtime.loop", "from ..tools.function import FunctionTool"),
            ("pi_agent_py.runtime.components", "from ..tools.shell import BashTool"),
        )
        for module, source in examples:
            with self.subTest(module=module, source=source):
                self.assertTrue(_violations(source, module))

    def test_allows_contracts_and_exact_default_exceptions(self) -> None:
        examples = (
            ("pi_agent_py.ports", "from .domain import Message"),
            ("pi_agent_py.ports", "from . import domain"),
            ("pi_agent_py.runtime.loop", "from ..ports import ModelProvider"),
            ("pi_agent_py.runtime.loop", "from ..providers.runtime import DirectModelRuntime"),
            ("pi_agent_py.runtime.loop", "from ..providers import runtime"),
            ("pi_agent_py.runtime.components", "from ..tools.schema import SubsetSchemaValidator"),
            ("pi_agent_py.runtime.components", "from ..tools import schema"),
            ("pi_agent_py.runtime.compaction_orchestration", "from ..context.compaction import prepare_compaction"),
        )
        for module, source in examples:
            with self.subTest(module=module, source=source):
                self.assertEqual(_violations(source, module), [])

    def test_resolves_package_relative_imports(self) -> None:
        self.assertEqual(_violations(
            "from .loop import Agent", "pi_agent_py.runtime", package=True,
        ), [])
        self.assertTrue(_violations(
            "from ..application import builder", "pi_agent_py.runtime", package=True,
        ))


if __name__ == "__main__":
    unittest.main()
