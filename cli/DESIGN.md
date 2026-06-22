# Notion Spec-to-Code 하네스 — 설계 문서

## 한 줄 요약

Notion에 API 명세를 작성하면 Gemini가 Spring Boot 코드를 자동 생성하고,
생성된 코드가 규칙을 어기거나 빌드에 실패하면 스스로 교정하는 Python CLI 도구.

---

## 왜 만들었나 — 그리고 핵심 문제

팀에서 Notion으로 API 명세를 관리하는데, DTO·Controller·Service 뼈대를 매번 손으로 만드는 게 반복 작업이었다.
Gemini로 자동 생성하면 되지만, 실제로 써보니 문제가 명확했다.

> **"프롬프트에 규칙을 적어도 Gemini가 자꾸 어겼다."**
> `ServiceImpl` 클래스 생성, 잘못된 import 경로, `@Entity` 파일 생성, `*Tests.java`가 `src/main/java`에 들어가는 문제 등.

이 프로젝트의 핵심 질문은 하나다.

> **"AI가 규칙을 따르도록 강제하려면 어떻게 시스템을 설계해야 하는가?"**

답은 **"말로 지시하지 말고 시스템이 막게 한다"** 였다.

---

## 전체 파이프라인

```
Notion 페이지 URL
    │
    ▼
[parser.py]  MCP 서버 → API 명세 JSON 파싱
    │
    ▼
[generator.py]  기존 프로젝트 스캔 → 프롬프트에 실제 데이터 주입 → Gemini 호출
    │
    ▼
[loop.py]  Pre-merge: 디스크 쓰기 없이 Gemini 일관성 검토
    │
    ▼
[merge.py]  diff 출력 → 사용자 승인 → 파일 기록
    │
    ▼
[loop.py]  Post-merge 3단계 검증 루프
              0. Python regex  — 금지 패턴 (즉시)
              1. compileJava   — 컴파일 에러 (수초)
              2. gradlew test  — 런타임 실패 (수십초)
              각 단계 실패 → Gemini 재교정 → 최대 3회
              3회 초과 → 파일 전체 롤백 → 사람에게 보고
```

---

## 제약 강제 전략 — 4개 레이어

이 시스템의 핵심은 **4개의 독립적인 레이어**가 서로 다른 방식으로 AI의 규칙 위반을 막는다는 것이다.
하나가 뚫려도 다음 레이어가 잡는다.

```
레이어   위치               방식                  강제력
──────  ─────────────────  ────────────────────  ────────────────────
L1      프롬프트 생성 전   실제 데이터 주입       추측 여지 원천 차단
L2      파일 쓰기 직후     Python regex           Gradle 없이 즉시
L3      컴파일/테스트      Gradle 피드백 루프     빌드 실패 = 교정
L4      도구 권한          Claude Code 권한 설정  물리적으로 불가능
```

---

### L1 — 프롬프트에 규칙 대신 실제 데이터 주입 (`generator.py`)

**문제**: "import 경로를 올바르게 써라"고 적어도 Gemini가 자주 틀렸다.

**해결**: 실제 프로젝트를 스캔해서 정확한 FQCN(완전한 클래스 경로)을 프롬프트에 직접 넣었다.

```python
# _scan_package_tree(): 기존 .java 파일 전체를 AST 없이 regex로 스캔
# {"OrderCreateRequest" → "com.example.dto.request.OrderCreateRequest"}
fqcn_map = _scan_package_tree(spring_root)

# 프롬프트 import 문 생성 시: 스캔값 우선 → 없으면 구조 기반 fallback
imports[resolved] = fqcn_map.get(resolved) or f"{base_package}.{subpkg}.{resolved}"
```

**추가**: 기존 파일이 있으면 그 내용을 프롬프트에 포함해 **UPDATE 모드**로 전환한다.
새 코드를 덮어쓰는 게 아니라 기존 커스텀 코드를 보존하면서 명세 변경분만 추가한다.

```python
# 기존 파일 발견 → 내용을 프롬프트에 포함, "신규 생성" → "기존 파일 수정" 지시로 전환
if existing_sections:
    mode_instruction = "아래 '기존 파일'을 기반으로 최신 명세에 맞게 수정(UPDATE)하라.\n기존 필드·어노테이션·커스텀 코드를 최대한 유지하고, 명세 변경분만 반영한다."
```

