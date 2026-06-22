from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from generator import (
    _build_dto_prompt,
    _build_group_prompt,
    _detect_package,
    _extract_group,
    _fix_classname_mismatch,
    _group_specs,
    _parse_files,
    _read_existing_file,
    _scan_package_tree,
    generate_code,
    generate_code_all,
)


class TestParseFiles:
    def test_parses_single_file_with_filename(self):
        text = "```java\n// UserRequest.java\npublic class UserRequest {}\n```"
        files = _parse_files(text)
        assert len(files) == 1
        assert files[0]["filename"] == "UserRequest.java"
        assert "UserRequest" in files[0]["content"]

    def test_parses_multiple_files(self):
        text = (
            "```java\n// A.java\nclass A {}\n```\n"
            "```java\n// B.java\nclass B {}\n```"
        )
        files = _parse_files(text)
        assert len(files) == 2
        assert files[0]["filename"] == "A.java"
        assert files[1]["filename"] == "B.java"

    def test_no_code_blocks_returns_empty(self):
        assert _parse_files("설명 텍스트만 있음") == []

    def test_no_filename_comment_extracts_class_name(self):
        text = "```java\npublic class Foo {}\n```"
        files = _parse_files(text)
        assert files[0]["filename"] == "Foo.java"

    def test_no_filename_and_no_class_skipped(self):
        # 파일명 주석도 없고 public class/interface 선언도 없으면 스킵
        text = "```java\nint x = 1;\n```"
        files = _parse_files(text)
        assert len(files) == 0

    def test_classname_mismatch_is_corrected(self):
        # 파일명은 MCP 명세의 class_name이지만 Gemini가 다른 클래스명을 생성한 경우
        text = "```java\n// OrderCreateRequest.java\npublic class CreateOrderRequestDto { public CreateOrderRequestDto() {} }\n```"
        files = _parse_files(text)
        assert files[0]["filename"] == "OrderCreateRequest.java"
        assert "public class OrderCreateRequest" in files[0]["content"]
        assert "CreateOrderRequestDto" not in files[0]["content"]

    def test_lowercase_filename_comment_defers_to_class_name(self):
        # Gemini가 주석에 소문자 파일명을 쓰고 본문엔 올바른 PascalCase 클래스명을 쓴 경우
        # → 코드 내 클래스명이 더 정확하므로 클래스명을 파일명으로 채택해야 함
        text = "```java\n// hello.java\npublic class HelloAnd { }\n```"
        files = _parse_files(text)
        assert files[0]["filename"] == "HelloAnd.java"
        assert "public class HelloAnd" in files[0]["content"]

    def test_pascalcase_filename_comment_wins_over_wrong_class_name(self):
        # 파일명 주석이 PascalCase라면 그게 정답 → 코드 내 클래스명을 교정
        text = "```java\n// OrderRequest.java\npublic class wrongName { }\n```"
        files = _parse_files(text)
        assert files[0]["filename"] == "OrderRequest.java"
        assert "public class OrderRequest" in files[0]["content"]


class TestFixClassnameMismatch:
    def test_no_change_when_match(self):
        content = "public class Foo {}"
        assert _fix_classname_mismatch("Foo.java", content) == content

    def test_renames_class_declaration(self):
        content = "public class WrongName { public WrongName() {} }"
        result = _fix_classname_mismatch("CorrectName.java", content)
        assert "public class CorrectName" in result
        assert "WrongName" not in result

    def test_renames_interface(self):
        content = "public interface WrongService { void doSomething(); }"
        result = _fix_classname_mismatch("CorrectService.java", content)
        assert "public interface CorrectService" in result

    def test_no_public_class_unchanged(self):
        content = "class PackagePrivate {}"
        result = _fix_classname_mismatch("SomeName.java", content)
        assert result == content

    def test_path_prefix_stripped_from_filename(self):
        text = "```java\n// com/example/dto/request/Foo.java\npublic class Foo {}\n```"
        files = _parse_files(text)
        assert files[0]["filename"] == "Foo.java"

    def test_empty_block_skipped(self):
        text = "```java\n// Empty.java\n\n```"
        files = _parse_files(text)
        assert len(files) == 0


