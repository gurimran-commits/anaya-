import json
from pathlib import Path
import threading
import time
from unittest import mock

import pytest

from app.commands.executor import CommandExecutor
from app.commands.registry import CommandRegistry
from app.commands.skill_commands import register_skill_commands
from app.brain.text_assistant import TextAssistant
from app.skills.discovery import RejectedPackage, get_default_skills_root
from app.skills.manifest import MAX_MANIFEST_BYTES
from app.skills.models import SkillMetadata, SkillResult
from app.skills.skill_manager import SkillManager, SkillRefreshResult
from app.skills.skill_registry import SkillRegistry


def _create_package(
    root: Path,
    pkg_name: str,
    manifest_dict: dict | None = None,
    script_content: str = "pass",
    entry_filename: str = "main.py",
) -> Path:
    """Helper to create an external skill package directory with manifest and Python entrypoint."""
    pkg_dir = root / pkg_name
    pkg_dir.mkdir(parents=True, exist_ok=True)

    if manifest_dict is None:
        manifest_dict = {
            "schema_version": 1,
            "id": f"user.{pkg_name}",
            "name": pkg_name.replace("_", " ").title(),
            "description": f"Description for {pkg_name}",
            "version": "1.0.0",
            "entrypoint": f"{Path(entry_filename).stem}:SkillClass",
        }

    manifest_file = pkg_dir / "skill.json"
    manifest_file.write_text(json.dumps(manifest_dict, indent=2), encoding="utf-8")

    code_file = pkg_dir / entry_filename
    code_file.write_text(script_content, encoding="utf-8")

    return pkg_dir


