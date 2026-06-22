from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────────────
# loop.py — AI 생성 코드 검증 & 자가 교정 루프
#
# 핵심 설계 원칙: "프롬프트로 규칙을 지시하는 것만으로는 충분하지 않다."
#   → Gemini가 잘못된 코드를 생성하면, 즉각적이고 명확한 에러 피드백을
#     Gemini에게 다시 전달해 자가 교정을 유도한다.
#
# 3단계 검증 파이프라인 (파일이 디스크에 기록된 직후 실행):
#   0. _validate_java_patterns  : Python regex — Gradle 없이 즉시, 컴파일 통과해도 잡힘
#   1. _run_gradle_compile      : compileJava  — 에러 메시지가 짧고 명확
#   2. _run_gradle_tests        : test         — 런타임 실패 감지
#
# 각 단계가 실패하면 해당 에러 로그를 그대로 _correct_with_gemini()에 전달한다.
# 에러 로그를 프롬프트에 포함하지 않으면 Gemini가 동일한 코드를 반복하므로 필수.
# ─────────────────────────────────────────────────────────────────────────────

import os
import re
import subprocess
from pathlib import Path

from google import genai

MAX_CORRECTIONS = 3  # 교정 최대 횟수. 초과 시 RuntimeError + 파일 롤백.

# ─── 금지 패턴 목록 ───────────────────────────────────────────────────────────
# Gemini가 자주 생성하는 규칙 위반 패턴들.
# "규칙을 자연어로 지시"하는 대신 이 목록에 추가해 결정론적으로 강제한다.
# 새로운 금지 패턴이 발견되면 CLAUDE.md 주석이 아니라 여기에 추가할 것.
# (컴파일은 통과하지만 프로젝트 아키텍처 규칙을 위반하는 코드들)
_FORBIDDEN_JAVA_PATTERNS: list[tuple[str, str]] = [
    (r"\bclass\s+\w+Impl\b",          "ServiceImpl 클래스 생성 금지"),
    (r"@Repository\b",                "@Repository 어노테이션 금지"),
    (r"@Entity\b",                    "@Entity 어노테이션 금지"),
    (r"@Table\b",                     "@Table 어노테이션 금지"),
    (r"import\s+\S+\.repository\.",   "Repository import 금지"),
    (r"import\s+javax\.persistence\.", "JPA persistence import 금지"),
    (r"import\s+jakarta\.persistence\.", "JPA persistence import 금지"),
    (r"extends\s+JpaRepository",      "JpaRepository 확장 금지"),
    (r"extends\s+CrudRepository",     "CrudRepository 확장 금지"),
]


def _validate_java_patterns(files: list[dict[str, str]]) -> str:
    """생성된 Java 파일에서 _FORBIDDEN_JAVA_PATTERNS 위반을 검사한다.

    반환값: 위반 없으면 빈 문자열, 있으면 _correct_with_gemini()에 바로 넘길 수 있는 문자열.

    Gradle을 실행하지 않아도 되므로 compileJava보다 빠르다.
    컴파일은 통과하지만 프로젝트 규칙을 어기는 패턴(@Entity, ServiceImpl 등)을
    교정 시도 횟수를 소모하지 않고 즉시 잡는다.
    """
    violations: list[str] = []
    for f in files:
        content = f.get("content", "")
        for pattern, msg in _FORBIDDEN_JAVA_PATTERNS:
            if re.search(pattern, content):
                violations.append(f"  {f['filename']}: {msg}")
    if not violations:
        return ""
    return "[금지 패턴 위반 — 아래 항목을 수정하라]\n" + "\n".join(violations)


def run_harness_tests(cli_root: Path) -> tuple[bool, str]:
    """하네스 Python 코드에 pytest를 실행해 자체 검증한다.

    Gemini 자동 교정 없이 결과만 반환한다. 호출자가 sys.exit(1) 처리.
    exit code 5 (수집된 테스트 없음)는 통과로 처리한다.
    """
    result = subprocess.run(
        ["python", "-m", "pytest", "tests/", "-v"],
        capture_output=True,
        text=True,
        cwd=cli_root,
    )
    passed = result.returncode in (0, 5)
    return passed, result.stdout + result.stderr


def _ensure_gradlew_executable(spring_root: Path) -> Path | None:
    """gradlew가 존재하고 실행 가능한지 확인한다. 없으면 None 반환."""
    gradlew = spring_root / "gradlew"
    if not gradlew.exists():
        return None
    if not os.access(gradlew, os.X_OK):
        gradlew.chmod(gradlew.stat().st_mode | 0o111)
    return gradlew


