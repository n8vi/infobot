#!/usr/bin/env python3
"""Run both test suites from any working directory: python run_tests.py."""
from pathlib import Path
import subprocess
import sys


def main():
    root=Path(__file__).resolve().parent
    failed=False
    for directory in (root,root/'infobot-goes'):
        result=subprocess.run([sys.executable,'-B','-m','unittest','discover','-v'],cwd=directory)
        failed=failed or result.returncode != 0
    return int(failed)


if __name__=='__main__':raise SystemExit(main())
