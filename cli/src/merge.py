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
# Test/Tests 양쪽 suffix를 모두 포함: Gemini가 복수형(Tests.java)을 생성하는 경우가 있음.
_FILENAME_SUFFIX_TO_SUBPKG: list[tuple[str, str]] = [
    ("ControllerTests.java", "controller"),
    ("ServiceTests.java", "service"),
    ("ControllerTest.java", "controller"),
    ("ServiceTest.java", "service"),
    ("Controller.java", "controller"),
    ("Service.java", "service"),
    ("Request.java", "dto.request"),
    ("Response.java", "dto.response"),
    ("Event.java", "dto.event"),
    ("Enum.java", "dto.enums"),
]

# 베이스 패키지 바로 아래에 올 수 있는 유효 서브패키지 첫 세그먼트 목록.
# 여기 없는 값(contrer, servi, ronse 등 Gemini 오탐)은 파일명 suffix로 재추론한다.
_VALID_SUBPKG_ROOTS = {
    "controller", "service", "dto", "entity",
    "repository", "config", "exception", "common",
}


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
    exclude_stems: frozenset[str] | None = None,
) -> list[tuple[Path, float]]:
    """파일명과 무관하게 내용 유사도가 SIMILARITY_THRESHOLD 이상인 .java 파일을 찾는다.

    Args:
        new_content:    새로 생성된 파일의 내용
        exclude_name:   이미 exact match로 확인한 파일명 (중복 방지)
        spring_root:    스프링 프로젝트 루트
        exclude_stems:  이번 배치에서 생성 중인 파일 stem 집합 (방금 쓴 파일이 오탐 방지)

    Returns:
        [(파일 경로, 유사도), ...] — 유사도 내림차순 정렬
    """
    results: list[tuple[Path, float]] = []
    exclude_stem = Path(exclude_name).stem.lower()
    batch_stems = exclude_stems or frozenset()

    for existing in spring_root.rglob("*.java"):
        if existing.stem.lower() == exclude_stem:
            continue  # exact match 로직에서 이미 처리한 파일은 건너뜀
        if existing.stem.lower() in batch_stems:
            continue  # 이번 배치에서 방금 생성한 파일은 유사 파일 후보에서 제외
        try:
            existing_text = existing.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue  # 읽기 실패 파일은 스킵
        ratio = _content_similarity(new_content, existing_text)
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
    from generator import _detect_package
    base = _detect_package(spring_root)

    pkg_match = re.search(r"^package\s+([\w.]+);", content, re.MULTILINE)
    if pkg_match:
        package = pkg_match.group(1)
        # 베이스 패키지로 시작하는지 확인하고, 서브패키지 첫 세그먼트가 유효한지 검증
        if package.startswith(base):
            subpkg_root = package[len(base):].lstrip(".").split(".")[0]
            if subpkg_root not in _VALID_SUBPKG_ROOTS and subpkg_root != "":
                # contrer, servi, ronse 등 Gemini 오탐 → 파일명 suffix로 재추론
                subpkg = _infer_subpkg_from_filename(filename)
                package = f"{base}.{subpkg}"
        else:
            # 베이스 패키지 불일치 → 파일명 suffix로 재추론
            subpkg = _infer_subpkg_from_filename(filename)
            package = f"{base}.{subpkg}"
    else:
        # package 선언 없음 → 베이스 패키지 감지 후 파일명으로 하위 경로 추론
        subpkg = _infer_subpkg_from_filename(filename)
        package = f"{base}.{subpkg}"

    package_path = Path(*package.split("."))

    # *Test.java / *Tests.java → test 소스셋, 그 외 → main 소스셋.
    # Gemini가 단수(Test)와 복수(Tests) 양쪽을 생성하므로 둘 다 처리해야 한다.
    # 복수형을 누락하면 TicketingApplicationTests.java 같은 파일이
    # src/main/java에 들어가 compileJava 단계에서 @SpringBootTest import 오류가 발생한다.
    source_set = "test" if (filename.endswith("Test.java") or filename.endswith("Tests.java")) else "main"
    target_dir = spring_root / "src" / source_set / "java" / package_path
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / filename


