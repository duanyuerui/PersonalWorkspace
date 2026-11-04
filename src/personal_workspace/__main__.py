"""python -m personal_workspace → core CLI（today | list | backup | stats）。"""
import sys

from . import core

if __name__ == "__main__":
    sys.exit(core.main())
