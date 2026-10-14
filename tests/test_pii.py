"""Обезличивание ПДн перед отправкой в DeepSeek и обратная подстановка в ответе."""
import pii


def test_masks_typical_document_pii_and_restores():
    text = ("Ответственный — Иванов Иван Иванович, тел. +7 (912) 345-67-89, ivanov@corp.ru. "
            "Согласовано: Петрова А.С. Реквизиты ООО «Ромашка»: ИНН 7701234567, КПП 770101001, "
            "р/с 40702810900000012345. СНИЛС 123-456-789 01. Паспорт серия 45 12 № 123456.")
    m = pii.Masker()
    masked = m.mask(text)
    for secret in ("Иванов", "912", "ivanov@", "Петрова", "Ромашка", "7701234567",
                   "770101001", "40702810900000012345", "123-456-789", "123456."):
        assert secret not in masked, (secret, masked)
    assert "[ФИО_1]" in masked and "[ТЕЛ_1]" in masked and "[ОРГ_1]" in masked
    assert m.unmask(masked) == text


def test_same_value_same_token_and_model_answer_restored():
    m = pii.Masker()
    masked = m.mask("Звонить Сидоров Пётр Петрович. Сидоров Пётр Петрович на месте с 9:00.")
    assert masked.count("[ФИО_1]") == 2
    assert m.unmask("Обратитесь к [ФИО_1].") == "Обратитесь к Сидоров Пётр Петрович."


def test_ordinary_text_untouched():
    text = ("Приложение А. Правила внутреннего трудового распорядка. Пожарная безопасность: "
            "раздел 3.2, приказ № 15 от 01.02.2026, смена 8 часов, Главный инженер.")
    assert pii.Masker().mask(text) == text


def test_names_in_oblique_cases():
    """В приказах ФИО чаще в косвенных падежах — словарь хранит именительный."""
    for text, secret in (("Назначить наставником Петрову Анну Сергеевну.", "Петров"),
                         ("Поручить Иванову Ивану Ивановичу провести инструктаж.", "Иванов"),
                         ("Согласовать с Сидоровым Петром Николаевичем.", "Сидоров"),
                         ("Выдать Троицкому Андрею Викторовичу спецодежду.", "Троицк"),
                         ("Письмо направлено Марии Ивановой.", "Иванов")):
        masked = pii.Masker().mask(text)
        assert secret not in masked and "[ФИО_1]" in masked, (text, masked)


def test_more_pii_kinds():
    cases = {
        "Тел. 8 (3452) 56-78-90": "56-78-90",
        "Городской (3452) 56-78-90, доб. 123": "56-78-90",
        "Паспорт серия 4512 номер 123456": "123456",
        "Дата рождения: 15.03.1990": "1990",
        "Проживает: г. Тюмень, ул. Ленина, д. 5, кв. 12": "Ленина",
        "Карта 2200 1234 5678 9010": "9010",
        "Табельный номер 004512": "004512",
        "Реквизиты ООО Ромашка": "Ромашка",
    }
    for text, secret in cases.items():
        assert secret not in pii.Masker().mask(text), (text, pii.Masker().mask(text))


def test_no_false_positives_on_regulation_text():
    for text in ("Главному Инженеру предоставить отчёт.",
                 "Правила Внутреннего Трудового Распорядка утверждены.",
                 "Совет Директоров Компании утвердил Положение о Премировании.",
                 "Средства Индивидуальной Защиты выдаются на Складе.",
                 "Оклад 45 000 рублей, премия 25%, надбавка 12%. Приказ № 15 от 01.02.2026.",
                 "Контроль за исполнением возложить на Главного Механика.",
                 "Версия 1.2.3.4 от 12.03.2025 г."):
        assert pii.Masker().mask(text) == text, text


def test_unmask_json_safe_and_tolerant():
    import json
    m = pii.Masker()
    m.mask('Работодатель ООО "Ромашка", ответственный Иванов Иван Иванович')
    out = m.unmask('{"body": "[ОРГ_1], [ФИО 1]"}', json_safe=True)
    assert json.loads(out)["body"] == 'ООО "Ромашка", Иванов Иван Иванович'
    # модель исказила метку — всё равно возвращаем реальное значение
    assert m.unmask("Обратитесь к ФИО_1.") == "Обратитесь к Иванов Иван Иванович."
    # чужая метка (её не было в запросе) остаётся как есть
    assert m.unmask("[ТЕЛ_7]") == "[ТЕЛ_7]"


def test_requisites_by_checksum_and_inn_kpp_pair():
    """ИНН/ОГРН без ключевого слова ловим по контрольной сумме; пара ИНН/КПП — целиком."""
    m = pii.Masker()
    masked = m.mask("Контрагент 7707083893/773601001, ОГРН 1027700132195, в договоре указан 7707083893.")
    for secret in ("7707083893", "773601001", "1027700132195"):
        assert secret not in masked, masked
    assert m.unmask(masked).startswith("Контрагент 7707083893/773601001")
    assert not pii._inn_ok("1234567890")
    assert pii.Masker().mask("Номер заказа 1234567890 от 01.02.2026.") == "Номер заказа 1234567890 от 01.02.2026."


def test_org_names_from_fns_dictionary(monkeypatch):
    monkeypatch.setattr(pii, "_ORG_SET", {"ромашка", "альфа строй"})
    for text, secret in (("Договор с «Ромашка» продлён.", "Ромашка"),
                         ("Материалы везёт поставщик Альфа Строй.", "Альфа"),
                         ("Альфа Строй выиграла тендер.", "Альфа")):
        masked = pii.Masker().mask(text)
        assert secret not in masked and "[ОРГ_1]" in masked, (text, masked)
    assert pii.Masker().mask("Ромашка растёт у проходной.") == "Ромашка растёт у проходной."


def test_custom_trade_secret_terms(monkeypatch, tmp_path):
    f = tmp_path / "pii_terms.txt"
    f.write_text("# коммерческая тайна\nПроект Сокол\nТюменьНефтеМаш\n", encoding="utf-8")
    monkeypatch.setattr(pii, "TERMS_PATH", f)
    monkeypatch.setattr(pii, "_TERMS", (None, None))
    m = pii.Masker()
    text = "Работы по теме «проект сокол» ведёт тюменьнефтемаш."      # регистр не важен, форма — дословно
    masked = m.mask(text)
    assert "сокол" not in masked.lower() and "нефтемаш" not in masked.lower() and "[КТ_2]" in masked, masked
    assert m.unmask(masked) == text


def test_fns_import_extracts_core_names_by_region():
    import io
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import import_org_names as imp
    xml = ('<?xml version="1.0" encoding="utf-8"?><Файл>'
           '<Документ><ОргВклМСП НаимОрг="ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ &quot;РОМАШКА&quot;" '
           'НаимОргСокр="ООО &quot;РОМАШКА&quot;" ИННЮЛ="7203000000"/></Документ>'
           '<Документ><ОргВклМСП НаимОрг="АКЦИОНЕРНОЕ ОБЩЕСТВО &quot;АЛЬФА СТРОЙ&quot;" ИННЮЛ="7701000000"/></Документ>'
           '</Файл>').encode("utf-8")
    assert set(imp.names_from_xml(io.BytesIO(xml), {"72"})) == {"ромашка"}
    assert set(imp.names_from_xml(io.BytesIO(xml), set())) == {"ромашка", "альфа строй"}
    assert imp.core_name("ООО СТРОЙМОНТАЖ") == "строймонтаж"
