"""Pytest configuration — add db-testing-tool root to sys.path so
'from app.xxx' imports work whether tests are run from the repo root
(python -m pytest db-testing-tool/tests/...) or from db-testing-tool/.
"""
import sys
from pathlib import Path

# Ensure the db-testing-tool package root is on sys.path
_root = Path(__file__).resolve().parent.parent  # …/db-testing-tool/
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
