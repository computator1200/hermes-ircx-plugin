# Core patch: per-message tool scoping

The IRCX plugin's per-channel / per-sender tool scoping
(`groups.<chan>.tools` / `tools_by_sender`) is enforced by a small, generic,
backward-compatible hook in two core Hermes files. Any platform adapter can
use it - it is not IRC-specific.

These two files live in the Hermes repo (on the VPS at
`~/.hermes/hermes-agent/`), not in this plugin directory. They are recorded
here so the change is reviewable and upstreamable. Backups were saved to
`/tmp/base.py.bak` and `/tmp/run.py.bak` before editing.

## 1. `gateway/platforms/base.py` - new `MessageEvent` field

Added after `internal: bool = False` in the `MessageEvent` dataclass:

```python
    # Optional per-message allowlist of toolset names attached by a
    # platform adapter for per-channel / per-sender tool scoping (e.g.
    # IRC groups.<chan>.tools / tools_by_sender).  When set and not the
    # wildcard ["*"], gateway/run.py narrows this turn's enabled
    # toolsets to the intersection.  See _apply_tool_scope.
    tool_scope: Optional[list] = None
```

Backward compatible: defaults to `None` (no behavior change for any
existing adapter).

## 2. `gateway/run.py` - generic enforcement helper + call site

New module-level helper (inserted just before `_telegramize_command_mentions`):

```python
def _apply_tool_scope(enabled_toolsets, tool_scope):
    """Narrow ``enabled_toolsets`` to a per-message allowlist.

    ``tool_scope`` (``MessageEvent.tool_scope``) is an optional list of
    toolset names a platform adapter attached to this turn (e.g. IRC
    per-channel ``tools`` / per-sender ``tools_by_sender``).  A falsy scope
    or one containing the wildcard ``"*"`` means "no restriction".
    Otherwise the result is the intersection (order preserved); an empty
    intersection means no native toolsets for this turn.
    """
    if not tool_scope or "*" in tool_scope:
        return enabled_toolsets
    scope = {str(t) for t in tool_scope}
    return [t for t in enabled_toolsets if t in scope]
```

Applied inside `_run_agent`, immediately after the existing
`enabled_toolsets`/`disabled_toolsets` resolution:

```python
        enabled_toolsets = sorted(_get_platform_tools(user_config, platform_key))
        agent_cfg_local = user_config.get("agent") or {}
        disabled_toolsets = agent_cfg_local.get("disabled_toolsets") or None
        # Per-channel / per-sender tool scoping attached by the platform
        # adapter (e.g. IRCX groups.<chan>.tools / tools_by_sender).
        enabled_toolsets = _apply_tool_scope(
            enabled_toolsets, getattr(message, "tool_scope", None)
        )
```

`enabled_toolsets` is already part of the per-session agent-cache signature
(`_agent_config_signature`), so narrowing it per message correctly produces a
distinct cached agent when the scope differs (e.g. a restricted sender vs an
unrestricted one in the same channel) - no stale-tool leakage.

## Semantics

- Entries are Hermes **toolset names** (e.g. `hermes-cli`, `web`, `memory`),
  not individual tool names - Hermes groups tools into toolsets, and the
  agent only accepts toolset-level enable/disable.
- `["*"]` or `None` = unrestricted.
- `tools_by_sender[<identity>]` (matched on the verified account / nick,
  case-insensitive) overrides the channel-wide `tools`.
- An empty list is treated as unrestricted (to avoid a misconfig silently
  removing every tool); use a scope of real toolset names to restrict.

## Tests

`tests/gateway/test_ircx_adapter.py`:
- `TestToolScopeCoreHelper` - the generic `_apply_tool_scope` (intersection,
  wildcard, none, order preservation) and the `MessageEvent.tool_scope` field.
- `TestAdapterToolScope` - the adapter attaches the correct scope per
  channel `tools`, per-sender `tools_by_sender` (with override + fallback),
  wildcard, no-config, and DM cases.

## Reverting

```bash
cp /tmp/base.py.bak ~/.hermes/hermes-agent/gateway/platforms/base.py
cp /tmp/run.py.bak  ~/.hermes/hermes-agent/gateway/run.py
```
(The plugin tolerates the revert: without the core hook, `tool_scope` is
simply ignored and tool scoping is a no-op - everything else still works.)
