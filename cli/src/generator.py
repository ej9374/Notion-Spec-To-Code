from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────────────
# generator.py — Notion 명세 → Gemini API → Spring Boot 코드 생성
#
# 핵심 설계 원칙: "Gemini에게 규칙을 지시하는 대신, 실제 데이터를 주입한다."
#   → 프롬프트에 "import 경로를 올바르게 써라"고 적는 것보다,
#     실제 프로젝트에서 스캔한 FQCN(완전한 클래스 경로)을 직접 넣어주는 게 효과적.
#   → 기존 파일이 있으면 그 내용을 프롬프트에 포함해 UPDATE 모드로 전환.
#
# 생성 전략:
#   - DTO       : 스펙 1개당 개별 생성 (MCP 클래스명 그대로 사용)
#   - Controller/Service/Test : URL 첫 segment 기준으로 그루핑 후 그룹당 1개 생성
#     예) /api/orders, /api/orders/{id}/cancel → OrderController 하나에 메서드 2개
# ─────────────────────────────────────────────────────────────────────────────

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

    URL에서 'api' 와 경로 파라미터(`{...}`)를 제외한 첫 번째 유효 segment를 PascalCase로 변환.
    복수형 's' 제거는 단어 전체가 알파벳인 경우에만 적용한다.
    예) /api/orders/{id}/cancel → 'Order'
        /api/products/search   → 'Product'
        /api/status            → 'Status'   (status → 잘못된 rstrip 방지)
        /api/{id}/items        → 'Item'     (경로 파라미터 건너뜀)
    """
    parts = [
        p for p in api_endpoint.split("/")
        if p and p != "api" and not (p.startswith("{") and p.endswith("}"))
    ]
    if not parts:
        return "Default"
    segment = parts[0]
    # 순수 알파벳, 's'로 끝남, 끝에서 두 번째 문자가 모음이 아니고 's'가 아닐 때만 복수형 제거.
    # - 모음 조건: status(-2='u'), bonus(-2='u') 등 비복수형 보호
    # - 's' 조건: access(-2='s'), process(-2='s') 등 이중-s 단어 보호
    if (
        segment.isalpha()
        and segment.endswith("s")
        and len(segment) > 2
        and segment[-2] not in "aeiou"
        and segment[-2] != "s"
    ):
        segment = segment[:-1]
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


def _build_dto_prompt(
    spec: dict[str, Any],
    base_package: str,
    spring_root: Path | None = None,
    fqcn_map: dict[str, str] | None = None,
) -> str:
    """DTO 생성용 프롬프트. 기존 DTO 파일이 있으면 UPDATE 모드로 전환한다.

    MCP가 반환한 class_name을 그대로 파일명으로 나열해 Gemini가 임의로
    바꾸지 못하도록 강제한다.
    fqcn_map이 있으면 기존 DTO 패키지 구조를 컨텍스트로 주입한다.
    """
    dto_defs = spec.get("dto_definitions", [])

    # description → 패키지 하위 경로 매핑
    _desc_to_subpkg = {
        "Request Body": "dto.request",
        "Response Body": "dto.response",
        "Data Payload": "dto.event",
    }

    # 각 파일마다 파일명 + 정확한 package 선언을 명시
    file_specs = "\n".join(
        "  - {cls}.java  →  package {pkg};".format(
            cls=d["class_name"],
            pkg=f"{base_package}.{_desc_to_subpkg.get(d.get('description', ''), 'dto')}",
        )
        for d in dto_defs
    )

    # 기존 DTO FQCN 컨텍스트 — Gemini가 동일한 패키지 패턴을 따르도록 강제
    fqcn_context = ""
    if fqcn_map:
        dto_fqcns = {
            cls: fqcn for cls, fqcn in fqcn_map.items()
            if any(seg in fqcn for seg in (".dto.", ".request.", ".response.", ".event.", ".enums."))
        }
        if dto_fqcns:
            fqcn_context = (
                "\n[기존 DTO FQCN — 아래 패키지 패턴을 그대로 따를 것]\n"
                + "\n".join(f"  {cls}: {fqcn}" for cls, fqcn in sorted(dto_fqcns.items()))
            )

    # 기존 DTO 파일 수집 → UPDATE 모드 전환
    existing_sections: list[str] = []
    if spring_root:
        for d in dto_defs:
            content = _read_existing_file(d["class_name"], spring_root)
            if content:
                existing_sections.append(
                    f"[기존 {d['class_name']}.java]\n```java\n{content}\n```"
                )

    if existing_sections:
        mode_instruction = (
            "아래 '기존 파일'을 기반으로 최신 명세에 맞게 수정(UPDATE)하라.\n"
            "기존 필드·어노테이션·커스텀 코드를 최대한 유지하고, 명세 변경분만 반영한다.\n\n"
            + "\n\n".join(existing_sections)
        )
        file_word = "수정할"
    else:
        mode_instruction = "아래 명세에 맞게 신규 DTO 파일을 생성하라."
        file_word = "생성할"

    first = dto_defs[0] if dto_defs else {"class_name": "ClassName", "description": "Request Body"}
    first_pkg = f"{base_package}.{_desc_to_subpkg.get(first.get('description', ''), 'dto')}"

    return f"""Spring Boot DTO 클래스를 생성/수정하라.

