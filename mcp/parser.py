import re

TARGET_DB_TITLES = {"Request Body", "Response Body", "Data Payload"}

_SPEC_JAVA_TYPE = {
    "string": "String",
    "int": "Integer",
    "integer": "Integer",
    "long": "Long",
    "boolean": "Boolean",
}


def _plain_text(rich_text_list: list) -> str:
    return "".join(item.get("plain_text", "") for item in rich_text_list)


def parse_page_header(properties: dict) -> dict:
    """페이지 properties에서 기능명, METHOD, URI 추출"""
    feature_name = _plain_text(properties.get("기능명", {}).get("title", []))
    method = (properties.get("METHOD", {}).get("select") or {}).get("name", "")
    uri = _plain_text(properties.get("URI", {}).get("rich_text", []))
    return {"feature_name": feature_name, "method": method, "uri": uri}


def _map_spec_type(spec_type: str) -> str:
    return _SPEC_JAVA_TYPE.get(spec_type.lower().strip(), "Object")


_ENUM_PATTERN = re.compile(r"([\w]+(?:\s*[|/,]\s*[\w]+)+)")


def _extract_enum_values(description: str) -> list[str]:
    match = _ENUM_PATTERN.search(description)
    if not match:
        return []
    return [v.strip() for v in re.split(r"[|/,]", match.group(1))]


def _resolve_java_type_and_constraints(spec_type: str, description: str) -> tuple[str, list[str], list[str]]:
    enum_values: list[str] = []
    # ISO-8601 → LocalDateTime으로 타입 변경
    if "ISO-8601" in description:
        java_type = "LocalDateTime"
    # "VIP | OPEN | ...", "A/B/C", "X, Y, Z" 패턴 → Enum
    elif _ENUM_PATTERN.search(description):
        java_type = "Enum"
        enum_values = _extract_enum_values(description)
    else:
        java_type = _map_spec_type(spec_type)

    return java_type, _parse_constraints(description, java_type), enum_values


def _parse_constraints(description: str, java_type: str) -> list[str]:
    constraints = []
    for token in re.split(r"[\s,]+", description.strip()):
        if not token:
            continue
        if token in ("NotNull", "not_null", "required"):
            if "NotNull" not in constraints:
                constraints.append("NotNull")
        elif token == "NotBlank":
            constraints.append("NotBlank")
        elif token.lower().startswith("max:"):
            n = token[4:]
            constraints.append(f"Size(max = {n})" if java_type == "String" else f"Max({n})")
        elif token.lower().startswith("min:"):
            constraints.append(f"Min({token[4:]})")
        elif re.match(r"Min\(\d+\)", token):
            constraints.append(token)
    return constraints


def _parse_row(props: dict, db_title: str) -> dict | None:
    name = _plain_text(props.get("이름", {}).get("title", []))
    if not name:
        return None

    if db_title == "Data Payload":
        sel = props.get("타입", {}).get("select") or {}
        spec_type = sel.get("name", "")
        description = _plain_text(props.get("텍스트", {}).get("rich_text", []))
    else:  # Request Body, Response Body
        opts = props.get("데이터 타입", {}).get("multi_select", [])
        spec_type = opts[0]["name"] if opts else ""
        description = _plain_text(props.get("설명(제한)", {}).get("rich_text", []))

    java_type, constraints, enum_values = _resolve_java_type_and_constraints(spec_type, description)
    field: dict = {
        "name": name,
        "java_type": java_type,
        "constraints": constraints,
        "description": description,
    }
    if enum_values:
        field["enum_values"] = enum_values
    return field


def parse_inline_db_rows(rows: list, db_title: str) -> list[dict]:
    """인라인 DB 행 목록 → 필드 목록. 중복 필드명은 description이 있는 쪽 우선."""
    seen: dict[str, dict] = {}
    for page in rows:
        field = _parse_row(page.get("properties", {}), db_title)
        if not field:
            continue
        name = field["name"]
        if name not in seen or (not seen[name]["description"] and field["description"]):
            seen[name] = field
    return list(seen.values())
