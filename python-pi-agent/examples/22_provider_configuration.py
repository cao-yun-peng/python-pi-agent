"""Load, inspect, and explicitly update top-level provider configuration.

From the repository root:

    Copy-Item agent.yaml.example agent.yaml
    python python-pi-agent/examples/22_provider_configuration.py set-key
    python python-pi-agent/examples/22_provider_configuration.py check
    python python-pi-agent/examples/22_provider_configuration.py chat

``check`` and ``set-key`` never call a model. ``chat`` uses the configured
provider and may incur API charges.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
from pathlib import Path

from pi_agent_py import (
    AgentProfile,
    AgentRuntimeBuilder,
    AgentEvent,
    CallbackEventSink,
    ConfigurationError,
    default_provider_catalog,
    format_config_diagnostics,
    load_app_config,
    parse_app_config,
    write_env_secret,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage explicit YAML + dotenv provider configuration.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=REPOSITORY_ROOT / "agent.yaml",
        help="Explicit YAML path; parent directories are never searched.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=REPOSITORY_ROOT / ".env",
        help="Explicit dotenv path; loading does not mutate os.environ.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("check", help="Validate and print masked diagnostics.")
    write = subcommands.add_parser(
        "set-key",
        help="Prompt securely and atomically write the configured API-key variable.",
    )
    write.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing non-empty value.",
    )
    subcommands.add_parser("chat", help="Start a live chat using the loaded config.")
    return parser


def set_key(config_path: Path, env_path: Path, *, force: bool) -> None:
    spec = parse_app_config(config_path)
    variable = spec.provider.api_key_env
    first = getpass.getpass(f"输入 {variable}（输入不会回显）: ")
    second = getpass.getpass("再次输入以确认: ")
    if first != second:
        raise ConfigurationError("两次输入的 API key 不一致，未写入任何内容")
    result = write_env_secret(env_path, variable, first, overwrite=force)
    action = "已替换" if result.replaced else "已写入"
    print(f"{action} {result.variable} -> {result.path}（密钥未显示）")


async def chat(config_path: Path, env_path: Path) -> None:
    loaded = load_app_config(config_path, env_path=env_path)
    print(format_config_diagnostics(loaded))

    def observe(event: AgentEvent) -> None:
        if event.kind == "text_delta":
            print(event.payload["text"], end="", flush=True)

    runtime = await (
        AgentRuntimeBuilder.from_loaded_config(
            loaded,
            provider_catalog=default_provider_catalog(),
        )
        .with_profile(
            AgentProfile(
                name="assistant",
                system_prompt="你是一个简洁、可靠的中文助手。",
            )
        )
        .with_event_sink(CallbackEventSink(observe))
        .build()
    )

    async with runtime:
        print("\nAgent 已启动，输入 /exit 退出。")
        while True:
            prompt = (await asyncio.to_thread(input, "\n你> ")).strip()
            if prompt in {"/exit", "/quit"}:
                return
            if not prompt:
                continue
            print("助手> ", end="", flush=True)
            result = await runtime.run("provider-config-chat", prompt)
            if result.usage is not None:
                print(
                    f"\n[tokens: {result.usage.input_tokens} + "
                    f"{result.usage.output_tokens} = {result.usage.total_tokens}]"
                )


def main() -> None:
    args = build_parser().parse_args()
    try:
        if args.command == "set-key":
            set_key(args.config, args.env_file, force=args.force)
        elif args.command == "check":
            loaded = load_app_config(args.config, env_path=args.env_file)
            print(format_config_diagnostics(loaded))
        else:
            asyncio.run(chat(args.config, args.env_file))
    except ConfigurationError as error:
        raise SystemExit(f"配置错误：{error}") from error


if __name__ == "__main__":
    main()
