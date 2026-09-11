"""Acceptance integration test suite for Level-32 Configured Runtime (Task 46/47).

Validates the complete execution pipeline for Level-32 (Cookiecutter #967) launched
with a configured command model (SourceKind.CONFIGURED_MODEL):
- StartSession -> MODEL_CONFIGURED -> Level-32 workspace materialization ->
  real run_local_session -> real DeterministicController -> real CancellableJsonlCommandTransport ->
  MODEL_REQUEST_STARTED emitted -> model directives dispatched -> honest completion/failure.
- Ensures no monkeypatching of run_local_session, _curated_fixture_dir, or controller.run.
- Proves real Level-32 workspace files exist under work_dir / level32_staging.
- Proves provider and model identity survive into journal provenance.
- Proves interactive Level-32 enforces pdb-on-uncertainty with proof_required=False and
  require_pdb_evidence_before_patch=False (F2).
- Proves materialization works when pyarrow is unavailable (F1).
- Proves the materialization failure path emits DIAGNOSIS_RECORDED and raises ConfiguredSourceError.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentic_debugger.agent.controller import DeterministicController
from agentic_debugger.application.configured_source import (
    ConfiguredSourceError,
    run_configured_session,
)
from agentic_debugger.application.emitter import SessionEventEmitter
from agentic_debugger.application.events import SessionEventKind, SourceKind
from agentic_debugger.application.history import HistoryStore
from agentic_debugger.application.journal import SessionEventJournal
from agentic_debugger.application.level32 import LEVEL32_TASK_ID
from agentic_debugger.application.level32_materialization import (
    LEVEL32_INTERNAL_TASK_ID,
    LEVEL32_PUBLIC_F2P,
    LEVEL32_PUBLIC_P2P,
    LEVEL32_SOURCE_SHA256,
    Level32MaterializationError,
    SourceAcquisitionMode,
    build_level32_interactive_scenario,
    build_level32_official_scenario,
    materialize_level32_task,
    sha256_file,
    write_public_scaffold,
)
from agentic_debugger.application.model_gateway import (
    ModelGateway,
    provider_runtime_identity,
)
from agentic_debugger.application.model_providers import ProviderModel
from agentic_debugger.application import provider_connections as pc
from agentic_debugger.application.sources import ModelExecutionError
from agentic_debugger.application.worker_scenarios import ScenarioContext
from agentic_debugger.cancellation import CancellationToken
from agentic_debugger.demo.policies import DemoPolicy
from agentic_debugger.evaluation.live import LiveModelAdapter, LiveModelConfig
from agentic_debugger.evaluation.runner import load_task
from agentic_debugger.runtime.test_runner import TestRunner
from agentic_debugger.runtime.workspace import TaskWorkspace
from agentic_debugger.ui.app import LocalApplicationV1
from agentic_debugger.ui.screens import StartSessionScreen
from ui_support import run_headless


HERMETIC_CONFIG_PY = """# -*- coding: utf-8 -*-

\"\"\"Global configuration handling.\"\"\"

from __future__ import unicode_literals
import copy
import logging
import os
import io

import poyo

from .exceptions import ConfigDoesNotExistException
from .exceptions import InvalidConfiguration


logger = logging.getLogger(__name__)

USER_CONFIG_PATH = os.path.expanduser('~/.cookiecutterrc')

BUILTIN_ABBREVIATIONS = {
    'gh': 'https://github.com/{0}.git',
    'gl': 'https://gitlab.com/{0}.git',
    'bb': 'https://bitbucket.org/{0}',
}

DEFAULT_CONFIG = {
    'cookiecutters_dir': os.path.expanduser('~/.cookiecutters/'),
    'replay_dir': os.path.expanduser('~/.cookiecutter_replay/'),
    'default_context': {},
    'abbreviations': BUILTIN_ABBREVIATIONS,
}


