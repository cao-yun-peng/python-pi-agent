# ADR 0011: Evaluate Token Threshold Before Each Session Run

## Status

Accepted

## Context

Durable compaction can be invoked manually, but applications otherwise need to duplicate token checks and trigger logic. Trigger policy changes independently from summary generation and persistence. The incoming user prompt may itself cross the threshold, while the persisted history is the only history safe to replace before the run begins.

## Decision

- Add a synchronous `CompactionPolicy` port returning a validated `CompactionDecision`.
- Implement `TokenThresholdCompactionPolicy` using the existing replaceable `TokenEstimator`.
- Trigger when the candidate estimate is greater than or equal to the configured threshold.
- Evaluate once in `AgentSession.run()` after loading history and initializing the system message, but before constructing the inner `Agent`.
- Include the incoming prompt in the estimate, but compact only persisted complete turns.
- Require an automatic policy to be paired with a `CompactionStrategy`.
- Emit `compaction_evaluated`; emit `compaction_skipped` when the threshold is reached but no older complete turn can be replaced.
- Reuse the same summary generation, optimistic store write, durable event, and replay path as manual compaction.

## Alternatives

- Put the threshold inside `CompactionStrategy`: couples when and how, making milestone- or cost-based policies harder to add.
- Reuse `ContextStrategy`: would make an irreversible durable transition from a request-only projection capability.
- Evaluate only persisted history: misses a large incoming prompt that crosses the threshold.
- Commit the prompt before compaction: preserves input earlier, but requires a second Agent entry point to avoid committing the prompt twice.
- Evaluate before every model request in a tool loop: more complete, but requires a stable runtime hook that can replace both Agent memory and the store snapshot atomically.

## Consequences

- Existing sessions are unchanged unless both policy and strategy are configured.
- Automatic compaction failure occurs before the new user prompt is committed and prevents that run from starting.
- The default character estimator remains approximate and does not include tool definitions, provider envelopes, or output reserve.
- A single run can still exceed the threshold later because of large tool results; this limitation is explicit rather than hidden in the Agent loop.
- Policy decisions become observable evidence independent of whether compaction executes.

## Verification

- Policy tests cover below-threshold, threshold-equal, invalid estimate, and configuration validation.
- Runtime tests cover automatic durable compaction, no-trigger behavior, and no-compactable-history skip.
- Existing manual failure, cancellation, JSONL replay, fork, and concurrency tests continue to exercise the shared execution path.
- The example prints the decision payload, compacted Provider context, and event trace.
