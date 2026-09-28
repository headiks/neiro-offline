"""pytest: модули приложения лежат в backend/ — подключаем его к sys.path; тесты читают
data/... относительно корня проекта, поэтому запускать их из корня (python tests/run_tests.py)."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(ROOT, "backend"), os.path.dirname(os.path.abspath(__file__))):
    if p not in sys.path:
        sys.path.insert(0, p)
