from __future__ import annotations

# MCP가 반환한 전체 스펙 리스트를 받아 Gemini API로 Spring Boot 코드를 생성하는 모듈.
#
# 생성 전략:
#   - DTO       : 스펙 1개당 개별 생성 (MCP 클래스명 그대로 사용)
#   - Controller/Service/Test : URL 첫 segment 기준으로 그루핑 후 그룹당 1개 생성
#     예) /api/orders, /api/orders/{id}/cancel → OrderController 하나에 메서드 2개

import json
import os
import re
from pathlib import Path
from typing import Any

from google import genai

# Gemini 응답에서 ```java 블록을 추출하는 정규식.
# 형식: ```java\n// 파일명.java\n코드내용\n```
_CODE_BLOCK_RE = re.compile(
    r"```java\s*\n(?://\s*(?P<filename>[^\n]+)\n)?(?P<content>.*?)```",
    re.DOTALL,
)


def _detect_package(spring_root: Path) -> str:
    """스프링 프로젝트에서 베이스 패키지명을 자동 감지한다.

    우선순위:
      1. @SpringBootApplication 어노테이션이 있는 파일의 패키지 → 가장 정확한 베이스 패키지
      2. 프로젝트 내 가장 짧은(계층이 얕은) 패키지
    .java 파일이 없으면 "com.example" 반환.
    """
    shortest_pkg: str | None = None

    for java_file in spring_root.rglob("*.java"):
        try:
            content = java_file.read_text()
        except Exception:
            continue
        match = re.search(r"^package\s+([\w.]+);", content, re.MULTILINE)
        if not match:
            continue
        pkg = match.group(1)

        # @SpringBootApplication이 있으면 해당 패키지가 베이스 패키지
        if "@SpringBootApplication" in content:
            return pkg

        # 가장 짧은 패키지를 fallback으로 보관
        if shortest_pkg is None or len(pkg.split(".")) < len(shortest_pkg.split(".")):
            shortest_pkg = pkg

    return shortest_pkg or "com.example"


def _extract_group(api_endpoint: str) -> str:
    """API endpoint URL에서 그룹명(PascalCase)을 추출한다.

    URL에서 'api' 를 제외한 첫 번째 유효 segment를 PascalCase로 변환.
    예) /api/orders/{id}/cancel → 'Order'
        /api/products/search   → 'Product'
    """
    parts = [p for p in api_endpoint.split("/") if p and p != "api"]
    if not parts:
        return "Default"
    segment = parts[0].rstrip("s")  # 복수형 제거: orders→order, products→product
    return segment.capitalize()


