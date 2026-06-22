from unittest.mock import AsyncMock, patch

import pytest

from parser import parse_all_specs, parse_spec


class TestParseSpec:
    def test_returns_dict_from_mcp(self):
        expected = {
            "api_endpoint": "/users",
            "method": "POST",
            "dto_definitions": [],
        }
        with patch("parser._call_tool", new_callable=AsyncMock, return_value=expected):
            result = parse_spec("https://notion.so/page-abc")
        assert result == expected

    def test_passes_correct_tool_name(self):
        with patch("parser._call_tool", new_callable=AsyncMock, return_value={}) as mock_tool:
            parse_spec("https://notion.so/page-abc")
            args = mock_tool.call_args
            assert args[0][0] == "get_dto_definition"

    def test_passes_page_url_argument(self):
        url = "https://notion.so/page-abc"
        with patch("parser._call_tool", new_callable=AsyncMock, return_value={}) as mock_tool:
            parse_spec(url)
            args = mock_tool.call_args
            assert args[0][1]["page_url"] == url


class TestParseAllSpecs:
    def test_returns_list_from_mcp(self):
        expected = [{"api_endpoint": "/users", "method": "POST", "dto_definitions": []}]
        with patch("parser._call_tool", new_callable=AsyncMock, return_value=expected):
            result = parse_all_specs("https://notion.so/db-abc")
        assert result == expected

    def test_passes_correct_tool_name(self):
        with patch("parser._call_tool", new_callable=AsyncMock, return_value=[]) as mock_tool:
            parse_all_specs("https://notion.so/db-abc")
            args = mock_tool.call_args
            assert args[0][0] == "get_all_dto_definitions"

    def test_passes_db_url_argument(self):
        url = "https://notion.so/db-abc"
        with patch("parser._call_tool", new_callable=AsyncMock, return_value=[]) as mock_tool:
            parse_all_specs(url)
            args = mock_tool.call_args
            assert args[0][1]["db_url"] == url
