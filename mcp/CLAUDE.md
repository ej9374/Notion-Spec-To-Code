# CLAUDE.md — Notion MCP 서버

## 프로젝트 구조
mcp/
├── server.py       # FastMCP 진입점, 툴 등록만 담당
├── parser.py       # Notion API 응답 파싱 전담
├── convention.py   # 컨벤션 프리셋 정의 전담
└── .env            # NOTION_API_KEY (절대 커밋 금지)

## 파일별 책임 (경계를 넘지 말 것)
| 파일 | 담당 | 담당하지 않는 것 |
|------|------|----------------|
| server.py | FastMCP 툴 등록, 응답 조립 | 파싱 로직, 컨벤션 로직 |
| parser.py | Notion JSON → 정제된 dict | 어노테이션 생성, API 호출 |
| convention.py | 어노테이션 프리셋 반환 | Notion 응답 처리 |

## Notion → Java 타입 매핑
| Notion 타입 | Java 타입 |
|-------------|-----------|
| rich_text | String |
| number | Long / Integer (비고 참조) |
| select | Enum |
| checkbox | Boolean |
| date | LocalDateTime |

새 타입 추가 시 **이 테이블도 반드시 함께 업데이트**.

## 파싱 규칙 (parser.py)
- 추출 대상: `rich_text`, `select`, `number`, `checkbox`, `date`
- 제거 대상: `id`, `created_time`, `last_edited_time`, `created_by`, `last_edited_by`, `archived`
- 비고(description) 컬럼에서 제약 조건 파싱:
  - `required` / `not_null` → `NotNull`
  - `not_blank` → `NotBlank`
  - `email` → `Email`
  - `max:N` → `Size(max=N)` 또는 `Max(N)` (타입에 따라)
  - `min:N` → `Min(N)`

## 컨벤션 프리셋 (convention.py)
모든 DTO 생성 시 아래를 **항상** 포함하도록 응답에 명시할 것:
- 클래스 레벨: `@Builder`, `@Getter`, `@NoArgsConstructor(access = AccessLevel.PROTECTED)`
  - **`@Builder`는 반드시 클래스 선언부 위에 붙일 것. 생성자에 붙이지 말 것.**
- 필드 레벨: 명세 기반 Bean Validation 어노테이션 (`@NotNull`, `@NotBlank`, `@Size`, `@Min`, `@Max` 등)

## server.py 응답 포맷 (이 스키마에서 벗어나지 말 것)
```json
{
  "api_endpoint": "/api/v1/users",
  "dto_definition": {
    "class_name": "UserCreateRequest",
    "description": "사용자 생성 요청 정보",
    "convention_preset": ["@Builder", "@Getter", "@NoArgsConstructor(access = AccessLevel.PROTECTED)"],
    "fields": [
      {
        "name": "email",
        "java_type": "String",
        "constraints": ["NotBlank", "Email"],
        "description": "사용자 이메일 주소"
      },
      {
        "name": "status",
        "java_type": "Enum",
        "enum_values": ["VIP", "OPEN", "CLOSED"],
        "constraints": ["NotNull"],
        "description": "VIP | OPEN | CLOSED, NotNull"
      },
      {
        "name": "age",
        "java_type": "Integer",
        "constraints": ["Min(0)", "Max(150)"],
        "description": "min:0, max:150"
      }
    ]
  }
}
```

### Java 파일 생성 규칙
- `convention_preset`의 어노테이션은 **모두 클래스 레벨**에 붙임 (`@Builder` 포함)
- `constraints` 배열의 각 항목은 필드 위에 `@`를 붙여 어노테이션으로 적용
- `java_type`이 `"Enum"`인 경우:
  - **별도 enum 파일**을 같은 패키지에 생성 (예: `Status.java`)
  - `enum_values` 배열의 값을 enum 상수로 정의
  - DTO 필드 타입은 생성한 enum 클래스명 사용 (예: `private Status status;`)

## 환경 변수
- `NOTION_API_KEY`는 `.env`에서만 로드 (python-dotenv 사용)
- 코드 내 하드코딩 금지, 로그 출력 금지

## 작업 전 체크리스트
1. 파싱 로직을 수정하는가? → `parser.py`만 건드릴 것
2. 어노테이션 규칙을 바꾸는가? → `convention.py`만 건드릴 것
3. 새 Notion 타입이 등장했는가? → 매핑 테이블 업데이트 후 `parser.py` 수정
4. 응답 포맷을 변경하는가? → 위 스키마와 반드시 맞출 것


## Notion 명세 구조 (파서 기준)

각 API 페이지는 API 명세서 DB의 행(row)이며, 상세 페이지 안에 인라인 DB가 있다.

### 페이지 properties에서 추출
- `기능명` → class_name 생성 (예: [2.1] 대기열 상태 조회 → QueueStatusResponse)
- `METHOD` → HTTP 메서드
- `URI` → API 엔드포인트

### 인라인 DB 종류별 역할
| DB 이름 | 역할 | 컬럼 구성 |
|---------|------|-----------|
| Request Header | 헤더 파라미터 | 이름 / 타입 / 설명 |
| Request Body | 요청 바디 필드 | 이름 / 데이터 타입(multi_select) / 설명(제한) |
| Response Body | 응답 바디 필드 | 이름 / 데이터 타입(multi_select) / 설명(제한) |
| Data Payload | SSE 이벤트 데이터 | 이름 / 타입(select) / 텍스트 |

### 타입 매핑 규칙
| 명세 타입 | Java 타입 |
|-----------|-----------|
| string | String |
| int / integer | Integer |
| long | Long |
| boolean | Boolean |

### 설명(제한) 컬럼 → 어노테이션 변환
- `NotNull` / `not_null` → `@NotNull`
- `NotBlank` → `@NotBlank`
- `max:N` → `@Size(max = N)` (String) / `@Max(N)` (숫자)
- `Min(N)` / `min:N` → `@Min(N)`
- `ISO-8601` → 타입을 LocalDateTime으로 변경
- `VIP | OPEN | ...` (파이프 구분) → Enum 타입으로 생성