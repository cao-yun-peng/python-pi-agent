"""Detached JSON mappings for domain contracts and observation context."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from .domain import JSONValue


class _Metadata(Mapping[str, "JSONValue"]):
    """Keep encoded values so nested values returned to callers are detached."""

    def __init__(self, values: Mapping[str, JSONValue]) -> None:
        encoded: dict[str, str] = {}
        try:
            for key, value in values.items():
                if not isinstance(key, str):
                    raise ValueError("metadata keys must be strings")
                _validate_json(value)
                encoded[key] = json.dumps(value, allow_nan=False, ensure_ascii=False)
        except (RecursionError, TypeError, ValueError) as error:
            raise ValueError(
                "metadata must contain finite JSON values with string keys"
            ) from error
        self._encoded = MappingProxyType(encoded)

    def __getitem__(self, key: str) -> JSONValue:
        return cast("JSONValue", json.loads(self._encoded[key]))

    def __iter__(self) -> Iterator[str]:
        return iter(self._encoded)

    def __len__(self) -> int:
        return len(self._encoded)

    def __deepcopy__(self, memo: dict[int, object]) -> _Metadata:
        return self


def _validate_json(value: object) -> None:
    if value is None or isinstance(value, (str, bool, int, float)):
        return
    if isinstance(value, list):
        for item in value:
            _validate_json(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("metadata keys must be strings")
            _validate_json(item)
        return
    raise ValueError("unsupported metadata value")


def _freeze_metadata(values: Mapping[str, JSONValue]) -> Mapping[str, JSONValue]:
    if isinstance(values, _Metadata):
        return values
    if not isinstance(values, Mapping):
        raise ValueError("metadata must be a mapping")
    return _Metadata(values)
