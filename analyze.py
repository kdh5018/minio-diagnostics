#!/usr/bin/env python3
"""R1 entrypoint. Standard library only; never collects or connects to a server."""
import os
import sys

# 0.5.13: 메모리 부족(MemoryError)은 처리하지 않은 예외의 종료 코드 1(--fail-on-priority와 겹침)이 아니라
# 전용 코드 6으로 끝낸다(Shield 0.5.12 재점검 N1). 안내 문구는 메모리가 모자란 시점에 새로 만들지 않도록
# 미리 인코딩해 둔다. 트레이스백은 보이지 않는다(입력 값이 나가지 않으며 사용자에게 의미가 없음).
EXIT_NO_MEMORY = 6
NO_MEMORY_MESSAGE = (
    '메모리 부족: 메모리 부족으로 보고서를 저장하지 않았습니다. 기존 확정 결과는 그대로입니다.\n'
    '  메모리 상한(ulimit 등)을 늘리거나 더 큰 분석 서버에서 실행하세요.\n'
).encode('utf-8')


def _run():
    if any(arg == '--health' or arg.startswith('--health=') for arg in sys.argv[1:]):
        from r2_health import main as health_main
        return health_main()
    from aistordoctor import DEFAULT_OUTPUT_DIR, main
    return main(default_out_dir=DEFAULT_OUTPUT_DIR)


def _no_memory_exit():
    try:
        sys.stderr.flush()
    except BaseException:
        pass
    try:
        os.write(2, NO_MEMORY_MESSAGE)
    except BaseException:
        pass
    return EXIT_NO_MEMORY


if __name__ == '__main__':
    try:
        code = _run()
    except MemoryError:
        code = _no_memory_exit()
    raise SystemExit(code)
