"""Explicit local schema validators, with an optional offline Draft 2020-12 backend."""

from __future__ import annotations

import json
from collections import OrderedDict
from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from ..domain import JSONObject, JSONValue
from ..errors import ConfigurationError, ToolValidationError
from .validation import validate_tool_arguments

if TYPE_CHECKING:
    from jsonschema.protocols import Validator
    from referencing import Registry, Resource
    from referencing._core import Resolver

_DIALECT = "https://json-schema.org/draft/2020-12/schema"
_TYPES = {"string", "integer", "number", "boolean", "null", "object", "array"}
_ANNOTATIONS = {
    "title",
    "description",
    "default",
    "examples",
    "$comment",
    "deprecated",
    "readOnly",
    "writeOnly",
}
_SUBSET = {
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "$schema",
} | _ANNOTATIONS


class SubsetSchemaValidator:
    """Preserve legacy validation while honestly reporting its limited profile."""

    def supports(self, schema: Mapping[str, JSONValue]) -> bool:
        try:
            return _supports_subset(_snapshot(schema))
        except (TypeError, ValueError, RecursionError):
            return False

    def validate(self, schema: Mapping[str, JSONValue], arguments: JSONObject) -> None:
        # Deliberately keep ignoring unsupported keywords on this legacy path.
        validate_tool_arguments(schema, arguments)


def _supports_subset(schema: JSONObject) -> bool:
    if not schema.keys() <= _SUBSET or schema.get("$schema", _DIALECT) != _DIALECT:
        return False
    expected = schema.get("type")
    types = [expected] if isinstance(expected, str) else expected
    if "type" in schema and (
        not isinstance(types, list)
        or not types
        or any(not isinstance(t, str) or t not in _TYPES for t in types)
        or len(set(cast(list[str], types))) != len(types)
    ):
        return False
    type_names = cast(list[str], types) if isinstance(types, list) else None
    for keyword in ("properties", "required", "additionalProperties"):
        if keyword in schema and (not isinstance(types, list) or "object" not in types):
            return False
    properties = schema.get("properties", {})
    if not isinstance(properties, dict) or any(
        not isinstance(value, dict) or not _supports_subset(value)
        for value in properties.values()
    ):
        return False
    required = schema.get("required", [])
    if not isinstance(required, list) or any(not isinstance(v, str) for v in required):
        return False
    if len(set(cast(list[str], required))) != len(required):
        return False
    if type(schema.get("additionalProperties", True)) is not bool:
        return False
    if "items" in schema:
        if not isinstance(types, list) or "array" not in types:
            return False
        items = schema["items"]
        if not isinstance(items, dict) or not _supports_subset(items):
            return False
    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, list) or not enum:
            return False
        # Python equality conflates bool/numbers and nested JSON containers.
        # Keep legacy validation unchanged, but never advertise those ambiguous cases.
        for value in enum:
            if isinstance(value, (dict, list)):
                return False
            if type(value) is bool and (
                type_names is None or "number" in type_names or "integer" in type_names
            ):
                return False
            if type(value) in (int, float) and (
                type_names is None or "boolean" in type_names
            ):
                return False
        if any(enum[i] == enum[j] for i in range(len(enum)) for j in range(i)):
            return False
    for keyword in ("title", "description", "$comment"):
        if keyword in schema and not isinstance(schema[keyword], str):
            return False
    for keyword in ("deprecated", "readOnly", "writeOnly"):
        if keyword in schema and type(schema[keyword]) is not bool:
            return False
    return "examples" not in schema or isinstance(schema["examples"], list)