**핵심 파일**: [src/generator.py](src/generator.py) — `_scan_package_tree`, `_build_dto_prompt`, `_build_group_prompt`

---

### L2 — 파일 쓰기 직후 Python 패턴 검사 (`loop.py`)

**문제**: `@Entity`, `ServiceImpl`, JPA import 같은 패턴은 컴파일은 통과하지만 프로젝트 아키텍처를 위반한다. Gradle을 돌릴 필요도 없는 문제인데 3회 교정 기회를 낭비했다.

**해결**: Gradle 없이 Python regex로 즉시 감지. 에러 메시지가 명확해서 Gemini 교정 품질이 높다.

```python
# "규칙을 자연어로 지시"하는 대신 이 목록에 추가해 결정론적으로 강제
_FORBIDDEN_JAVA_PATTERNS = [
    (r"\bclass\s+\w+Impl\b",           "ServiceImpl 클래스 생성 금지"),
    (r"@Entity\b",                     "@Entity 어노테이션 금지"),
    (r"@Repository\b",                 "@Repository 어노테이션 금지"),
    (r"import\s+jakarta\.persistence\.", "JPA persistence import 금지"),
    (r"extends\s+JpaRepository",       "JpaRepository 확장 금지"),
    # ...
]
```

새로운 금지 패턴이 발견되면 프롬프트에 쓰는 게 아니라 이 목록에 코드로 추가한다.

**핵심 파일**: [src/loop.py](src/loop.py) — `_FORBIDDEN_JAVA_PATTERNS`, `_validate_java_patterns`

---

### L3 — 컴파일 → 테스트 피드백 루프 (`loop.py`)

**문제**: 전체 Gradle test는 느리고, 에러 로그가 길어서 Gemini가 핵심을 놓쳤다.

**해결**: `compileJava`를 먼저 실행해 빠르고 명확한 에러를 Gemini에 전달한다.

```
단계  방법              감지                           속도    에러 메시지 특성
────  ────────────────  ─────────────────────────────  ──────  ──────────────────
1     compileJava       cannot find symbol, 잘못된 import  수초    짧고 정확
2     gradlew test      런타임 오류, 테스트 실패          수십초  길고 상세
```

각 단계 실패 시 해당 에러 로그를 그대로 Gemini에게 넘긴다.
에러 로그를 프롬프트에 포함하지 않으면 Gemini가 동일한 코드를 반복하므로 필수다.

```python
def _correct_with_gemini(files, error_log, attempt):
    # error_log = 패턴 위반 / 컴파일 에러 / 테스트 실패 중 하나
    # 어떤 단계에서 실패했든 동일한 함수로 Gemini에 전달
    prompt = f"[생성된 코드]\n{files_text}\n\n[에러 로그]\n{error_log}\n\n에러를 분석하고 수정된 전체 코드를 출력하세요."
```

3회 초과 실패 시 생성된 파일을 **전부 롤백**하고 RuntimeError로 사람에게 보고한다.

**핵심 파일**: [src/loop.py](src/loop.py) — `run_correction_loop`, `_correct_with_gemini`

---

### L4 — Claude Code 도구 권한 설정 (`.claude/settings.json`)

**문제**: Claude(AI 어시스턴트)가 직접 Java 파일을 써버리면 Gemini 파이프라인을 우회한다.

**해결**: Claude Code의 `permissions.deny`로 Java 파일 직접 쓰기를 물리적으로 차단했다.

```json
{
  "permissions": {
    "deny": [
      "Write(*.java)",
      "Read(./.env)",
      "Read(./.env.*)"
    ],
    "ask": [
      "Bash(git push *)",
      "Bash(rm *)"
    ]
  },
  "hooks": {
    "PostToolUse": [{
      "matcher": "Write|Edit",
      "hooks": [{ "command": "bash .claude/hooks/post-pytest.sh" }]
    }]
  }
}
```

**추가**: `PostToolUse` Hook — Claude가 `.py` 파일을 수정할 때마다 pytest가 자동 실행된다.
"파일 수정 후 pytest 실행하세요"라고 지시해도 AI가 잊을 수 있다. Hook은 잊지 않는다.

