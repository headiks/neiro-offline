"""
Прогон всех тестов проекта: каждый test_*.py — в отдельном процессе.

Почему не один `pytest`: юнит-тесты подменяют тяжёлые модули заглушками в sys.modules
(test_stubs.py), и в общем процессе заглушка из одного файла ломает соседний. Отдельный
процесс на файл — те же условия, что при запуске `python test_x.py` руками.

Тесты с PostgreSQL берут NEIROMASTER_TEST_DSN (по умолчанию локальная neiromaster_test);
нет БД — такие тесты пропускаются, а не падают.

Запуск:  python tests/run_tests.py            # все (из корня проекта)
         python tests/run_tests.py pii api    # только файлы, в имени которых есть эти слова
Код выхода 0 — всё зелёное (пропуски допустимы), 1 — есть падения.
"""
import os
import re
import subprocess
import tempfile
import sys
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent                         # тесты читают data/... от корня проекта
# Файлы-сценарии без pytest-функций: гоняем как скрипты (python test_x.py).
SCRIPTS = {"test_accounts.py", "test_duplicate_plan.py", "test_scheduling_logging.py"}


def main(filters):
    files = sorted(p for p in TESTS.glob("test_*.py") if p.name != "test_stubs.py")
    if filters:
        files = [p for p in files if any(f in p.name for f in filters)]
    failed, total_passed, total_skipped = [], 0, 0
    # Ключ ПДн тестов — во временной папке, не в data/secrets рабочей копии.
    env = {"NEIROMASTER_PII_KEY_FILE": os.path.join(tempfile.mkdtemp(), "pii.key"), **os.environ,
           "PYTHONPATH": os.pathsep.join([str(ROOT / "backend"), str(TESTS)])}   # модули — в backend/
    t0 = time.time()
    for path in files:
        cmd = ([sys.executable, str(path)] if path.name in SCRIPTS
               else [sys.executable, "-m", "pytest", "-q", "-p", "no:warnings", "-p", "no:cacheprovider", str(path)])
        proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=900)
        out = (proc.stdout + proc.stderr).strip()
        last = out.splitlines()[-1] if out else ""
        if proc.returncode not in (0, 5):          # 5 — pytest не нашёл тестов
            failed.append(path.name)
            print(f"FAIL  {path.name}\n{out[-3000:]}\n")
            continue
        passed = sum(int(n) for n in re.findall(r"(\d+) passed", last))
        skipped = sum(int(n) for n in re.findall(r"(\d+) skipped", last))
        total_passed += passed
        total_skipped += skipped + (1 if "SKIP" in out else 0)
        print(f"ok    {path.name:34} {last}")
    print(f"\nфайлов: {len(files)}, тестов пройдено: {total_passed}, пропущено: {total_skipped}, "
          f"упало файлов: {len(failed)} · {time.time() - t0:.1f} с")
    if failed:
        print("Упали:", ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
