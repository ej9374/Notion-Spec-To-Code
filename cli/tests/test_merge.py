from pathlib import Path
from unittest.mock import patch

import pytest

from merge import (
    SIMILARITY_THRESHOLD,
    _content_similarity,
    _find_exact_file,
    _find_similar_by_content,
    _resolve_target_path,
    _show_diff,
    merge_files,
)


class TestFindExactFile:
    def test_finds_by_stem(self, tmp_path):
        target = tmp_path / "UserRequest.java"
        target.write_text("class UserRequest {}")
        assert _find_exact_file("UserRequest.java", tmp_path) == target

    def test_returns_none_when_not_found(self, tmp_path):
        assert _find_exact_file("NonExistent.java", tmp_path) is None

    def test_case_insensitive_match(self, tmp_path):
        target = tmp_path / "UserRequest.java"
        target.write_text("class UserRequest {}")
        assert _find_exact_file("userrequest.java", tmp_path) == target

    def test_finds_in_nested_subdirectory(self, tmp_path):
        nested = tmp_path / "src" / "main" / "java"
        nested.mkdir(parents=True)
        target = nested / "UserRequest.java"
        target.write_text("class UserRequest {}")
        assert _find_exact_file("UserRequest.java", tmp_path) == target


class TestContentSimilarity:
    def test_identical_content_is_1(self):
        assert _content_similarity("hello", "hello") == 1.0

    def test_completely_different_is_low(self):
        assert _content_similarity("aaa", "zzz") < 0.5

    def test_partially_similar(self):
        a = "class UserCreateRequest { String name; String email; }"
        b = "class UserRegistrationRequest { String name; String email; String phone; }"
        ratio = _content_similarity(a, b)
        assert 0.0 < ratio < 1.0


class TestFindSimilarByContent:
    def test_finds_similar_file(self, tmp_path):
        existing = tmp_path / "UserCreateRequest.java"
        existing.write_text("class Foo { String name; String email; }")
        new_content = "class Bar { String name; String email; }"

        results = _find_similar_by_content(new_content, "Bar.java", tmp_path)
        assert len(results) == 1
        assert results[0][0] == existing
        assert results[0][1] >= SIMILARITY_THRESHOLD

    def test_excludes_exact_name_match(self, tmp_path):
        # exact match 파일은 이미 1단계에서 처리하므로 결과에서 제외돼야 함
        same_name = tmp_path / "Bar.java"
        same_name.write_text("class Bar { String name; }")
        new_content = "class Bar { String name; }"

        results = _find_similar_by_content(new_content, "Bar.java", tmp_path)
        assert all(r[0].stem.lower() != "bar" for r in results)

    def test_returns_empty_when_nothing_similar(self, tmp_path):
        existing = tmp_path / "Unrelated.java"
        existing.write_text("public interface CompletelyDifferent { void xyz(); }")
        new_content = "class Foo { @NotNull Long concertId; @NotNull Long seatId; }"

        results = _find_similar_by_content(new_content, "Foo.java", tmp_path)
        assert results == []

    def test_sorted_by_similarity_descending(self, tmp_path):
        base = "class Req { String name; String email; String phone; }"
        (tmp_path / "A.java").write_text("class A { String name; String email; String phone; }")
        (tmp_path / "B.java").write_text("class B { String name; }")

        results = _find_similar_by_content(base, "New.java", tmp_path)
        if len(results) >= 2:
            assert results[0][1] >= results[1][1]


class TestShowDiff:
    def test_shows_additions_and_removals(self):
        diff = _show_diff("class A { int x; }", "class A { int y; }", "A.java")
        assert "-" in diff
        assert "+" in diff

    def test_empty_string_for_identical_content(self):
        content = "class A {}"
        assert _show_diff(content, content, "A.java") == ""

    def test_contains_filename_headers(self):
        diff = _show_diff("old", "new", "Test.java")
        assert "기존/Test.java" in diff
        assert "신규/Test.java" in diff


class TestResolveTargetPath:
    def test_uses_package_declaration(self, tmp_path):
        content = "package com.example.dto;\npublic class Foo {}"
        path = _resolve_target_path("Foo.java", content, tmp_path)
        assert path == tmp_path / "src/main/java/com/example/dto/Foo.java"

    def test_creates_parent_directories(self, tmp_path):
        content = "package com.example.dto;\npublic class Foo {}"
        path = _resolve_target_path("Foo.java", content, tmp_path)
        assert path.parent.exists()

    def test_fallback_package_when_missing(self, tmp_path):
        content = "public class Foo {}"
        path = _resolve_target_path("Foo.java", content, tmp_path)
        assert "com/example/dto" in str(path)

    def test_test_files_go_to_test_directory(self, tmp_path):
        content = "package com.example.controller;\npublic class OrderControllerTest {}"
        path = _resolve_target_path("OrderControllerTest.java", content, tmp_path)
        assert "src/test/java" in str(path)
        assert "src/main/java" not in str(path)

    def test_tests_plural_files_go_to_test_directory(self, tmp_path):
        """*Tests.java (복수형)도 src/test/java로 라우팅되어야 한다.
        Gemini가 복수형을 생성할 때 src/main/java에 들어가면
        @SpringBootTest import 오류로 compileJava가 실패한다.
        """
        content = "package eunji.ticketing;\npublic class TicketingApplicationTests {}"
        path = _resolve_target_path("TicketingApplicationTests.java", content, tmp_path)
        assert "src/test/java" in str(path)
        assert "src/main/java" not in str(path)

    def test_non_test_files_go_to_main_directory(self, tmp_path):
        content = "package com.example.controller;\npublic class OrderController {}"
        path = _resolve_target_path("OrderController.java", content, tmp_path)
        assert "src/main/java" in str(path)
        assert "src/test/java" not in str(path)


