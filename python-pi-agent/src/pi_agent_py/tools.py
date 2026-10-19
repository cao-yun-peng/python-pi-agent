"""Tool implementations, approval policies, and a small JSON Schema validator."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping

from .domain import (
    ApprovalDecision,
    JSONObject,
    JSONValue,
    ToolCall,
    ToolDefinition,
    ToolOutput,
)
from .errors import ToolValidationError

ToolHandler = Callable[[JSONObject], Awaitable[ToolOutput]]


class FunctionTool:
    """Adapts an async Python function to the Tool port."""

    def __init__(self, definition: ToolDefinition, handler: ToolHandler) -> None:
        self._definition = definition
        self._handler = handler

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(self, arguments: JSONObject) -> ToolOutput:
        return await self._handler(arguments)


class DenyByDefaultApprovalPolicy:
    """Secure default used when no interactive approval adapter is configured."""

    async def review(
        self,
        call: ToolCall,
        definition: ToolDefinition,
    ) -> ApprovalDecision:
        del call, definition
        return ApprovalDecision(
            approved=False,
            reason="No approval policy was configured for this protected tool.",
        )


class StaticApprovalPolicy:
    """Deterministic policy useful for tests and pre-authorized applications."""

    def __init__(self, approved: bool, *, reason: str | None = None) -> None:
        self._decision = ApprovalDecision(approved=approved, reason=reason)
        self.requests: list[tuple[ToolCall, ToolDefinition]] = []

    async def review(
        self,
        call: ToolCall,
        definition: ToolDefinition,
    ) -> ApprovalDecision:
        self.requests.append((call, definition))
        return self._decision


def validate_tool_arguments(
    schema: Mapping[str, JSONValue],
    arguments: JSONObject,
) -> None:
    """Validate the deliberately small JSON Schema subset used by this milestone.

    Supported keywords are `type`, `properties`, `required`,
    `additionalProperties`, `items`, and `enum`. Unsupported keywords are ignored
    until a later standards-compliance milestone.
    """
    violations: list[str] = []
    _validate_value(arguments, schema, path="$", violations=violations)
    if violations:
        raise ToolValidationError(tuple(violations))


def _validate_value(
    value: object,
    schema: Mapping[str, JSONValue],
    *,
    path: str,
    violations: list[str],
) -> None:
    expected = schema.get("type")
    if isinstance(expected, str) and not _matches_type(value, expected):
        violations.append(f"{path}: expected {expected}")
        return

    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        violations.append(f"{path}: value is not in enum")

    if expected == "object" and isinstance(value, dict):
        _validate_object(value, schema, path=path, violations=violations)
    elif expected == "array" and isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                _validate_value(
                    item,
                    items,
                    path=f"{path}[{index}]",
                    violations=violations,
                )


def _validate_object(
    value: dict[object, object],
    schema: Mapping[str, JSONValue],
    *,
    path: str,
    violations: list[str],
) -> None:
    properties_value = schema.get("properties", {})
    properties = properties_value if isinstance(properties_value, dict) else {}
    required_value = schema.get("required", [])
    required = required_value if isinstance(required_value, list) else []

    for name in required:
        if isinstance(name, str) and name not in value:
            violations.append(f"{path}.{name}: required property is missing")

    allow_additional = schema.get("additionalProperties", True) is not False
    for name, item in value.items():
        if not isinstance(name, str):
            violations.append(f"{path}: object keys must be strings")
            continue
        property_schema = properties.get(name)
        if isinstance(property_schema, dict):
            _validate_value(
                item,
                property_schema,
                path=f"{path}.{name}",
                violations=violations,
            )
        elif not allow_additional:
            violations.append(f"{path}.{name}: additional property is not allowed")


def _matches_type(value: object, expected: str) -> bool:
    checks: dict[str, Callable[[object], bool]] = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: (
            isinstance(item, (int, float)) and not isinstance(item, bool)
        ),
        "boolean": lambda item: isinstance(item, bool),
        "null": lambda item: item is None,
    }
    check = checks.get(expected)
    return True if check is None else check(value)
