"""Sidecar store for KiroCrew's per-agent bookkeeping.

kiro-cli validates ``~/.kiro/agents/*.json`` with serde ``deny_unknown_fields``
and rejects the *entire* spec on any unknown key, then silently falls back to
the default agent (``--agent <name>`` resolves to default with only a stderr
"no agent with name X found" line). KiroCrew therefore keeps its private
per-agent bookkeeping OUT of the kiro spec and in this sidecar, so every spec
stays schema-valid for kiro-cli.

Three values are tracked, all kept in this sidecar rather than the kiro spec:

- ``model_managed`` (bool): whether an agent's ``model`` should track the
  shipped ``defaults.json`` (so a default bump propagates) or is an explicit
  user pick frozen against future bumps.
- ``cc_model`` (str): a per-agent model for the ``claude_code`` provider (that
  backend can't pick a per-agent model from ``--agent`` the way kiro-cli does).
- ``config_model`` (str): the model the global ``agent.model`` last propagated
  into this agent's spec, recorded only when that write REPLACED a different
  value. Provenance for the write, so returning the global to "auto" can un-pin
  a spec model Kiro Crew itself wrote. Equality is deliberately not treated as
  provenance: the Agent Templates editor and a hand edit also write the spec, so
  a spec that already agrees with the global may be someone else's pick. The
  accepted cost is that a pin made before this record existed has no provenance
  and is never un-pinned.

State file (``~/.kiro/crew/agent_model_state.json``, honoring ``KIROCREW_HOME``)::

    {
      "kirocrew":           {"model_managed": true, "config_model": "<pinned id>"},
      "kirocrew-heartbeat": {"cc_model": "claude-sonnet-4.6"}
    }

This is a near-leaf module: it imports only the stdlib plus the leaf
``config.paths`` and ``atomic_write`` helpers, so it never participates in the
``agent`` <-> ``config.loader`` import cycle.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

_STATE_FILENAME = "agent_model_state.json"
_MODEL_MANAGED = "model_managed"
_CC_MODEL = "cc_model"
_CONFIG_MODEL = "config_model"

# Guards in-process read-modify-write races (e.g. dashboard PATCH vs gateway
# refresh). Cross-process atomicity is provided by ``atomic_write``.
_lock = threading.RLock()


def _state_path() -> Path:
    """Return the sidecar path (resolved fresh so KIROCREW_HOME is honored)."""
    return config_dir() / _STATE_FILENAME


def _read() -> dict:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict) -> None:
    atomic_write(_state_path(), json.dumps(data, indent=2, sort_keys=True) + "\n")


def _entry(data: dict, name: str) -> dict:
    entry = data.get(name)
    return entry if isinstance(entry, dict) else {}


def get_model_managed(name: str) -> bool | None:
    """Return the agent's managed flag, or ``None`` when unset (grandfathered)."""
    with _lock:
        value = _entry(_read(), name).get(_MODEL_MANAGED)
    return bool(value) if isinstance(value, bool) else None


def set_model_managed(name: str, value: bool) -> None:
    with _lock:
        data = _read()
        entry = data.get(name)
        if not isinstance(entry, dict):
            entry = {}
        entry[_MODEL_MANAGED] = bool(value)
        data[name] = entry
        _write(data)


def get_cc_model(name: str) -> str | None:
    """Return the agent's claude_code-provider model, or ``None`` when unset."""
    with _lock:
        value = _entry(_read(), name).get(_CC_MODEL)
    return value if isinstance(value, str) and value else None


def set_cc_model(name: str, value: str | None) -> None:
    """Set (or clear, when ``value`` is falsy) the agent's claude_code model."""
    with _lock:
        data = _read()
        entry = data.get(name)
        if not isinstance(entry, dict):
            entry = {}
        if value:
            entry[_CC_MODEL] = str(value)
        else:
            entry.pop(_CC_MODEL, None)
        if entry:
            data[name] = entry
        else:
            data.pop(name, None)
        _write(data)


def get_config_model(name: str) -> str | None:
    """Return the model the global ``agent.model`` propagated into this agent's
    spec, or ``None`` when the global never pinned one."""
    with _lock:
        value = _entry(_read(), name).get(_CONFIG_MODEL)
    return value if isinstance(value, str) and value else None


def set_config_model(name: str, value: str | None) -> None:
    """Record (or clear, when ``value`` is falsy) the propagated global model."""
    with _lock:
        data = _read()
        entry = data.get(name)
        if not isinstance(entry, dict):
            entry = {}
        if value:
            entry[_CONFIG_MODEL] = str(value)
        else:
            entry.pop(_CONFIG_MODEL, None)
        if entry:
            data[name] = entry
        else:
            data.pop(name, None)
        _write(data)


def clear_config_model_if(name: str, expected: str) -> bool:
    """Clear ``config_model`` only if it still equals *expected*.

    Compare-and-clear under the same lock as the read, so a retirement decided
    against one observed value cannot discard a NEWER record written between the
    decision and the write (two rebuilds overlapping — one going to "auto", one
    propagating a concrete model). Returns True when the clear happened.
    """
    if not expected:
        return False
    with _lock:
        data = _read()
        entry = data.get(name)
        if not isinstance(entry, dict) or entry.get(_CONFIG_MODEL) != expected:
            return False
        entry.pop(_CONFIG_MODEL, None)
        if entry:
            data[name] = entry
        else:
            data.pop(name, None)
        _write(data)
    return True


def prune(name: str) -> None:
    """Drop an agent's entry entirely (call when the agent is deleted)."""
    with _lock:
        data = _read()
        if name in data:
            data.pop(name, None)
            _write(data)
