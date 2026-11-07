# Проверка results.json перед отправкой.
# Запуск: python validate.py results/dev/results.json
# Если есть ошибки, скрипт завершается с кодом 1.

import json
import os
import sys

from checklist import CRITERIA_IDS, is_allowed_value
from scoring import calc_score

EXPECTED_COUNT = {"dev": 65, "prod": 10}


def load_turn_counts(set_name):
    """Сколько реплик в каждом диалоге (из файла для сайта), чтобы проверить evidence_turns."""
    path = os.path.join("web", "data", "dialogs_%s.json" % set_name)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {str(d["conversation_id"]): len(d["turns"]) for d in data["dialogs"]}


def validate(path):
    errors = []
    warnings = []

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as error:
        return ["Файл не читается как JSON: %s" % error], warnings

    # какой это набор: dev или prod (по имени папки)
    set_name = os.path.basename(os.path.dirname(os.path.abspath(path)))

    # шапка файла
    if data.get("schema_version") != "1.0":
        errors.append('schema_version должен быть "1.0"')
    if not data.get("team_name"):
        errors.append("не заполнен team_name")
    model = data.get("model") or {}
    if model.get("mode") not in ("local", "external_api", "hybrid"):
        errors.append("model.mode должен быть local, external_api или hybrid")
    if not model.get("name"):
        errors.append("не заполнен model.name")

    results = data.get("results")
    if not isinstance(results, list):
        return errors + ["нет списка results"], warnings

    print("Записей в файле:", len(results))
    expected = EXPECTED_COUNT.get(set_name)
    if expected and len(results) != expected:
        errors.append("в %s должно быть ровно %d записей, а их %d" % (set_name, expected, len(results)))

    turn_counts = load_turn_counts(set_name)
    seen_ids = set()
    ok_count = 0

    for record in results:
        dialog_id = record.get("conversation_id")
        where = "[%s]" % dialog_id
        if dialog_id in (None, ""):
            errors.append("запись без conversation_id")
        if dialog_id in seen_ids:
            errors.append("%s повторяется" % where)
        seen_ids.add(dialog_id)

        status = record.get("status")
        if status == "error":
            if record.get("criteria") != [] or record.get("score") is not None or not record.get("error"):
                errors.append('%s у ошибки должны быть criteria = [], score = null и текст error' % where)
            warnings.append("%s не оценён: %s" % (where, record.get("error")))
            continue
        if status != "ok":
            errors.append('%s status должен быть "ok" или "error"' % where)
            continue
        ok_count += 1

        criteria = record.get("criteria") or []
        ids = [c.get("criterion_id") for c in criteria]
        if sorted(ids) != sorted(CRITERIA_IDS):
            errors.append("%s нужно ровно 8 критериев по одному разу, а есть: %s" % (where, ids))
            continue

        values = {}
        for c in criteria:
            cid = c["criterion_id"]
            values[cid] = c.get("value")
            if not is_allowed_value(cid, c.get("value")):
                errors.append("%s %s: недопустимое значение %r" % (where, cid, c.get("value")))
            if not isinstance(c.get("rationale"), str) or not c["rationale"].strip():
                errors.append("%s %s: пустой rationale" % (where, cid))
            turns = c.get("evidence_turns")
            if not isinstance(turns, list):
                errors.append("%s %s: evidence_turns должен быть списком" % (where, cid))
                continue
            max_turn = turn_counts.get(str(dialog_id))
            for n in turns:
                if not isinstance(n, int) or isinstance(n, bool) or n < 1:
                    errors.append("%s %s: плохой номер реплики %r" % (where, cid, n))
                elif max_turn and n > max_turn:
                    errors.append("%s %s: реплики №%d нет (в диалоге %d)" % (where, cid, n, max_turn))

        score = record.get("score") or {}
        expected_score = calc_score(values)
        if score.get("final_score") != expected_score["final_score"]:
            errors.append("%s final_score %r, а по формуле %r" % (
                where, score.get("final_score"), expected_score["final_score"]))
        if score.get("critical_flag") is not values.get("critical"):
            errors.append("%s critical_flag должен совпадать с критерием critical" % where)

    print("Оценено (ok):", ok_count, " ошибок оценки (error):", len(results) - ok_count)
    if not turn_counts:
        warnings.append("не нашёл web/data/dialogs_%s.json, номера реплик проверены только на > 0" % set_name)
    return errors, warnings


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Использование: python validate.py results/dev/results.json")
        sys.exit(1)
    errors, warnings = validate(sys.argv[1])
    for w in warnings:
        print("Предупреждение:", w)
    for e in errors:
        print("ОШИБКА:", e)
    if errors:
        print("\nФайл НЕ готов к отправке. Ошибок:", len(errors))
        sys.exit(1)
    print("\nФайл в порядке, можно отправлять.")
