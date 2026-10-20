# ADR 0010: Durable Compaction as a Replayable Replacement Event

## Status

Accepted

## Context

Token budget projection can omit old turns from one model request but does not reduce the restored session state. A real compaction must preserve a semantic summary across process restarts without destructively rewriting the append-only event log. Summary generation may fail or be cancelled, and another writer may update the session while summarization is running.

## Decision

- Add a `CompactionStrategy` port that summarizes a caller-selected immutable message sequence.
- Select only a contiguous prefix of complete conversation turns after leading system messages, while keeping a configurable number of recent turns verbatim.
- Represent the durable change as one `context_compacted` event containing the replacement start, replaced message count, and a tagged user-role summary message.
- Apply compaction only through `replay_session()`; retain original message events in the log.
- Add `SessionStore.compact()` with optimistic `expected_version` checking and reuse the pure `prepare_session_compaction()` transition in both stores.
- Generate and validate the summary before writing. Failure and cancellation produce capability events but no durable event.
- Write JSONL schema v3 while retaining v1/v2 message-event decoding.
- Expose manual `AgentSession.compact()` first; defer automatic thresholds and retry policy.

## Alternatives

- Rewrite or delete old JSONL records: reduces disk usage but destroys auditability, complicates crash recovery, and breaks immutable history assumptions.
- Store only a new snapshot: loads quickly but introduces a second source of truth and requires snapshot/log consistency rules.
- Make compaction another `ContextStrategy`: simpler wiring, but its summary would not survive restart and could be regenerated inconsistently on every request.
- Summarize and persist in one Provider method: couples model I/O to storage and prevents deterministic replacement of either capability.
- Automatically compact on every budget failure immediately: convenient, but combines trigger policy, retry, cost, and irreversible summarization before the core transition is proven.

## Consequences

- Current messages can be shorter than the durable log, so session version is event count rather than message count.
- Replay remains the single state reconstruction path for in-memory and JSONL providers.
- Original content remains on disk; compaction reduces model context, not storage size or privacy exposure.
- Repeated compaction may summarize an earlier summary, which needs future quality evaluation.
- Summary generation can incur model cost in real profiles, but default tests remain deterministic and free.
- The current complete-turn boundary is conservative and cannot compact part of a very large turn.

## Verification

- Pure transition tests cover replacement projection, stale versions, and invalid ranges.
- Store tests cover append after compaction and JSONL reopen/replay.
- Runtime tests prove the next model request sees the summary and recent tail.
- Failure and cancellation tests prove no compaction event is appended.
- Model strategy tests cover tool-call rejection, deterministic input, and cancellation.
- The runnable example demonstrates JSONL persistence, restart, trace events, and continued conversation.
