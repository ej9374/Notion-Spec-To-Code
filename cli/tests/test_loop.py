from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from loop import (
    MAX_CORRECTIONS,
    _FORBIDDEN_JAVA_PATTERNS,
    _run_gradle_compile,
    _validate_java_patterns,
    run_correction_loop,
    run_harness_tests,
)

_ONE_FILE = lambda tmp_path: [  # noqa: E731
    {"filename": "A.java", "content": "class A {}", "path": str(tmp_path / "A.java")}
]


class TestRunCorrectionLoop:
    def test_skips_when_no_files(self, tmp_path):
        """기록된 파일이 없으면 Gradle 테스트 없이 즉시 반환한다."""
        run_correction_loop([], tmp_path)

    def test_skips_when_no_gradlew(self, tmp_path):
        """gradlew가 없으면 컴파일·테스트 모두 생략하고 즉시 반환한다."""
        run_correction_loop(_ONE_FILE(tmp_path), tmp_path)

    def test_passes_immediately_on_success(self, tmp_path):
        files = _ONE_FILE(tmp_path)
        with patch("loop._run_gradle_compile", return_value=(True, "")):
            with patch("loop._run_gradle_tests", return_value=(True, "")) as mock_test:
                run_correction_loop(files, tmp_path)
        mock_test.assert_called_once_with(tmp_path)

    def test_corrects_on_compile_failure(self, tmp_path):
        """컴파일 실패 시 Gemini 교정 후 재시도한다."""
        files = _ONE_FILE(tmp_path)
        fixed = [{"filename": "A.java", "content": "class A { /* fixed */ }", "path": str(tmp_path / "A.java")}]

        compile_results = [(False, "error: cannot find symbol"), (True, "")]
        with patch("loop._run_gradle_compile", side_effect=compile_results):
            with patch("loop._run_gradle_tests", return_value=(True, "")):
                with patch("loop._correct_with_gemini", return_value=fixed) as mock_correct:
                    with patch("loop._rewrite_files", return_value=fixed):
                        run_correction_loop(files, tmp_path)

        mock_correct.assert_called_once()

    def test_corrects_on_test_failure(self, tmp_path):
        """컴파일은 통과했지만 테스트 실패 시 Gemini 교정 후 재시도한다."""
        files = _ONE_FILE(tmp_path)
        fixed = [{"filename": "A.java", "content": "class A { /* fixed */ }", "path": str(tmp_path / "A.java")}]

        with patch("loop._run_gradle_compile", return_value=(True, "")):
            with patch("loop._run_gradle_tests", side_effect=[(False, "FAILED"), (True, "")]):
                with patch("loop._correct_with_gemini", return_value=fixed) as mock_correct:
                    with patch("loop._rewrite_files", return_value=fixed):
                        run_correction_loop(files, tmp_path)

        mock_correct.assert_called_once()

    def test_raises_after_max_corrections_on_compile(self, tmp_path):
        """컴파일이 계속 실패하면 MAX_CORRECTIONS 후 RuntimeError."""
        files = _ONE_FILE(tmp_path)
        with patch("loop._run_gradle_compile", return_value=(False, "컴파일 에러")):
            with patch("loop._correct_with_gemini", return_value=files):
                with patch("loop._rewrite_files", return_value=files):
                    with pytest.raises(RuntimeError, match=f"교정 루프 {MAX_CORRECTIONS}회 초과"):
                        run_correction_loop(files, tmp_path)

    def test_raises_after_max_corrections_on_test(self, tmp_path):
        """테스트가 계속 실패하면 MAX_CORRECTIONS 후 RuntimeError."""
        files = _ONE_FILE(tmp_path)
        with patch("loop._run_gradle_compile", return_value=(True, "")):
            with patch("loop._run_gradle_tests", return_value=(False, "NullPointerException")):
                with patch("loop._correct_with_gemini", return_value=files):
                    with patch("loop._rewrite_files", return_value=files):
                        with pytest.raises(RuntimeError, match="NullPointerException"):
                            run_correction_loop(files, tmp_path)

    def test_max_corrections_constant_is_3(self):
        assert MAX_CORRECTIONS == 3


