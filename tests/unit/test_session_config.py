"""Readiness-authority tests for the unified session configuration model.

`derive_readiness` is the single authority every session-setup surface
must render from; these tests pin the matrix of target × model × task
outcomes, the disabled-not-hidden row policy, and the no-silent-mutation
guarantee.
"""

from __future__ import annotations

import pytest

from agentic_debugger.ui.session_config import (
    OFFLINE_CHOICE,
    PROVIDER_COMMANDCODE,
    PROVIDER_CONFIGURED,
    PROVIDER_OFFLINE,
    PROVIDER_OLLAMA,
    PROVIDER_OPENCODE,
    ROW_AUTO_RETRY,
    ROW_BUG,
    ROW_DEBUGGER,
    ROW_MODEL,
    ROW_PROJECT,
    ROW_TASK,
    ROW_TIME_LIMIT,
    SEVERITY_ERROR,
    ModelChoice,
    ModelOption,
    ProjectStatus,
    SessionCatalog,
    SessionConfig,
    TARGET_CURATED,
    TARGET_LADDER,
    TARGET_LOCAL_PROJECT,
    TaskOption,
    derive_readiness,
    model_compatibility,
)


def _catalog(**overrides) -> SessionCatalog:
    catalog = SessionCatalog(
        tasks=(
            # curated first, ladder last — product order
            TaskOption("curated-off-by-one-002", "Return the complete recent window"),
            TaskOption(
                "pdb-required-boundary-006",
                "Level 6/100",
                ladder=True,
                detail="Exact PDB required",
            ),
        ),
        models=(
            ModelOption(PROVIDER_OFFLINE, "", "Offline"),
            ModelOption(
                PROVIDER_OLLAMA, "qwen3.5:cloud", "qwen3.5", detail="Ollama Cloud"
            ),
            ModelOption(
                PROVIDER_OPENCODE,
                "opencode-go/glm-5.3",
                "glm-5.3",
                available=False,
                unavailable_reason="OpenCode auth store not found",
            ),
            ModelOption(
                PROVIDER_CONFIGURED, "dummy", "Dummy command model", detail="command: dummy"
            ),
        ),
        ladder_models=(
            ModelOption(
                PROVIDER_OLLAMA, "qwen3.5:cloud", "qwen3.5", detail="live_verified"
            ),
        ),
    )
    if overrides:
        return SessionCatalog(
            **{**catalog.__dict__, **overrides}
        )
    return catalog


def _config(**overrides) -> SessionConfig:
    defaults = dict(
        target=TARGET_CURATED,
        task_id="curated-off-by-one-002",
        model=OFFLINE_CHOICE,
    )
    defaults.update(overrides)
    return SessionConfig(**defaults)


_CLEAN = ProjectStatus("C:/repo", True, "clean", "Git: repo @ abc1234")
_DIRTY = ProjectStatus("C:/repo", False, "dirty", "Project has uncommitted changes.")


class TestCuratedTarget:
    def test_offline_curated_task_is_ready(self):
        readiness = derive_readiness(_config(), _catalog(), _CLEAN)
        assert readiness.ready is True
        assert readiness.run_label == "Run evidence demo"
        assert "ready" in readiness.status_line.lower()

    def test_missing_task_blocks(self):
        readiness = derive_readiness(_config(task_id=None), _catalog(), _CLEAN)
        assert readiness.ready is False
        assert readiness.status_line.startswith("Start unavailable")
        assert readiness.issues[0].field == ROW_TASK

    def test_unknown_task_blocks(self):
        readiness = derive_readiness(_config(task_id="nope"), _catalog(), _CLEAN)
        assert readiness.ready is False

    def test_ladder_task_under_curated_target_blocks(self):
        readiness = derive_readiness(
            _config(task_id="pdb-required-boundary-006"), _catalog(), _CLEAN
        )
        assert readiness.ready is False
        assert any(item.field == ROW_TASK for item in readiness.issues)

    def test_provider_model_curated_task_is_ready(self):
        config = _config(model=ModelChoice(PROVIDER_OLLAMA, "qwen3.5:cloud", "qwen3.5"))
        readiness = derive_readiness(config, _catalog(), _CLEAN)
        assert readiness.ready is True
        assert readiness.run_label == "Start session"

    def test_unavailable_provider_model_blocks_with_reason(self):
        config = _config(model=ModelChoice(PROVIDER_OPENCODE, "opencode-go/glm-5.3", "glm-5.3"))
        readiness = derive_readiness(config, _catalog(), _CLEAN)
        assert readiness.ready is False
        assert any(
            item.field == ROW_MODEL and "auth store" in item.message
            for item in readiness.issues
        )

    def test_configured_error_blocks_only_configured_selection(self):
        catalog = _catalog(configured_error="unsupported version 9")
        ok = derive_readiness(
            _config(model=ModelChoice(PROVIDER_OLLAMA, "qwen3.5:cloud", "qwen3.5")),
            catalog,
            _CLEAN,
        )
        assert ok.ready is True
        blocked = derive_readiness(
            _config(model=ModelChoice(PROVIDER_CONFIGURED, "dummy", "Dummy")),
            catalog,
            _CLEAN,
        )
        assert blocked.ready is False
        assert any("Configuration error" in item.message for item in blocked.issues)