def _expand_path(path):
    \"\"\"Expand both environment variables and user home in the given path.\"\"\"
    path = os.path.expandvars(path)
    path = os.path.expanduser(path)
    return path


def get_config(config_path):
    \"\"\"Retrieve the config from the specified path, returning a config dict.\"\"\"
    if not os.path.exists(config_path):
        raise ConfigDoesNotExistException

    logger.debug('config_path is {0}'.format(config_path))
    with io.open(config_path, encoding='utf-8') as file_handle:
        try:
            yaml_dict = poyo.parse_string(file_handle.read())
        except poyo.exceptions.PoyoException as e:
            raise InvalidConfiguration(
                'Unable to parse YAML file {}. Error: {}'
                ''.format(config_path, e)
            )

    config_dict = copy.copy(DEFAULT_CONFIG)
    config_dict.update(yaml_dict)

    raw_replay_dir = config_dict['replay_dir']
    config_dict['replay_dir'] = _expand_path(raw_replay_dir)

    raw_cookies_dir = config_dict['cookiecutters_dir']
    config_dict['cookiecutters_dir'] = _expand_path(raw_cookies_dir)

    return config_dict


def get_user_config(config_file=None, default_config=False):
    \"\"\"Return the user config as a dict.

    If ``default_config`` is True, ignore ``config_file`` and return default
    values for the config parameters.

    If a path to a ``config_file`` is given, that is different from the default
    location, load the user config from that.

    Otherwise look up the config file path in the ``COOKIECUTTER_CONFIG``
    environment variable. If set, load the config from this path. This will
    raise an error if the specified path is not valid.

    If the environment variable is not set, try the default config file path
    before falling back to the default config values.
    \"\"\"
    # Do NOT load a config. Return defaults instead.
    if default_config:
        return copy.copy(DEFAULT_CONFIG)

    # Load the given config file
    if config_file and config_file is not USER_CONFIG_PATH:
        return get_config(config_file)

    try:
        # Does the user set up a config environment variable?
        env_config_file = os.environ['COOKIECUTTER_CONFIG']
    except KeyError:
        # Load an optional user config if it exists
        # otherwise return the defaults
        if os.path.exists(USER_CONFIG_PATH):
            return get_config(USER_CONFIG_PATH)
        else:
            return copy.copy(DEFAULT_CONFIG)
    else:
        # There is a config environment variable. Try to load it.
        # Do not check for existence, so invalid file paths raise an error.
        return get_config(env_config_file)
