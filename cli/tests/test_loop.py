from pathlib import Path
from unittest.mock import patch

import pytest

from loop import MAX_CORRECTIONS, run_correction_loop


class TestRunCorrectionLoop:
    def test_skips_when_no_files(self, tmp_path):
        """기록된 파일이 없으면 Gradle 테스트 없이 즉시 반환한다."""
        run_correction_loop([], tmp_path)  # 예외 없이 종료되면 통과

    def test_skips_when_no_gradlew(self, tmp_path):
        """gradlew가 없으면 테스트를 생략하고 즉시 반환한다."""
        files = [{"filename": "A.java", "content": "class A {}", "path": str(tmp_path / "A.java")}]
        run_correction_loop(files, tmp_path)  # gradlew 없으면 통과

    def test_passes_immediately_on_success(self, tmp_path):
        files = [{"filename": "A.java", "content": "class A {}", "path": str(tmp_path / "A.java")}]

        with patch("loop._run_gradle_tests", return_value=(True, "")) as mock_run:
            run_correction_loop(files, tmp_path)

        mock_run.assert_called_once_with(tmp_path)

    def test_calls_correction_on_failure(self, tmp_path):
        files = [{"filename": "A.java", "content": "class A {}", "path": str(tmp_path / "A.java")}]
        fixed = [{"filename": "A.java", "content": "class A { /* fixed */ }", "path": str(tmp_path / "A.java")}]

        with patch("loop._run_gradle_tests", side_effect=[(False, "error"), (True, "")]):
            with patch("loop._correct_with_gemini", return_value=fixed) as mock_correct:
                with patch("loop._rewrite_files", return_value=fixed):
                    run_correction_loop(files, tmp_path)

        mock_correct.assert_called_once()

    def test_raises_after_max_corrections(self, tmp_path):
        files = [{"filename": "A.java", "content": "class A {}", "path": str(tmp_path / "A.java")}]

        with patch("loop._run_gradle_tests", return_value=(False, "에러 로그")):
            with patch("loop._correct_with_gemini", return_value=files):
                with patch("loop._rewrite_files", return_value=files):
                    with pytest.raises(RuntimeError, match=f"교정 루프 {MAX_CORRECTIONS}회 초과"):
                        run_correction_loop(files, tmp_path)

    def test_error_message_contains_log(self, tmp_path):
        files = [{"filename": "A.java", "content": "class A {}", "path": str(tmp_path / "A.java")}]

        with patch("loop._run_gradle_tests", return_value=(False, "NullPointerException")):
            with patch("loop._correct_with_gemini", return_value=files):
                with patch("loop._rewrite_files", return_value=files):
                    with pytest.raises(RuntimeError, match="NullPointerException"):
                        run_correction_loop(files, tmp_path)

    def test_max_corrections_constant_is_3(self):
        assert MAX_CORRECTIONS == 3