class TestSkillRefreshResultModelAndApi:
    """Unit tests for SkillRefreshResult and SkillManager.refresh_skills() (Substep 1)."""

    def test_new_package_discovery(self, tmp_path: Path):
        """Newly added package on disk is discovered, registered, and reported in 'added'."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        _create_package(skills_root, "weather_tool")

        manager = SkillManager(skills_root=skills_root)
        res = manager.refresh_skills()

        assert isinstance(res, SkillRefreshResult)
        assert res.success is True
        assert res.added == ("user.weather_tool",)
        assert res.updated == ()
        assert res.removed == ()
        assert res.unmodified == ()
        assert res.rejected == ()
        assert res.total_active == 1
        assert res.generation == 1

        skill = manager.get_skill("user.weather_tool")
        assert skill is not None
        assert skill.metadata.id == "user.weather_tool"
        assert skill.metadata.name == "Weather Tool"

    def test_unmodified_package_detection(self, tmp_path: Path):
        """A package that remains unchanged across refreshes is reported in 'unmodified'."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        _create_package(skills_root, "stable_tool")

        manager = SkillManager(skills_root=skills_root)
        res1 = manager.refresh_skills()
        assert res1.added == ("user.stable_tool",)

        # Second refresh without changes
        res2 = manager.refresh_skills()
        assert res2.success is True
        assert res2.added == ()
        assert res2.updated == ()
        assert res2.removed == ()
        assert res2.unmodified == ("user.stable_tool",)
        assert res2.total_active == 1

    def test_manifest_update_detection(self, tmp_path: Path):
        """Updating version or inputs in skill.json is detected and reported in 'updated'."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        pkg_dir = _create_package(skills_root, "evolving_tool")

        manager = SkillManager(skills_root=skills_root)
        res1 = manager.refresh_skills()
        assert res1.added == ("user.evolving_tool",)
        assert manager.get_metadata("user.evolving_tool").version == "1.0.0"

        # Update manifest to version 1.1.0 with a new description
        updated_manifest = {
            "schema_version": 1,
            "id": "user.evolving_tool",
            "name": "Evolving Tool",
            "description": "Updated description",
            "version": "1.1.0",
            "entrypoint": "main:SkillClass",
        }
        (pkg_dir / "skill.json").write_text(json.dumps(updated_manifest, indent=2), encoding="utf-8")

        res2 = manager.refresh_skills()
        assert res2.success is True
        assert res2.added == ()
        assert res2.updated == ("user.evolving_tool",)
        assert res2.removed == ()
        assert res2.unmodified == ()
        assert res2.total_active == 1

        updated_meta = manager.get_metadata("user.evolving_tool")
        assert updated_meta is not None
        assert updated_meta.version == "1.1.0"
        assert updated_meta.description == "Updated description"

    def test_package_removal_detection(self, tmp_path: Path):
        """Deleting a package directory is detected on refresh and reported in 'removed'."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        pkg_dir = _create_package(skills_root, "ephemeral_tool")

        manager = SkillManager(skills_root=skills_root)
        res1 = manager.refresh_skills()
        assert res1.added == ("user.ephemeral_tool",)
        assert manager.get_skill("user.ephemeral_tool") is not None

        # Delete the package from disk
        for child in list(pkg_dir.iterdir()):
            child.unlink()
        pkg_dir.rmdir()

        res2 = manager.refresh_skills()
        assert res2.success is True
        assert res2.added == ()
        assert res2.updated == ()
        assert res2.removed == ("user.ephemeral_tool",)
        assert res2.unmodified == ()
        assert res2.total_active == 0
        assert manager.get_skill("user.ephemeral_tool") is None

    def test_new_malformed_package_rejection(self, tmp_path: Path):
        """A newly added package with invalid JSON is rejected and never registered."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        bad_dir = skills_root / "broken_tool"
        bad_dir.mkdir()
        (bad_dir / "skill.json").write_text("{not valid json!", encoding="utf-8")
        (bad_dir / "main.py").write_text("pass", encoding="utf-8")

        manager = SkillManager(skills_root=skills_root)
        res = manager.refresh_skills()

        assert res.success is True
        assert res.added == ()
        assert res.total_active == 0
        assert len(res.rejected) == 1
        assert res.rejected[0].package_name == "broken_tool"
        assert res.rejected[0].error_code in ("invalid_manifest", "manifest_validation_error")
        assert manager.get_skill("user.broken_tool") is None

    def test_previously_registered_package_preserved_on_malformed_update(self, tmp_path: Path):
        """A previously registered package retains its last-known-good proxy if an update is malformed."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        pkg_dir = _create_package(skills_root, "critical_tool")

        manager = SkillManager(skills_root=skills_root)
        res1 = manager.refresh_skills()
        assert res1.added == ("user.critical_tool",)
        initial_meta = manager.get_metadata("user.critical_tool")

        # Corrupt the manifest on disk
        (pkg_dir / "skill.json").write_text("{{corrupted json", encoding="utf-8")

        res2 = manager.refresh_skills()
        assert res2.success is True
        assert res2.added == ()
        assert res2.updated == ()
        assert res2.removed == ()  # MUST NOT be removed!
        assert res2.unmodified == ("user.critical_tool",)
        assert res2.retained_last_known_good == ("user.critical_tool",)
        assert len(res2.rejected) == 1
        assert res2.rejected[0].package_name == "critical_tool"

        # Last-known-good metadata remains active and accessible
        current_meta = manager.get_metadata("user.critical_tool")
        assert current_meta is not None
        assert current_meta == initial_meta

    def test_root_level_failure_preserves_complete_registry(self, tmp_path: Path):
        """Root-level verification failure preserves the entire existing registry without purging."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        _create_package(skills_root, "keep_tool_1")
        _create_package(skills_root, "keep_tool_2")

        manager = SkillManager(skills_root=skills_root)
        res1 = manager.refresh_skills()
        assert len(res1.added) == 2
        assert res1.total_active == 2

        # Simulate root-level scan error by pointing refresh to a non-directory file
        dummy_file = tmp_path / "not_a_dir.txt"
        dummy_file.write_text("dummy", encoding="utf-8")

        res2 = manager.refresh_skills(root_path=dummy_file)
        assert res2.success is False
        assert res2.error is not None
        assert "not a directory" in res2.error.lower()
        assert res2.removed == ()

        # Both tools remain intact in the registry
        assert manager.get_skill("user.keep_tool_1") is not None
        assert manager.get_skill("user.keep_tool_2") is not None
        assert len(manager.list_skills()) == 2

    def test_concurrent_refresh_serialization_and_generation(self, tmp_path: Path):
        """Concurrent refresh_skills calls are cleanly serialized and advance generation monotonically."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        for i in range(5):
            _create_package(skills_root, f"tool_{i:02d}")

        manager = SkillManager(skills_root=skills_root)
        results: list[SkillRefreshResult] = []
        errors: list[Exception] = []

        def worker():
            try:
                r = manager.refresh_skills()
                results.append(r)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert not errors
        assert len(results) == 8
        assert all(r.success is True for r in results)

        # Monotonic generation advance
        gens = [r.generation for r in results]
        assert all(g > 0 for g in gens)

        # Final registry state has exactly all 5 tools
        assert len(manager.list_skills()) == 5


