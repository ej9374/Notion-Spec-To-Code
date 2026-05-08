from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from generator import (
    _build_dto_prompt,
    _build_group_prompt,
    _detect_package,
    _extract_group,
    _group_specs,
    _parse_files,
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