API: {spec.get("method")} {spec.get("api_endpoint")}
베이스 패키지: {base_package}{fqcn_context}
명세(JSON):
{json.dumps(spec, ensure_ascii=False, indent=2)}

{mode_instruction}

{file_word} 파일과 package 선언 (아래 내용을 정확히 그대로 사용 — 절대 변경 금지):
{file_specs}

규칙:
- 클래스명·파일명은 위 목록 그대로 사용
- package 선언도 위 목록 그대로 사용 (임의로 변경하거나 세그먼트 추가 금지)
- convention_preset 어노테이션 그대로 적용
- enum_values 있는 필드 → 별도 Enum 파일로 분리 (package {base_package}.dto.enums)
- seats[] 같은 배열 표기 필드 → Inner 클래스 분리 (package {base_package}.dto.inner)
- 각 파일은 ```java 블록으로, 첫 줄에 위 목록의 파일명 그대로 주석 (경로 구분자 '/' 포함 금지)

출력 형식:
```java
// {first['class_name']}.java
package {first_pkg};

// imports ...

public class {first['class_name']} {{
}}
```""".strip()


def _read_existing_file(stem: str, spring_root: Path) -> str | None:
    """stem이 일치하는 기존 .java 파일 내용을 반환한다. 없으면 None.

    rglob 순서는 파일시스템에 따라 비결정적이므로 sorted()로 경로 알파벳 순 정렬 후 첫 번째 선택.
    """
    for java_file in sorted(spring_root.rglob("*.java")):
        if java_file.stem.lower() == stem.lower():
            try:
                return java_file.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass
    return None


def _build_group_prompt(
    group_name: str,
    specs: list[dict[str, Any]],
    base_package: str,
    existing_classes: dict[str, str] | None = None,
    spring_root: Path | None = None,
    fqcn_map: dict[str, str] | None = None,
) -> str:
    """Controller/Service/Test 생성용 프롬프트.

    - 기존 .java 파일이 있으면 해당 파일의 클래스명 우선 사용 (MCP 이름 대신)
    - 기존 Controller/Service가 있으면 UPDATE 모드 (신규 생성 금지)
    - fqcn_map으로 실제 스캔된 FQCN을 사용해 import 경로 오류를 원천 차단
    """
    ec = existing_classes or {}
    fm = fqcn_map or {}
    _desc_to_subpkg = {
        "Request Body": "dto.request",
        "Response Body": "dto.response",
        "Data Payload": "dto.event",
    }

    endpoints_lines: list[str] = []
    imports: dict[str, str] = {}

    for s in specs:
        dtos = s.get("dto_definitions", [])
        # 기존 파일에 같은 stem이 있으면 기존 클래스명 우선
        req_names = [_resolve_class_name(d["class_name"], ec) for d in dtos if d.get("description") == "Request Body"]
        res_names = [_resolve_class_name(d["class_name"], ec) for d in dtos if d.get("description") in ("Response Body", "Data Payload")]
        endpoints_lines.append(
            "  - {method} {endpoint}  |  Request: {req}  |  Response: {res}".format(
                method=s["method"],
                endpoint=s["api_endpoint"],
                req=req_names,
                res=res_names,
            )
        )
        for d in dtos:
            resolved = _resolve_class_name(d["class_name"], ec)
            subpkg = _desc_to_subpkg.get(d.get("description", ""), "dto")
            # WHY: _scan_package_tree()로 실제 파일에서 읽은 FQCN을 우선 사용.
            # 스캔값이 없으면 base_package + subpkg 조합으로 fallback하지만,
            # 이 경우 실제 프로젝트 구조와 다를 수 있어 컴파일 에러로 이어질 수 있다.
            imports[resolved] = fm.get(resolved) or f"{base_package}.{subpkg}.{resolved}"

    endpoints_summary = "\n".join(endpoints_lines)
    import_lines = "\n".join(f"import {fqcn};" for fqcn in sorted(imports.values()))

    # 기존 파일 내용 포함 (Controller, Service, Test 모두)
    existing_sections: list[str] = []
    update_mode = False
    for stem in [
        f"{group_name}Controller",
        f"{group_name}Service",
        f"{group_name}ControllerTest",
        f"{group_name}ServiceTest",
    ]:
        if spring_root:
            content = _read_existing_file(stem, spring_root)
            if content:
                existing_sections.append(f"[기존 {stem}.java]\n```java\n{content}\n```")
                update_mode = True

    if update_mode:
        mode_instruction = (
            "아래 '기존 파일'을 기반으로 신규 엔드포인트를 추가하는 방식으로 수정(UPDATE)하라.\n"
            "기존 메서드·코드는 유지하고, 누락된 엔드포인트만 추가한다.\n"
            "기존 // TODO 주석을 절대 구현체로 바꾸지 말 것.\n\n"
            + "\n\n".join(existing_sections)
        )
        file_instruction = "수정할 파일"
    else:
        mode_instruction = "아래 엔드포인트 목록에 맞게 신규 파일 4개를 생성하라."
        file_instruction = "생성할 파일 4개"

    return f"""Spring Boot Controller, Service, Test 뼈대를 생성/수정하라.
비즈니스 로직은 절대 구현하지 않는다. 개발자가 채울 수 있도록 // TODO 주석만 남긴다.

그룹명: {group_name}
패키지: {base_package}

{mode_instruction}

엔드포인트 목록:
{endpoints_summary}

사용할 DTO import (아래 클래스명과 경로를 정확히 그대로 사용 — 절대 변경 금지):
{import_lines}

{file_instruction}:
1. {group_name}Controller.java
   - @RestController, @RequiredArgsConstructor
   - 위 엔드포인트를 각각 public 메서드로 포함
   - 메서드 본문: // TODO: (해당 기능 한글 설명) 한 줄만
   - ResponseEntity 반환, 반환값은 ResponseEntity.ok().build()

2. {group_name}Service.java
   - 인터페이스 아님, @Service @RequiredArgsConstructor 달린 구현 클래스
   - 엔드포인트에 대응하는 public 메서드 포함
   - 메서드 본문: // TODO: (해당 기능 한글 설명) 한 줄만
   - entity, repository 생성·참조 금지

3. {group_name}ControllerTest.java
   - @WebMvcTest 기반
   - 테스트 메서드 시그니처만 작성, 본문은 // TODO: (테스트 목적 한글 설명) 한 줄만
   - DTO 인스턴스 생성(new DTO()) 절대 금지 — mock, given/when/then 코드 작성 금지

4. {group_name}ServiceTest.java
   - @ExtendWith(MockitoExtension.class) 기반
   - 테스트 메서드 시그니처만 작성, 본문은 // TODO: (테스트 목적 한글 설명) 한 줄만
   - DTO 인스턴스 생성(new DTO()) 절대 금지 — mock, given/when/then 코드 작성 금지

절대 금지:
- 비즈니스 로직 구현 (조건문, 계산, DB 조회 등)
- entity, repository 파일 생성 또는 import
- ServiceImpl 클래스 생성
- DTO 클래스명·변수명 임의 변경
- 테스트 메서드 본문에 new DTO(), mock(), given(), when(), assertThat() 등 실제 코드 작성

출력 형식:
- 각 파일은 ```java 블록, 첫 줄에 // ClassName.java 형식의 주석 (경로 구분자 '/' 포함 금지)