class TestDetectPackage:
    def test_detects_from_spring_boot_application(self, tmp_path):
        app = tmp_path / "src/main/java/com/example/ticketing/TicketingApplication.java"
        app.parent.mkdir(parents=True)
        app.write_text(
            "@SpringBootApplication\npublic class TicketingApplication {}\n"
            "package com.example.ticketing;"
        )
        dto = tmp_path / "src/main/java/com/example/ticketing/dto/request/Req.java"
        dto.parent.mkdir(parents=True)
        dto.write_text("package com.example.ticketing.dto.request;\npublic class Req {}")
        assert _detect_package(tmp_path) == "com.example.ticketing"

    def test_fallback_to_shortest_package(self, tmp_path):
        controller = tmp_path / "OrderController.java"
        controller.write_text("package com.example.controller;\npublic class OrderController {}")
        dto = tmp_path / "OrderRequest.java"
        dto.write_text("package com.example.dto.request;\npublic class OrderRequest {}")
        assert _detect_package(tmp_path) == "com.example.controller"

    def test_no_java_files_returns_default(self, tmp_path):
        assert _detect_package(tmp_path) == "com.example"

    def test_single_segment_package(self, tmp_path):
        java_file = tmp_path / "Foo.java"
        java_file.write_text("package myapp;\n\nclass Foo {}")
        assert _detect_package(tmp_path) == "myapp"


class TestExtractGroup:
    def test_basic_resource(self):
        assert _extract_group("/api/orders") == "Order"

    def test_nested_path(self):
        assert _extract_group("/api/orders/{id}/cancel") == "Order"

    def test_different_resource(self):
        assert _extract_group("/api/products/search") == "Product"

    def test_no_api_prefix(self):
        assert _extract_group("/users/{id}") == "User"

    def test_status_not_singularized(self):
        # 'status'는 복수형이 아니므로 's' 제거 금지 → 'Statu' 아닌 'Status'
        assert _extract_group("/api/status") == "Status"

    def test_access_not_singularized(self):
        assert _extract_group("/api/access") == "Access"

    def test_process_not_singularized(self):
        assert _extract_group("/api/process") == "Process"


class TestGroupSpecs:
    def test_groups_by_first_segment(self):
        specs = [
            {"api_endpoint": "/api/orders", "method": "GET", "dto_definitions": []},
            {"api_endpoint": "/api/orders/{id}/cancel", "method": "POST", "dto_definitions": []},
            {"api_endpoint": "/api/products", "method": "GET", "dto_definitions": []},
        ]
        groups = _group_specs(specs)
        assert "Order" in groups
        assert "Product" in groups
        assert len(groups["Order"]) == 2
        assert len(groups["Product"]) == 1

    def test_single_spec(self):
        specs = [{"api_endpoint": "/api/users", "method": "GET", "dto_definitions": []}]
        groups = _group_specs(specs)
        assert list(groups.keys()) == ["User"]


class TestBuildDtoPrompt:
    def test_contains_class_name(self):
        spec = {
            "api_endpoint": "/api/orders",
            "method": "POST",
            "dto_definitions": [{"class_name": "OrderCreateRequest", "description": "Request Body", "fields": []}],
        }
        prompt = _build_dto_prompt(spec, "com.example")
        assert "OrderCreateRequest" in prompt

    def test_contains_package(self):
        spec = {"api_endpoint": "/", "method": "GET", "dto_definitions": []}
        prompt = _build_dto_prompt(spec, "com.myapp")
        assert "com.myapp" in prompt

    def test_update_mode_when_existing_dto(self, tmp_path):
        existing = tmp_path / "OrderCreateRequest.java"
        existing.write_text("package com.example.dto.request;\npublic class OrderCreateRequest { private String item; }")
        spec = {
            "api_endpoint": "/api/orders",
            "method": "POST",
            "dto_definitions": [{"class_name": "OrderCreateRequest", "description": "Request Body", "fields": []}],
        }
        prompt = _build_dto_prompt(spec, "com.example", tmp_path)
        assert "UPDATE" in prompt or "수정" in prompt
        assert "기존 OrderCreateRequest.java" in prompt
        assert "private String item" in prompt

    def test_no_existing_dto_is_new_mode(self, tmp_path):
        spec = {
            "api_endpoint": "/api/orders",
            "method": "POST",
            "dto_definitions": [{"class_name": "OrderCreateRequest", "description": "Request Body", "fields": []}],
        }
        prompt = _build_dto_prompt(spec, "com.example", tmp_path)
        assert "기존" not in prompt


