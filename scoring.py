# Подсчёт итогового балла. Балл считает этот код, а не ИИ.
# Такая же формула повторена в web/app.js (функция calcScore).

import math

from checklist import CRITERIA


def criterion_share(criterion, value):
    """Доля выполнения критерия от 0 до 1.
    true = 1, false = 0, числа делим на максимум шкалы (consultation: /20, dialogue: /10)."""
    numbers = [v for v in criterion["values"] if type(v) in (int, float)]
    if numbers:
        return value / max(numbers)
    if value is True:
        return 1.0
    return 0.0


def calc_score(values, criteria=CRITERIA):
    """values - словарь {criterion_id: value}, criteria - чек-лист (по умолчанию из гайда).
    Возвращает {"final_score": число 0..100, "critical_flag": True/False}."""
    total = 0.0         # набранные баллы (вес * доля)
    total_weight = 0    # сумма весов применимых критериев

    for criterion in criteria:
        if criterion["weight"] == 0:
            continue    # грубые ошибки (вес 0) в сумму не входят
        value = values.get(criterion["id"])
        if value is None:
            continue    # N/A: критерий выбрасываем, его вес "растворяется"
        total += criterion["weight"] * criterion_share(criterion, value)
        total_weight += criterion["weight"]

    if total_weight == 0:
        final_score = 0.0   # все критерии N/A - на практике не бывает (contact всегда есть)
    else:
        # округляем "по-школьному" (0.5 вверх), ровно так же, как в app.js,
        # чтобы сайт и Python всегда давали одинаковый балл
        final_score = math.floor(total / total_weight * 100 * 100 + 0.5) / 100

    return {
        "final_score": final_score,
        "critical_flag": values.get("critical") is True,
    }
