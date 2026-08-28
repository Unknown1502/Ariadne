"""Configuration must do something or not exist.

Three separate times, a setting was accepted, validated, and reported to the console while
no code path read it: `RUNTIME_STORE=firestore`, `EVENT_BUS=pubsub`, and then eleven more.
Every instance produced the same failure - an operator believes a capability is on, the
system says it is on, and it is not.

Nothing in a normal test suite catches this, because the code that exists all works. These
tests check the *absence* of code instead: that every declared setting is read somewhere,
and that settings for unbuilt capabilities refuse to start rather than being ignored.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from backend.config import Settings, reset_settings_cache

SKIP_DIRS = {".venv", "node_modules", "var", ".git", "ariadne.egg-info", "__pycache__", "dist"}
REPO = pathlib.Path(__file__).resolve().parents[2]


def declared_settings() -> list[str]:
    source = (REPO / "backend" / "config.py").read_text(encoding="utf-8")
    names = set(re.findall(r"^\s{4}(\w+):\s", source, re.MULTILINE))
    return sorted(names - {"model_config"})


def source_outside_config() -> str:
    chunks: list[str] = []
    for pattern in ("*.py", "*.ts", "*.tsx"):
        for path in REPO.rglob(pattern):
            if any(part in SKIP_DIRS for part in path.parts) or path.name == "config.py":
                continue
            chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(chunks)


def rejected_settings() -> set[str]:
    """Settings whose only job is to be refused.

    These are read by the validator inside config.py, which the scan below deliberately
    excludes. That is a real consumer - the setting exists so an operator asking for an
    unbuilt capability gets an error instead of silence - so it counts as wired.
    """
    source = (REPO / "backend" / "config.py").read_text(encoding="utf-8")
    body = source.split("_unimplemented_features_fail_loudly", 1)
    if len(body) < 2:
        return set()
    return set(re.findall(r"self\.(\w+)", body[1]))


class TestEverySettingIsRead:
    def test_no_setting_is_read_by_nothing(self) -> None:
        # The exact check that found eleven dead settings.
        code = source_outside_config()
        exempt = rejected_settings()
        unread = [
            name
            for name in declared_settings()
            if name not in exempt and not re.search(rf"\b{name}\b", code)
        ]
        assert not unread, (
            f"these settings are declared but no code reads them: {unread}. "
            f"Either wire them up, reject them in _unimplemented_features_fail_loudly, or "
            f"delete them - a setting that does nothing is a promise the system does not keep."
        )

    def test_the_exemption_list_is_not_a_loophole(self) -> None:
        # If the rejection validator ever stopped naming these, they would become silently
        # ignored again and the exemption would hide it.
        exempt = rejected_settings()
        assert exempt, "no settings are rejected; the exemption path should be empty, not broken"
        assert exempt <= set(declared_settings())

    def test_the_check_would_catch_a_dead_setting(self) -> None:
        # Guards the guard: if the scan silently matched everything, it would pass forever.
        code = source_outside_config()
        assert not re.search(r"\bthis_setting_does_not_exist\b", code)


class TestUnimplementedFeaturesFailLoudly:
    @pytest.mark.parametrize(
        "variable",
        ["ENABLE_MEMORY_BANK", "ENABLE_AGENT_GATEWAY", "ENABLE_MODEL_ARMOR"],
    )
    def test_enabling_an_unbuilt_capability_is_refused(
        self, variable: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(variable, "true")
        reset_settings_cache()
        with pytest.raises(ValueError, match="not implemented"):
            Settings()
        reset_settings_cache()

    def test_requesting_artifact_storage_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLOUD_STORAGE_BUCKET", "my-bucket")
        reset_settings_cache()
        with pytest.raises(ValueError, match="not implemented"):
            Settings()
        reset_settings_cache()

    def test_the_error_names_what_was_requested(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENABLE_MODEL_ARMOR", "true")
        reset_settings_cache()
        with pytest.raises(ValueError, match="ENABLE_MODEL_ARMOR"):
            Settings()
        reset_settings_cache()

    def test_leaving_them_unset_is_fine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for variable in (
            "ENABLE_MEMORY_BANK", "ENABLE_AGENT_GATEWAY",
            "ENABLE_MODEL_ARMOR", "CLOUD_STORAGE_BUCKET",
        ):
            monkeypatch.delenv(variable, raising=False)
        reset_settings_cache()
        assert Settings().enable_memory_bank is False
        reset_settings_cache()


class TestNoDeterminismBreakingKnobs:
    def test_there_is_no_temperature_setting(self) -> None:
        # Every agent pins temperature to 0.0 so the same explanation compiles to the same
        # claim. A knob whose only effect is to break that guarantee should not exist.
        assert "gemini_temperature" not in declared_settings()

    def test_agents_pin_temperature_explicitly(self) -> None:
        for name in ("investigator", "experimenter", "governor_advisor"):
            source = (REPO / "backend" / "agents" / f"{name}.py").read_text(encoding="utf-8")
            assert "temperature=0.0" in source, name


class TestGlobalCeilingsTightenOnly:
    def test_a_lower_ceiling_is_applied(self) -> None:
        from backend.agents.registry import EXPERIMENTER_MANIFEST, apply_limits

        bounded = apply_limits(EXPERIMENTER_MANIFEST, loop_budget=1, timeout_seconds=5.0)
        assert bounded.loop_budget == 1
        assert bounded.timeout_seconds == 5.0

    def test_a_higher_ceiling_does_not_loosen_a_manifest(self) -> None:
        # Per-agent budgets are deliberate. The Verifier gets one attempt because a
        # deterministic computation that failed once fails identically; a global override
        # that raised it would erase that reasoning.
        from backend.agents.registry import VERIFIER_MANIFEST, apply_limits

        bounded = apply_limits(VERIFIER_MANIFEST, loop_budget=10, timeout_seconds=300.0)
        assert bounded.loop_budget == 1
        assert bounded.timeout_seconds == 15.0

    def test_ceilings_reach_the_live_pipeline(self, tmp_path, monkeypatch) -> None:
        from backend.core.clock import ManualClock
        from backend.runtime.orchestrator import build_pipeline
        from backend.storage.runtime import LocalRuntimeStore
        from backend.storage.sql import in_memory_ledger
        from tests.factories import T0

        monkeypatch.setenv("AGENT_LOOP_BUDGET", "1")
        reset_settings_cache()
        ledger = in_memory_ledger()
        try:
            pipeline = build_pipeline(
                ledger=ledger,
                runtime=LocalRuntimeStore(tmp_path / "runtime", clock=ManualClock(T0)),
                clock=ManualClock(T0),
            )
            assert pipeline._investigator.manifest.loop_budget == 1
        finally:
            ledger.dispose()
            reset_settings_cache()


class TestValidityWindowIsWired:
    def test_the_configured_window_reaches_the_lineage_service(
        self, tmp_path, monkeypatch
    ) -> None:
        from datetime import timedelta

        from backend.core.clock import ManualClock
        from backend.runtime.orchestrator import build_pipeline
        from backend.storage.runtime import LocalRuntimeStore
        from backend.storage.sql import in_memory_ledger
        from tests.factories import T0

        monkeypatch.setenv("EVIDENCE_VALIDITY_DAYS", "7")
        reset_settings_cache()
        ledger = in_memory_ledger()
        try:
            pipeline = build_pipeline(
                ledger=ledger,
                runtime=LocalRuntimeStore(tmp_path / "runtime", clock=ManualClock(T0)),
                clock=ManualClock(T0),
            )
            assert pipeline._lineage._validity == timedelta(days=7)
        finally:
            ledger.dispose()
            reset_settings_cache()

    def test_debt_staleness_stays_policy_versioned(self) -> None:
        # Deliberately *not* driven by the environment: debt has to be comparable across
        # deployments, so its threshold travels with the policy version instead.
        from backend.governance.policy import DEFAULT_POLICY

        assert DEFAULT_POLICY.thresholds.stale_days == 90
        assert "stale_days" not in declared_settings()


class TestEnvExampleMatchesReality:
    def test_the_example_declares_no_setting_that_was_removed(self) -> None:
        example = (REPO / ".env.example").read_text(encoding="utf-8")
        declared = {name.upper() for name in declared_settings()}
        documented = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", example, re.MULTILINE))
        stale = documented - declared
        assert not stale, f".env.example documents settings that no longer exist: {stale}"
