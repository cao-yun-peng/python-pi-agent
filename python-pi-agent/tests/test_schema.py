"""Offline profile discovery and full JSON Schema contract tests."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from unittest.mock import patch

from pi_agent_py.api import (
    ConfigurationError,
    JsonSchemaValidator,
    SubsetSchemaValidator,
    ToolValidationError,
)


class SubsetSchemaTests(unittest.TestCase):
    def test_default_compatibility_and_honest_supports(self):
        validator = SubsetSchemaValidator()
        # Legacy validation still ignores pattern; supports never promises to enforce it.
        schema = {
            "type": "object",
            "properties": {"value": {"type": "string", "pattern": "^safe$"}},
        }
        self.assertFalse(validator.supports(schema))
        validator.validate(schema, {"value": "anything"})
        supported = {
            "type": "object",
            "properties": {
                "items": {"type": ["array", "null"], "items": {"type": "integer"}},
                "mode": {"type": "string", "enum": ["head", "tail"], "default": "head"},
            },
            "required": ["items"],
            "additionalProperties": False,
        }
        self.assertTrue(validator.supports(supported))
        validator.validate(supported, {"items": None})
        with self.assertRaises(ToolValidationError):
            validator.validate(supported, {"items": [True]})

    def test_subset_rejects_unknown_assertions_and_malformed_shapes(self):
        cases = [
            {"anyOf": []},
            {"$ref": "#"},
            {"minimum": 1},
            {"type": "mystery"},
            {"type": []},
            {"type": None},
            {"type": ["object", "object"]},
            {"type": ["object", True]},
            {"required": ["a"]},
            {"items": {"type": "string"}},
            {"type": "object", "additionalProperties": {"type": "string"}},
            {"type": "object", "required": [False]},
            {"type": "object", "properties": {"a": False}},
            {"enum": []},
            {"enum": [1, True]},
            {"type": "boolean", "enum": [1]},
            {"description": 3},
            {"examples": "bad"},
            {"$schema": "unknown"},
        ]
        for schema in cases:
            with self.subTest(schema=schema):
                self.assertFalse(SubsetSchemaValidator().supports(schema))

    def test_optional_dependencies_are_not_imported_by_public_api(self):
        code = """
import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'jsonschema', 'referencing', 'mcp', 'mcp_types', 'httpx2'}:
            raise ModuleNotFoundError('blocked for optional-dependency test')
sys.meta_path.insert(0, Block())
from pi_agent_py.api import AgentComponents, SubsetSchemaValidator, JsonSchemaValidator, ConfigurationError
from pi_agent_py.testing import FakeMcpConnector, FakeMcpEndpoint
from pi_agent_py.tools.mcp.plugin import mcp_plugin
from pi_agent_py.tools.mcp.tool import McpTool
assert not {'jsonschema', 'referencing', 'mcp', 'mcp_types', 'httpx2'} & sys.modules.keys()
try:
    JsonSchemaValidator()
except ConfigurationError as error:
    assert '[jsonschema]' in str(error)
    assert isinstance(error.__cause__, ImportError)
else:
    raise AssertionError('missing dependency was ignored')
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", code],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