def merge_files(files: list[dict[str, str]], spring_root: Path) -> list[dict[str, str]]:
    """생성된 파일 목록을 순회하며 스프링 프로젝트에 반영한다.

    처리 순서:
      0. 파일당 exact/similar/new 분류를 한 번만 스캔해 캐싱 (요약 테이블 + 실제 merge 공유)
      1. 요약 테이블 출력
      2. 파일별: 캐싱된 분류로 diff 출력 → 승인
      3. exact match 없으면 유사 파일 대체 여부 확인
      4. 유사 파일도 없으면 신규 생성 승인

    Returns:
        실제로 디스크에 기록된 파일 정보 목록 (교정 루프에 전달하기 위해 반환)
    """
    # 이번 배치에서 생성 중인 파일 stem 집합 — 유사 파일 스캔 시 오탐 방지
    batch_stems = frozenset(Path(f["filename"]).stem.lower() for f in files)

    # 파일당 분류를 한 번만 계산 — 요약 테이블과 실제 merge가 동일 결과를 재사용
    classifications: dict[str, tuple[Path | None, list[tuple[Path, float]]]] = {}
    for f in files:
        fn = f["filename"]
        ex = _find_exact_file(fn, spring_root)
        sim = [] if ex else _find_similar_by_content(f["content"], fn, spring_root, batch_stems)
        classifications[fn] = (ex, sim)

    # 요약 테이블 출력
    print("\n[변경 예정 파일 목록]")
    for f in files:
        fn = f["filename"]
        ex, sim = classifications[fn]
        if ex:
            print(f"  ✏️  수정: {fn}  →  {ex}")
        elif sim:
            print(f"  ⚠️  유사 파일 존재: {fn}  (기존: {sim[0][0].name}, 유사도 {sim[0][1]:.0%})")
        else:
            print(f"  🆕 신규: {fn}")
    print()

    written: list[dict[str, str]] = []
    for file_info in files:
        filename = file_info["filename"]
        new_content = file_info["content"]
        ex, sim = classifications[filename]
        try:
            written_path = _merge_single(filename, new_content, spring_root, batch_stems, _cached=(ex, sim))
            if written_path:
                written.append({"filename": filename, "content": new_content, "path": str(written_path)})
        except Exception as exc:
            print(f"  [오류] {filename} 처리 중 예외 발생: {exc}")
            raise
    return written


def _merge_single(
    filename: str,
    new_content: str,
    spring_root: Path,
    batch_stems: frozenset[str] = frozenset(),
    _cached: tuple[Path | None, list[tuple[Path, float]]] | None = None,
) -> Path | None:
    """파일 1개를 스프링 프로젝트에 반영하고 기록된 경로를 반환한다.

    _cached가 주어지면 _find_exact_file/_find_similar_by_content 재호출 없이 캐싱된 값 사용.
    사용자가 거절하면 None 반환.
    """
    if _cached is not None:
        existing, similar_files = _cached
    else:
        # 단독 호출 시 (테스트 등) — 직접 스캔
        existing = _find_exact_file(filename, spring_root)
        similar_files = [] if existing else _find_similar_by_content(new_content, filename, spring_root, batch_stems)

    # 1단계: 파일명이 같은 파일 수정
    if existing:
        diff_text = _show_diff(existing.read_text(), new_content, filename)
        if not _ask_approval(diff_text, filename):
            print(f"  건너뜀: {filename}")
            return None
        existing.write_text(new_content, encoding="utf-8")
        print(f"  수정됨: {existing}")
        return existing

    # 2단계: 내용이 유사한 파일 대체
    for similar_path, ratio in similar_files:
        diff_text = _show_diff(similar_path.read_text(), new_content, filename)
        if _ask_overwrite_similar(similar_path, ratio, filename):
            print(diff_text or "  (내용 동일)")
            similar_path.write_text(new_content, encoding="utf-8")
            print(f"  대체됨: {similar_path} → {filename} 내용으로 덮어씀")
            return similar_path

    # 3단계: 신규 파일 생성
    if not _ask_approval("", filename):
        print(f"  건너뜀: {filename}")
        return None
    target = _resolve_target_path(filename, new_content, spring_root)
    target.write_text(new_content, encoding="utf-8")
    print(f"  생성됨: {target}")
    return target