```java
// {group_name}Service.java
package {base_package}.service;

@Service
@RequiredArgsConstructor
public class {group_name}Service {{

    public void exampleMethod() {{
        // TODO: 예시 기능 설명
    }}
}}
```""".strip()


def _scan_package_tree(spring_root: Path) -> dict[str, str]:
    """스프링 프로젝트의 .java 파일을 스캔해 {클래스명 → FQCN} 매핑을 반환한다.

    예) {"OrderCreateRequest" → "com.example.dto.request.OrderCreateRequest"}

    WHY: Gemini에게 "import 경로를 올바르게 써라"고 지시하는 것보다,
    프로젝트에 실제로 존재하는 클래스의 FQCN을 직접 넣어주면
    Gemini가 추측할 여지가 없어진다.
    → _build_dto_prompt: 기존 DTO 패키지 패턴을 컨텍스트로 제공
    → _build_group_prompt: 실제 FQCN으로 import 문 생성 (추측 fallback 차단)
    """
    fqcn_map: dict[str, str] = {}
    for java_file in spring_root.rglob("*.java"):
        try:
            content = java_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        pkg_match = re.search(r"^package\s+([\w.]+);", content, re.MULTILINE)
        cls_match = re.search(r"\bpublic\s+(?:class|interface|enum|record)\s+(\w+)", content)
        if pkg_match and cls_match:
            cls = cls_match.group(1)
            fqcn_map[cls] = f"{pkg_match.group(1)}.{cls}"
    return fqcn_map


def _scan_existing_classes(spring_root: Path) -> dict[str, str]:
    """스프링 프로젝트의 기존 .java 파일을 스캔해 {stem_lower → 실제_클래스명} 매핑을 반환한다.

    Controller/Service/Test 생성 시 MCP class_name 대신 프로젝트 내 실제 이름을 참조하기 위해 사용.
    예) "ordercreaterequst" → "OrderCreateRequest"
    """
    existing: dict[str, str] = {}
    for java_file in spring_root.rglob("*.java"):
        try:
            content = java_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        match = re.search(r"\bpublic\s+(?:class|interface|enum|record)\s+(\w+)", content)
        class_name = match.group(1) if match else java_file.stem
        existing[java_file.stem.lower()] = class_name
    return existing


def _resolve_class_name(mcp_name: str, existing: dict[str, str]) -> str:
    """MCP 클래스명을 프로젝트 기존 파일과 대조해 실제 사용할 이름을 결정한다.

    기존 파일에 동일 stem(대소문자 무시)이 있으면 → 기존 파일의 클래스명 사용
    없으면 → MCP 클래스명 그대로 사용
    """
    return existing.get(mcp_name.lower(), mcp_name)


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

    Controller/Service/Test 생성 전에 기존 .java 파일을 스캔해
    MCP class_name 대신 프로젝트에 이미 존재하는 클래스명을 우선 사용한다.
    """
    base_package = _detect_package(spring_root) if spring_root else "com.example"
    existing_classes = _scan_existing_classes(spring_root) if spring_root else {}
    fqcn_map = _scan_package_tree(spring_root) if spring_root else {}
    all_files: list[dict[str, str]] = []

    # 1단계: DTO (스펙 1개당 개별 호출, 기존 파일 있으면 UPDATE 모드)
    for spec in specs:
        prompt = _build_dto_prompt(spec, base_package, spring_root, fqcn_map=fqcn_map)
        all_files.extend(_call_gemini(prompt))

    # 2단계: Controller/Service/Test (그룹별 호출)
    # 디스크 재스캔 후, 방금 생성된 DTO 파일(아직 디스크에 없음)의 FQCN을 in-memory에서 직접 추출
    if spring_root:
        existing_classes = _scan_existing_classes(spring_root)
        fqcn_map = _scan_package_tree(spring_root)
    # in-memory 생성 DTO의 package 선언으로 실제 FQCN을 확정 — fallback 추정 경로보다 정확
    for f in all_files:
        pkg_m = re.search(r"^package\s+([\w.]+);", f["content"], re.MULTILINE)
        cls_m = re.search(r"\bpublic\s+(?:class|interface|enum|record)\s+(\w+)", f["content"])
        if pkg_m and cls_m:
            cls = cls_m.group(1)
            fqcn_map[cls] = f"{pkg_m.group(1)}.{cls}"
            existing_classes[Path(f["filename"]).stem.lower()] = cls
    groups = _group_specs(specs)
    for group_name, group_specs in groups.items():
        prompt = _build_group_prompt(
            group_name, group_specs, base_package,
            existing_classes=existing_classes,
            spring_root=spring_root,
            fqcn_map=fqcn_map,
        )
        all_files.extend(_call_gemini(prompt))

    return all_files


