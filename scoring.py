# Подсчёт итогового балла. Балл считает этот код, а не ИИ.
# Такая же формула повторена в web/app.js (функция calcScore).

import math

from checklist import get_criterion

# Максимум шкалы для числовых критериев
SCALE_MAX = {"consultation": 20, "dialogue": 10}


def criterion_share(criterion_id, value):
    """Доля выполнения критерия от 0 до 1.
    true = 1, false = 0, числа делим на максимум шкалы."""
    if criterion_id in SCALE_MAX:
        return value / SCALE_MAX[criterion_id]
    if value is True:
        return 1.0
    return 0.0


def calc_score(values):
    """values - словарь {criterion_id: value}.
    Возвращает {"final_score": число 0..100, "critical_flag": True/False}."""
    total = 0.0         # набранные баллы (вес * доля)
    total_weight = 0    # сумма весов применимых критериев

    for criterion_id, value in values.items():
        if criterion_id == "critical":
            continue    # грубые ошибки в сумму не входят
        if value is None:
            continue    # N/A: критерий выбрасываем, его вес "растворяется"
        weight = get_criterion(criterion_id)["weight"]
        total += weight * criterion_share(criterion_id, value)
        total_weight += weight

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
