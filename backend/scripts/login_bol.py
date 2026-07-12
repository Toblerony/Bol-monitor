"""Deprecated — use login-bol.bat or scripts/bol_login_sync.py"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
raise SystemExit(
    subprocess.call([sys.executable, str(ROOT / "scripts" / "bol_login_sync.py")])
)
