"""Pytest 設定：把專案根目錄加到 sys.path，讓 tests 能 import ebadge_cli."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
