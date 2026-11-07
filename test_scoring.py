# Простые проверки формулы и нумерации реплик.
# Запуск: python3 test_scoring.py

from scoring import calc_score
from evaluate import normalize_dialog, format_transcript, check_answer


def test_example_from_guide():
    # пример из Student Guide: (10 + 15 + 15 + 10.5 + 5) / 65 * 100 = 85.38
    values = {"contact": True, "discovery": None, "identification": None,
              "consultation": 15, "empathy": True, "dialogue": 7,
              "closure": True, "critical": False}
    score = calc_score(values)
    assert score["final_score"] == 85.38, score
    assert score["critical_flag"] is False


def test_all_true():
    values = {"contact": True, "discovery": True, "identification": True,
              "consultation": 20, "empathy": True, "dialogue": 10,
              "closure": True, "critical": False}
    assert calc_score(values)["final_score"] == 100.0


def test_all_false():
    values = {"contact": False, "discovery": False, "identification": False,
              "consultation": 0, "empathy": False, "dialogue": 0,
              "closure": False, "critical": True}
    score = calc_score(values)
    assert score["final_score"] == 0.0
    assert score["critical_flag"] is True


def test_many_na():
    # применимы только contact (10, выполнен), empathy (15, нет), dialogue (15, 4 из 10)
    # (10 + 0 + 6) / 40 * 100 = 40.0
    values = {"contact": True, "discovery": None, "identification": None,
              "consultation": None, "empathy": False, "dialogue": 4,
              "closure": None, "critical": False}
    assert calc_score(values)["final_score"] == 40.0


def test_critical_does_not_zero_score():
    # грубая ошибка не обнуляет балл, а только поднимает флаг
    values = {"contact": True, "discovery": True, "identification": True,
              "consultation": 20, "empathy": True, "dialogue": 10,
              "closure": True, "critical": True}
    score = calc_score(values)
    assert score["final_score"] == 100.0
    assert score["critical_flag"] is True


def test_turn_numbering_counts_all_turns():
    # нумерация идёт по ВСЕМУ массиву: клиент, бот, событие, оператор
    raw = {"conversation_id": "t-1", "messages": [
        {"role": "client", "text": "Здравствуйте"},
        {"role": "bot", "text": "Соединяю с оператором"},
        {"role": "system", "text": "Оператор подключился"},
        {"role": "client", "text": "Алло?"},
        {"role": "operator", "text": "Добрый день, меня зовут Айгерим"},
    ]}
    dialog = normalize_dialog(raw)
    numbers = [t["n"] for t in dialog["turns"]]
    assert numbers == [1, 2, 3, 4, 5], numbers
    # первая реплика оператора имеет номер 5, а НЕ 1
    operator_turns = [t for t in dialog["turns"] if t["role"] == "operator"]
    assert operator_turns[0]["n"] == 5
    assert "[5] Оператор: Добрый день" in format_transcript(dialog)
    assert dialog["turns"][2]["role"] == "event"


def test_check_answer_finds_bad_turn():
    # ссылка на реплику 9 в диалоге из 5 реплик - ошибка
    good = {"value": True, "rationale": "ок", "evidence_turns": [1]}
    answer = {cid: dict(good) for cid in
              ["contact", "discovery", "identification", "empathy", "closure", "critical"]}
    answer["consultation"] = {"value": 15, "rationale": "ок", "evidence_turns": [2]}
    answer["dialogue"] = {"value": 7, "rationale": "ок", "evidence_turns": [9]}
    problems = check_answer(answer, 5)
    assert len(problems) == 1 and "9" in problems[0], problems
    # недопустимое значение: dialogue = 5 (можно только 0, 4, 7, 10)
    answer["dialogue"] = {"value": 5, "rationale": "ок", "evidence_turns": [2]}
    assert check_answer(answer, 5)


if __name__ == "__main__":
    tests = [value for name, value in list(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print("OK  ", test.__name__)
    print("Все тесты прошли:", len(tests))