"""


def _seed_hermetic_level32_source(cache_dir: Path) -> Path:
    """Populate a hermetic, test-owned Cookiecutter source tree without Docker or host caches."""
    pkg = cache_dir / "cookiecutter"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(
        "# -*- coding: utf-8 -*-\n\n__version__ = '1.5.1'\n",
        encoding="utf-8",
        newline="\n",
    )
    (pkg / "exceptions.py").write_text(
        "# -*- coding: utf-8 -*-\n\n"
        "class CookiecutterException(Exception):\n    pass\n\n"
        "class ConfigDoesNotExistException(CookiecutterException):\n    pass\n\n"
        "class InvalidConfiguration(CookiecutterException):\n    pass\n",
        encoding="utf-8",
        newline="\n",
    )
    config_file = pkg / "config.py"
    config_file.write_text(
        HERMETIC_CONFIG_PY,
        encoding="utf-8",
        newline="\n",
    )
    assert sha256_file(config_file) == LEVEL32_SOURCE_SHA256

    test_config_dir = cache_dir / "tests" / "test-config"
    test_config_dir.mkdir(parents=True, exist_ok=True)
    (test_config_dir / "valid-config.yaml").write_text(
        "default_context:\n"
        '    full_name: "Firstname Lastname"\n'
        '    email: "firstname.lastname@gmail.com"\n'
        '    github_username: "example"\n'
        'cookiecutters_dir: "/home/example/some-path-to-templates"\n'
        'replay_dir: "/home/example/some-path-to-replay-files"\n',
        encoding="utf-8",
        newline="\n",
    )
    return cache_dir


def _setup_test_providers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "provider-configurations.json"
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(config_file))
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path / "config-dir"))
    _secure_store: dict[str, str] = {}
    monkeypatch.setattr(
        pc, "save_secure_credential", lambda k, v: _secure_store.setdefault(k, v) or True
    )
    monkeypatch.setattr(pc, "load_secure_credential", lambda k: _secure_store.get(k))
    monkeypatch.setattr(pc, "has_secure_credential", lambda k: k in _secure_store)
    monkeypatch.setattr(
        pc,
        "delete_secure_credential",
        lambda k: bool(_secure_store.pop(k, None) is not None),
    )
    monkeypatch.setattr(
        pc, "provider_quarantine_path", lambda: tmp_path / "quarantine.json"
    )
    pc._QUARANTINED_PROVIDERS.clear()
    pc.clear_all_session_keys()
    pc.add_provider_config(
        name="CommandCode GOAT",
        base_url="https://api.commandcode.ai/provider/v1",
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        provider_id="commandcode_goat",
        transport_profile=pc.TRANSPORT_COMMANDCODE_GOAT,
    )
    pc.set_session_key("commandcode_goat", "sk-live-test-level32-synthetic-key")


def test_level32_ui_start_session_selects_and_launches_configured_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner scenario: launch Level 32 with a configured command model through UI StartSession."""
    _setup_test_providers(tmp_path, monkeypatch)
    app = LocalApplicationV1(history_store=HistoryStore(tmp_path / "history"))
    models = (
        ProviderModel(
            "commandcode_goat",
            "muse/muse-spark-1.3",
            "Muse Spark 1.3 Contributor",
            "CommandCode GOAT",
            True,
        ),
    )
    monkeypatch.setattr(
        "agentic_debugger.ui.screens_setup.list_provider_models", lambda **_kwargs: models
    )
    start_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(app, "start_live_session", lambda **kw: start_calls.append(kw))

    monkeypatch.setattr(
        ModelGateway.default(),
        "get_provider_status",
        lambda p: SimpleNamespace(is_configured=True),
    )

    async def scenario(pilot):
        await pilot.press("s")
        start = pilot.app.screen
        assert isinstance(start, StartSessionScreen)

        # Select Capability Ladder -> Level 32
        start._choice_selected("target", "ladder")
        start._choice_selected("task", LEVEL32_TASK_ID)

        # Select Configured Model: Muse Spark 1.3 Contributor
        start._choice_selected("model", "commandcode_goat:muse/muse-spark-1.3")
        assert start.start_available is True
        start.action_start()

    run_headless(app, scenario, size=(120, 32))

    assert len(start_calls) == 1
    call = start_calls[0]
    assert call["task_id"] == LEVEL32_TASK_ID
    assert call["source_kind"] is SourceKind.CONFIGURED_MODEL
    assert call["model_provider"] == "commandcode_goat"
    assert call["profile_id"] == "muse/muse-spark-1.3"