def _group_specs(specs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """전체 스펙 리스트를 URL 첫 segment 기준으로 그루핑한다.

    반환: {"Order": [spec1, spec2, ...], "Product": [spec3], ...}
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for spec in specs:
        group = _extract_group(spec["api_endpoint"])
        groups.setdefault(group, []).append(spec)
    return groups


def _build_dto_prompt(spec: dict[str, Any], base_package: str) -> str:
    """DTO 생성용 프롬프트. 스펙 1개를 받아 DTO 클래스들만 요청한다."""
    return f"""Spring Boot DTO 클래스를 생성하라.

API: {spec.get("method")} {spec.get("api_endpoint")}
패키지: {base_package}
명세(JSON):
{json.dumps(spec, ensure_ascii=False, indent=2)}

규칙:
- 클래스명은 명세의 class_name 그대로 사용 (절대 변경 금지)
- convention_preset 어노테이션 그대로 적용
- Request Body  → package {base_package}.dto.request
- Response Body → package {base_package}.dto.response
- Data Payload  → package {base_package}.dto.event
- enum_values 있는 필드 → 별도 Enum 파일로 분리 (package {base_package}.dto.enums)
- seats[] 같은 배열 표기 필드 → Inner 클래스 분리 (package {base_package}.dto.inner)
- 각 파일은 ```java 블록으로, 첫 줄에 // ClassName.java 형식의 주석 (경로 구분자 '/' 절대 포함 금지)
  올바른 예: // OrderCreateRequest.java
  잘못된 예: // com/example/dto/request/OrderCreateRequest.java

출력 형식:
```java
// ClassName.java
package {base_package}.dto.request;

// imports ...

public class ClassName {{
}}
```""".strip()


def _build_group_prompt(
    group_name: str,
    specs: list[dict[str, Any]],
    base_package: str,
) -> str:
    """Controller/Service/Test 생성용 프롬프트. 그룹 내 모든 스펙을 받아 한 번에 요청한다."""
    endpoints_summary = "\n".join(
        "  - {method} {endpoint}  |  Request: {req}  |  Response: {res}".format(
            method=s["method"],
            endpoint=s["api_endpoint"],
            req=[d["class_name"] for d in s["dto_definitions"] if d["description"] == "Request Body"],
            res=[d["class_name"] for d in s["dto_definitions"] if d["description"] in ("Response Body", "Data Payload")],
        )
        for s in specs
    )

    return f"""Spring Boot Controller, Service, Test 뼈대를 생성하라.

그룹명: {group_name}
패키지: {base_package}

엔드포인트 목록:
{endpoints_summary}

생성할 파일 4개:
1. {group_name}Controller.java  - 위 엔드포인트를 각각 메서드로 포함
2. {group_name}Service.java     - 인터페이스만 (구현체 생성 금지)
3. {group_name}ControllerTest.java - MockMvc, 성공/실패 케이스 메서드명 + // TODO
4. {group_name}ServiceTest.java    - Mockito given/when/then + // TODO

규칙:
- 비즈니스 로직 구현 금지, 모두 // TODO: 주석으로 남길 것
- Service 구현체(ServiceImpl) 절대 생성 금지
- Controller는 ResponseEntity 반환
- 각 파일은 ```java 블록으로, 첫 줄에 // ClassName.java 형식의 주석 (경로 구분자 '/' 절대 포함 금지)
  올바른 예: // OrderController.java
  잘못된 예: // com/example/controller/OrderController.java

출력 형식:
```java
// {group_name}Controller.java
package {base_package}.controller;

// imports ...

@RestController
public class {group_name}Controller {{
}}
```""".strip()


def _call_gemini(prompt: str) -> list[dict[str, str]]:
    """Gemini API를 호출하고 응답에서 java 코드블록을 파싱해 반환한다."""
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
    )
    return _parse_files(response.text)


def generate_code_all(
    specs: list[dict[str, Any]],
    spring_root: Path | None = None,
) -> list[dict[str, str]]:
    """전체 스펙 리스트를 받아 DTO + Controller + Service + Test를 모두 생성한다.

    1단계: 스펙별 DTO 생성
    2단계: URL 기준 그루핑 후 그룹별 Controller/Service/Test 생성
    """
    base_package = _detect_package(spring_root) if spring_root else "com.example"
    all_files: list[dict[str, str]] = []

    # 1단계: DTO (스펙 1개당 개별 호출)
    for spec in specs:
        prompt = _build_dto_prompt(spec, base_package)
        all_files.extend(_call_gemini(prompt))

    # 2단계: Controller/Service/Test (그룹별 호출)
    groups = _group_specs(specs)
    for group_name, group_specs in groups.items():
        prompt = _build_group_prompt(group_name, group_specs, base_package)
        all_files.extend(_call_gemini(prompt))

    return all_files


def generate_code(
    dto_spec: dict[str, Any],
    spring_root: Path | None = None,
) -> list[dict[str, str]]:
    """단일 스펙 DTO 생성 (하위 호환 유지 및 테스트용)."""
    base_package = _detect_package(spring_root) if spring_root else "com.example"
    return _call_gemini(_build_dto_prompt(dto_spec, base_package))


def _parse_files(text: str) -> list[dict[str, str]]:
    """Gemini 응답에서 ```java 블록을 추출해 파일 목록으로 변환한다.

    파일명 처리 우선순위:
      1. // 파일명.java 주석이 있으면 basename만 추출 (경로 구분자 제거)
      2. 없으면 코드 본문에서 public class/interface/enum/record 이름으로 대체
      3. 그래도 없으면 블록 스킵 (Unknown 파일 생성 방지)
    """
    files: list[dict[str, str]] = []
    for match in _CODE_BLOCK_RE.finditer(text):
        content = match.group("content").strip()
        if not content:
            continue

        raw_name = (match.group("filename") or "").strip()
        if raw_name:
            filename = Path(raw_name).name  # 경로 포함 시 basename만 추출
            if not filename.endswith(".java"):
                filename += ".java"
        else:
            # 파일명 주석 누락 시 클래스명으로 대체
            class_match = re.search(
                r"\bpublic\s+(?:class|interface|enum|record)\s+(\w+)", content
            )
            if not class_match:
                continue  # 파일명도 클래스명도 없으면 스킵
            filename = class_match.group(1) + ".java"

        files.append({"filename": filename, "content": content})
    return files
