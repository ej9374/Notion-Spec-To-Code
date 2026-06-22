from unittest.mock import MagicMock, patch

import pytest

from poller import NotionPoller, _extract_page_id


class TestExtractPageId:
    def test_32hex_in_url(self):
        url = "https://notion.so/page-1234567890abcdef1234567890abcdef"
        assert _extract_page_id(url) == "1234567890abcdef1234567890abcdef"

    def test_strips_query_params(self):
        url = "https://notion.so/page-abc123def456abc123def456abc123de?pvs=4"
        assert "?" not in _extract_page_id(url)

    def test_dashed_uuid(self):
        url = "https://notion.so/12345678-1234-1234-1234-123456789abc"
        assert _extract_page_id(url) == "123456781234123412341234 56789abc".replace(" ", "")

    def test_strips_fragment(self):
        url = "https://notion.so/page-aabbccddeeff00112233445566778899#section"
        assert "#" not in _extract_page_id(url)


class TestNotionPoller:
    def test_init_raises_without_api_key(self, monkeypatch):
        monkeypatch.delenv("NOTION_API_KEY", raising=False)
        with pytest.raises(ValueError, match="NOTION_API_KEY"):
            NotionPoller("https://notion.so/page-1234567890abcdef1234567890abcdef")

    def test_has_changed_false_when_time_same(self, monkeypatch):
        monkeypatch.setenv("NOTION_API_KEY", "fake-key")
        with patch("poller.Client") as mock_cls:
            mock_cls.return_value.pages.retrieve.return_value = {
                "last_edited_time": "2024-01-01T00:00:00.000Z"
            }
            poller = NotionPoller("https://notion.so/page-1234567890abcdef1234567890abcdef")
            changed, t = poller.has_changed("2024-01-01T00:00:00.000Z")
        assert not changed
        assert t == "2024-01-01T00:00:00.000Z"

    def test_has_changed_true_when_time_differs(self, monkeypatch):
        monkeypatch.setenv("NOTION_API_KEY", "fake-key")
        with patch("poller.Client") as mock_cls:
            mock_cls.return_value.pages.retrieve.return_value = {
                "last_edited_time": "2024-02-01T00:00:00.000Z"
            }
            poller = NotionPoller("https://notion.so/page-1234567890abcdef1234567890abcdef")
            changed, t = poller.has_changed("2024-01-01T00:00:00.000Z")
        assert changed
        assert t == "2024-02-01T00:00:00.000Z"

    def test_calls_notion_pages_retrieve(self, monkeypatch):
        monkeypatch.setenv("NOTION_API_KEY", "fake-key")
        with patch("poller.Client") as mock_cls:
            mock_client = MagicMock()
            mock_client.pages.retrieve.return_value = {"last_edited_time": "t"}
            mock_cls.return_value = mock_client

            poller = NotionPoller("https://notion.so/page-1234567890abcdef1234567890abcdef")
            poller.has_changed("")

            mock_client.pages.retrieve.assert_called_once_with(
                page_id="1234567890abcdef1234567890abcdef"
            )