def test_level32_configured_runtime_end_to_end_materialization_and_model_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Execute Level-32 configured session without monkeypatching run_local_session.

    Proves:
    1. Level-32 workspace is truthfully materialized under work_dir / level32_staging.
    2. Real run_local_session runs with materialized fixture_dir and scenario.
    3. Real DeterministicController runs with real CancellableJsonlCommandTransport.
    4. MODEL_CONFIGURED and MODEL_REQUEST_STARTED events are recorded in journal.
    5. Real child model script receives request on stdin and executes.
    6. Interactive Level-32 scenario has exact_public_reproduction=False.
    7. Adapter proof_required=False, controller require_pdb_evidence_before_patch=False,
       and policy=pdb-on-uncertainty (F2).
    """
    _setup_test_providers(tmp_path, monkeypatch)

    # Point LOCALAPPDATA to nonexistent path to prove no dependency on host caches
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "nonexistent_localappdata"))

    # Use hermetic test cache seam without requiring Docker or host cache
    hermetic_cache = tmp_path / "hermetic_cache" / "cookiecutter-967-base-source"
    _seed_hermetic_level32_source(hermetic_cache)
    monkeypatch.setattr(
        "agentic_debugger.application.level32_materialization.default_base_source_cache_dir",
        lambda: hermetic_cache,
    )

    # Spy on adapter and controller construction to verify F2 invariants
    captured_adapter: dict[str, Any] = {}
    orig_adapter_init = LiveModelAdapter.__init__

    def spy_adapter_init(self: Any, *args: Any, **kwargs: Any) -> None:
        orig_adapter_init(self, *args, **kwargs)
        captured_adapter["proof_required"] = self.proof_required
        captured_adapter["policy"] = self.policy

    monkeypatch.setattr(LiveModelAdapter, "__init__", spy_adapter_init)

    captured_controller_configs: list[Any] = []
    orig_controller_init = DeterministicController.__init__

    def spy_controller_init(
        self: Any, registry: Any, model: Any, config: Any, **kwargs: Any
    ) -> None:
        captured_controller_configs.append(config)
        orig_controller_init(self, registry, model, config, **kwargs)

    monkeypatch.setattr(DeterministicController, "__init__", spy_controller_init)

    # Create a deterministic fake model subprocess script
    fake_model_path = tmp_path / "fake_model_runner.py"
    marker_path = tmp_path / "model_invoked_marker.txt"
    fake_model_path.write_text(
        f"""from __future__ import annotations
import json
import sys
from pathlib import Path

marker = Path({repr(str(marker_path))})
line = sys.stdin.readline()
if not line:
    sys.exit(0)

# Record invocation marker proving real process execution
count = int(marker.read_text()) if marker.exists() else 0
marker.write_text(str(count + 1))

# Step 1: run reproduction; Step 2: terminate cleanly
if count == 0:
    content = json.dumps({{"kind": "action", "name": "run_reproduction", "arguments": {{"phase": "baseline"}}}})
else:
    content = json.dumps({{"kind": "transition", "state": "FAILED"}})