class TestBuildGroupPrompt:
    def test_contains_group_name(self):
        specs = [{"api_endpoint": "/api/orders", "method": "GET", "dto_definitions": []}]
        prompt = _build_group_prompt("Order", specs, "com.example")
        assert "OrderController" in prompt
        assert "OrderService" in prompt

    def test_contains_all_endpoints(self):
        specs = [
            {"api_endpoint": "/api/orders", "method": "GET", "dto_definitions": []},
            {"api_endpoint": "/api/orders/{id}", "method": "DELETE", "dto_definitions": []},
        ]
        prompt = _build_group_prompt("Order", specs, "com.example")
        assert "GET" in prompt
        assert "DELETE" in prompt

    def test_update_mode_includes_existing_test_files(self, tmp_path):
        test_file = tmp_path / "OrderControllerTest.java"
        test_file.write_text("package com.example.controller;\npublic class OrderControllerTest { @Test void existingTest() {} }")
        specs = [{"api_endpoint": "/api/orders", "method": "GET", "dto_definitions": []}]
        prompt = _build_group_prompt("Order", specs, "com.example", spring_root=tmp_path)
        assert "기존 OrderControllerTest.java" in prompt
        assert "existingTest" in prompt

    def test_update_mode_includes_existing_service_test(self, tmp_path):
        svc_test = tmp_path / "OrderServiceTest.java"
        svc_test.write_text("package com.example.service;\npublic class OrderServiceTest { @Test void givenOrder() {} }")
        specs = [{"api_endpoint": "/api/orders", "method": "GET", "dto_definitions": []}]
        prompt = _build_group_prompt("Order", specs, "com.example", spring_root=tmp_path)
        assert "기존 OrderServiceTest.java" in prompt
        assert "givenOrder" in prompt


class TestReadExistingFile:
    def test_finds_file_by_stem(self, tmp_path):
        f = tmp_path / "OrderRequest.java"
        f.write_text("public class OrderRequest {}", encoding="utf-8")
        result = _read_existing_file("OrderRequest", tmp_path)
        assert result is not None
        assert "OrderRequest" in result

    def test_returns_none_when_not_found(self, tmp_path):
        assert _read_existing_file("NonExistent", tmp_path) is None

    def test_case_insensitive_stem_match(self, tmp_path):
        (tmp_path / "OrderRequest.java").write_text("class OrderRequest {}", encoding="utf-8")
        assert _read_existing_file("orderrequest", tmp_path) is not None

    def test_returns_alphabetically_first_when_multiple_matches(self, tmp_path):
        """동일 stem 파일이 여러 경로에 존재할 때 경로 알파벳 순으로 첫 번째를 반환한다.
        rglob 순서는 파일시스템에 따라 비결정적이므로 sorted()로 정렬 후 선택해야 한다.
        """
        adir = tmp_path / "aaa"
        adir.mkdir()
        zdir = tmp_path / "zzz"
        zdir.mkdir()
        # 알파벳 순으로 나중인 파일을 먼저 생성해 rglob 순서와 알파벳 순서가 다를 수 있도록 유도
        (zdir / "Foo.java").write_text("// from zzz", encoding="utf-8")
        (adir / "Foo.java").write_text("// from aaa", encoding="utf-8")

        result = _read_existing_file("Foo", tmp_path)
        # sorted() 적용 시 aaa 디렉토리가 zzz보다 먼저 오므로 "// from aaa" 반환
        assert result == "// from aaa"


class TestScanPackageTree:
    def test_returns_fqcn_for_java_files(self, tmp_path):
        f = tmp_path / "OrderCreateRequest.java"
        f.write_text("package com.example.dto.request;\npublic class OrderCreateRequest {}")
        result = _scan_package_tree(tmp_path)
        assert result["OrderCreateRequest"] == "com.example.dto.request.OrderCreateRequest"

    def test_empty_dir_returns_empty(self, tmp_path):
        assert _scan_package_tree(tmp_path) == {}

    def test_skips_files_without_package_or_class(self, tmp_path):
        (tmp_path / "Broken.java").write_text("not valid java")
        assert _scan_package_tree(tmp_path) == {}

    def test_scans_nested_directories(self, tmp_path):
        nested = tmp_path / "src/main/java/com/example"
        nested.mkdir(parents=True)
        (nested / "MyService.java").write_text(
            "package com.example;\npublic class MyService {}"
        )
        result = _scan_package_tree(tmp_path)
        assert result["MyService"] == "com.example.MyService"


class TestBuildGroupPromptFqcn:
    def test_uses_actual_fqcn_over_constructed(self):
        """fqcn_map에 실제 FQCN이 있으면 구성된 경로 대신 사용한다."""
        specs = [{
            "api_endpoint": "/api/orders",
            "method": "POST",
            "dto_definitions": [{"class_name": "OrderCreateRequest", "description": "Request Body", "fields": []}],
        }]
        fqcn_map = {"OrderCreateRequest": "com.actual.path.dto.OrderCreateRequest"}
        prompt = _build_group_prompt("Order", specs, "com.example", fqcn_map=fqcn_map)
        assert "com.actual.path.dto.OrderCreateRequest" in prompt
        assert "com.example.dto.request.OrderCreateRequest" not in prompt

    def test_falls_back_to_constructed_when_no_fqcn(self):
        """fqcn_map에 없으면 base_package 기반으로 구성한다."""
        specs = [{
            "api_endpoint": "/api/orders",
            "method": "POST",
            "dto_definitions": [{"class_name": "OrderCreateRequest", "description": "Request Body", "fields": []}],
        }]
        prompt = _build_group_prompt("Order", specs, "com.example", fqcn_map={})
        assert "com.example.dto.request.OrderCreateRequest" in prompt


