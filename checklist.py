# Чек-лист оценки оператора: 8 критериев.
# Коды, допустимые значения и веса взяты из приложения к Student Guide (docs/).
#
# TODO: описания критериев ("description") написаны по названиям критериев.
# Когда будет полный Student Guide с определениями, сверить и заменить текст.

import json
import os
import re

CRITERIA = [
    {
        "id": "contact",
        "name": "Установление контакта",
        "values": [True, False],
        "weight": 10,
        "description": "Оператор поздоровался, представился (имя или компания), "
                       "обратился к клиенту вежливо в начале диалога.",
    },
    {
        "id": "discovery",
        "name": "Выявление потребности",
        "values": [True, False, None],
        "weight": 15,
        "description": "Оператор задал уточняющие вопросы и понял, что именно нужно клиенту. "
                       "N/A (null), если клиент сразу ясно и полностью изложил вопрос "
                       "и уточнять было нечего.",
    },
    {
        "id": "identification",
        "name": "Идентификация",
        "values": [True, False, None],
        "weight": 20,
        "description": "Если вопрос касается личного счёта, денег, документов или персональных данных, "
                       "оператор проверил личность клиента по правилам (ИИН, номер договора, "
                       "кодовое слово и т.п.) ДО того, как раскрыть данные. "
                       "N/A (null), если вопрос общий и идентификация не нужна.",
    },
    {
        "id": "consultation",
        "name": "Корректность и полнота",
        "values": [0, 5, 10, 15, 20, None],
        "weight": 20,
        "description": "Насколько правильно и полно оператор ответил. 20 - всё верно и полно; "
                       "15 - основное решено, упущена деталь; 10 - ответ частичный; "
                       "5 - ответ в основном неверный или бесполезный; 0 - нет ответа или "
                       "ответ вредный. N/A (null), если консультации не требовалось "
                       "(например, клиент сразу ушёл).",
    },
    {
        "id": "empathy",
        "name": "Эмпатия",
        "values": [True, False],
        "weight": 15,
        "description": "Оператор доброжелателен, проявляет понимание и внимание к ситуации клиента, "
                       "не грубит, не игнорирует эмоции клиента.",
    },
    {
        "id": "dialogue",
        "name": "Ведение, скорость, грамотность",
        "values": [0, 4, 7, 10],
        "weight": 15,
        "description": "Оператор ведёт диалог уверенно и по делу, отвечает без долгих пауз, "
                       "пишет/говорит грамотно. 10 - отлично; 7 - мелкие недочёты (повторы, "
                       "небольшие паузы, опечатки); 4 - заметные проблемы; 0 - диалог ведётся плохо.",
    },
    {
        "id": "closure",
        "name": "Завершение",
        "values": [True, False, None],
        "weight": 5,
        "description": "Оператор корректно завершил диалог: уточнил, остались ли вопросы, "
                       "поблагодарил или попрощался. N/A (null), если диалог оборвал клиент "
                       "и оператор не мог завершить.",
    },
    {
        "id": "critical",
        "name": "Грубые ошибки",
        "values": [True, False],
        "weight": 0,  # без веса: в итоговый балл не входит, идёт отдельным флагом
        "description": "true, если оператор допустил грубое нарушение: грубость или оскорбление, "
                       "раскрытие данных без идентификации, заведомо ложная информация, "
                       "обещание доходности, просьба сообщить пароль/код из SMS и т.п. "
                       "Иначе false.",
    },
]

# Список кодов по порядку, удобно для проверок
CRITERIA_IDS = [c["id"] for c in CRITERIA]

# Чек-лист из гайда (выше) - официальный: по нему считаются results.json для жюри.
# Рабочий чек-лист, который можно менять на сайте (добавить критерий, поменять вес),
# хранится в checklist.json. По нему идёт живая оценка новых диалогов и баллы на сайте.
ACTIVE_PATH = "checklist.json"


def get_criterion(criterion_id, criteria=CRITERIA):
    """Найти критерий по коду."""
    for c in criteria:
        if c["id"] == criterion_id:
            return c
    return None


def is_allowed_value(criterion_id, value, criteria=CRITERIA):
    """Проверить, что значение допустимо для критерия.
    Важно: в Python True == 1, поэтому сравниваем ещё и тип."""
    criterion = get_criterion(criterion_id, criteria)
    if criterion is None:
        return False
    for allowed in criterion["values"]:
        if value == allowed and type(value) == type(allowed):
            return True
    return False


def load_active_checklist():
    """Рабочий чек-лист из checklist.json. Если файла нет - копия официального."""
    if os.path.exists(ACTIVE_PATH):
        with open(ACTIVE_PATH, encoding="utf-8") as f:
            return json.load(f)["criteria"]
    return [dict(c, official=True) for c in CRITERIA]


def check_checklist(criteria):
    """Проверить чек-лист перед сохранением. Возвращает список ошибок."""
    errors = []
    ids = [c.get("id") for c in criteria]
    for official in CRITERIA_IDS:
        if official not in ids:
            errors.append("Нельзя удалять критерий из гайда: " + official)
    if len(ids) != len(set(ids)):
        errors.append("Коды критериев повторяются")
    for c in criteria:
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,30}", str(c.get("id", ""))):
            errors.append("Код критерия только латиницей, например politeness: %r" % c.get("id"))
        if not str(c.get("name", "")).strip():
            errors.append("У критерия %s нет названия" % c.get("id"))
        if not isinstance(c.get("weight"), int) or not 0 <= c["weight"] <= 100:
            errors.append("Вес критерия %s - целое число от 0 до 100" % c.get("id"))
        if c.get("id") != "critical" and c.get("weight") == 0:
            errors.append("Вес 0 бывает только у грубых ошибок, у %s нужен вес больше 0" % c.get("id"))
        values = c.get("values")
        if not isinstance(values, list) or len([v for v in values if v is not None]) < 2:
            errors.append("У критерия %s должно быть хотя бы 2 значения" % c.get("id"))
    return errors


def save_active_checklist(criteria):
    """Сохранить рабочий чек-лист. Возвращает список ошибок (пустой = сохранено)."""
    errors = check_checklist(criteria)
    if not errors:
        with open(ACTIVE_PATH, "w", encoding="utf-8") as f:
            json.dump({"criteria": criteria}, f, ensure_ascii=False, indent=2)
    return errors