response = {{
    "provider_completion_schema_version": "provider-completion-v1",
    "directive_content": content,
    "usage": {{"prompt_tokens": 20, "completion_tokens": 15, "total_tokens": 35}},
}}
print(json.dumps(response))
sys.stdout.flush()
""",
        encoding="utf-8",
    )

    # Bind provider resolution to the fake model command
    import agentic_debugger.application.configured_source as cs

    c = pc.get_provider_config("commandcode_goat")
    authority = provider_runtime_identity(c)

    def wrapped_resolve(provider: str, model_id: str, **kwargs: Any):
        cfg = LiveModelConfig(
            model_name="muse-spark-1.3",
            command=(sys.executable, str(fake_model_path)),
            request_timeout_seconds=30,
            tool_version="test-v1",
        )
        return (
            cfg,
            {
                "display_name": "Muse Spark 1.3 Contributor",
                "route": "direct_api",
                "api_protocol": "chat_completions",
                "provider_model_id": model_id,
                "endpoint": "https://api.commandcode.ai/provider/v1",
                "provider_runtime_identity": authority,
                "protocol_version": "1.3",
            },
            "f" * 64,
        )

    monkeypatch.setattr(cs, "_resolve_registry_model", wrapped_resolve)

    journal_path = tmp_path / "journal.events.jsonl"
    journal = SessionEventJournal(
        journal_path,
        session_id="sess-l32-acceptance",
        task_id=LEVEL32_TASK_ID,
        source_kind=SourceKind.CONFIGURED_MODEL,
    )
    emitter = SessionEventEmitter(
        sink=journal,
        session_id="sess-l32-acceptance",
        task_id=LEVEL32_TASK_ID,
        source_kind=SourceKind.CONFIGURED_MODEL,
    )
    work_dir = tmp_path / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    ctx = ScenarioContext(
        work_dir=work_dir,
        emitter=emitter,
        token=CancellationToken(),
    )

    # Execute configured session through real local execution pipeline
    with pytest.raises(ModelExecutionError, match="controller run ended without completion"):
        run_configured_session(
            ctx,
            {
                "provider": "commandcode_goat",
                "model_id": "muse/muse-spark-1.3",
                "policy": "pdb-on-uncertainty",
            },
        )

    # 1. Verify Level-32 workspace was materialized under work_dir / level32_staging
    staging_fixture = (
        work_dir
        / "level32_staging"
        / "agentic_debugger"
        / "datasets"
        / "curated"
        / LEVEL32_INTERNAL_TASK_ID
    )
    assert staging_fixture.is_dir()
    assert (staging_fixture / "cookiecutter" / "config.py").is_file()
    assert (staging_fixture / "tests" / "test_pdb_public_config_merge.py").is_file()
    assert (staging_fixture / "poyo.py").is_file()
    assert (staging_fixture / "task.json").is_file()
    task_manifest = json.loads(
        (staging_fixture / "task.json").read_text(encoding="utf-8")
    )
    assert task_manifest["reproduction"]["argv"][0] == sys.executable
    assert task_manifest["tests"]["full_suite_argv"][0] == sys.executable

    # 2. Verify fake model runner was actually executed
    assert marker_path.is_file()
    assert int(marker_path.read_text()) >= 1

    # 3. Verify authoritative journal records
    events = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
    event_kinds = [e["event_kind"] for e in events]

    assert SessionEventKind.MODEL_CONFIGURED.value in event_kinds
    assert SessionEventKind.MODEL_REQUEST_STARTED.value in event_kinds
    assert SessionEventKind.MODEL_REQUEST_COMPLETED.value in event_kinds
    assert SessionEventKind.MODEL_DIRECTIVE_ACCEPTED.value in event_kinds

    configured_events = [e for e in events if e["event_kind"] == SessionEventKind.MODEL_CONFIGURED.value]
    assert len(configured_events) == 1
    cfg_payload = configured_events[0]["payload"]
    assert cfg_payload["provider"] == "commandcode_goat"
    assert cfg_payload["profile_id"] == "muse/muse-spark-1.3"
    assert cfg_payload["display_name"] == "Muse Spark 1.3 Contributor"

    # 4. Mandatory F2 regression: interactive route proof invariants
    assert captured_adapter.get("proof_required") is False
    assert captured_adapter.get("policy") == DemoPolicy.PDB_ON_UNCERTAINTY
    assert len(captured_controller_configs) >= 1
    assert captured_controller_configs[0].require_pdb_evidence_before_patch is False


def test_level32_materialization_failure_emits_diagnosis_and_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When Level-32 workspace materialization fails, emit DIAGNOSIS_RECORDED and fail honestly."""
    _setup_test_providers(tmp_path, monkeypatch)
    import agentic_debugger.application.configured_source as cs

    def fail_materialization(_staging_root: Path, **_kwargs: Any) -> Path:
        raise Level32MaterializationError("simulated Docker export failure")

    monkeypatch.setattr(cs, "materialize_level32_task", fail_materialization)

    c = pc.get_provider_config("commandcode_goat")
    authority = provider_runtime_identity(c)

    def wrapped_resolve(provider: str, model_id: str, **kwargs: Any):
        cfg = LiveModelConfig(
            model_name="muse-spark-1.3",
            command=("echo", "hi"),
            request_timeout_seconds=30,
            tool_version="test-v1",
        )
        return (
            cfg,
            {
                "display_name": "Muse Spark 1.3 Contributor",
                "route": "direct_api",
                "api_protocol": "chat_completions",
                "provider_model_id": model_id,
                "endpoint": "https://api.commandcode.ai/provider/v1",
                "provider_runtime_identity": authority,
            },
            "e" * 64,
        )

    monkeypatch.setattr(cs, "_resolve_registry_model", wrapped_resolve)

    journal_path = tmp_path / "journal_fail.events.jsonl"
    journal = SessionEventJournal(
        journal_path,
        session_id="sess-l32-fail",
        task_id=LEVEL32_TASK_ID,
        source_kind=SourceKind.CONFIGURED_MODEL,
    )
    emitter = SessionEventEmitter(
        sink=journal,
        session_id="sess-l32-fail",
        task_id=LEVEL32_TASK_ID,
        source_kind=SourceKind.CONFIGURED_MODEL,
    )
    work_dir = tmp_path / "work_fail"
    work_dir.mkdir(parents=True, exist_ok=True)
    ctx = ScenarioContext(
        work_dir=work_dir,
        emitter=emitter,
        token=CancellationToken(),
    )

    with pytest.raises(ConfiguredSourceError, match="Level-32 workspace preparation failed"):
        run_configured_session(
            ctx,
            {
                "provider": "commandcode_goat",
                "model_id": "muse/muse-spark-1.3",
                "policy": "pdb-on-uncertainty",
            },
        )

    events = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
    diagnosis_events = [e for e in events if e["event_kind"] == SessionEventKind.DIAGNOSIS_RECORDED.value]
    assert len(diagnosis_events) == 1
    diag = diagnosis_events[0]["payload"]
    assert "Level-32 workspace preparation failed: simulated Docker export failure" in diag["text"]
    assert diag["confidence"] == "observed"