class TestLocalProjectTarget:
    def _local(self, **overrides):
        defaults = dict(
            target=TARGET_LOCAL_PROJECT,
            task_id=None,
            project_path="C:/repo",
            bug_description="crash on empty input",
            model=ModelChoice(PROVIDER_OLLAMA, "qwen3.5:cloud", "qwen3.5"),
        )
        defaults.update(overrides)
        return SessionConfig(**defaults)

    def test_clean_project_with_model_is_ready(self):
        readiness = derive_readiness(self._local(), _catalog(), _CLEAN)
        assert readiness.ready is True
        assert readiness.run_label == "Start debugging"

    def test_dirty_project_blocks_with_message(self):
        readiness = derive_readiness(self._local(), _catalog(), _DIRTY)
        assert readiness.ready is False
        assert any(
            item.field == ROW_PROJECT and "uncommitted" in item.message
            for item in readiness.issues
        )

    def test_missing_bug_blocks(self):
        readiness = derive_readiness(self._local(bug_description="  "), _catalog(), _CLEAN)
        assert readiness.ready is False
        assert any(item.field == ROW_BUG for item in readiness.issues)

    def test_offline_model_blocks_with_guidance(self):
        readiness = derive_readiness(self._local(model=OFFLINE_CHOICE), _catalog(), _CLEAN)
        assert readiness.ready is False
        assert any(
            item.field == ROW_MODEL and "live model" in item.message
            for item in readiness.issues
        )

    def test_task_row_disabled_not_hidden(self):
        readiness = derive_readiness(self._local(), _catalog(), _CLEAN)
        assert ROW_TASK in readiness.rows
        assert readiness.rows[ROW_TASK].enabled is False
        assert readiness.rows[ROW_TASK].reason


class TestLadderTarget:
    def _ladder(self, **overrides):
        defaults = dict(
            target=TARGET_LADDER,
            task_id="pdb-required-boundary-006",
            model=ModelChoice(PROVIDER_OLLAMA, "qwen3.5:cloud", "qwen3.5"),
        )
        defaults.update(overrides)
        return SessionConfig(**defaults)

    def test_qualified_rung_and_model_is_ready(self):
        readiness = derive_readiness(self._ladder(), _catalog(), _CLEAN)
        assert readiness.ready is True
        assert readiness.run_label == "Start session"
        assert any("operator contract" in note for note in readiness.notes)

    def test_curated_task_under_ladder_blocks(self):
        readiness = derive_readiness(self._ladder(task_id="curated-off-by-one-002"), _catalog(), _CLEAN)
        assert readiness.ready is False
        assert any(item.field == ROW_TASK for item in readiness.issues)

    def test_empty_roster_blocks(self):
        # For lower ladder rungs, an empty qualified roster does not block
        # a generic executable provider model.  Level-32 remains the
        # frozen qualified treatment.
        catalog = _catalog(ladder_models=())
        readiness = derive_readiness(self._ladder(), catalog, _CLEAN)
        # Lower ladder Level 6 with an available Ollama model via
        # find_model is executable even when ladder_models is empty
        assert readiness.ready is True
        # Level-32 with empty roster must still report qualification gap
        from agentic_debugger.application.level32 import LEVEL32_TASK_ID

        level32_catalog = _catalog(
            tasks=(
                TaskOption("curated-off-by-one-002", "Return the complete recent window"),
                TaskOption(LEVEL32_TASK_ID, "Level 32/100 — Cookiecutter #967", ladder=True),
            ),
            ladder_models=(),
        )
        level32_readiness = derive_readiness(
            SessionConfig(
                target=TARGET_LADDER,
                task_id=LEVEL32_TASK_ID,
                model=ModelChoice(PROVIDER_OLLAMA, "qwen3.5:cloud", "qwen3.5"),
            ),
            level32_catalog,
            _CLEAN,
        )
        assert level32_readiness.ready is False
        assert any("qualified Ollama" in item.message for item in level32_readiness.issues)

    def test_non_ollama_model_blocks(self):
        # Lower ladder interactive runs now accept any executable provider
        catalog = SessionCatalog(
            tasks=(
                TaskOption("curated-off-by-one-002", "Return the complete recent window"),
                TaskOption("pdb-required-boundary-006", "Level 6/100", ladder=True),
            ),
            models=(
                ModelOption(PROVIDER_OFFLINE, "", "Offline"),
                ModelOption(PROVIDER_OLLAMA, "qwen3.5:cloud", "qwen3.5"),
                ModelOption(PROVIDER_COMMANDCODE, "zai-org/glm-5.2", "GLM 5.2", available=True),
            ),
            ladder_models=(
                ModelOption(PROVIDER_OLLAMA, "qwen3.5:cloud", "qwen3.5"),
            ),
        )
        readiness = derive_readiness(
            self._ladder(model=ModelChoice(PROVIDER_COMMANDCODE, "zai-org/glm-5.2", "glm-5.2")),
            catalog,
            _CLEAN,
        )
        assert readiness.ready is True

    def test_provider_identity_collision_cannot_satisfy_qualification(self):
        collision = ModelChoice(
            PROVIDER_COMMANDCODE,
            "qwen3.5:cloud",
            "CommandCode alias collision",
        )
        # Catalog contains the colliding CommandCode model as executable,
        # but ladder qualification remains bound to provider identity.
        catalog = SessionCatalog(
            tasks=(
                TaskOption("curated-off-by-one-002", "Return the complete recent window"),
                TaskOption("pdb-required-boundary-006", "Level 6/100", ladder=True),
            ),
            models=(
                ModelOption(PROVIDER_OFFLINE, "", "Offline"),
                ModelOption(PROVIDER_OLLAMA, "qwen3.5:cloud", "qwen3.5"),
                ModelOption(PROVIDER_COMMANDCODE, "qwen3.5:cloud", "CommandCode alias collision", available=True),
            ),
            ladder_models=(
                ModelOption(PROVIDER_OLLAMA, "qwen3.5:cloud", "qwen3.5"),
            ),
        )
        assert catalog.ladder_model(collision) is None
        readiness = derive_readiness(
            self._ladder(model=collision),
            catalog,
            _CLEAN,
        )
        # Collision is executable as a CommandCode model for lower ladder,
        # but not as a qualified Ollama treatment
        assert readiness.ready is True
        assert catalog.ladder_model(collision) is None

    def test_frozen_rows_are_disabled_with_reasons(self):
        readiness = derive_readiness(self._ladder(), _catalog(), _CLEAN)
        for row in (ROW_DEBUGGER, ROW_TIME_LIMIT, ROW_AUTO_RETRY):
            assert readiness.rows[row].enabled is False
            assert readiness.rows[row].reason


