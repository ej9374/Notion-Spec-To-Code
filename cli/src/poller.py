from __future__ import annotations

# Notion 페이지의 변경 여부를 감지하는 모듈.
# MCP 서버를 거치지 않고 Notion API를 직접 호출해 last_edited_time만 비교한다.
# (MCP를 경유하면 매번 Gemini까지 호출되어 비용이 발생하기 때문)

import os
import re

from notion_client import Client


def _extract_page_id(url: str) -> str:
    """Notion 페이지 URL에서 32자리 hex 페이지 ID를 추출한다.

    Notion URL 형식은 두 가지가 있다:
      - 대시 없음: https://notion.so/페이지이름-1234567890abcdef1234567890abcdef
      - UUID:      https://notion.so/12345678-1234-1234-1234-123456789abc
    둘 다 처리하고, 대시를 제거한 32자리 문자열로 반환한다.
    """
    url = url.split("?")[0].split("#")[0].rstrip("/")
    segment = url.split("/")[-1]
    match = re.search(
        r"([0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}|[0-9a-f]{32})",
        segment,
        re.I,
    )
    if match:
        return match.group(1).replace("-", "")
    return segment


class NotionPoller:
    """Notion 페이지 변경을 감지하는 폴러.

    watch 모드에서 일정 간격으로 has_changed()를 호출해
    last_edited_time이 바뀌면 코드 재생성 파이프라인을 트리거한다.
    """

    def __init__(self, url: str) -> None:
        """NOTION_API_KEY 환경변수로 Notion 클라이언트를 초기화한다."""
        token = os.environ.get("NOTION_API_KEY")
        if not token:
            raise ValueError("NOTION_API_KEY 환경변수가 필요합니다.")
        self._client = Client(auth=token)
        self._page_id = _extract_page_id(url)
        self._url = url

    def _get_last_edited_time(self) -> str:
        """Notion API로 페이지의 마지막 수정 시각(ISO-8601 문자열)을 가져온다."""
        page = self._client.pages.retrieve(page_id=self._page_id)
        return page["last_edited_time"]

    def has_changed(self, last_edited: str) -> tuple[bool, str]:
        """현재 last_edited_time과 이전 값을 비교해 변경 여부를 반환한다.

        Returns:
            (변경됐으면 True, 현재 last_edited_time 문자열)

        사용 패턴:
            changed, new_ts = poller.has_changed(last_ts)
            if changed:
                last_ts = new_ts
                run(...)
        """
        current = self._get_last_edited_time()
        return current != last_edited, current