def test_interactive_level32_materialization_without_pyarrow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interactive Level-32 workspace materialization succeeds when pyarrow is unavailable (F1)."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "guaranteed_nonexistent_localappdata"))

    # Force pyarrow to be unimportable
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    monkeypatch.setitem(sys.modules, "pyarrow.parquet", None)

    hermetic_cache = tmp_path / "cache" / "cookiecutter-967-base-source"
    _seed_hermetic_level32_source(hermetic_cache)

    staging_root = tmp_path / "staging"
    fixture = materialize_level32_task(
        staging_root,
        mode=SourceAcquisitionMode.INTERACTIVE_CACHE_FIRST,
        cache_dir=hermetic_cache,
    )

    assert fixture.is_dir()
    assert (fixture / "cookiecutter" / "config.py").is_file()
    assert (fixture / "tests" / "test_pdb_public_config_merge.py").is_file()
    assert (fixture / "poyo.py").is_file()
    assert (fixture / "task.json").is_file()

    task_data = json.loads((fixture / "task.json").read_text(encoding="utf-8"))
    assert task_data["task_id"] == LEVEL32_INTERNAL_TASK_ID
    assert task_data["tests"]["fail_to_pass"] == [LEVEL32_PUBLIC_F2P]
    assert task_data["tests"]["pass_to_pass"] == [LEVEL32_PUBLIC_P2P]
    assert task_data["reproduction"]["argv"][0] == sys.executable
    assert task_data["tests"]["full_suite_argv"][0] == sys.executable


def test_official_versus_interactive_scenario_proof_contracts() -> None:
    """Prove official scenario preserves exact PDB proof while interactive route does not (F2)."""
    official = build_level32_official_scenario()
    interactive = build_level32_interactive_scenario()

    assert official.runtime_probe.exact_public_reproduction is True
    assert official.runtime_probe.call_source == "get_config('unused-public-driver-path')"

    assert interactive.runtime_probe.exact_public_reproduction is False
    assert interactive.runtime_probe.call_source == "get_config('tests/test-config/valid-config.yaml')"


