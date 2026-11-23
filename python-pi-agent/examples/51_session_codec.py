"""Inspect the shared schema v6 boundary without files or model requests."""

import json

from pi_agent_py.domain import DurableSessionEvent, Message, ProviderState
from pi_agent_py.errors import SessionReplayError
from pi_agent_py.storage._codec import _decode_event, _encode_event


def main() -> None:
    event = DurableSessionEvent(
        "demo",
        1,
        "message_committed",
        Message(
            "assistant",
            "hello",
            thinking="plan",
            provider_state=(ProviderState("fake", {"id": "opaque"}),),
        ),
    )
    encoded = _encode_event(event)
    restored = _decode_event(encoded, 1)
    print(
        f"schema={json.loads(encoded)['schema_version']} identity={restored.event_id} round_trip={restored == event}"
    )
    try:
        _decode_event('{"schema_version":5}', 2)
    except SessionReplayError as error:
        print(f"rejected={type(error).__name__}: {error}")


if __name__ == "__main__":
    main()