def generate_code(
    dto_spec: dict[str, Any],
    spring_root: Path | None = None,
) -> list[dict[str, str]]:
    """단일 스펙 DTO 생성 (하위 호환 유지 및 테스트용)."""
    base_package = _detect_package(spring_root) if spring_root else "com.example"
    return _call_gemini(_build_dto_prompt(dto_spec, base_package))


def _fix_classname_mismatch(filename: str, content: str) -> str:
    """파일명과 public class/interface/enum/record 선언명이 다르면 코드 내 이름을 교정한다.

    Java는 public class명이 파일명과 반드시 일치해야 컴파일된다.
    Gemini가 파일명 지시를 무시하고 다른 클래스명을 쓰는 경우를 방어한다.
    """
    expected = Path(filename).stem
    decl_match = re.search(
        r"\bpublic\s+(?:class|interface|enum|record)\s+(\w+)", content
    )
    if not decl_match or decl_match.group(1) == expected:
        return content  # 일치하면 그대로
    old_name = decl_match.group(1)
    # 파일 내 모든 해당 식별자를 교체 (단어 경계 기준)
    return re.sub(r"\b" + re.escape(old_name) + r"\b", expected, content)


def _sanitize_filename(raw: str) -> str:
    """Gemini가 출력한 파일명 주석을 정리해 순수 파일명만 반환한다.

    처리하는 케이스:
      - 경로 구분자 포함: "com/example/dto/Foo.java" → "Foo.java"
      - FQN 점 표기: "eunji.ticketing.entity.Payment.java" → "Payment.java"
      - 괄호 등 부가 텍스트: "Foo.java (변경 없음)" → "Foo.java"
    """
    # 경로 구분자 제거 → basename
    name = Path(raw).name
    # 공백 이후 부가 텍스트 제거: "Foo.java (변경 없음)" → "Foo.java"
    name = name.split(" ")[0].split("\t")[0]
    # .java 확장자 보정
    if not name.endswith(".java"):
        name += ".java"
    # FQN 점 표기 처리: "eunji.ticketing.entity.Payment.java" → "Payment.java"
    # 확장자 제외 부분에 점이 있으면 마지막 세그먼트만 취함
    stem = name[:-5]  # ".java" 제거
    if "." in stem:
        stem = stem.rsplit(".", 1)[-1]
        name = stem + ".java"
    return name