def test_level32_hostile_path_reproduction_uses_bound_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hostile PATH regression: interactive task execution uses bound interpreter (sys.executable).

    Proves:
    1. Interactive task materialization binds task["reproduction"]["argv"][0] and
       task["tests"]["full_suite_argv"][0] to sys.executable.
    2. When a hostile fake 'python' executable is placed earlier on PATH,
       running reproduction via the real TestRunner executes the bound interpreter
       and does NOT invoke the fake python on PATH.
    3. Official operator route preserves historical scientific default ("python").
    """
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "guaranteed_nonexistent_localappdata"))

    # Seed hermetic source cache
    cache_dir = tmp_path / "hermetic_cache" / "cookiecutter-967-base-source"
    _seed_hermetic_level32_source(cache_dir)

    # 1. Interactive materialization binds sys.executable
    staging_interactive = tmp_path / "staging_interactive"
    fixture = materialize_level32_task(
        staging_interactive,
        mode=SourceAcquisitionMode.INTERACTIVE_CACHE_FIRST,
        cache_dir=cache_dir,
    )
    task_data = json.loads((fixture / "task.json").read_text(encoding="utf-8"))
    assert task_data["reproduction"]["argv"][0] == sys.executable
    assert task_data["tests"]["full_suite_argv"][0] == sys.executable

    # 2. Hostile PATH environment setup
    fake_bin = tmp_path / "fake_bin"
    fake_bin.mkdir(parents=True, exist_ok=True)
    fake_marker = tmp_path / "fake_python_called.txt"

    if sys.platform == "win32":
        fake_py = fake_bin / "python.bat"
        fake_py.write_text(
            f"@echo off\r\necho called > \"{fake_marker}\"\r\nexit /b 99\r\n",
            encoding="utf-8",
        )
    else:
        fake_py = fake_bin / "python"
        fake_py.write_text(
            f"#!/bin/sh\necho called > \"{fake_marker}\"\nexit 99\n",
            encoding="utf-8",
        )
        fake_py.chmod(0o755)

    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")

    # Load task and execute reproduction through real TestRunner in real disposable workspace
    task = load_task(str(fixture / "task.json"))
    workspaces_dir = tmp_path / "workspaces"
    workspaces_dir.mkdir(parents=True, exist_ok=True)
    workspace = TaskWorkspace(str(fixture), parent_dir=str(workspaces_dir))
    runner = TestRunner(workspace)

    run_result = runner.run_reproduction(task)

    # Prove fake python on PATH was never executed
    assert not fake_marker.exists()
    # Unpatched baseline reproduction exits with 1 (expected failure)
    assert run_result.command_result.exit_code == 1
    assert run_result.reproduction_match is True
    assert run_result.passed is False

    # 3. Official operator route preserves historical "python"
    official_fixture = tmp_path / "official_scaffold"
    write_public_scaffold(official_fixture, "Official problem statement")
    official_task_data = json.loads(
        (official_fixture / "task.json").read_text(encoding="utf-8")
    )
    assert official_task_data["reproduction"]["argv"][0] == "python"
    assert official_task_data["tests"]["full_suite_argv"][0] == "python"

    # Also test materialize_level32_task in official mode with mocked official row
    official_staging = tmp_path / "official_staging"
    monkeypatch.setattr(
        "agentic_debugger.application.level32_materialization.load_official_row",
        lambda **_kw: {"problem_statement": "Official problem statement"},
    )
    monkeypatch.setattr(
        "agentic_debugger.application.level32_materialization.copy_image_source",
        lambda *args, **kwargs: None,
    )
    official_mat_fixture = materialize_level32_task(
        official_staging,
        mode=SourceAcquisitionMode.OFFICIAL_FROZEN_DOCKER_ONLY,
    )
    mat_task_data = json.loads(
        (official_mat_fixture / "task.json").read_text(encoding="utf-8")
    )
    assert mat_task_data["reproduction"]["argv"][0] == "python"
    assert mat_task_data["tests"]["full_suite_argv"][0] == "python"
