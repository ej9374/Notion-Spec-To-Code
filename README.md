# Notion-Spec-To-Code

# 1. 커스텀 MCP 만들기

## 1. 개요 (Product Overview)

- **목표:** Notion API 명세서와 코드 사이의 '싱크(Sync) 불일치'와 '반복적 오타'를 원천 차단한다.
- **핵심 가치:** "명세가 곧 코드다(Spec as Code)." 단순한 텍스트 전달을 넘어, 팀의 컨벤션과 제약 조건을 강제하는 역할

---

## 2. 배경 및 문제 정의 (Problem Statement)

### 문제

1. **시중 Notion MCP의 노이즈:** 불필요한 메타데이터까지 AI에게 전달되어 토큰 낭비 및 할루시네이션 유발
2. **수동 컨벤션 주입:** `@Getter`, `@Builder`, `@Size` 등의 규칙을 매번 프롬프트로 설명해야 함
3. **데이터 무결성 부족:** 노션에 적힌 제약 조건이 코드에 누락되어도 검증할 방법이 없음

### 해결

- **커스텀 파싱:** 필요한 속성(Properties)만 쏙 뽑아 정제된 상태로 AI에게 제공.
- **엔지니어링 가드레일:** MCP 내부 로직에 팀 컨벤션을 하드코딩/변수화하여 AI가 항상 일관된 코드를 뱉게 함.

---

## 3. 핵심 기능 (Key Features)

### **[F-1] 지능형 속성 파서**

- Notion API 응답 중 `rich_text`, `select`, `number` 타입을 Java 타입(`String`, `Long`, `Enum` 등)으로 자동 매핑.
- 지저분한 중첩 JSON을 `{ "fieldName": "userId", "type": "Long" }` 형태의 클린 데이터로 변환

### **[F-2] 제약 조건 자동 주입**

- 노션의 '비고'나 '제한사항' 컬럼을 읽어 `@NotNull`, `@Size`, `@Min`, `@Max` 등의 Bean Validation 어노테이션 생성 가이드 포함

### **[F-3] 컨벤션 프리셋**

- 변수에 따라 미리 정의된 어노테이션 세트(`@Builder`, `AccessLevel.PROTECTED` 등)를 결과물에 적용하도록 지시 포함

---

## 4. 기술 스택

- **언어:** Python
- **프레임워크:** FastMCP (Model Context Protocol SDK)
- **외부 API:** Notion API

---

## 5. 유스케이스 및 워크플로우 (User Flow)

1. **사용자:** 노션 명세 페이지 링크를 복사.
2. **Claude:** mcp 실행.
3. **MCP:** 노션 API 호출 -> 데이터 정제 -> 컨벤션 결합 -> Claude에게 응답
4. **Claude:** 최적화된 Java 코드를 작성하고 필요 시 파일 시스템에 즉시 반영

---

## 6. 기대 효과 (Success Metrics)

- **개발 속도:** API 1개당 DTO 생성 및 검증 시간 **단축** (수동 타이핑 대비).
- **정확도:** 명세서와 코드 간 필드명/타입 불일치 사고 예방
- **협업 효율:** MCP만 사용하면 팀 컨벤션을 완벽히 준수한 코드를 작성 가능.

---