---

## 방어적 출력 처리 (`generator.py`)

Gemini의 응답이 불완전하거나 형식이 잘못됐을 때를 대비한 방어 로직들이다.

### 파일명 정제 (`_sanitize_filename`)

Gemini가 파일명 주석을 경로나 FQN으로 쓰는 경우를 자동 교정한다.

```python
# Gemini가 주석에 쓰는 잘못된 형식들
"com/example/dto/request/Foo.java" → "Foo.java"   # 경로 구분자
"eunji.ticketing.entity.Payment.java" → "Payment.java"  # FQN 점 표기
"Foo.java (변경 없음)" → "Foo.java"               # 부가 텍스트
```

### 파일명·클래스명 불일치 교정 (`_fix_classname_mismatch`)

Java는 파일명과 public class명이 반드시 일치해야 컴파일된다.
Gemini가 파일명 지시를 무시하고 다른 클래스명을 쓸 때 자동으로 교정한다.

```python
# 파일명: OrderCreateRequest.java, 코드 내 클래스명: CreateOrderRequestDto
# → 코드 내 모든 CreateOrderRequestDto를 OrderCreateRequest로 치환
```

---

## 파일 병합 전략 (`merge.py`)

### Content Similarity 감지

파일명이 달라도 내용 유사도가 60% 이상이면 중복으로 판단하고 사용자에게 확인한다.
API 명세가 바뀌어 클래스명이 달라진 경우에도 기존 커스텀 코드를 보존할 수 있다.

```python
SIMILARITY_THRESHOLD = 0.6  # difflib.SequenceMatcher 기반

# 파일명 exact match가 없을 때 전체 .java 파일과 내용 유사도 비교
similar_files = _find_similar_by_content(new_content, filename, spring_root)
# → 유사 파일 발견 시 diff 출력 후 사용자에게 대체 여부 확인
```

### 소스셋 라우팅

`*Test.java`와 `*Tests.java` 모두 `src/test/java/`로 보낸다.
복수형을 놓치면 `@SpringBootTest` import가 `compileJava` 단계에서 실패한다.

```python
source_set = "test" if (
    filename.endswith("Test.java") or filename.endswith("Tests.java")
) else "main"
```

---

## 테스트 전략

**원칙**: AI API 호출·파일시스템·Gradle은 모킹, 순수 로직만 단위 테스트.

| 파일 | 주요 테스트 케이스 |
|---|---|
| `test_generator.py` | 파일명 교정, 패키지 감지, FQCN 주입, UPDATE 모드 전환 |
| `test_loop.py` | 3단계 각각 실패 → 교정 흐름, 롤백 조건, 금지 패턴 감지 |
| `test_merge.py` | 유사도 감지, diff 생성, Test/Tests 소스셋 라우팅 |

```bash
python -m pytest tests/ -q  # 108개 테스트
```

---

## 기술 스택

| 영역 | 기술 |
|---|---|
| CLI | Python 3.11+, argparse, uv |
| AI 코드 생성 | Gemini 2.5 Flash (google-genai) |
| 명세 파싱 | MCP 서버 (notion-client) |
| 빌드 검증 | Gradle (compileJava, test) |
| 패턴 검증 | Python re (regex) |
| 도구 제약 | Claude Code permissions / hooks |
| 테스트 | pytest, unittest.mock |

---

## 파일 구조

```
src/
├── main.py       — CLI 진입점, 파이프라인 오케스트레이션
├── parser.py     — Notion MCP 호출, 명세 JSON 파싱
├── generator.py  — FQCN 스캔, Gemini 프롬프트 설계, 응답 파싱
├── loop.py       — 금지 패턴 목록, 3단계 검증, 자가 교정 루프
└── merge.py      — 유사도 감지, diff, 사용자 승인, 소스셋 라우팅

.claude/
├── settings.json — 도구 권한(deny Java 쓰기), PostToolUse hook
├── hooks/post-pytest.sh — .py 수정 시 pytest 자동 실행
└── skills/       — 모듈별 컨텍스트 문서 (Claude Code 스킬)
```
