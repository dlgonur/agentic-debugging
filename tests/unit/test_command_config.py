"""Task 8 unit gates: the app-owned command-model configuration contract.

Covers profile validation (malformed, duplicate ids, empty executable,
relative-executable ambiguity, shell metacharacters, huge argv/env,
credential-looking argv/env, control characters), the bounded app-owned
config store (missing file, malformed JSON, unknown fields, no execution on
load), safe fingerprints, and the safe UI summaries.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from agentic_debugger.application.command_config import (
    COMMAND_CONFIG_SCHEMA_VERSION,
    MAX_CONFIG_DIAGNOSTIC_BYTES,
    CommandConfigError,
    CommandConfigNotFoundError,
    CommandModelConfigStore,
    CommandModelProfile,
    _MAX_CONFIG_FILE_BYTES,
)

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "command_models" / "dummy_command_model.py"


def make_profile(**overrides) -> dict:
    profile = {
        "profile_id": "dummy",
        "display_name": "Dummy command model",
        "executable": sys.executable,
        "argv": [str(FIXTURE_PATH), "valid", "--state-dir", "s"],
    }
    profile.update(overrides)
    return profile


def write_config(root: Path, profiles: list) -> None:
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "command-models.json").write_text(
        json.dumps({"schema_version": COMMAND_CONFIG_SCHEMA_VERSION, "profiles": profiles}),
        encoding="utf-8",
    )


class TestProfileValidation:
    def test_valid_profile(self):
        profile = CommandModelProfile.from_mapping(make_profile())
        assert profile.profile_id == "dummy"
        assert profile.live_command()[0] == sys.executable
        assert len(profile.configuration_fingerprint) == 64

    def test_profile_id_pattern_rejected(self):
        for bad in ("", "UPPER", "with space", "-lead", "a" * 65, "has/sep"):
            with pytest.raises(CommandConfigError):
                CommandModelProfile.from_mapping(make_profile(profile_id=bad))

    def test_empty_executable_rejected(self):
        with pytest.raises(CommandConfigError):
            CommandModelProfile.from_mapping(make_profile(executable=""))

    @pytest.mark.parametrize(
        "executable",
        [
            "relative/with/separators.exe",
            ".\\relative.exe",
            "..\\up.exe",
            "folder/tool",
            # Blocker E: drive-relative Windows paths are NOT absolute;
            # their resolution depends on the per-drive current directory.
            "C:relative.exe",
            "C:..\\evil.exe",
            "C:",
            "d:folder\\tool.exe",
            # mixed-separator traversal forms
            "folder\\sub/tool.exe",
            "./model.exe",
            "../model.exe",
        ],
    )
    def test_relative_executable_ambiguity_rejected(self, executable):
        with pytest.raises(CommandConfigError):
            CommandModelProfile.from_mapping(make_profile(executable=executable))

    def test_bare_name_and_absolute_path_accepted(self):
        CommandModelProfile.from_mapping(make_profile(executable="python"))
        CommandModelProfile.from_mapping(make_profile(executable="python.exe"))
        CommandModelProfile.from_mapping(make_profile(executable=r"C:\tools\model.exe"))
        CommandModelProfile.from_mapping(make_profile(executable="/usr/bin/model"))
        # true UNC absolute path
        CommandModelProfile.from_mapping(
            make_profile(executable=r"\\server\share\model.exe")
        )

    def test_public_constructor_defaults_are_self_consistent(self):
        # Blocker E: a direct valid construction using the public dataclass
        # type and its advertised tuple defaults must not fail because its
        # own default representation is rejected by validation.
        profile = CommandModelProfile(
            profile_id="dummy",
            display_name="Dummy command model",
            executable="python",
            argv=("arg1", "arg2"),
            environment=(("MY_VAR", "value"),),
        )
        assert profile.argv == ("arg1", "arg2")
        assert profile.environment == (("MY_VAR", "value"),)
        # the fully-default construction is valid too
        minimal = CommandModelProfile(
            profile_id="dummy",
            display_name="Dummy command model",
            executable="python",
        )
        assert minimal.argv == ()
        assert minimal.environment == ()

    def test_protocol_version_pinned_to_runtime_authority(self):
        # Blocker D: exactly one truthful protocol authority.
        from agentic_debugger.evaluation.live import LIVE_PROTOCOL_VERSION

        # current supported protocol accepted (explicitly and by default)
        profile = CommandModelProfile.from_mapping(
            make_profile(protocol_version=LIVE_PROTOCOL_VERSION)
        )
        assert profile.protocol_version == LIVE_PROTOCOL_VERSION
        omitted = CommandModelProfile.from_mapping(make_profile())
        assert omitted.protocol_version == LIVE_PROTOCOL_VERSION
        # a wrong value is rejected before any executable launch
        with pytest.raises(CommandConfigError):
            CommandModelProfile.from_mapping(make_profile(protocol_version="999"))
        with pytest.raises(CommandConfigError):
            CommandModelProfile.from_mapping(make_profile(protocol_version="1.2"))
        # provenance equals the runtime constant, always
        assert profile.summary().protocol_version == LIVE_PROTOCOL_VERSION

    @pytest.mark.parametrize(
        "argv",
        [
            ["--api-key=sk-abc"],
            ["--token", "abc"],
            ["--password=secret"],
            ["--authorization", "Bearer xyz"],
            ["value with token=abc"],
        ],
    )
    def test_credential_shaped_argv_rejected(self, argv):
        with pytest.raises(CommandConfigError):
            CommandModelProfile.from_mapping(make_profile(argv=argv))

    def test_shell_metacharacters_are_inert_explicit_argv(self):
        # Explicit argv through shell=False: metacharacters are literal
        # arguments, never interpreted by the application.
        profile = CommandModelProfile.from_mapping(
            make_profile(argv=["cmd", "|", "powershell", "-c", "x"])
        )
        assert profile.argv == ("cmd", "|", "powershell", "-c", "x")

    def test_argv_bounds_rejected(self):
        with pytest.raises(CommandConfigError):
            CommandModelProfile.from_mapping(make_profile(argv=["x"] * 32))
        with pytest.raises(CommandConfigError):
            CommandModelProfile.from_mapping(make_profile(argv=["x" * 513]))

    def test_control_characters_rejected(self):
        with pytest.raises(CommandConfigError):
            CommandModelProfile.from_mapping(make_profile(display_name="bad\x00name"))
        with pytest.raises(CommandConfigError):
            CommandModelProfile.from_mapping(make_profile(argv=["bad\narg"]))

    def test_environment_overrides_validated(self):
        profile = CommandModelProfile.from_mapping(
            make_profile(environment={"MY_VAR": "value", "OTHER": "2"})
        )
        assert profile.environment == (("MY_VAR", "value"), ("OTHER", "2"))
        with pytest.raises(CommandConfigError):
            CommandModelProfile.from_mapping(make_profile(environment={"BAD=NAME": "x"}))
        with pytest.raises(CommandConfigError):
            CommandModelProfile.from_mapping(make_profile(environment={"API_KEY": "sk-abc"}))
        with pytest.raises(CommandConfigError):
            CommandModelProfile.from_mapping(make_profile(environment={"SECRET": "sk-abc"}))
        with pytest.raises(CommandConfigError):
            CommandModelProfile.from_mapping(
                make_profile(environment={f"v{i}": "x" for i in range(9)})
            )

    def test_display_name_credential_shaped_rejected(self):
        with pytest.raises(CommandConfigError):
            CommandModelProfile.from_mapping(make_profile(display_name="api_key=sk-abc"))

    def test_request_timeout_bounds(self):
        with pytest.raises(CommandConfigError):
            CommandModelProfile.from_mapping(make_profile(request_timeout_seconds=0))
        with pytest.raises(CommandConfigError):
            CommandModelProfile.from_mapping(make_profile(request_timeout_seconds=301))
        with pytest.raises(CommandConfigError):
            CommandModelProfile.from_mapping(make_profile(request_timeout_seconds="60"))

    def test_unknown_profile_fields_rejected(self):
        with pytest.raises(CommandConfigError):
            CommandModelProfile.from_mapping(make_profile(shell="powershell"))

    def test_fingerprint_is_deterministic_and_safe(self):
        one = CommandModelProfile.from_mapping(make_profile())
        two = CommandModelProfile.from_mapping(make_profile())
        assert one.configuration_fingerprint == two.configuration_fingerprint
        assert CommandModelProfile.from_mapping(
            make_profile(argv=["x"])
        ).configuration_fingerprint != one.configuration_fingerprint


class TestConfigStore:
    def test_missing_config_is_empty(self, tmp_path):
        store = CommandModelConfigStore(tmp_path)
        assert store.list_profiles() == ()

    def test_load_and_get(self, tmp_path):
        write_config(tmp_path, [make_profile(profile_id="one"), make_profile(profile_id="two")])
        store = CommandModelConfigStore(tmp_path)
        assert [p.profile_id for p in store.list_profiles()] == ["one", "two"]
        assert store.get("two").display_name == "Dummy command model"
        with pytest.raises(CommandConfigNotFoundError):
            store.get("missing")

    def test_duplicate_profile_id_rejected(self, tmp_path):
        write_config(tmp_path, [make_profile(profile_id="dup"), make_profile(profile_id="dup")])
        with pytest.raises(CommandConfigError):
            CommandModelConfigStore(tmp_path).list_profiles()

    def test_malformed_config_rejected(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "command-models.json").write_text("{not-json", encoding="utf-8")
        with pytest.raises(CommandConfigError):
            CommandModelConfigStore(tmp_path).list_profiles()

    def test_wrong_schema_version_rejected(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "command-models.json").write_text(
            json.dumps({"schema_version": "command-models-v999", "profiles": []}),
            encoding="utf-8",
        )
        with pytest.raises(CommandConfigError):
            CommandModelConfigStore(tmp_path).list_profiles()

    def test_unknown_top_level_fields_rejected(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "command-models.json").write_text(
            json.dumps({"schema_version": COMMAND_CONFIG_SCHEMA_VERSION, "profiles": [], "extra": 1}),
            encoding="utf-8",
        )
        with pytest.raises(CommandConfigError):
            CommandModelConfigStore(tmp_path).list_profiles()

    def test_invalid_profile_inside_store_rejected(self, tmp_path):
        write_config(tmp_path, [make_profile(executable="rel/ative.exe")])
        with pytest.raises(CommandConfigError):
            CommandModelConfigStore(tmp_path).list_profiles()

    def test_oversized_config_rejected(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "command-models.json").write_text("x" * (300 * 1024), encoding="utf-8")
        with pytest.raises(CommandConfigError):
            CommandModelConfigStore(tmp_path).list_profiles()

    def test_loading_never_executes_code(self, tmp_path):
        # A config value that would execute if evaluated must stay inert
        # (json.loads only; no constructors, no Python evaluation).
        write_config(
            tmp_path,
            [
                make_profile(
                    display_name="__import__('os').system('echo pwned')",
                    argv=["x"],
                )
            ],
        )
        store = CommandModelConfigStore(tmp_path)
        assert store.list_profiles()[0].display_name.startswith("__import__")

    def test_summaries_are_safe_and_concise(self, tmp_path):
        write_config(tmp_path, [make_profile()])
        summary = CommandModelConfigStore(tmp_path).summaries()[0]
        mapping = summary.to_mapping()
        assert mapping["profile_id"] == "dummy"
        assert mapping["display_name"] == "Dummy command model"
        assert len(mapping["configuration_fingerprint"]) == 64
        # Summaries never expose argv or environment overrides.
        assert "argv" not in mapping
        assert "environment" not in mapping


class TestWorkerScenarioParamShape:
    def test_profile_can_cross_the_worker_boundary_as_scenario_params(self):
        # The worker protocol accepts bounded strings; the profile reference
        # is exactly that: config root + profile id, never a live object.
        from agentic_debugger.application.worker_protocol import _scenario_params

        params = _scenario_params(
            {"config_root": str(Path.cwd()), "profile_id": "dummy", "policy": "pdb-on-uncertainty"}
        )
        assert params["profile_id"] == "dummy"


def write_raw_config(root: Path, text: str) -> None:
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "command-models.json").write_text(text, encoding="utf-8")


class TestSafeDiagnostics:
    """Repair Pass 2 Blocker 1: malformed config must never leak raw
    untrusted values/keys through diagnostics, and every diagnostic must
    stay within the explicit UTF-8 byte bound.
    """

    SECRET = "API_KEY=supersecret-value"

    def test_credential_shaped_schema_version_not_echoed(self, tmp_path):
        write_raw_config(
            tmp_path,
            json.dumps({"schema_version": self.SECRET, "profiles": []}),
        )
        with pytest.raises(CommandConfigError) as excinfo:
            CommandModelConfigStore(tmp_path).list_profiles()
        assert self.SECRET not in str(excinfo.value)
        assert "unsupported command-model configuration version" in str(excinfo.value)

    def test_credential_shaped_unknown_top_level_key_not_echoed(self, tmp_path):
        write_raw_config(
            tmp_path,
            json.dumps(
                {"schema_version": COMMAND_CONFIG_SCHEMA_VERSION, "profiles": [], self.SECRET: 1}
            ),
        )
        with pytest.raises(CommandConfigError) as excinfo:
            CommandModelConfigStore(tmp_path).list_profiles()
        assert self.SECRET not in str(excinfo.value)
        assert "unknown field" in str(excinfo.value)

    def test_credential_shaped_unknown_profile_key_not_echoed(self, tmp_path):
        write_config(tmp_path, [make_profile(**{self.SECRET: 1})])
        with pytest.raises(CommandConfigError) as excinfo:
            CommandModelConfigStore(tmp_path).list_profiles()
        assert self.SECRET not in str(excinfo.value)
        assert "unknown field" in str(excinfo.value)

    def test_credential_shaped_environment_name_not_echoed(self, tmp_path):
        write_config(tmp_path, [make_profile(environment={self.SECRET: "x"})])
        with pytest.raises(CommandConfigError) as excinfo:
            CommandModelConfigStore(tmp_path).list_profiles()
        assert self.SECRET not in str(excinfo.value)
        assert "environment" in str(excinfo.value)

    def test_credential_shaped_protocol_version_not_echoed(self):
        secret = "token=supersecret-value"
        with pytest.raises(CommandConfigError) as excinfo:
            CommandModelProfile.from_mapping(make_profile(protocol_version=secret))
        assert secret not in str(excinfo.value)
        assert "protocol_version" in str(excinfo.value)

    def test_huge_schema_version_stays_within_the_bound(self, tmp_path):
        write_raw_config(
            tmp_path,
            json.dumps({"schema_version": "X" * 200000, "profiles": []}),
        )
        with pytest.raises(CommandConfigError) as excinfo:
            CommandModelConfigStore(tmp_path).list_profiles()
        assert len(str(excinfo.value).encode("utf-8")) <= MAX_CONFIG_DIAGNOSTIC_BYTES

    def test_huge_unknown_field_name_stays_within_the_bound(self, tmp_path):
        write_raw_config(
            tmp_path,
            json.dumps(
                {
                    "schema_version": COMMAND_CONFIG_SCHEMA_VERSION,
                    "profiles": [],
                    "Y" * 200000: 1,
                }
            ),
        )
        with pytest.raises(CommandConfigError) as excinfo:
            CommandModelConfigStore(tmp_path).list_profiles()
        assert len(str(excinfo.value).encode("utf-8")) <= MAX_CONFIG_DIAGNOSTIC_BYTES

    def test_huge_environment_name_stays_within_the_bound(self, tmp_path):
        write_config(tmp_path, [make_profile(environment={"Z" * 129: "x"})])
        with pytest.raises(CommandConfigError) as excinfo:
            CommandModelConfigStore(tmp_path).list_profiles()
        assert len(str(excinfo.value).encode("utf-8")) <= MAX_CONFIG_DIAGNOSTIC_BYTES

    def test_ordinary_configuration_errors_remain_actionable(self, tmp_path):
        # wrong schema version
        write_raw_config(
            tmp_path,
            json.dumps({"schema_version": "command-models-v999", "profiles": []}),
        )
        with pytest.raises(CommandConfigError) as excinfo:
            CommandModelConfigStore(tmp_path).list_profiles()
        assert "unsupported command-model configuration version" in str(excinfo.value)
        # unknown top-level field (count is actionable)
        write_raw_config(
            tmp_path,
            json.dumps(
                {"schema_version": COMMAND_CONFIG_SCHEMA_VERSION, "profiles": [], "extra": 1}
            ),
        )
        with pytest.raises(CommandConfigError) as excinfo:
            CommandModelConfigStore(tmp_path).list_profiles()
        assert "1 unknown field" in str(excinfo.value)
        # invalid profile inside the store keeps its index
        write_config(tmp_path, [make_profile(executable="rel/ative.exe")])
        with pytest.raises(CommandConfigError) as excinfo:
            CommandModelConfigStore(tmp_path).list_profiles()
        assert "profile 0 is invalid" in str(excinfo.value)
        # a valid profile id may be named in "profile not found"
        write_config(tmp_path, [make_profile()])
        with pytest.raises(CommandConfigNotFoundError) as excinfo:
            CommandModelConfigStore(tmp_path).get("missing")
        assert "profile not found: 'missing'" in str(excinfo.value)
        # a raw unvalidated profile id is never repr()'d
        with pytest.raises(CommandConfigNotFoundError) as excinfo:
            CommandModelConfigStore(tmp_path).get(self.SECRET)
        assert self.SECRET not in str(excinfo.value)

    def test_every_diagnostic_is_bounded_by_construction(self):
        # The bound is enforced at construction time, in one place.
        huge = "A" * 100000
        exc = CommandConfigError(huge)
        assert len(str(exc).encode("utf-8")) <= MAX_CONFIG_DIAGNOSTIC_BYTES
        assert len(exc.args[0].encode("utf-8")) <= MAX_CONFIG_DIAGNOSTIC_BYTES


class TestAuthoritativeBoundedRead:
    """Repair Pass 2 Blocker 2: the 256 KiB config-file bound must be an
    authoritative read bound, not a pre-read stat observation.
    """

    def test_bounded_valid_config_loads(self, tmp_path):
        # ordinary valid bounded input
        write_config(tmp_path, [make_profile()])
        profiles = CommandModelConfigStore(tmp_path).list_profiles()
        assert [p.profile_id for p in profiles] == ["dummy"]

    def test_oversized_config_rejected(self, tmp_path):
        write_raw_config(tmp_path, "x" * (_MAX_CONFIG_FILE_BYTES + 1))
        with pytest.raises(CommandConfigError) as excinfo:
            CommandModelConfigStore(tmp_path).list_profiles()
        assert "byte bound" in str(excinfo.value)
        assert len(str(excinfo.value).encode("utf-8")) <= MAX_CONFIG_DIAGNOSTIC_BYTES

    def test_stale_small_stat_cannot_permit_an_oversized_read(self, tmp_path, monkeypatch):
        # TOCTOU seam: the file is small on disk (any pre-read stat sees a
        # small size) but the actual read yields an oversized body, as if
        # the file grew between stat and read.  The authoritative bounded
        # read must reject it; a stale small stat can never permit an
        # oversized read.
        oversized = b"x" * (_MAX_CONFIG_FILE_BYTES + 1024)
        config_dir = tmp_path / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "command-models.json"
        config_path.write_bytes(b"{}")  # small on disk

        real_open = Path.open

        def fake_open(self, *args, **kwargs):
            if self == config_path:
                return io.BytesIO(oversized)
            return real_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", fake_open)

        with pytest.raises(CommandConfigError) as excinfo:
            CommandModelConfigStore(tmp_path).list_profiles()
        assert "byte bound" in str(excinfo.value)

    def test_read_is_bounded_without_any_stat_authority(self, tmp_path, monkeypatch):
        # Repair Pass 3: the bounded read must not depend on ANY pre-read
        # stat/is_file observation.  The loader's correctness is asserted
        # with a valid mechanism (never by mutating ``os.stat_result``, whose
        # fields are read-only): ``Path.stat`` and ``Path.is_file`` are
        # monkeypatched to fail loudly, and the loader must still (a) load a
        # valid config, (b) reject an oversized file via the bounded read,
        # and (c) keep missing-file empty semantics -- proving the direct
        # bounded open is the single filesystem authority.
        config_dir = tmp_path / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "command-models.json"

        def forbidden_stat(self, *args, **kwargs):
            raise AssertionError(
                "config loader must not call Path.stat (no pre-read authority)"
            )

        def forbidden_is_file(self, *args, **kwargs):
            raise AssertionError(
                "config loader must not call Path.is_file (no preflight)"
            )

        monkeypatch.setattr(Path, "stat", forbidden_stat)
        monkeypatch.setattr(Path, "is_file", forbidden_is_file)

        # (a) a valid bounded config still loads without any stat authority
        config_path.write_bytes(
            json.dumps(
                {"schema_version": COMMAND_CONFIG_SCHEMA_VERSION, "profiles": [make_profile()]}
            ).encode("utf-8")
        )
        profiles = CommandModelConfigStore(tmp_path).list_profiles()
        assert [p.profile_id for p in profiles] == ["dummy"]

        # (b) an oversized file is still rejected by the bounded read itself
        config_path.write_bytes(b"y" * (_MAX_CONFIG_FILE_BYTES + 1))
        with pytest.raises(CommandConfigError) as excinfo:
            CommandModelConfigStore(tmp_path).list_profiles()
        assert "byte bound" in str(excinfo.value)

        # (c) a genuinely missing file keeps the empty-profile semantics
        config_path.unlink()
        assert CommandModelConfigStore(tmp_path).list_profiles() == ()

    def test_directory_or_read_error_is_a_safe_bounded_error(self, tmp_path):
        # Repair Pass 3: with no is_file() preflight, an existing path that
        # is a directory (or otherwise unreadable) must become a safe bounded
        # config error -- never conflated with the missing-file semantics.
        config_dir = tmp_path / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        # Make the config path itself a directory: open() then fails with
        # IsADirectoryError (POSIX) / PermissionError (Windows), both OSError.
        (config_dir / "command-models.json").mkdir()
        with pytest.raises(CommandConfigError) as excinfo:
            CommandModelConfigStore(tmp_path).list_profiles()
        assert "could not be read" in str(excinfo.value)
        assert len(str(excinfo.value).encode("utf-8")) <= MAX_CONFIG_DIAGNOSTIC_BYTES

    def test_config_disappearance_during_load_is_empty(self, tmp_path, monkeypatch):
        # A file that disappears during load keeps the missing-file
        # semantics (empty configuration), never a crash.
        config_dir = tmp_path / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "command-models.json"
        config_path.write_bytes(b"{}")

        real_open = Path.open

        def vanishing_open(self, *args, **kwargs):
            if self == config_path:
                raise FileNotFoundError("config vanished")
            return real_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", vanishing_open)
        assert CommandModelConfigStore(tmp_path).list_profiles() == ()

    def test_malformed_utf8_is_a_safe_bounded_error(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "command-models.json").write_bytes(b"\xff\xfe\x00{not utf-8")
        with pytest.raises(CommandConfigError) as excinfo:
            CommandModelConfigStore(tmp_path).list_profiles()
        assert "could not be read" in str(excinfo.value)
        assert len(str(excinfo.value).encode("utf-8")) <= MAX_CONFIG_DIAGNOSTIC_BYTES

    def test_malformed_json_is_a_safe_bounded_error(self, tmp_path):
        write_raw_config(tmp_path, "{not-json")
        with pytest.raises(CommandConfigError) as excinfo:
            CommandModelConfigStore(tmp_path).list_profiles()
        assert "could not be read" in str(excinfo.value)
        assert len(str(excinfo.value).encode("utf-8")) <= MAX_CONFIG_DIAGNOSTIC_BYTES
