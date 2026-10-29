"""Owned, self-bounded stdlib subprocess actor for writer admission tests."""
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wsflow.writer_admission import WriterDomain, WriterUnavailable


def main():
    mode, control, effect = sys.argv[1:4]
    domain, effect = WriterDomain(Path(control)), Path(effect)
    try:
        with domain.writing(timeout=.15):
            effect.write_text(mode, encoding='utf-8')
            if mode == 'crash':
                os._exit(91)
            if mode == 'hold':
                # No descendants. Parent owns the Popen handle and kills/reaps it.
                time.sleep(8)
        print(json.dumps({'status': 'complete'}), flush=True)
    except WriterUnavailable as error:
        print(json.dumps({'status': 'refused', 'reason': error.reason}), flush=True)


if __name__ == '__main__':
    main()
