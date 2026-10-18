#!/usr/bin/env python3
"""wsFlow 실행.

  python run.py [--port 8765] [--store projects.json] [--no-browser] [--verbose]
  python run.py --check-jira <WF_HOME> [--fetch]   자격·인증·스코프·필드 경로 확인
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wsflow.server import serve  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="wsFlow — 로컬 웹 workspace 관리자")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--store", default=str(Path(__file__).resolve().parent / "projects.json"))
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--check-jira", metavar="WF_HOME",
                    help="자격·인증·스코프·필드 경로를 확인하고 끝낸다(읽기 전용). 서버를 띄우지 않는다")
    ap.add_argument("--fetch", action="store_true",
                    help="--check-jira 와 함께: 확인 후 원본을 .workflow/raw/ 에 남긴다")
    a = ap.parse_args()
    if a.check_jira:
        from wsflow import selfcheck  # noqa: PLC0415
        return selfcheck.run(a.check_jira, a.fetch)
    httpd = serve(a.store, a.host, a.port, open_browser=not a.no_browser)
    url = f"http://{a.host}:{httpd.server_address[1]}/?t={httpd.wsflow_token}"
    print(f"wsFlow  {url}")
    print(f"        (store: {a.store})  Ctrl+C 로 종료")
    print("        ↑ 토큰이 붙은 이 주소로 여십시오. 토큰 없이는 API 가 403 을 냅니다(F-004).")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