class TestRowStability:
    """Controls never disappear: every row exists for every target."""

    @pytest.mark.parametrize("target", [TARGET_CURATED, TARGET_LOCAL_PROJECT, TARGET_LADDER])
    def test_all_rows_present_for_every_target(self, target):
        readiness = derive_readiness(
            _config(target=target), _catalog(), ProjectStatus.unchecked("")
        )
        expected = {
            "target", "task", "project", "bug", "repro", "verify",
            "model", "debugger", "time_limit", "auto_retry",
        }
        assert set(readiness.rows.keys()) == expected

    def test_target_switch_does_not_mutate_other_selections(self):
        config = _config(model=ModelChoice(PROVIDER_OLLAMA, "qwen3.5:cloud", "qwen3.5"))
        switched = config.with_target(TARGET_LOCAL_PROJECT)
        assert switched.model == config.model
        assert switched.task_id == config.task_id
        assert switched.target == TARGET_LOCAL_PROJECT

    def test_unknown_target_fails_closed(self):
        with pytest.raises(ValueError):
            _config().with_target("cloud")


class TestModelCompatibility:
    def test_offline_compatibility_matrix(self):
        assert model_compatibility(TARGET_CURATED, ModelOption(PROVIDER_OFFLINE, "", "Offline")) == (True, "")
        ok, _ = model_compatibility(TARGET_LOCAL_PROJECT, ModelOption(PROVIDER_OFFLINE, "", "Offline"))
        assert ok is False
        ok, _ = model_compatibility(TARGET_LADDER, ModelOption(PROVIDER_OFFLINE, "", "Offline"))
        assert ok is False

    def test_subscription_models_allowed_for_curated_and_local(self):
        option = ModelOption(PROVIDER_OPENCODE, "opencode-go/glm-5.3", "glm-5.3")
        assert model_compatibility(TARGET_CURATED, option)[0] is True
        assert model_compatibility(TARGET_LOCAL_PROJECT, option)[0] is True

    def test_non_ollama_models_blocked_for_ladder_with_reason(self):
        # Interactive ladder now accepts any live provider model; the
        # qualification distinction is enforced at readiness for the
        # frozen Level-32 treatment, not via the generic compatibility
        # matrix.
        for provider in (PROVIDER_OPENCODE, PROVIDER_COMMANDCODE, PROVIDER_CONFIGURED):
            ok, reason = model_compatibility(
                TARGET_LADDER, ModelOption(provider, "m", "m")
            )
            assert ok is True
            assert reason == ""


class TestStatusLineAuthority:
    def test_status_line_names_the_first_error(self):
        readiness = derive_readiness(
            _config(task_id=None, model=OFFLINE_CHOICE), _catalog(), _CLEAN
        )
        assert readiness.ready is False
        assert readiness.status_line.startswith("Start unavailable — ")
        assert "task" in readiness.status_line.lower()

    def test_ready_line_names_the_action(self):
        readiness = derive_readiness(_config(), _catalog(), _CLEAN)
        assert readiness.ready is True
        assert "run evidence demo" in readiness.status_line.lower()
