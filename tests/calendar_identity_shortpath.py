"""Qualify the actual UID browser fixture under an owned Windows 8.3 TEMP alias."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    if os.name!='nt':
        raise RuntimeError('this qualification requires Windows short-path support')
    with tempfile.TemporaryDirectory(prefix='wsflow_uid_shortpath_') as folder:
        canonical=Path(folder).resolve()
        api=ctypes.WinDLL('kernel32',use_last_error=True).GetShortPathNameW
        api.argtypes=[wintypes.LPCWSTR,wintypes.LPWSTR,wintypes.DWORD];api.restype=wintypes.DWORD
        buffer=ctypes.create_unicode_buffer(32768);length=api(str(canonical),buffer,len(buffer))
        if not length or length>=len(buffer):raise OSError('short-path observation failed')
        short=Path(buffer.value)
        if str(short)==str(canonical) or short.resolve()!=canonical:
            raise RuntimeError('no distinct equivalent short path; qualification was not executed')
        env={**os.environ,'TEMP':str(short),'TMP':str(short),'TMPDIR':str(short),'PYTHON':sys.executable,'PYTHONDONTWRITEBYTECODE':'1'}
        cp=subprocess.run(['node','tests/calendar_identity_browser.cjs'],env=env,capture_output=True,timeout=110)
        sys.stdout.buffer.write(cp.stdout);sys.stderr.buffer.write(cp.stderr)
        print(json.dumps(dict(short_alias=str(short),canonical=str(canonical),distinct_equivalent_alias=True,exit_code=cp.returncode)),flush=True)
        return cp.returncode


if __name__=='__main__':sys.exit(main())