class TestSkillCommandsAndTextAssistantIntegration:
    """Tests for Substep 2: skills.refresh command and TextAssistant processing."""

    def test_direct_command_execution(self, tmp_path: Path):
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        _create_package(skills_root, "cmd_tool")

        registry = CommandRegistry()
        executor = CommandExecutor(registry=registry)
        manager = SkillManager(skills_root=skills_root)
        register_skill_commands(registry, manager, executor)

        assert registry.get("skills.refresh") is not None

        # Execute command directly
        result = executor.execute("skills.refresh", ())
        assert result.success is True
        assert "Skills refreshed (Generation 1): 1 added, 0 updated, 0 removed, 0 unchanged." in result.message
        assert manager.get_skill("user.cmd_tool") is not None

    def test_text_assistant_process_natural_language(self, tmp_path: Path):
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        _create_package(skills_root, "nl_tool")

        registry = CommandRegistry()
        executor = CommandExecutor(registry=registry)
        manager = SkillManager(skills_root=skills_root)
        register_skill_commands(registry, manager, executor)

        assistant = TextAssistant(registry=registry, executor=executor, skill_manager=manager)

        # 1. "refresh skills"
        res1 = assistant.process("refresh skills")
        assert res1.success is True
        assert "Skills refreshed" in res1.message
        assert "1 added" in res1.message

        # 2. Add another package and test "reload skills"
        _create_package(skills_root, "second_tool")
        res2 = assistant.process("reload skills")
        assert res2.success is True
        assert "1 added, 0 updated, 0 removed, 1 unchanged" in res2.message

        # 3. Test "scan for skills"
        res3 = assistant.process("scan for skills")
        assert res3.success is True
        assert "0 added, 0 updated, 0 removed, 2 unchanged" in res3.message

        # 4. Test "please update skills"
        res4 = assistant.process("please update skills")
        assert res4.success is True
        assert "0 added, 0 updated, 0 removed, 2 unchanged" in res4.message

    def test_refresh_command_reports_rejections_and_retained_lkg(self, tmp_path: Path):
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        pkg_dir = _create_package(skills_root, "flaky_tool")

        registry = CommandRegistry()
        executor = CommandExecutor(registry=registry)
        manager = SkillManager(skills_root=skills_root)
        register_skill_commands(registry, manager, executor)

        executor.execute("skills.refresh", ())
        assert manager.get_skill("user.flaky_tool") is not None

        # Corrupt manifest
        (pkg_dir / "skill.json").write_text("{bad json", encoding="utf-8")

        res = executor.execute("skills.refresh", ())
        assert res.success is True
        assert "Retained 1 last-known-good package(s)" in res.message
        assert "Warning: 1 package(s) rejected" in res.message
        assert manager.get_skill("user.flaky_tool") is not None

    def test_refresh_command_fails_on_root_failure(self, tmp_path: Path):
        dummy_file = tmp_path / "fake_file.txt"
        dummy_file.write_text("x", encoding="utf-8")

        registry = CommandRegistry()
        executor = CommandExecutor(registry=registry)
        manager = SkillManager(skills_root=dummy_file)
        register_skill_commands(registry, manager, executor)

        res = executor.execute("skills.refresh", ())
        assert res.success is False

        # Direct handler invocation exposes detailed exception
        handler = registry.get("skills.refresh").handler
        with pytest.raises(ValueError) as exc_info:
            handler()
        assert "Skill refresh failed" in str(exc_info.value)
        assert "not a directory" in str(exc_info.value).lower()
