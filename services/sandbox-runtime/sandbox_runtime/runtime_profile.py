"""Fail-closed policy for the sandbox execution profile.

The current runtime still has deliberate development shortcuts: running the SDK
in the worker's own process, fixture substrates/stages, and local virtual keys.
Those shortcuts remain useful for tests, but must never be mistaken for a secure
deployment. This module concentrates the validation, reads the environment on
every call (no import-time state) and fails before any consequential work.
"""
from __future__ import annotations

import os
from enum import Enum


PROFILE_ENV_VAR = "DSE_DEPLOYMENT_PROFILE"
SANDBOX_INPROCESS_ENV_VAR = "DSE_SANDBOX_INPROCESS"
SUBSTRATE_ENV_VAR = "DSE_CODER_SUBSTRATE"
MODEL_GATEWAY_FIXTURE_ENV_VAR = "DSE_MODEL_GATEWAY_ALLOW_FIXTURE"


class RuntimeProfile(str, Enum):
    dev = "dev"
    test = "test"
    production = "production"


class RuntimeProfileViolation(RuntimeError):
    """Configuration or fallback incompatible with the selected profile."""


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_REAL_SUBSTRATES = frozenset({"openhands", "claude-agent"})


def _flag(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise RuntimeProfileViolation(
        f"{name}={raw!r} invalid; use an explicit boolean "
        "(1/0, true/false, yes/no or on/off)"
    )


def current_runtime_profile(value: str | None = None) -> RuntimeProfile:
    """Resolve the profile without caching so tests and preflight stay faithful."""
    raw = (value if value is not None else os.environ.get(PROFILE_ENV_VAR, "dev"))
    normalized = raw.strip().lower()
    # Accepting the usual aliases does not weaken security: both select the more
    # restrictive branch, instead of accidentally falling into the dev default.
    if normalized in {"prod", "pilot"}:
        normalized = RuntimeProfile.production.value
    try:
        return RuntimeProfile(normalized)
    except ValueError as exc:
        valid = ", ".join(profile.value for profile in RuntimeProfile)
        raise RuntimeProfileViolation(
            f"{PROFILE_ENV_VAR}={raw!r} unknown; valid values: {valid}"
        ) from exc


def model_gateway_fixture_allowed() -> bool:
    """Effective value of the fallback, with strict parsing and the legacy default."""
    return _flag(MODEL_GATEWAY_FIXTURE_ENV_VAR, default=True)


def sandbox_inprocess_enabled() -> bool:
    """Agent turn mode (Phase 1, plan 09).

    dev/test: default True (compatibility — in-process remains the path for
    tests without docker); `DSE_SANDBOX_INPROCESS=0` turns on the isolated mode
    (RemoteSubstrate → agent-runner inside the sandbox).
    production: ALWAYS False — the turn only exists isolated; setting the flag
    to true in production is already a violation in `validate_runtime_profile`.
    """
    if current_runtime_profile() is RuntimeProfile.production:
        return False
    return _flag(SANDBOX_INPROCESS_ENV_VAR, default=True)


def validate_runtime_profile(
    *,
    require_real_substrate: bool = False,
    require_real_gateway: bool = False,
    local_fallback: str | None = None,
    substrate_name: str | None = None,
) -> RuntimeProfile:
    """Validate the controls that apply to the current operation.

    In ``dev``/``test`` only the flag syntax is validated, and only when the
    flags are consulted. In ``production`` every violation is aggregated into a
    single exception, with no attempt to degrade to a local path.

    ``local_fallback`` must describe the shortcut the operation would take. As
    long as the isolated ``agent-runner`` is not wired into ``execute_stage``,
    the agent Activities pass this argument and are therefore deliberately
    unavailable in production.
    """
    profile = current_runtime_profile()
    if profile is not RuntimeProfile.production:
        return profile

    violations: list[str] = []
    if _flag(SANDBOX_INPROCESS_ENV_VAR, default=False):
        violations.append(f"{SANDBOX_INPROCESS_ENV_VAR} enables execution outside the sandbox")

    if require_real_substrate:
        chosen = (substrate_name or os.environ.get(SUBSTRATE_ENV_VAR, "fake")).strip().lower()
        if chosen not in _REAL_SUBSTRATES:
            violations.append(
                f"{SUBSTRATE_ENV_VAR}={chosen or '<empty>'!r} is not an approved real "
                f"substrate ({', '.join(sorted(_REAL_SUBSTRATES))})"
            )

    if require_real_gateway and model_gateway_fixture_allowed():
        violations.append(
            f"{MODEL_GATEWAY_FIXTURE_ENV_VAR} allows a fixture virtual key/local fallback"
        )

    if local_fallback:
        violations.append(f"local fallback forbidden: {local_fallback}")

    if violations:
        raise RuntimeProfileViolation(
            "production profile refused by sandbox-runtime: " + "; ".join(violations)
        )
    return profile


def validate_runtime_startup() -> RuntimeProfile:
    """Static preflight for loading the runtime in the worker.

    The caller that loads the Activities may invoke this function before it
    starts polling. The provisioning Activities also invoke it, so an integrator
    that has not adopted the preflight yet stays protected.

    O parâmetro `isolated_stage_execution_available` e o ramo que ele alimentava
    saíram em 2026-08-20: ele vinha de `SandboxDriver.supports_isolated_stage_execution`,
    constante `True` nos dois únicos drivers que existem, então a condição
    `is False` nunca podia ser verdadeira. O que este preflight de fato protege
    — recusar produção com sandbox in-process — mora em `validate_runtime_profile`
    logo abaixo, e continua intacto.
    """
    return validate_runtime_profile(
        require_real_substrate=True,
        require_real_gateway=True,
    )


def reject_local_agent_execution(stage: str) -> RuntimeProfile:
    """Block the agent/stand-in paths that are still local when in production."""
    return validate_runtime_profile(
        require_real_substrate=True,
        require_real_gateway=True,
        local_fallback=f"stage {stage!r} still runs in the worker's process/workspace",
    )
