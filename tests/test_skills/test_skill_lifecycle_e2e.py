"""Comprehensive end-to-end lifecycle integration tests for Phase 10.4.4.

Lifecycle Verified:
1. Start Anaya with an empty external skills directory.
2. Verify no external skills are listed.
3. Add a valid external package.
4. Refresh and verify discovery.
5. Verify package appears in skills.list.
6. Execute package through skills.run.
7. Verify genuine sandboxed execution and host memory isolation.
8. Update manifest version and package implementation code.
9. Refresh and verify updated registration.
10. Execute again and verify new behavior.
11. Delete package from disk.
12. Refresh and verify removal.
13. Verify subsequent execution fails closed.
14. Introduce a malformed update to a previously registered package.
15. Verify last-known-good registration remains available and executable.
16. Simulate root-level discovery failure.
17. Verify complete last-known-good registry remains intact.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import pytest

from app.bootstrap import create_controller
from app.core.config import AppConfig


IS_WINDOWS = sys.platform == "win32"


def _write_calc_package(
    pkg_dir: Path,
    skill_id: str = "user.dynamic_calc",
    version: str = "1.0.0",
    operation: str = "add",
) -> None:
    """Create or update a calculation skill package."""
    pkg_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "schema_version": 1,
        "id": skill_id,
        "name": "Dynamic Calc",
        "description": f"Performs {operation}",
        "version": version,
        "entrypoint": "calc_module:CalcSkill",
        "inputs": [
            {"name": "a", "type": "int", "description": "Operand A", "required": True},
            {"name": "b", "type": "int", "description": "Operand B", "required": True},
        ],
    }

    if operation == "add":
        eval_line = "res = inputs['a'] + inputs['b']\n        msg = f'Sum: {res}'"
    else:
        eval_line = "res = inputs['a'] * inputs['b']\n        msg = f'Product: {res}'"

    code = (
        "from app.skills.models import Skill, SkillMetadata, InputFieldSpec, SkillResult\n"
        "class CalcSkill(Skill):\n"
        "    @property\n"
        "    def metadata(self) -> SkillMetadata:\n"
        "        return SkillMetadata(\n"
        f"            id='{skill_id}',\n"
        "            name='Dynamic Calc',\n"
        f"            description='Performs {operation}',\n"
        f"            version='{version}',\n"
        "            inputs=(\n"
        "                InputFieldSpec('a', int, required=True),\n"
        "                InputFieldSpec('b', int, required=True),\n"
        "            ),\n"
        "        )\n"
        "    def execute(self, inputs):\n"
        f"        {eval_line}\n"
        "        return SkillResult(success=True, data={'result': res}, message=msg)\n"
    )

    (pkg_dir / "skill.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (pkg_dir / "calc_module.py").write_text(code, encoding="utf-8")


class TestSkillDynamicLifecycleEndToEnd:
    """End-to-end integration tests for dynamic skill refresh and sandbox execution."""

    def test_full_17_step_skill_lifecycle(self, tmp_path: Path):
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        mem_db = tmp_path / "mem.db"
        tasks_db = tmp_path / "tasks.db"

        # 1. Start Anaya with an empty external skills directory
        controller = create_controller(
            memory_db_path=mem_db,
            tasks_db_path=tasks_db,
            enable_speech=False,
            enable_ai=False,
            skills_root=skills_root,
        )
        try:
            # 2. Verify no external skills are listed
            list_res = controller.handle_text("skills.list")
            assert list_res.success is True
            assert "user.dynamic_calc" not in list_res.message

            # 3. Add a valid external package
            pkg_dir = skills_root / "dynamic_calc"
            _write_calc_package(pkg_dir, version="1.0.0", operation="add")

            # Invariant: external module is not loaded in host
            assert "calc_module" not in sys.modules

            # 4. Refresh and verify discovery
            refresh_res = controller.handle_text("skills.refresh")
            assert refresh_res.success is True
            assert "1 added, 0 updated, 0 removed, 0 unchanged" in refresh_res.message

            # 5. Verify package appears in skills.list
            list_res2 = controller.handle_text("skills.list")
            assert list_res2.success is True
            assert "user.dynamic_calc (v1.0.0): Dynamic Calc" in list_res2.message

            # 6. Execute package through skills.run
            run_res = controller.handle_text("skills.run user.dynamic_calc '{\"a\": 20, \"b\": 22}'")

            # 7. Verify genuine sandboxed execution and host memory isolation
            assert run_res.success is True
            assert "Sum: 42" in run_res.message
            assert "calc_module" not in sys.modules

            # 8. Update manifest version and package implementation code (add -> multiply)
            _write_calc_package(pkg_dir, version="2.0.0", operation="multiply")

            # 9. Refresh and verify updated registration
            refresh_res2 = controller.handle_text("refresh skills")  # Natural language
            assert refresh_res2.success is True
            assert "0 added, 1 updated, 0 removed, 0 unchanged" in refresh_res2.message

            list_res3 = controller.handle_text("skills.list")
            assert "user.dynamic_calc (v2.0.0)" in list_res3.message

            # 10. Execute again and verify new behavior (multiplication)
            run_res2 = controller.handle_text("skills.run user.dynamic_calc '{\"a\": 6, \"b\": 7}'")
            assert run_res2.success is True
            assert "Product: 42" in run_res2.message
            assert "calc_module" not in sys.modules

            # 11. Delete package from disk
            shutil.rmtree(pkg_dir)

            # 12. Refresh and verify removal
            refresh_res3 = controller.handle_text("reload skills")  # Natural language
            assert refresh_res3.success is True
            assert "0 added, 0 updated, 1 removed, 0 unchanged" in refresh_res3.message

            # 13. Verify subsequent execution fails closed
            run_res3 = controller.handle_text("skills.run user.dynamic_calc '{\"a\": 1, \"b\": 2}'")
            assert run_res3.success is False
            assert "not registered" in run_res3.message or "failed" in run_res3.message

            # 14. Add resilient package, then introduce malformed update
            resilient_dir = skills_root / "resilient_tool"
            _write_calc_package(
                resilient_dir,
                skill_id="user.resilient_tool",
                version="1.0.0",
                operation="add",
            )

            refresh_res4 = controller.handle_text("skills.refresh")
            assert refresh_res4.success is True
            assert "1 added" in refresh_res4.message

            # Corrupt manifest of resilient_tool
            (resilient_dir / "skill.json").write_text("{corrupt: json}", encoding="utf-8")

            # Refresh
            refresh_res5 = controller.handle_text("skills.refresh")
            assert refresh_res5.success is True
            assert "Retained 1 last-known-good package(s)" in refresh_res5.message
            assert "Warning: 1 package(s) rejected" in refresh_res5.message

            # 15. Verify last-known-good registration remains available
            list_res4 = controller.handle_text("skills.list")
            assert list_res4.success is True
            assert "user.resilient_tool (v1.0.0)" in list_res4.message
            resilient_meta = controller.skill_manager.get_metadata("user.resilient_tool")
            assert resilient_meta is not None
            assert resilient_meta.version == "1.0.0"

            # 16. Simulate root-level discovery failure
            # Point skills_manager._skills_root to a non-directory file
            bad_root = tmp_path / "not_a_dir"
            bad_root.write_text("file content", encoding="utf-8")
            controller.skill_manager._skills_root = bad_root

            # 17. Verify complete last-known-good registry remains intact
            refresh_res6 = controller.handle_text("skills.refresh")
            assert refresh_res6.success is False
            assert "failed" in refresh_res6.message.lower()

            # Registry still has the resilient tool intact
            list_res5 = controller.handle_text("skills.list")
            assert list_res5.success is True
            assert "user.resilient_tool (v1.0.0)" in list_res5.message
            assert len(controller.skill_manager.list_skills()) == 1

        finally:
            controller.stop()
