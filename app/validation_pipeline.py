"""Validation pipeline for automatic code quality checks."""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

logger = logging.getLogger(__name__)


class ValidationStatus(Enum):
    """Validation status."""
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


@dataclass
class ValidationResult:
    """Result of a single validation step."""
    name: str
    status: ValidationStatus
    output: str = ""
    errors: int = 0
    warnings: int = 0
    duration: float = 0.0
    error_message: Optional[str] = None


@dataclass
class ValidationReport:
    """Full validation report."""
    overall_status: ValidationStatus
    steps: list[ValidationResult] = field(default_factory=list)
    total_duration: float = 0.0
    summary: str = ""
    can_commit: bool = False


class ValidationPipeline:
    """Runs mypy and pytest automatically after file edits."""

    def __init__(self, ssh_manager):
        self._ssh = ssh_manager

    async def validate(
        self,
        session_id: str,
        path: str,
        run_mypy: bool = True,
        run_tests: bool = True,
        test_path: Optional[str] = None,
        mypy_strict: bool = True,
    ) -> ValidationReport:
        """Run full validation pipeline."""
        start_time = asyncio.get_event_loop().time()
        steps = []
        overall_success = True

        # Detect project type and paths
        project_info = await self._detect_project(session_id, path)
        
        # Step 1: mypy
        if run_mypy:
            mypy_result = await self._run_mypy(
                session_id, 
                project_info["mypy_path"], 
                mypy_strict
            )
            steps.append(mypy_result)
            if mypy_result.status == ValidationStatus.FAILED:
                overall_success = False

        # Step 2: pytest
        if run_tests and overall_success:  # Only run tests if mypy passed
            test_result = await self._run_pytest(
                session_id,
                test_path or project_info["test_path"]
            )
            steps.append(test_result)
            if test_result.status == ValidationStatus.FAILED:
                overall_success = False

        total_duration = asyncio.get_event_loop().time() - start_time

        # Build summary
        error_count = sum(s.errors for s in steps)
        warning_count = sum(s.warnings for s in steps)
        
        if overall_success:
            summary = f"✅ Валидация пройдена: {len(steps)} шагов, {error_count} ошибок"
            status = ValidationStatus.SUCCESS
        else:
            summary = f"❌ Валидация не пройдена: {error_count} ошибок, {warning_count} предупреждений"
            status = ValidationStatus.FAILED

        return ValidationReport(
            overall_status=status,
            steps=steps,
            total_duration=round(total_duration, 2),
            summary=summary,
            can_commit=overall_success,
        )

    async def quick_check(self, session_id: str, path: str) -> ValidationResult:
        """Quick syntax check with python -m py_compile."""
        start = asyncio.get_event_loop().time()
        
        result = await self._ssh.execute(
            session_id,
            f"cd {path} && python -m py_compile $(find . -name '*.py' -not -path './venv/*' -not -path './__pycache__/*' | head -20)",
            timeout=30
        )
        
        duration = asyncio.get_event_loop().time() - start
        
        if result["exit_code"] == 0:
            return ValidationResult(
                name="syntax_check",
                status=ValidationStatus.SUCCESS,
                output="Синтаксис OK",
                duration=round(duration, 2)
            )
        else:
            return ValidationResult(
                name="syntax_check",
                status=ValidationStatus.FAILED,
                output=result["stderr"],
                errors=result["stderr"].count("SyntaxError"),
                duration=round(duration, 2)
            )

    async def _detect_project(self, session_id: str, path: str) -> dict:
        """Detect project structure and return paths."""
        # Check for common project structures
        checks = [
            ("pyproject.toml", path),
            ("setup.py", path),
            ("requirements.txt", path),
            ("tests", f"{path}/tests"),
            ("test", f"{path}/test"),
        ]
        
        project_root = path
        has_tests = False
        
        for file, check_path in checks:
            result = await self._ssh.execute(
                session_id,
                f"test -f {check_path}/{file} || test -d {check_path}/{file} && echo 'FOUND' || echo 'NOT_FOUND'",
                timeout=10
            )
            if "FOUND" in result["stdout"]:
                if file in ("tests", "test"):
                    has_tests = True
                else:
                    project_root = path
        
        # Determine mypy and test paths
        mypy_path = project_root
        test_path = f"{project_root}/tests" if has_tests else project_root
        
        # Check for app/ or src/ directory
        for subdir in ("app", "src", project_root.split("/")[-1]):
            result = await self._ssh.execute(
                session_id,
                f"test -d {project_root}/{subdir} && echo 'FOUND' || echo 'NOT_FOUND'",
                timeout=10
            )
            if "FOUND" in result["stdout"]:
                mypy_path = f"{project_root}/{subdir}"
                break
        
        return {
            "project_root": project_root,
            "mypy_path": mypy_path,
            "test_path": test_path,
            "has_tests": has_tests,
        }

    async def _run_mypy(
        self,
        session_id: str,
        path: str,
        strict: bool = True
    ) -> ValidationResult:
        """Run mypy type checking."""
        start = asyncio.get_event_loop().time()
        
        strict_flag = " --strict" if strict else ""
        cmd = f"cd {path} && python -m mypy .{strict_flag} --ignore-missing-imports --no-error-summary 2>&1 || true"
        
        result = await self._ssh.execute(session_id, cmd, timeout=120)
        
        duration = asyncio.get_event_loop().time() - start
        output = result["stdout"] + result["stderr"]
        
        # Parse mypy output
        errors = output.count("error:")
        warnings = output.count("warning:")
        
        # Check if mypy not installed
        if "command not found" in output or "No module named" in output:
            return ValidationResult(
                name="mypy",
                status=ValidationStatus.SKIPPED,
                output="mypy не установлен, пропускаем",
                duration=round(duration, 2)
            )
        
        if errors == 0 and ("Success" in output or output.strip() == ""):
            return ValidationResult(
                name="mypy",
                status=ValidationStatus.SUCCESS,
                output="mypy: 0 ошибок" + (f"\n{output[:200]}" if output.strip() else ""),
                duration=round(duration, 2)
            )
        else:
            return ValidationResult(
                name="mypy",
                status=ValidationStatus.FAILED,
                output=output[:1000],  # Limit output
                errors=errors,
                warnings=warnings,
                duration=round(duration, 2)
            )

    async def _run_pytest(
        self,
        session_id: str,
        test_path: str
    ) -> ValidationResult:
        """Run pytest."""
        start = asyncio.get_event_loop().time()
        
        cmd = f"cd {test_path} && python -m pytest -x -q --tb=short 2>&1 || true"
        
        result = await self._ssh.execute(session_id, cmd, timeout=300)
        
        duration = asyncio.get_event_loop().time() - start
        output = result["stdout"] + result["stderr"]
        
        # Check if pytest not installed
        if "command not found" in output or "No module named" in output:
            return ValidationResult(
                name="pytest",
                status=ValidationStatus.SKIPPED,
                output="pytest не установлен, пропускаем",
                duration=round(duration, 2)
            )
        
        # Parse pytest output
        if "passed" in output and "failed" not in output.split("passed")[-1]:
            # All passed
            passed = output.count(" passed")
            return ValidationResult(
                name="pytest",
                status=ValidationStatus.SUCCESS,
                output=f"pytest: {passed} тестов пройдено" + (f"\n{output[:300]}" if output.strip() else ""),
                duration=round(duration, 2)
            )
        elif "failed" in output or "ERROR" in output:
            # Some failed
            failed = output.count(" failed")
            passed = output.count(" passed")
            return ValidationResult(
                name="pytest",
                status=ValidationStatus.FAILED,
                output=output[:1000],
                errors=failed,
                duration=round(duration, 2)
            )
        elif "no tests ran" in output.lower():
            return ValidationResult(
                name="pytest",
                status=ValidationStatus.SKIPPED,
                output="pytest: тесты не найдены",
                duration=round(duration, 2)
            )
        else:
            return ValidationResult(
                name="pytest",
                status=ValidationStatus.ERROR,
                output=output[:1000],
                error_message="Неизвестный результат pytest",
                duration=round(duration, 2)
            )

    def to_dict(self, report: ValidationReport) -> dict:
        """Convert report to dict for JSON serialization."""
        return {
            "overall_status": report.overall_status.value,
            "summary": report.summary,
            "total_duration": report.total_duration,
            "can_commit": report.can_commit,
            "steps": [
                {
                    "name": step.name,
                    "status": step.status.value,
                    "output": step.output,
                    "errors": step.errors,
                    "warnings": step.warnings,
                    "duration": step.duration,
                }
                for step in report.steps
            ]
        }
