# Compatibility lifecycle

OpenOPC keeps wire and checkpoint compatibility only when it has an explicit
owner and removal target. New code must use the canonical surface in this
table; compatibility paths must not gain new behavior.

| Compatibility surface | Canonical surface | Removal target |
|---|---|---|
| `TaskRouter` | Explicit user/session execution metadata | 0.3 |
| `ExecutionMode.PROJECT_MODE` and `SINGLE_AGENT` | `ExecutionMode.TASK_MODE` | 0.3 |
| `ExecutionMode.MULTI_AGENT` | `ExecutionMode.COMPANY_MODE` | 0.3 |
| `_execute_multi_agent` checkpoint entrypoint | Company WorkItem executor | 0.2 |
| Unversioned Office UI WebSocket envelopes | `protocol_version: 1` | 0.3 |
| `blocking` collaboration argument | Receipt/status-based protocol | 0.3 |

`org` remains a supported public selector for a saved custom organization; it
is normalized to Company execution with `company_profile=custom` and is not a
deprecated synonym. Persisted historical values continue to be read through
the release in which their compatibility surface is removed.

Before a removal target is implemented, search checkpoints, persisted fixture
databases, CLI help, and frontend payloads in addition to Python call sites.
Every removal must include a migration note and a rejection test for the old
write path.
