"""Derive a Tool contract from an explicitly assembled async function."""

from __future__ import annotations

import inspect
import json
import re
import types
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Annotated, Any, Literal, Union, cast, get_args, get_origin, get_type_hints, overload

from ..domain import JSONObject, JSONValue, ToolDefinition, ToolExecutionContext, ToolOutput, ToolSideEffects

ToolFunction = Callable[..., Awaitable[object]]


class ToolError(Exception):
    """An error the tool author explicitly permits the model to see."""

    def __init__(self, message: str, *, structured_content: JSONValue = None) -> None:
        super().__init__(message)
        self.structured_content = structured_content


class DecoratedTool:
    """Tool adapter retaining the original coroutine as fn and __wrapped__."""

    def __init__(
        self, fn: ToolFunction, definition: ToolDefinition,
        annotations: dict[str, object], context_parameter: str | None,
    ) -> None:
        self.fn = fn
        self.__wrapped__ = fn
        self._definition = definition
        self._annotations = annotations
        self._context_parameter = context_parameter

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(self, arguments: JSONObject, context: ToolExecutionContext) -> ToolOutput:
        # Validation belongs to the runtime. Python binds missing/default args.
        kwargs = {
            name: _coerce(self._annotations.get(name), value)
            for name, value in arguments.items()
        }
        if self._context_parameter is not None:
            kwargs[self._context_parameter] = context
        try:
            value = await self.fn(**kwargs)
        except ToolError as error:
            return ToolOutput(str(error), error.structured_content, is_error=True)
        if isinstance(value, ToolOutput):
            return value
        if isinstance(value, str):
            return ToolOutput(value)
        if value is None:
            return ToolOutput("")
        try:
            _require_json(value)
        except (TypeError, ValueError, RecursionError) as error:
            raise TypeError("tool return must be a JSON value") from error
        return ToolOutput(
            json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False),
            cast(JSONValue, value),
        )


@overload
def tool(fn: ToolFunction, *, name: str | None = None, description: str | None = None,
         side_effects: ToolSideEffects = "none", requires_approval: bool = False,
         retry_safe: bool = False, replay_safe: bool = False,
         timeout_seconds: float | None = None) -> DecoratedTool: ...


@overload
def tool(fn: None = None, *, name: str | None = None, description: str | None = None,
         side_effects: ToolSideEffects = "none", requires_approval: bool = False,
         retry_safe: bool = False, replay_safe: bool = False,
         timeout_seconds: float | None = None) -> Callable[[ToolFunction], DecoratedTool]: ...


