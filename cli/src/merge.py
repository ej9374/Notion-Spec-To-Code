from __future__ import annotations

# 생성된 Java 파일을 스프링 프로젝트에 반영하는 모듈.
# 기존 파일이 있으면 diff를 먼저 보여주고 사용자 승인을 받은 뒤 덮어쓴다.
# (CLAUDE.md 절대 규칙: 기존 파일 덮어쓰기 전 반드시 diff 보여줄 것)

import difflib
import re
from pathlib import Path

# 파일명이 다르더라도 내용 유사도가 이 값 이상이면 "비슷한 파일"로 간주한다.
SIMILARITY_THRESHOLD = 0.6

# 파일명 suffix → 패키지 하위 경로 매핑.
# _resolve_target_path에서 package 선언이 없거나 베이스 패키지와 맞지 않을 때 사용한다.
_FILENAME_SUFFIX_TO_SUBPKG: list[tuple[str, str]] = [
    ("ControllerTest.java", "controller"),
    ("ServiceTest.java", "service"),
    ("Controller.java", "controller"),
    ("Service.java", "service"),
    ("Request.java", "dto.request"),
    ("Response.java", "dto.response"),
    ("Event.java", "dto.event"),
    ("Enum.java", "dto.enums"),
]


def _infer_subpkg_from_filename(filename: str) -> str:
    """파일명 suffix로 패키지 하위 경로를 추론한다. 매칭 없으면 'dto' 반환."""
    for suffix, subpkg in _FILENAME_SUFFIX_TO_SUBPKG:
        if filename.endswith(suffix):
            return subpkg
    return "dto"


def _find_exact_file(filename: str, spring_root: Path) -> Path | None:
    """파일명 stem이 정확히 일치하는 .java 파일을 반환한다 (대소문자 무시)."""
    stem = Path(filename).stem.lower()
    for existing in spring_root.rglob("*.java"):
        if existing.stem.lower() == stem:
            return existing
    return None


def _content_similarity(a: str, b: str) -> float:
    """두 문자열의 유사도를 0.0~1.0 사이로 반환한다.

    difflib.SequenceMatcher 기반으로, 1.0이면 완전히 동일한 내용이다.
    """
    return difflib.SequenceMatcher(None, a, b).ratio()


def _find_similar_by_content(
    new_content: str,
    exclude_name: str,
    spring_root: Path,
) -> list[tuple[Path, float]]:
    """파일명과 무관하게 내용 유사도가 SIMILARITY_THRESHOLD 이상인 .java 파일을 찾는다.

    Args:
        new_content:  새로 생성된 파일의 내용
        exclude_name: 이미 exact match로 확인한 파일명 (중복 방지)
        spring_root:  스프링 프로젝트 루트

    Returns:
        [(파일 경로, 유사도), ...] — 유사도 내림차순 정렬
    """
    results: list[tuple[Path, float]] = []
    exclude_stem = Path(exclude_name).stem.lower()

    for existing in spring_root.rglob("*.java"):
        if existing.stem.lower() == exclude_stem:
            continue  # exact match 로직에서 이미 처리한 파일은 건너뜀
        ratio = _content_similarity(new_content, existing.read_text())
        if ratio >= SIMILARITY_THRESHOLD:
            results.append((existing, ratio))

    results.sort(key=lambda x: x[1], reverse=True)
    return results


def _ask_overwrite_similar(similar_path: Path, ratio: float, filename: str) -> bool:
    """내용이 유사한 기존 파일을 발견했을 때 덮어쓸지 사용자에게 묻는다."""
    print(
        f"\n⚠️  내용 유사 파일 발견: {similar_path.name} (유사도 {ratio:.0%})\n"
        f"   새 파일: {filename}\n"
        f"   기존 파일: {similar_path}"
    )
    answer = input("기존 파일을 새 내용으로 대체하시겠습니까? [y/N] ").strip().lower()
    return answer in ("y", "yes")


def _show_diff(old_content: str, new_content: str, filename: str) -> str:
    """기존 파일과 새 파일의 unified diff를 문자열로 반환한다.

    내용이 동일하면 빈 문자열("")을 반환한다.
    """
    diff = difflib.unified_diff(
        old_content.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile=f"기존/{filename}",
        tofile=f"신규/{filename}",
    )
    return "".join(diff)