class TestMergeFiles:
    def test_skips_on_denial(self, tmp_path):
        files = [{"filename": "UserRequest.java", "content": "class UserRequest {}"}]
        with patch("merge._ask_approval", return_value=False):
            merge_files(files, tmp_path)
        assert list(tmp_path.rglob("*.java")) == []

    def test_creates_new_file_on_approval(self, tmp_path):
        content = "package com.example.dto;\npublic class UserRequest {}"
        files = [{"filename": "UserRequest.java", "content": content}]
        with patch("merge._ask_approval", return_value=True):
            merge_files(files, tmp_path)
        assert len(list(tmp_path.rglob("*.java"))) == 1

    def test_overwrites_exact_match_on_approval(self, tmp_path):
        old_file = tmp_path / "UserRequest.java"
        old_file.write_text("class UserRequest { /* old */ }")
        files = [{"filename": "UserRequest.java", "content": "class UserRequest { /* new */ }"}]
        with patch("merge._ask_approval", return_value=True):
            merge_files(files, tmp_path)
        assert "new" in old_file.read_text()

    def test_overwrites_similar_file_on_approval(self, tmp_path):
        # 파일명은 다르지만 내용이 유사한 경우
        old_file = tmp_path / "UserCreateRequest.java"
        old_file.write_text("class UserCreateRequest { String name; String email; }")
        new_content = "class UserRegistrationRequest { String name; String email; String phone; }"
        files = [{"filename": "UserRegistrationRequest.java", "content": new_content}]

        with patch("merge._ask_overwrite_similar", return_value=True):
            merge_files(files, tmp_path)

        assert "UserRegistrationRequest" in old_file.read_text()

    def test_creates_new_when_similar_declined(self, tmp_path):
        # 유사 파일 덮어쓰기를 거절하면 새 파일로 생성
        old_file = tmp_path / "UserCreateRequest.java"
        old_file.write_text("class UserCreateRequest { String name; String email; }")
        new_content = "package p;\nclass UserRegistrationRequest { String name; String email; String phone; }"
        files = [{"filename": "UserRegistrationRequest.java", "content": new_content}]

        with patch("merge._ask_overwrite_similar", return_value=False):
            with patch("merge._ask_approval", return_value=True):
                merge_files(files, tmp_path)

        java_files = {f.name for f in tmp_path.rglob("*.java")}
        assert "UserCreateRequest.java" in java_files
        assert "UserRegistrationRequest.java" in java_files

    def test_processes_multiple_files(self, tmp_path):
        files = [
            {"filename": "A.java", "content": "package p;\nclass A {}"},
            {"filename": "B.java", "content": "package p;\nclass B {}"},
        ]
        # A.java와 B.java는 내용이 유사해서 유사 파일 질문도 mock 처리
        with patch("merge._ask_approval", return_value=True):
            with patch("merge._ask_overwrite_similar", return_value=False):
                merge_files(files, tmp_path)
        assert len(list(tmp_path.rglob("*.java"))) == 2

    def test_exception_in_merge_single_propagates(self, tmp_path):
        """_merge_single에서 발생한 예외가 merge_files 밖으로 전파돼야 한다.
        CLAUDE.md: '에러는 절대 묵살하지 말고 위로 올릴 것'
        """
        files = [{"filename": "A.java", "content": "class A {}"}]
        with patch("merge._merge_single", side_effect=RuntimeError("disk full")):
            with pytest.raises(RuntimeError, match="disk full"):
                merge_files(files, tmp_path)

    def test_find_functions_called_once_per_file_not_twice(self, tmp_path):
        """요약 테이블과 실제 merge가 각각 파일시스템을 스캔하지 않고 한 번만 스캔해야 한다."""
        files = [{"filename": "A.java", "content": "package p;\nclass A {}"}]
        with patch("merge._find_exact_file", return_value=None) as mock_exact:
            with patch("merge._find_similar_by_content", return_value=[]) as mock_similar:
                with patch("merge._ask_approval", return_value=False):
                    merge_files(files, tmp_path)
        assert mock_exact.call_count == 1, f"_find_exact_file {mock_exact.call_count}회 호출 (1회 예상)"
        assert mock_similar.call_count == 1, f"_find_similar_by_content {mock_similar.call_count}회 호출 (1회 예상)"