def _run_gradle_compile(spring_root: Path) -> tuple[bool, str]:
    """compileJava만 실행해 컴파일 에러를 빠르게 감지한다.

    전체 테스트 대신 compileJava를 먼저 실행하는 이유:
    - 속도: 컴파일만 하면 수초, 전체 테스트는 수십 초
    - 에러 명확성: "cannot find symbol: class OrderRequest" 같은 단순 메시지
      → Gemini가 정확히 어떤 import가 틀렸는지 파악 가능
    - 전체 테스트 로그는 길고 노이즈가 많아 Gemini가 핵심을 놓치는 경우가 있었음
    """
    gradlew = _ensure_gradlew_executable(spring_root)
    if gradlew is None:
        return True, "(gradlew 없음 — 컴파일 생략)"

    result = subprocess.run(
        [str(gradlew), "compileJava", "--rerun-tasks"],
        capture_output=True,
        text=True,
        cwd=spring_root,
    )
    return result.returncode == 0, result.stdout + result.stderr


def _run_gradle_tests(spring_root: Path) -> tuple[bool, str]:
    """스프링 프로젝트 루트에서 Gradle 테스트를 실행하고 결과를 반환한다.

    Returns:
        (테스트 전부 통과하면 True, stdout+stderr 합친 로그 문자열)
    """
    gradlew = _ensure_gradlew_executable(spring_root)
    if gradlew is None:
        return True, "(gradlew 없음 — 테스트 생략)"

    result = subprocess.run(
        [str(gradlew), "test", "--rerun-tasks"],
        capture_output=True,
        text=True,
        cwd=spring_root,
    )
    return result.returncode == 0, result.stdout + result.stderr


def _correct_with_gemini(
    files: list[dict[str, str]],
    error_log: str,
    attempt: int,
) -> list[dict[str, str]]:
    """검증 실패 로그를 포함해 Gemini에게 코드 수정을 요청한다.

    Args:
        files:     현재 스프링 프로젝트에 기록된 Java 파일 목록 (filename, content, path)
        error_log: 실패한 단계의 에러 로그 (패턴 위반 / 컴파일 에러 / 테스트 실패 중 하나)
        attempt:   현재 시도 횟수 (프롬프트에 포함해 Gemini가 맥락을 파악하게 함)

    error_log는 3가지 유형이 올 수 있다:
      - _validate_java_patterns() 결과: "[금지 패턴 위반]..." 형식의 명시적 규칙 위반
      - _run_gradle_compile() 결과: "cannot find symbol" 같은 컴파일 에러
      - _run_gradle_tests() 결과: JUnit 테스트 실패 스택 트레이스

    에러 로그를 반드시 프롬프트에 포함해야 한다.
    포함하지 않으면 Gemini가 이전과 동일한 코드를 반복해서 출력한다.
    수정된 파일이 없으면(파싱 실패) 원본을 그대로 반환한다.
    """
    from generator import _parse_files

    files_text = "\n\n".join(
        f"```java\n// {f['filename']}\n{f['content']}\n```"
        for f in files
    )

    prompt = f"""다음 Java 코드에서 검증 실패가 발생했습니다 (시도 {attempt}/{MAX_CORRECTIONS}).

[생성된 코드]
{files_text}

[에러 로그]
{error_log}

에러를 분석하고 수정된 전체 코드를 출력하세요.
이전과 동일한 프롬프트를 반복하지 말고, 위 에러 로그를 직접 해결하세요.""".strip()

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
    )
    corrected = _parse_files(response.text)
    return corrected if corrected else files


def _rewrite_files(files: list[dict[str, str]], spring_root: Path) -> list[dict[str, str]]:
    """교정된 파일 내용을 올바른 경로에 덮어쓴다.

    교정 후 package 선언이 바뀔 수 있으므로 기존 path를 신뢰하지 않고
    항상 content의 package 선언을 기반으로 경로를 재계산한다.
    (잘못된 경로 누적 방지 — contrer, servi 등 Gemini 오탐 경로 재사용 방지)
    """
    from merge import _resolve_target_path

    updated: list[dict[str, str]] = []
    for f in files:
        target = _resolve_target_path(f["filename"], f["content"], spring_root)
        target.write_text(f["content"], encoding="utf-8")
        print(f"  재작성: {target}")
        updated.append({**f, "path": str(target)})
    return updated


def run_premerge_review(files: list[dict[str, str]]) -> list[dict[str, str]]:
    """디스크 쓰기 없이 Gemini가 생성된 파일들의 일관성을 검토·교정한다.

    merge 승인 전에 실행해 사용자에게 보여줄 최종 코드 품질을 높인다.
    주로 확인하는 항목:
      - Controller/Service/Test의 DTO 클래스 참조가 실제 DTO 파일명과 일치하는지
      - import 경로가 올바른지
      - 파일명과 public class명이 일치하는지

    파일이 없거나 GEMINI_API_KEY가 없으면 원본 그대로 반환한다.
    """
    if not files:
        return files

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return files

    from generator import _parse_files

    files_text = "\n\n".join(
        f"```java\n// {f['filename']}\n{f['content']}\n```"
        for f in files
    )

    prompt = f"""다음 Spring Boot 파일들이 생성됐습니다.
아래 항목을 검토하고 문제가 있으면 수정하라:
1. Controller/Service/Test에서 DTO 클래스명이 실제 DTO 파일명(확장자 제외)과 정확히 일치하는지
2. import 경로가 파일의 package 선언과 일치하는지
3. public class명이 파일명(확장자 제외)과 일치하는지
4. 변수명이 DTO 클래스명 규칙(camelCase)을 따르는지
5. Service가 인터페이스가 아닌 @Service @RequiredArgsConstructor 클래스인지

절대 하지 말 것:
- // TODO 주석을 구현체 코드로 바꾸지 말 것
- 비즈니스 로직을 추측해서 채우지 말 것
- ServiceImpl 클래스를 추가하지 말 것
- entity, repository 파일을 추가하거나 import하지 말 것
- 테스트 메서드 본문에 new DTO(), mock(), given(), assertThat() 등 실제 코드를 추가하지 말 것

수정이 없더라도 모든 파일을 동일한 ```java 블록 형식으로 출력하라.

{files_text}""".strip()

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
    )
    reviewed = _parse_files(response.text)

    # Gemini가 일부 파일을 누락하면 원본으로 채운다
    reviewed_map = {f["filename"]: f for f in reviewed}
    return [reviewed_map.get(f["filename"], f) for f in files]