class TestValidateJavaPatterns:
    def _file(self, name: str, content: str) -> dict:
        return {"filename": name, "content": content}

    def test_clean_file_returns_empty_string(self):
        files = [self._file("OrderService.java", "@Service\npublic class OrderService {}")]
        assert _validate_java_patterns(files) == ""

    def test_detects_service_impl(self):
        files = [self._file("OrderServiceImpl.java", "public class OrderServiceImpl implements OrderService {}")]
        result = _validate_java_patterns(files)
        assert "ServiceImpl 클래스 생성 금지" in result
        assert "OrderServiceImpl.java" in result

    def test_detects_repository_annotation(self):
        files = [self._file("OrderRepository.java", "@Repository\npublic interface OrderRepository {}")]
        assert "@Repository 어노테이션 금지" in _validate_java_patterns(files)

    def test_detects_entity_annotation(self):
        files = [self._file("Order.java", "@Entity\npublic class Order {}")]
        assert "@Entity 어노테이션 금지" in _validate_java_patterns(files)

    def test_detects_jpa_import(self):
        files = [self._file("Foo.java", "import jakarta.persistence.Entity;\npublic class Foo {}")]
        assert "JPA persistence import 금지" in _validate_java_patterns(files)

    def test_detects_repository_import(self):
        files = [self._file("Foo.java", "import org.springframework.data.jpa.repository.JpaRepository;\npublic class Foo {}")]
        assert "Repository import 금지" in _validate_java_patterns(files)

    def test_multiple_files_multiple_violations(self):
        files = [
            self._file("A.java", "@Entity\npublic class A {}"),
            self._file("B.java", "public class BImpl {}"),
        ]
        result = _validate_java_patterns(files)
        assert "A.java" in result
        assert "B.java" in result

    def test_forbidden_patterns_list_not_empty(self):
        assert len(_FORBIDDEN_JAVA_PATTERNS) > 0


class TestCorrectionLoopPatternValidation:
    def test_corrects_on_pattern_violation(self, tmp_path):
        """금지 패턴 위반 시 Gemini 교정 후 재시도한다."""
        files = [{"filename": "A.java", "content": "@Entity\npublic class A {}", "path": str(tmp_path / "A.java")}]
        clean = [{"filename": "A.java", "content": "public class A {}", "path": str(tmp_path / "A.java")}]

        with patch("loop._run_gradle_compile", return_value=(True, "")):
            with patch("loop._run_gradle_tests", return_value=(True, "")):
                with patch("loop._correct_with_gemini", return_value=clean) as mock_correct:
                    with patch("loop._rewrite_files", return_value=clean):
                        run_correction_loop(files, tmp_path)

        mock_correct.assert_called_once()

    def test_raises_after_max_corrections_on_pattern(self, tmp_path):
        """패턴 위반이 계속되면 MAX_CORRECTIONS 후 RuntimeError."""
        files = [{"filename": "A.java", "content": "@Entity\npublic class A {}", "path": str(tmp_path / "A.java")}]

        with patch("loop._correct_with_gemini", return_value=files):
            with patch("loop._rewrite_files", return_value=files):
                with pytest.raises(RuntimeError, match="교정 루프"):
                    run_correction_loop(files, tmp_path)


class TestRunGradleCompile:
    def test_returns_true_when_no_gradlew(self, tmp_path):
        passed, log = _run_gradle_compile(tmp_path)
        assert passed is True
        assert "gradlew 없음" in log

    def test_returns_false_on_nonzero_exit(self, tmp_path):
        (tmp_path / "gradlew").write_text("#!/bin/sh\nexit 1")
        (tmp_path / "gradlew").chmod(0o755)
        passed, _ = _run_gradle_compile(tmp_path)
        assert passed is False

    def test_returns_true_on_zero_exit(self, tmp_path):
        (tmp_path / "gradlew").write_text("#!/bin/sh\necho 'BUILD SUCCESSFUL'\nexit 0")
        (tmp_path / "gradlew").chmod(0o755)
        passed, log = _run_gradle_compile(tmp_path)
        assert passed is True
        assert "BUILD SUCCESSFUL" in log


class TestRunHarnessTests:
    def test_passes_when_pytest_exits_zero(self, tmp_path):
        with patch("loop.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="1 passed", stderr="")
            passed, log = run_harness_tests(tmp_path)
        assert passed is True
        assert "1 passed" in log

    def test_fails_when_pytest_exits_nonzero(self, tmp_path):
        with patch("loop.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="FAILED test_foo")
            passed, log = run_harness_tests(tmp_path)
        assert passed is False
        assert "FAILED" in log

    def test_no_tests_collected_is_treated_as_pass(self, tmp_path):
        with patch("loop.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=5, stdout="no tests ran", stderr="")
            passed, log = run_harness_tests(tmp_path)
        assert passed is True

    def test_runs_pytest_in_cli_root(self, tmp_path):
        with patch("loop.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            run_harness_tests(tmp_path)
        _, kwargs = mock_run.call_args
        assert kwargs["cwd"] == tmp_path

    def test_command_targets_tests_directory(self, tmp_path):
        with patch("loop.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            run_harness_tests(tmp_path)
        args, _ = mock_run.call_args
        assert "tests/" in args[0]
