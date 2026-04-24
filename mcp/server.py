import re
import os

from fastmcp import FastMCP
from notion_client import Client
from dotenv import load_dotenv
from google import genai

from parser import parse_page_header, parse_inline_db_rows, TARGET_DB_TITLES
from convention import get_convention_preset

load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

mcp = FastMCP("notion-spec-to-code")
notion = Client(auth=os.getenv("NOTION_API_KEY"))
gemini = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))


def _extract_page_id(url: str) -> str:
    url = url.split("?")[0].split("#")[0].rstrip("/")
    segment = url.split("/")[-1]
    match = re.search(r"([0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}|[0-9a-f]{32})", segment, re.I)
    if match:
        return match.group(1).replace("-", "")
    return segment


_SUFFIX_MAP = {
    "Request Body": "Request",
    "Response Body": "Response",
    "Data Payload": "Event",
}


def _make_class_name(feature_name: str, db_title: str) -> str:
    cleaned = re.sub(r"^\[[\d.]+\]\s*", "", feature_name).strip()
    suffix = _SUFFIX_MAP.get(db_title, "")

    response = gemini.models.generate_content(
        model="gemini-2.5-flash",
        contents=(
            f"Convert this Korean API feature name to a Java class name.\n"
            f"Feature name: {cleaned}\n"
            f"Required suffix: {suffix}\n"
            f"Rules:\n"
            f"- Translate Korean words to natural English\n"
            f"- Use UpperCamelCase\n"
            f"- Append '{suffix}' at the end if not already present\n"
            f"- Reply with ONLY the class name, no explanation"
        ),
    )
    return response.text.strip()


@mcp.tool()
def get_dto_definition(page_url: str) -> dict:
    """Notion API 명세 페이지에서 DTO 정의를 생성합니다.
    page_url: Notion 페이지 URL (API 명세서 DB의 행)
    """
    page_id = _extract_page_id(page_url)

    page = notion.pages.retrieve(page_id=page_id)
    header = parse_page_header(page["properties"])

    blocks = notion.blocks.children.list(block_id=page_id)

    dto_definitions = []
    for block in blocks["results"]:
        if block["type"] != "child_database":
            continue
        db_title = block["child_database"].get("title", "").strip()
        if db_title not in TARGET_DB_TITLES:
            continue

        db_id = block["id"].replace("-", "")
        db_meta = notion.databases.retrieve(database_id=db_id)
        data_sources = db_meta.get("data_sources", [])
        if not data_sources:
            continue

        rows = notion.data_sources.query(data_source_id=data_sources[0]["id"])
        fields = parse_inline_db_rows(rows["results"], db_title)

        dto_definitions.append({
            "class_name": _make_class_name(header["feature_name"], db_title),
            "description": db_title,
            "convention_preset": get_convention_preset(),
            "fields": fields,
        })

    return {
        "api_endpoint": header["uri"],
        "method": header["method"],
        "dto_definitions": dto_definitions,
    }


if __name__ == "__main__":
    mcp.run()