def _ask_approval(diff_text: str, filename: str) -> bool:
    """파일명과 diff를 출력하고 사용자에게 적용 여부를 묻는다.

    diff_text가 비어있으면 새 파일임을 표시한다.
    "y" 또는 "yes" 입력 시 True 반환, 그 외 False 반환.
    """
    print(f"\n=== {filename} ===")
    if diff_text:
        print(diff_text)
    else:
        print("(새 파일)")
    answer = input("이 파일을 적용하시겠습니까? [y/N] ").strip().lower()
    return answer in ("y", "yes")


def _resolve_target_path(filename: str, content: str, spring_root: Path) -> Path:
    """생성된 코드의 package 선언을 읽어 저장 경로를 결정한다.

    *Test.java 파일은 src/test/java, 그 외는 src/main/java 아래에 놓는다.

    package 선언 우선순위:
      1. 코드 내 package 선언이 있으면 그대로 사용
      2. 없으면 파일명 suffix로 추론 (Request→dto.request, Controller→controller 등)
      3. 그래도 모르면 "dto" fallback
    베이스 패키지는 스프링 프로젝트의 기존 .java 파일에서 자동 감지한다.
    """
    pkg_match = re.search(r"^package\s+([\w.]+);", content, re.MULTILINE)
    if pkg_match:
        package = pkg_match.group(1)
    else:
        # package 선언 없음 → 베이스 패키지 감지 후 파일명으로 하위 경로 추론
        from generator import _detect_package
        base = _detect_package(spring_root)
        subpkg = _infer_subpkg_from_filename(filename)
        package = f"{base}.{subpkg}"

    package_path = Path(*package.split("."))

    # 파일명이 Test.java로 끝나면 test 소스셋, 그 외는 main 소스셋
    source_set = "test" if filename.endswith("Test.java") else "main"
    target_dir = spring_root / "src" / source_set / "java" / package_path
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / filename


def merge_files(files: list[dict[str, str]], spring_root: Path) -> list[dict[str, str]]:
    """생성된 파일 목록을 순회하며 스프링 프로젝트에 반영한다.

    처리 순서 (파일 하나당):
      1. 파일명 exact match 확인
      2. exact match 없으면 내용 유사도 검사 (SIMILARITY_THRESHOLD 이상인 파일)
      3. diff 출력 후 사용자 승인 요청
      4. 승인 → 덮어쓰기 or 신규 생성 / 거절 → 건너뜀

    Returns:
        실제로 디스크에 기록된 파일 정보 목록 (교정 루프에 전달하기 위해 반환)
    """
    written: list[dict[str, str]] = []
    for file_info in files:
        filename = file_info["filename"]
        new_content = file_info["content"]
        try:
            written_path = _merge_single(filename, new_content, spring_root)
            if written_path:
                written.append({"filename": filename, "content": new_content, "path": str(written_path)})
        except Exception as exc:
            print(f"  [오류] {filename} 처리 중 예외 발생: {exc}")
    return written


def _merge_single(filename: str, new_content: str, spring_root: Path) -> Path | None:
    """파일 1개를 스프링 프로젝트에 반영하고 기록된 경로를 반환한다.

    사용자가 거절하거나 예외가 나면 None 반환. 예외는 merge_files에서 캐치한다.
    """
    # 1단계: 파일명이 같은 파일 탐색
    existing = _find_exact_file(filename, spring_root)

    if existing:
        diff_text = _show_diff(existing.read_text(), new_content, filename)
        if not _ask_approval(diff_text, filename):
            print(f"  건너뜀: {filename}")
            return None
        existing.write_text(new_content)
        print(f"  수정됨: {existing}")
        return existing

    # 2단계: 파일명은 다르지만 내용이 유사한 파일 탐색
    similar_files = _find_similar_by_content(new_content, filename, spring_root)
    for similar_path, ratio in similar_files:
        diff_text = _show_diff(similar_path.read_text(), new_content, filename)
        if _ask_overwrite_similar(similar_path, ratio, filename):
            print(diff_text or "  (내용 동일)")
            similar_path.write_text(new_content)
            print(f"  대체됨: {similar_path} → {filename} 내용으로 덮어씀")
            return similar_path

    # 3단계: 유사 파일도 없거나 모두 거절 → 새 파일로 생성
    if not _ask_approval("", filename):
        print(f"  건너뜀: {filename}")
        return None
    target = _resolve_target_path(filename, new_content, spring_root)
    target.write_text(new_content)
    print(f"  생성됨: {target}")
    return target