def tool(
    fn: ToolFunction | None = None, *, name: str | None = None,
    description: str | None = None, side_effects: ToolSideEffects = "none",
    requires_approval: bool = False, retry_safe: bool = False,
    replay_safe: bool = False, timeout_seconds: float | None = None,
) -> DecoratedTool | Callable[[ToolFunction], DecoratedTool]:
    """Create a tool value; no registration or I/O occurs during decoration."""
    def decorate(function: ToolFunction) -> DecoratedTool:
        tool_name = name if name is not None else function.__name__
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tool_name):
            raise ValueError(f"tool {tool_name!r}: invalid name")
        if not inspect.iscoroutinefunction(function):
            raise TypeError(f"tool {tool_name}: requires an async coroutine function")
        doc = inspect.getdoc(function) or ""
        summary = description if description is not None else re.split(r"\n\s*\n", doc)[0]
        if not summary.strip():
            raise ValueError(f"tool {tool_name}: description must not be empty")
        signature = inspect.signature(function)
        # Keep the recursive JSONObject alias opaque to schema derivation.
        raw_annotations = {
            key: dict[str, object] if value == JSONObject else value
            for key, value in function.__annotations__.items()
        }
        holder = types.SimpleNamespace(__annotations__=raw_annotations)
        localns = {"JSONObject": dict[str, object], "JSONValue": JSONValue}
        try:
            hints = get_type_hints(holder, function.__globals__, localns, include_extras=True)
        except Exception as error:
            # Identify the failing annotation without evaluating strings ourselves.
            for parameter in signature.parameters.values():
                try:
                    holder = types.SimpleNamespace(__annotations__={parameter.name: raw_annotations.get(parameter.name)})
                    get_type_hints(holder, function.__globals__, localns, include_extras=True)
                except Exception:
                    raise TypeError(f"tool {tool_name} parameter {parameter.name}: cannot resolve annotation") from error
            raise TypeError(f"tool {tool_name} return: cannot resolve annotation") from error
        properties: JSONObject = {}
        required: list[JSONValue] = []
        annotations: dict[str, object] = {}
        context_parameter = None
        for parameter in signature.parameters.values():
            label = f"tool {tool_name} parameter {parameter.name}"
            if parameter.kind not in (parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY):
                raise TypeError(f"{label}: unsupported parameter kind")
            if parameter.name not in hints:
                raise TypeError(f"{label}: annotation required")
            annotation = hints[parameter.name]
            annotations[parameter.name] = annotation
            if annotation is ToolExecutionContext:
                if context_parameter is not None:
                    raise TypeError(f"{label}: at most one context parameter is allowed")
                context_parameter = parameter.name
                continue
            try:
                schema = _schema(annotation)
                if parameter.default is not parameter.empty:
                    _require_json(parameter.default)
                    schema["default"] = deepcopy(parameter.default)
                else:
                    required.append(parameter.name)
            except (TypeError, ValueError, RecursionError) as error:
                raise TypeError(f"{label}: unsupported annotation or default") from error
            properties[parameter.name] = schema
        definition = ToolDefinition(
            tool_name, summary.strip(),
            {"type": "object", "properties": properties, "required": required, "additionalProperties": False},
            side_effects=side_effects, requires_approval=requires_approval,
            retry_safe=retry_safe, replay_safe=replay_safe, timeout_seconds=timeout_seconds,
        )
        return DecoratedTool(function, definition, annotations, context_parameter)
    return decorate if fn is None else decorate(fn)


def _schema(annotation: object) -> JSONObject:
    primitives = {str: "string", int: "integer", float: "number", bool: "boolean"}
    if isinstance(annotation, type) and annotation in primitives:
        return {"type": primitives[annotation]}
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Annotated:
        schema = _schema(args[0])
        descriptions = [item for item in args[1:] if isinstance(item, str)]
        if descriptions:
            schema["description"] = descriptions[0]
        return schema
    if origin in (Union, types.UnionType) and len(args) == 2 and type(None) in args:
        schema = _schema(next(item for item in args if item is not type(None)))
        schema["type"] = [schema["type"], "null"]
        if "enum" in schema:
            schema["enum"] = [*cast(list[JSONValue], schema["enum"]), None]
        return schema
    if origin is Literal and args and type(args[0]) in (str, int, bool):
        if all(type(item) is type(args[0]) for item in args):
            return {"type": primitives[type(args[0])], "enum": list(args)}
    if origin is list and len(args) == 1:
        return {"type": "array", "items": _schema(args[0])}
    if annotation == JSONObject or (origin is dict and args in ((str, object), (str, Any))):
        return {"type": "object"}
    raise TypeError("unsupported tool parameter type")


def _coerce(annotation: object, value: object) -> object:
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Annotated:
        return _coerce(args[0], value)
    if origin in (Union, types.UnionType) and value is not None:
        return _coerce(next(item for item in args if item is not type(None)), value)
    if annotation is float and type(value) is int:
        return float(value)
    if origin is list and isinstance(value, list):
        return [_coerce(args[0], item) for item in value]
    return value


def _require_json(value: object) -> None:
    if value is None or type(value) in (str, int, float, bool):
        json.dumps(value, allow_nan=False)
    elif isinstance(value, list):
        for item in value:
            _require_json(item)
    elif isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _require_json(item)
    else:
        raise TypeError("expected a JSON value")