def _rollback_files(paths: list[str]) -> None:
    """교정 루프 실패 시 작성된 파일을 모두 삭제한다."""
    print("\n[롤백] 생성된 파일을 삭제합니다...")
    for path_str in paths:
        p = Path(path_str)
        try:
            if p.exists():
                p.unlink()
                print(f"  삭제: {p}")
        except Exception as exc:
            print(f"  삭제 실패: {p} — {exc}")


def run_correction_loop(
    written_files: list[dict[str, str]],
    spring_root: Path,
) -> None:
    """Gradle 테스트 실행 → 실패 시 Gemini 교정 → 재작성을 반복하는 루프.

    Args:
        written_files: merge_files()가 반환한, 실제로 기록된 파일 목록
                       각 항목에 filename, content, path 키가 있다.
        spring_root:   스프링 프로젝트 루트 (gradlew 위치)

    gradlew가 없으면 테스트를 생략하고 즉시 반환한다.
    MAX_CORRECTIONS 횟수를 초과해도 실패하면 작성 파일을 롤백하고 RuntimeError를 발생시킨다.
    """
    if not written_files:
        return

    # 롤백용: 루프 전체에서 기록된 모든 파일 경로 추적
    all_written_paths: list[str] = [f["path"] for f in written_files if f.get("path")]
    current_files = written_files

    for attempt in range(1, MAX_CORRECTIONS + 1):
        # 0단계: 금지 패턴 검사 — Gradle 없이 즉시 감지 (컴파일 통과해도 잡힘)
        violation_log = _validate_java_patterns(current_files)
        if violation_log:
            print(f"\n[패턴 위반] 시도 {attempt}/{MAX_CORRECTIONS}\n{violation_log}")
            if attempt == MAX_CORRECTIONS:
                _rollback_files(all_written_paths)
                raise RuntimeError(
                    f"교정 루프 {MAX_CORRECTIONS}회 초과. 수동 확인 필요.\n\n{violation_log}"
                )
            print("  Gemini로 교정 중 (패턴 위반)...")
            corrected = _correct_with_gemini(current_files, violation_log, attempt)
            current_files = _rewrite_files(corrected, spring_root)
            for f in current_files:
                if f.get("path") and f["path"] not in all_written_paths:
                    all_written_paths.append(f["path"])
            continue

        # 1단계: 컴파일 체크 — 에러 메시지가 명확해 Gemini 교정 품질이 높다
        compiled, compile_log = _run_gradle_compile(spring_root)
        if not compiled:
            print(f"\n[컴파일 실패] 시도 {attempt}/{MAX_CORRECTIONS}")
            if attempt == MAX_CORRECTIONS:
                _rollback_files(all_written_paths)
                raise RuntimeError(
                    f"교정 루프 {MAX_CORRECTIONS}회 초과. 수동 확인 필요.\n\n{compile_log}"
                )
            print("  Gemini로 교정 중 (컴파일 에러)...")
            corrected = _correct_with_gemini(current_files, compile_log, attempt)
            current_files = _rewrite_files(corrected, spring_root)
            for f in current_files:
                if f.get("path") and f["path"] not in all_written_paths:
                    all_written_paths.append(f["path"])
            continue

        # 2단계: 전체 테스트
        passed, test_log = _run_gradle_tests(spring_root)
        print(f"\n[테스트 {'통과' if passed else '실패'}] 시도 {attempt}/{MAX_CORRECTIONS}")

        if passed:
            print("  모든 테스트 통과.")
            return

        if attempt == MAX_CORRECTIONS:
            _rollback_files(all_written_paths)
            raise RuntimeError(
                f"교정 루프 {MAX_CORRECTIONS}회 초과. 수동 확인 필요.\n\n{test_log}"
            )

        print("  Gemini로 교정 중...")
        corrected = _correct_with_gemini(current_files, test_log, attempt)
        current_files = _rewrite_files(corrected, spring_root)
        for f in current_files:
            if f.get("path") and f["path"] not in all_written_paths:
                all_written_paths.append(f["path"])