class JsonSchemaValidator:
    """Offline Draft 2020-12; format is annotation, unknown assertions are rejected.

    Explicitly supplied resources are copied and never fetched. Compilation is
    cached in a bounded instance-owned cache, not a process-global registry.
    """

    def __init__(
        self, *, resources: Mapping[str, JSONObject] | None = None, cache_size: int = 32
    ) -> None:
        if type(cache_size) is not int or cache_size <= 0:
            raise ValueError("cache_size must be a positive integer")
        try:
            from jsonschema import Draft202012Validator
            from referencing import Registry, Resource
            from referencing.jsonschema import DRAFT202012
        except ImportError as error:
            raise ConfigurationError(
                "JsonSchemaValidator requires pi-agent-py[jsonschema]"
            ) from error

        self._backend = Draft202012Validator
        self._specification = DRAFT202012
        # An explicit empty Registry uses referencing's failing retriever,
        # instead of jsonschema's backward-compatible network retrieval.
        self._registry: Registry[JSONValue] = Registry()
        self._cache_size = cache_size
        self._cache: OrderedDict[str, Validator] = OrderedDict()
        try:
            for uri, schema in (resources or {}).items():
                if not isinstance(uri, str) or not uri or "#" in uri:
                    raise ValueError("resource URI must be nonempty and fragment-free")
                resource = Resource.from_contents(
                    _snapshot(schema), default_specification=DRAFT202012
                )
                self._registry = self._registry.with_resource(uri, resource)
            self._registry = self._registry.crawl()
            for uri in self._registry:
                resource = self._registry[uri]
                self._check_profile(resource, self._registry.resolver(uri), set())
        except Exception as error:
            raise ConfigurationError("invalid local JSON Schema resources") from error

    def supports(self, schema: Mapping[str, JSONValue]) -> bool:
        try:
            self._compile(schema)
        except ConfigurationError:
            return False
        return True

    def validate(self, schema: Mapping[str, JSONValue], arguments: JSONObject) -> None:
        validator = self._compile(schema)
        try:
            instance = _snapshot(arguments)
        except (TypeError, ValueError, RecursionError) as error:
            raise ToolValidationError(("$: arguments must be finite JSON",)) from error
        violations = []
        try:
            for violation in validator.iter_errors(instance):
                # Neither validation messages nor instance paths are safe: both can contain secrets.
                violations.append(
                    f"$: JSON Schema assertion failed ({violation.validator})"
                )
                if len(violations) == 8:
                    break
        except Exception as error:
            raise ConfigurationError("JSON Schema evaluation failed") from error
        if violations:
            raise ToolValidationError(tuple(violations))

    def _compile(self, schema: Mapping[str, JSONValue]) -> Validator:
        from referencing import Resource

        try:
            snapshot = _snapshot(schema)
            key = json.dumps(
                snapshot, sort_keys=True, allow_nan=False, separators=(",", ":")
            )
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
            resource = Resource.from_contents(
                snapshot, default_specification=self._specification
            )
            registry = self._registry.with_resource("", resource).crawl()
            self._check_profile(resource, registry.resolver_with_root(resource), set())
            validator = self._backend(snapshot, registry=registry)
        except Exception as error:
            raise ConfigurationError("unsupported or invalid JSON Schema") from error
        self._cache[key] = validator
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return validator

    def _check_profile(
        self,
        resource: Resource[JSONValue],
        resolver: Resolver[JSONValue],
        seen: set[int],
    ) -> None:
        from referencing import Resource

        node = resource.contents
        if id(node) in seen:
            return
        seen.add(id(node))
        if isinstance(node, bool):
            return
        if not isinstance(node, dict):
            raise ValueError("expected a schema object")
        self._backend.check_schema(node)
        known = (
            set(self._backend.VALIDATORS)
            | _ANNOTATIONS
            | {
                "$schema",
                "$id",
                "$defs",
                "$anchor",
                "$dynamicAnchor",
                "$vocabulary",
                "format",
                "contentEncoding",
                "contentMediaType",
                "contentSchema",
            }
        )
        if not node.keys() <= known or node.get("$schema", _DIALECT) != _DIALECT:
            raise ValueError("unsupported schema profile")
        vocabularies = node.get("$vocabulary", {})
        if not isinstance(vocabularies, dict):
            raise ValueError("invalid vocabulary declaration")
        for uri, required in vocabularies.items():
            supported = {
                "core",
                "applicator",
                "unevaluated",
                "validation",
                "meta-data",
                "format-annotation",
                "content",
            }
            if type(required) is not bool or (
                required
                and uri
                not in {
                    f"https://json-schema.org/draft/2020-12/vocab/{v}"
                    for v in supported
                }
            ):
                raise ValueError("unsupported required vocabulary")
        for keyword in ("$ref", "$dynamicRef"):
            if keyword in node:
                ref = node[keyword]
                if not isinstance(ref, str):
                    raise ValueError("invalid reference")
                resolved = resolver.lookup(ref)
                self._check_profile(
                    Resource.from_contents(
                        resolved.contents, default_specification=self._specification
                    ),
                    resolved.resolver,
                    seen,
                )
        for child in resource.subresources():
            self._check_profile(child, resolver.in_subresource(child), seen)


def _snapshot(value: Mapping[str, JSONValue]) -> JSONObject:
    def check(item: object) -> None:
        if item is None or type(item) in (str, int, float, bool):
            return
        if isinstance(item, list):
            for child in item:
                check(child)
        elif isinstance(item, dict) and all(isinstance(key, str) for key in item):
            for child in item.values():
                check(child)
        else:
            raise TypeError("expected finite JSON")

    data = dict(value)
    check(data)
    return cast(JSONObject, json.loads(json.dumps(data, allow_nan=False)))
