from __future__ import annotations

# Notion 명세 페이지를 DTO JSON으로 변환하는 모듈.
# 직접 Notion API를 호출하지 않고, ../mcp/server.py 를 subprocess로 띄워
# MCP 프로토콜(stdio)로 통신한다.
# MCP 서버 내부에서 Notion API + Gemini API를 호출해 정제된 DTO 정의를 반환한다.

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

# 이 파일(cli/src/parser.py) 기준으로 mcp 디렉토리 절대경로를 계산
_MCP_DIR = Path(__file__).resolve().parent.parent.parent / "mcp"
_MCP_SERVER_PATH = _MCP_DIR / "server.py"


async def _call_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
    """MCP 서버를 subprocess로 실행하고, 지정한 툴을 호출해 결과를 반환한다.

    Args:
        tool_name:  MCP 서버에 등록된 툴 이름
                    ("get_dto_definition" 또는 "get_all_dto_definitions")
        arguments:  툴에 넘길 인자 딕셔너리

    MCP 통신 흐름:
        1. python mcp/server.py 를 subprocess로 실행
        2. stdio(표준입출력)로 MCP 핸드셰이크
        3. session.call_tool() 로 원하는 툴 호출
        4. 응답 JSON 파싱 후 반환

    PYTHONPATH에 mcp 디렉토리를 추가하는 이유:
        server.py가 같은 디렉토리의 parser.py, convention.py를 import하기 때문
    """
    params = StdioServerParameters(
        command="python",
        args=[str(_MCP_SERVER_PATH)],
        env={**os.environ, "PYTHONPATH": str(_MCP_DIR)},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments=arguments)
            # MCP 툴이 에러를 반환한 경우 content가 에러 메시지이므로 JSON 파싱 전에 확인
            if result.isError:
                error_text = result.content[0].text if result.content else "알 수 없는 오류"  # type: ignore[union-attr]
                raise RuntimeError(f"MCP 툴 '{tool_name}' 실패: {error_text}")
            raw = result.content[0].text  # type: ignore[union-attr]
            return json.loads(raw)


def parse_spec(page_url: str) -> dict[str, Any]:
    """단일 API 명세 페이지 URL → DTO 정의 딕셔너리 반환.

    반환 형식:
        {
            "api_endpoint": "/api/users",
            "method": "POST",
            "dto_definitions": [ { "class_name": "...", "fields": [...] } ]
        }
    """
    return asyncio.run(_call_tool("get_dto_definition", {"page_url": page_url}))


def parse_all_specs(db_url: str) -> list[dict[str, Any]]:
    """API 명세 DB URL → 모든 행(API)의 DTO 정의 리스트 반환.

    DB 안의 모든 API 명세 페이지를 순회하므로
    단일 페이지 URL이 아닌 상위 DB URL을 넘겨야 한다.
    """
    return asyncio.run(_call_tool("get_all_dto_definitions", {"db_url": db_url}))
