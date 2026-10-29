from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from pi_agent_py import DockerProcessSandbox, SandboxProcessRequest


_DOCKER_IMAGE = os.environ.get("PI_AGENT_DOCKER_IMAGE")


@unittest.skipUnless(
    _DOCKER_IMAGE,
    "set PI_AGENT_DOCKER_IMAGE to run Docker isolation tests",
)
class DockerSandboxIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_workspace_write_and_escape_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            sandbox = DockerProcessSandbox(
                workspace,
                image=_DOCKER_IMAGE or "missing",
            )

            write_result = await sandbox.run(
                SandboxProcessRequest(
                    ("/bin/sh", "-lc", "printf 'inside' > result.txt"),
                    mode="workspace-write",
                )
            )
            escape_result = await sandbox.run(
                SandboxProcessRequest(
                    ("/bin/sh", "-lc", "touch /workspace/../escaped.txt"),
                    mode="workspace-write",
                )
            )
            read_only_result = await sandbox.run(
                SandboxProcessRequest(
                    ("/bin/sh", "-lc", "touch forbidden.txt"),
                    mode="read-only",
                )
            )

            self.assertEqual(write_result.exit_code, 0)
            self.assertEqual((workspace / "result.txt").read_text(), "inside")
            self.assertNotEqual(escape_result.exit_code, 0)
            self.assertFalse((workspace / "escaped.txt").exists())
            self.assertNotEqual(read_only_result.exit_code, 0)
            self.assertFalse((workspace / "forbidden.txt").exists())


if __name__ == "__main__":
    unittest.main()