class TestBuildDtoPromptFqcn:
    def test_injects_existing_dto_fqcn_context(self):
        """fqcn_map에 DTO 관련 클래스가 있으면 프롬프트에 컨텍스트가 추가된다."""
        spec = {
            "api_endpoint": "/api/orders",
            "method": "POST",
            "dto_definitions": [{"class_name": "OrderCreateRequest", "description": "Request Body", "fields": []}],
        }
        fqcn_map = {"PaymentRequest": "com.example.dto.request.PaymentRequest"}
        prompt = _build_dto_prompt(spec, "com.example", fqcn_map=fqcn_map)
        assert "PaymentRequest" in prompt
        assert "com.example.dto.request.PaymentRequest" in prompt

    def test_no_fqcn_context_when_map_empty(self):
        spec = {
            "api_endpoint": "/api/orders",
            "method": "POST",
            "dto_definitions": [{"class_name": "OrderCreateRequest", "description": "Request Body", "fields": []}],
        }
        prompt = _build_dto_prompt(spec, "com.example", fqcn_map={})
        assert "기존 DTO FQCN" not in prompt


class TestGenerateCodeAll:
    def test_calls_gemini_for_dtos_and_groups(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

        mock_response = MagicMock()
        mock_response.text = "```java\n// Foo.java\npublic class Foo {}\n```"

        specs = [
            {"api_endpoint": "/api/orders", "method": "POST",
             "dto_definitions": [{"class_name": "OrderCreateRequest", "description": "Request Body", "fields": []}]},
            {"api_endpoint": "/api/products", "method": "GET",
             "dto_definitions": [{"class_name": "ProductResponse", "description": "Response Body", "fields": []}]},
        ]

        with patch("generator.genai.Client") as mock_cls:
            mock_client = MagicMock()
            mock_client.models.generate_content.return_value = mock_response
            mock_cls.return_value = mock_client

            files = generate_code_all(specs)

        # 스펙 2개(DTO) + 그룹 2개(Controller/Service/Test) = Gemini 4번 호출
        assert mock_client.models.generate_content.call_count == 4
        assert len(files) > 0

    def test_returns_empty_for_empty_specs(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
        with patch("generator.genai.Client"):
            files = generate_code_all([])
        assert files == []

    def test_in_memory_dto_fqcn_injected_into_group_prompt(self, monkeypatch, tmp_path):
        """Gemini가 DTO를 비표준 패키지에 생성했을 때, 그 FQCN이 그룹 프롬프트의 import에 반영돼야 한다.
        디스크에 아직 없는 in-memory 생성 파일의 FQCN을 2단계 프롬프트에 주입하는 동작을 검증.
        """
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

        # Gemini가 DTO를 비표준 패키지(dto.inner)에 생성한 경우
        dto_text = (
            "```java\n// OrderCreateRequest.java\n"
            "package com.example.dto.inner;\n"
            "public class OrderCreateRequest {}\n```"
        )
        group_text = "```java\n// OrderController.java\npublic class OrderController {}\n```"

        specs = [{
            "api_endpoint": "/api/orders",
            "method": "POST",
            "dto_definitions": [{"class_name": "OrderCreateRequest", "description": "Request Body", "fields": []}],
        }]

        with patch("generator.genai.Client") as mock_cls:
            mock_client = MagicMock()
            mock_client.models.generate_content.side_effect = [
                MagicMock(text=dto_text),
                MagicMock(text=group_text),
            ]
            mock_cls.return_value = mock_client

            generate_code_all(specs, tmp_path)

        # 두 번째 Gemini 호출(그룹 프롬프트)에 실제 생성된 DTO의 FQCN이 포함돼야 함
        group_prompt = mock_client.models.generate_content.call_args_list[1].kwargs["contents"]
        assert "com.example.dto.inner.OrderCreateRequest" in group_prompt
        # fallback 경로(dto.request)가 아닌 실제 패키지를 사용해야 함
        assert "com.example.dto.request.OrderCreateRequest" not in group_prompt
