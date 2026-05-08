from __future__ import annotations

# CLI 진입점. "notion-harness run" 과 "notion-harness watch" 두 서브커맨드를 제공한다.
#
# 전체 파이프라인 흐름:
#   Notion DB URL
#     → parser.py  : MCP 서버 호출 → DTO JSON 목록
#     → generator.py: Gemini API 호출 → Java 파일 목록
#     → merge.py   : diff 출력 → 사용자 승인 → 스프링 프로젝트에 파일 반영
#     → loop.py    : Gradle 테스트 실행 → 실패 시 Gemini 교정 후 재작성 (최대 3회)

import argparse
import time
from pathlib import Path

from dotenv import load_dotenv

# 프로세스 시작 시 .env 파일을 읽어 환경변수로 등록
load_dotenv()


def run(url: str, spring_root: Path) -> None:
    """Notion DB URL을 받아 파이프라인을 한 번 실행한다.

    1. parse_all_specs: MCP로 전체 API 명세 → DTO 정의 목록
    2. generate_code:   각 DTO 명세 → Gemini → Java 파일 목록
    3. merge_files:     사용자 승인 후 스프링 프로젝트에 파일 반영 (먼저 기록해야 Gradle 빌드 가능)
    4. run_correction_loop: Gradle 테스트 실행 → 실패 시 Gemini 교정 후 재작성 (최대 3회)
    """
    import json
    from generator import generate_code_all
    from loop import run_correction_loop
    from merge import merge_files
    from parser import parse_all_specs

    specs = parse_all_specs(url)
    print(f"\n[MCP 결과] {len(specs)}개 API 스펙")
    print(json.dumps(specs, ensure_ascii=False, indent=2))

    all_files = generate_code_all(specs, spring_root)

    print("\n[생성된 파일 목록]")
    for f in all_files:
        print(f"  - {f['filename']}")

    # 먼저 파일을 스프링 프로젝트에 반영해야 Gradle 빌드가 가능하다
    written_files = merge_files(all_files, spring_root)

    # 파일이 실제로 기록된 경우에만 Gradle 테스트 + 교정 루프를 실행한다
    if written_files:
        run_correction_loop(written_files, spring_root)


def watch(url: str, spring_root: Path, interval: int = 30) -> None:
    """Notion 페이지를 주기적으로 폴링하다가 변경이 감지되면 run()을 호출한다.

    폴링은 last_edited_time 비교로만 하므로 Gemini 비용이 발생하지 않는다.
    변경이 감지됐을 때만 MCP + Gemini 파이프라인이 실행된다.
    """
    from poller import NotionPoller

    poller = NotionPoller(url)

    # 시작 시점의 타임스탬프를 기준으로 설정 — 이후 변경분만 감지하기 위함
    _, last_edited = poller.has_changed("")
    print(f"폴링 시작 (기준 시각: {last_edited}, 간격: {interval}초) — Ctrl+C로 종료")

    while True:
        time.sleep(interval)
        changed, last_edited_new = poller.has_changed(last_edited)
        if changed:
            print(f"변경 감지: {url}")
            last_edited = last_edited_new
            run(url, spring_root)


def main() -> None:
    """argparse로 CLI 인터페이스를 정의하고 서브커맨드를 디스패치한다.

    사용법:
        notion-harness run   --url <Notion DB URL>
        notion-harness watch --url <Notion DB URL> [--interval 초]

    --root 옵션으로 스프링 프로젝트 루트를 지정할 수 있다.
    지정하지 않으면 현재 작업 디렉토리(os.getcwd())를 사용한다.
    """
    parser = argparse.ArgumentParser(prog="notion-harness")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        metavar="DIR",
        help="스프링 프로젝트 루트 (기본값: 현재 디렉토리)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="한 번 실행")
    run_parser.add_argument("--url", required=True, help="Notion DB URL")

    watch_parser = subparsers.add_parser("watch", help="변경 폴링 루프")
    watch_parser.add_argument("--url", required=True, help="Notion DB URL")
    watch_parser.add_argument("--interval", type=int, default=30, metavar="SEC")

    args = parser.parse_args()

    if args.command == "run":
        run(args.url, args.root)
    elif args.command == "watch":
        watch(args.url, args.root, args.interval)


if __name__ == "__main__":
    main()
