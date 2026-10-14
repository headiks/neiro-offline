"""
Словарь названий организаций для обезличивания (pii.py) из открытых данных ФНС.

Источник — любой открытый набор ФНС с атрибутами НаимОрг / НаимОргСокр / ИННЮЛ, например
«Единый реестр субъектов МСП» (https://www.nalog.gov.ru/opendata/7707329152-rsmp/) или
«Сведения о среднесписочной численности работников». Скачайте zip-архив набора на сервер
и запустите:

    python scripts/import_org_names.py data-rsmp.zip --region 72 --region 86
    python scripts/import_org_names.py data-rsmp.zip                # вся страна (много памяти)

--region — первые две цифры ИНН (код региона): по всей стране это миллионы названий
(сотни МБ в памяти каждого процесса), по своим регионам — десятки тысяч. Результат —
data/org_dict/fns.txt (одно название в строке, без ООО и кавычек). pii.py читает все
*.txt из data/org_dict при старте; рядом можно положить свой список контрагентов.
"""
import argparse
import re
import sys
import zipfile
from pathlib import Path
from xml.etree.ElementTree import iterparse

import _path  # noqa: F401
import pii

OUT = Path("data/org_dict/fns.txt")
_FORMS = re.compile(r"^(?:общество с ограниченной ответственностью|акционерное общество|публичное акционерное "
                    r"общество|закрытое акционерное общество|открытое акционерное общество|ооо|ао|пао|зао|оао|"
                    r"нко|фгуп|муп|гуп)\s+", re.I)


def core_name(full: str) -> str:
    """«ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "РОМАШКА"» -> «ромашка» (как в тексте документов)."""
    quoted = re.findall(r"[\"«“]([^\"»”]+)[\"»”]", full or "")
    name = quoted[0] if quoted else _FORMS.sub("", (full or "").strip())
    name = pii.norm_org(name)
    return name if len(name) >= 3 and not name.replace(" ", "").isdigit() else ""


def names_from_xml(stream, regions: set):
    for _, el in iterparse(stream):
        attrs = el.attrib
        if "НаимОрг" in attrs or "НаимОргСокр" in attrs:
            if not regions or attrs.get("ИННЮЛ", "")[:2] in regions:
                for key in ("НаимОрг", "НаимОргСокр"):
                    n = core_name(attrs.get(key, ""))
                    if n:
                        yield n
        el.clear()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="zip-архив набора ФНС, папка с xml или один xml")
    ap.add_argument("--region", action="append", default=[], help="код региона (2 цифры ИНН), можно несколько")
    args = ap.parse_args(argv)
    src, regions, names = Path(args.source), set(args.region), set()
    if src.suffix.lower() == ".zip":
        with zipfile.ZipFile(src) as z:
            for member in (m for m in z.namelist() if m.lower().endswith(".xml")):
                with z.open(member) as f:
                    names.update(names_from_xml(f, regions))
    else:
        for fn in ([src] if src.is_file() else sorted(src.glob("*.xml"))):
            with open(fn, "rb") as f:
                names.update(names_from_xml(f, regions))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(sorted(names)) + "\n", encoding="utf-8")
    print(f"Записано названий: {len(names)} -> {OUT}. Перезапустите сайт и worker'ы.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
