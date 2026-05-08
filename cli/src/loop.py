from __future__ import annotations

# 스프링 프로젝트에 파일이 기록된 후 Gradle 테스트를 실행하고,
# 실패하면 Gemini로 자동 교정 후 파일을 직접 재작성하는 모듈.
# 최대 MAX_CORRECTIONS(3)회까지 시도하고, 그래도 실패하면 사람에게 보고한다.

import os
import subprocess
from pathlib import Path

from google import genai

# 교정 최대 시도 횟수. 이 횟수를 초과하면 RuntimeError를 발생시킨다.
MAX_CORRECTIONS = 3


def _run_gradle_tests(spring_root: Path) -> tuple[bool, str]:
    """스프링 프로젝트 루트에서 Gradle 테스트를 실행하고 결과를 반환한다.

    Returns:
        (테스트 전부 통과하면 True, stdout+stderr 합친 로그 문자열)

    ./gradlew 실행 권한이 없으면 자동으로 chmod +x를 시도한다.
    """
    gradlew = spring_root / "gradlew"
    if not gradlew.exists():
        return True, "(gradlew 없음 — 테스트 생략)"

    if not os.access(gradlew, os.X_OK):
        gradlew.chmod(gradlew.stat().st_mode | 0o111)

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
    """테스트 실패 로그를 포함해 Gemini에게 코드 수정을 요청한다.

    Args:
        files:     현재 스프링 프로젝트에 기록된 Java 파일 목록 (filename, content, path)
        error_log: Gradle 테스트가 출력한 에러 로그 전문
        attempt:   현재 시도 횟수 (프롬프트에 포함해 Gemini가 맥락을 파악하게 함)

    에러 로그를 반드시 프롬프트에 포함해야 한다.
    포함하지 않으면 Gemini가 이전과 동일한 코드를 반복해서 출력한다.

    수정된 파일이 없으면(파싱 실패) 원본을 그대로 반환한다.
    """
    from generator import _parse_files

    files_text = "\n\n".join(
        f"```java\n// {f['filename']}\n{f['content']}\n```"
        for f in files
    )

    prompt = f"""다음 Java 코드에서 Gradle 테스트 실패가 발생했습니다 (시도 {attempt}/{MAX_CORRECTIONS}).

[생성된 코드]
{files_text}

[Gradle 테스트 에러 로그]
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
    """교정된 파일 내용을 기존 경로에 덮어쓴다.

    path 키가 있으면 그 경로에, 없으면 spring_root를 기준으로 경로를 새로 결정한다.
    교정은 사용자가 이미 초기 merge에서 승인했으므로 추가 확인 없이 바로 재작성한다.
    """
    from merge import _resolve_target_path

    updated: list[dict[str, str]] = []
    for f in files:
        target = Path(f["path"]) if f.get("path") else _resolve_target_path(
            f["filename"], f["content"], spring_root
        )
        target.write_text(f["content"])
        print(f"  재작성: {target}")
        updated.append({**f, "path": str(target)})
    return updated


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
    MAX_CORRECTIONS 횟수를 초과해도 실패하면 RuntimeError를 발생시킨다.
    """
    if not written_files:
        return

    current_files = written_files

    for attempt in range(1, MAX_CORRECTIONS + 1):
        passed, error_log = _run_gradle_tests(spring_root)
        print(f"\n[테스트 {'통과' if passed else '실패'}] 시도 {attempt}/{MAX_CORRECTIONS}")

        if passed:
            print("  모든 테스트 통과.")
            return

        if attempt == MAX_CORRECTIONS:
            raise RuntimeError(
                f"교정 루프 {MAX_CORRECTIONS}회 초과. 수동 확인 필요.\n\n{error_log}"
            )

        print("  Gemini로 교정 중...")
        corrected = _correct_with_gemini(current_files, error_log, attempt)
        current_files = _rewrite_files(corrected, spring_root)