def _parse_files(text: str) -> list[dict[str, str]]:
    """Gemini 응답에서 ```java 블록을 추출해 파일 목록으로 변환한다.

    파일명 처리 우선순위:
      1. // 파일명.java 주석이 있으면 _sanitize_filename으로 정리
      2. 없으면 코드 본문에서 public class/interface/enum/record 이름으로 대체
      3. 그래도 없으면 블록 스킵 (Unknown 파일 생성 방지)

    추출 후 파일명과 클래스명 불일치를 자동 교정한다.
    """
    files: list[dict[str, str]] = []
    for match in _CODE_BLOCK_RE.finditer(text):
        content = match.group("content").strip()
        if not content:
            continue

        raw_name = (match.group("filename") or "").strip()
        class_match = re.search(
            r"\bpublic\s+(?:class|interface|enum|record)\s+(\w+)", content
        )
        class_name_in_code = class_match.group(1) if class_match else None

        if raw_name:
            filename = _sanitize_filename(raw_name)
            stem = Path(filename).stem
            # 파일명 주석이 소문자 시작(PascalCase 아님)이면 Gemini가 축약한 것으로 판단.
            # 코드 내 클래스명이 PascalCase라면 그쪽이 더 정확하므로 클래스명을 파일명으로 채택.
            if stem and not stem[0].isupper() and class_name_in_code and class_name_in_code[0].isupper():
                filename = class_name_in_code + ".java"
        else:
            # 파일명 주석 누락 시 클래스명으로 대체
            if not class_name_in_code:
                continue  # 파일명도 클래스명도 없으면 스킵
            filename = class_name_in_code + ".java"

        # 파일명과 클래스명 불일치 교정 (Java 컴파일 오류 사전 방지)
        content = _fix_classname_mismatch(filename, content)
        files.append({"filename": filename, "content": content})
    return files