@unittest.skipUnless(
    importlib.util.find_spec("jsonschema"),
    "install [jsonschema] for full validator tests",
)
class JsonSchemaTests(unittest.TestCase):
    def test_complex_assertions_and_internal_references(self):
        schema = {
            "$defs": {"code": {"type": "string", "pattern": "^[A-Z]{2}$"}},
            "type": "object",
            "required": ["code", "choice", "count"],
            "properties": {
                "code": {"$ref": "#/$defs/code"},
                "choice": {
                    "anyOf": [{"type": "null"}, {"type": "integer", "minimum": 2}]
                },
                "count": {"type": "integer", "maximum": 5},
            },
            "additionalProperties": False,
        }
        validator = JsonSchemaValidator()
        self.assertTrue(validator.supports(schema))
        validator.validate(schema, {"code": "AB", "choice": None, "count": 2.0})
        for arguments in (
            {"code": "bad", "choice": 1, "count": 7},
            {"choice": None, "count": 1},
            {"code": "AB", "choice": True, "count": 1},
        ):
            with (
                self.subTest(arguments=arguments),
                self.assertRaises(ToolValidationError),
            ):
                validator.validate(schema, arguments)

    def test_offline_resources_and_anchors(self):
        resources = {
            "https://schemas.example.test/item": {"type": "integer", "minimum": 3}
        }
        validator = JsonSchemaValidator(resources=resources)
        resources["https://schemas.example.test/item"]["minimum"] = 100
        schema = {
            "type": "object",
            "properties": {"value": {"$ref": "https://schemas.example.test/item"}},
        }
        self.assertTrue(validator.supports(schema))
        validator.validate(schema, {"value": 3})
        with self.assertRaises(ToolValidationError):
            validator.validate(schema, {"value": 2})
        anchored = {
            "$defs": {"code": {"$anchor": "code", "type": "string"}},
            "type": "object",
            "properties": {"value": {"$ref": "#code"}},
        }
        self.assertTrue(validator.supports(anchored))
        validator.validate(anchored, {"value": "yes"})

    def test_external_and_missing_refs_fail_without_io(self):
        validator = JsonSchemaValidator()
        with (
            patch(
                "urllib.request.urlopen",
                side_effect=AssertionError("network forbidden"),
            ),
            patch(
                "socket.create_connection",
                side_effect=AssertionError("network forbidden"),
            ),
        ):
            for ref in (
                "https://server.invalid/schema",
                "file:///private.json",
                "#/$defs/missing",
            ):
                schema = {"type": "object", "properties": {"unused": {"$ref": ref}}}
                self.assertFalse(validator.supports(schema))
                with self.assertRaises(ConfigurationError) as caught:
                    validator.validate(schema, {})
                self.assertIsNotNone(caught.exception.__cause__)

    def test_profile_and_schema_errors_are_not_argument_errors(self):
        validator = JsonSchemaValidator()
        for schema in (
            {"type": "bad"},
            {"required": "bad"},
            {"type": "object", "anyOf": []},
            {"$schema": "http://json-schema.org/draft-07/schema#"},
            {"$vocabulary": {"https://unknown.invalid/vocab": True}},
            {
                "$vocabulary": {
                    "https://json-schema.org/draft/2020-12/vocab/format-assertion": True
                }
            },
            {"unknownAssertion": True},
        ):
            with self.subTest(schema=schema):
                self.assertFalse(validator.supports(schema))
                with self.assertRaises(ConfigurationError):
                    validator.validate(schema, {})
        # Reference-looking data in annotations is not a schema to fetch or execute.
        annotation = {
            "type": "object",
            "default": {"$ref": "https://not-a-schema.invalid"},
            "properties": {"email": {"type": "string", "format": "email"}},
        }
        self.assertTrue(validator.supports(annotation))
        validator.validate(annotation, {"email": "not an email"})

    def test_snapshots_errors_and_instance_cache_are_safe(self):
        resource = {"type": "string", "const": "original"}
        frozen = JsonSchemaValidator(resources={"urn:example:local": resource})
        resource["const"] = "changed"
        frozen.validate(
            {"type": "object", "properties": {"value": {"$ref": "urn:example:local"}}},
            {"value": "original"},
        )
        validator = JsonSchemaValidator(cache_size=1)
        schema = {"type": "object", "properties": {"value": {"const": "allowed"}}}
        self.assertTrue(validator.supports(schema))
        schema["properties"]["value"]["const"] = "new"
        validator.validate(schema, {"value": "new"})
        with self.assertRaises(ToolValidationError) as caught:
            validator.validate(schema, {"value": "secret-credential"})
        self.assertNotIn("secret-credential", str(caught.exception))
        self.assertNotIn("new", str(caught.exception))
        for bad in (float("nan"), object()):
            self.assertFalse(validator.supports({"default": bad}))
            with self.assertRaises(ToolValidationError):
                validator.validate({"type": "object"}, {"bad": bad})
        with self.assertRaises(ValueError):
            JsonSchemaValidator(cache_size=True)
        with self.assertRaises(ConfigurationError):
            JsonSchemaValidator(resources={"invalid#anchor": {}})
