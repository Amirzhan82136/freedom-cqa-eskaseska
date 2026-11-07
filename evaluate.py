# Главный скрипт: загрузка диалогов -> оценка ИИ -> проверка -> расчёт балла -> сохранение.
#
# Запуск:
#   python evaluate.py --set dev     -> results/dev/results.json
#   python evaluate.py --set prod    -> results/prod/results.json
#   python evaluate.py --set demo    -> демо-данные для сайта (без ключа, из кэша)

import argparse
import json
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import anthropic
import requests
from dotenv import load_dotenv

from checklist import CRITERIA, CRITERIA_IDS, is_allowed_value
from scoring import calc_score

load_dotenv()  # читаем ключи из файла .env

TEAM_NAME = "EskaSeska"
MODEL = os.getenv("CQA_MODEL", "claude-opus-5-5")
MAX_ATTEMPTS = 3          # сколько раз спрашиваем модель, если ответ плохой
EXPECTED_COUNT = {"dev": 65, "prod": 10}

ROLE_NAMES = {"client": "Клиент", "operator": "Оператор", "bot": "Бот", "event": "Событие"}

# Защита от одновременной записи в кэш и печати из разных потоков
lock = threading.Lock()


# ---------------------------------------------------------------------------
# 1. Загрузка диалогов
# ---------------------------------------------------------------------------

def first_of(data, keys, default=None):
    """Взять первое непустое поле из списка возможных названий.
    Нужно, потому что точный формат API может отличаться."""
    if not isinstance(data, dict):
        return default
    for key in keys:
        if data.get(key) not in (None, ""):
            return data[key]
    return default


def pick_list(data):
    """API может вернуть список сразу или внутри {"items": [...]}."""
    if isinstance(data, list):
        return data
    return first_of(data, ["items", "conversations", "dialogs", "data", "results"], [data])


def fetch_from_api(api_url, set_name):
    """Скачать диалоги из API организаторов.
    TODO: сверить адреса запросов с документацией API, когда её выдадут.
    Сейчас: GET {CQA_API_URL}/conversations?set=dev, затем при необходимости
    GET {CQA_API_URL}/conversations/{id} для полного транскрипта."""
    headers = {}
    token = os.getenv("CQA_API_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token

    response = requests.get(api_url.rstrip("/") + "/conversations",
                            params={"set": set_name}, headers=headers, timeout=60)
    response.raise_for_status()
    items = pick_list(response.json())

    dialogs = []
    for item in items:
        # если в списке нет транскрипта, догружаем диалог отдельно
        if not first_of(item, ["transcript", "turns", "messages", "utterances", "segments"]):
            dialog_id = first_of(item, ["conversation_id", "id"])
            one = requests.get(api_url.rstrip("/") + "/conversations/" + str(dialog_id),
                               headers=headers, timeout=60)
            one.raise_for_status()
            item = one.json()
        dialogs.append(item)
    return dialogs


def read_local_files(folder):
    """Прочитать все .json файлы из папки data/<набор>/."""
    dialogs = []
    for name in sorted(os.listdir(folder)):
        if name.endswith(".json") and name != "results.json":
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                dialogs.extend(pick_list(json.load(f)))
    return dialogs


def to_seconds(value):
    """Перевести время в секунды: 12.5, "01:23", "00:01:23". Иначе None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and re.fullmatch(r"\d+(:\d+){1,2}(\.\d+)?", value.strip()):
        seconds = 0.0
        for part in value.strip().split(":"):
            seconds = seconds * 60 + float(part)
        return seconds
    return None


def normalize_role(role):
    """Привести роль к одной из: client, operator, bot, event."""
    role = str(role).lower()
    if any(word in role for word in ["client", "customer", "user", "клиент", "caller"]):
        return "client"
    if any(word in role for word in ["operator", "agent", "manager", "support", "оператор", "employee"]):
        return "operator"
    if any(word in role for word in ["bot", "assistant", "бот", "ivr"]):
        return "bot"
    return "event"


def detect_language(text):
    """Грубо определить язык по буквам, если API его не прислал."""
    if re.search(r"[әғқңөұүһі]", text.lower()):
        return "kk"
    if re.search(r"[а-яё]", text.lower()):
        return "ru"
    return "en"


def normalize_dialog(raw):
    """Привести диалог к простому виду, с которым работает весь проект.
    ВАЖНО: реплики нумеруем с 1 по ВСЕМУ исходному массиву (клиент, оператор,
    бот, события). Ничего не выбрасываем, даже пустые реплики."""
    turns_raw = first_of(raw, ["transcript", "turns", "messages", "utterances", "segments", "dialog"], [])
    if isinstance(turns_raw, dict):
        turns_raw = first_of(turns_raw, ["turns", "messages", "segments", "utterances"], [])

    turns = []
    for index, t in enumerate(turns_raw):
        if isinstance(t, str):
            t = {"text": t}
        time_value = first_of(t, ["start", "start_time", "startTime", "begin", "timestamp", "time", "ts"])
        turns.append({
            "n": index + 1,
            "role": normalize_role(first_of(t, ["role", "speaker", "author", "sender", "from", "participant", "type"], "")),
            "text": str(first_of(t, ["text", "content", "message", "body", "utterance"], "")),
            "start": to_seconds(time_value),
            "end": to_seconds(first_of(t, ["end", "end_time", "endTime", "stop"])),
            "time_label": time_value if isinstance(time_value, str) else None,
        })

    audio = first_of(raw, ["audio_url", "audio", "audio_file", "recording_url", "record_url", "audio_path"])
    channel_text = str(first_of(raw, ["channel", "type", "source", "kind"], "")).lower()
    if any(word in channel_text for word in ["call", "voice", "phone", "звон", "audio"]) or audio:
        channel = "call"
    else:
        channel = "chat"

    language = str(first_of(raw, ["language", "lang", "locale"], "")).lower()[:2]
    if language == "kz":
        language = "kk"
    if language not in ("ru", "kk", "en"):
        language = detect_language(" ".join(t["text"] for t in turns))

    return {
        "conversation_id": first_of(raw, ["conversation_id", "id", "dialog_id"]),
        "channel": channel,
        "language": language,
        "audio": audio,
        "turns": turns,
    }


def load_dialogs(set_name):
    """Получить диалоги: из API (если задан CQA_API_URL) или из папки data/<набор>/."""
    api_url = os.getenv("CQA_API_URL")
    folder = os.path.join("data", set_name)
    if api_url and set_name != "demo":
        print("Скачиваю диалоги из API:", api_url)
        raw = fetch_from_api(api_url, set_name)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "api_dump.json"), "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)   # копия на всякий случай
    else:
        if not os.path.isdir(folder):
            raise SystemExit("Нет данных: задайте CQA_API_URL в .env или положите .json файлы в " + folder)
        print("Читаю диалоги из папки", folder)
        raw = read_local_files(folder)

    dialogs = []
    seen = set()
    for item in raw:
        dialog = normalize_dialog(item)
        if dialog["conversation_id"] in seen:
            continue    # повтор одного и того же диалога пропускаем
        seen.add(dialog["conversation_id"])
        dialogs.append(dialog)
    return dialogs


# ---------------------------------------------------------------------------
# 2. Запрос к ИИ
# ---------------------------------------------------------------------------

def format_time(seconds):
    """123.4 -> "02:03"."""
    seconds = int(seconds)
    return "%02d:%02d" % (seconds // 60, seconds % 60)


def format_transcript(dialog):
    """Собрать текст: "[1] Клиент (00:05): ...". Номер = номер в исходном массиве."""
    lines = []
    for turn in dialog["turns"]:
        label = "[%d] %s" % (turn["n"], ROLE_NAMES[turn["role"]])
        if turn["start"] is not None:
            label += " (%s)" % format_time(turn["start"])
        lines.append(label + ": " + turn["text"])
    return "\n".join(lines)


def build_system_prompt():
    """Инструкция для ИИ: критерии, допустимые значения, правила."""
    lines = [
        "Ты - эксперт контроля качества (CQA) в брокерской компании Freedom Broker.",
        "Оцени работу ОПЕРАТОРА в диалоге с клиентом по чек-листу из 8 критериев.",
        "Оценивай только оператора. Реплики клиента, бота и системные события - это контекст, "
        "за них оператор баллы не теряет.",
        "",
        "Критерии и допустимые значения (null = N/A, критерий не применим к этому диалогу):",
    ]
    for c in CRITERIA:
        values = ", ".join(json.dumps(v) for v in c["values"])
        lines.append("- %s (%s). Значения: %s. %s" % (c["id"], c["name"], values, c["description"]))
    lines += [
        "",
        "Правила:",
        "1. N/A (null) ставь только там, где он разрешён, и только если критерий действительно "
        "не подходит к диалогу. N/A - это не ноль и не ошибка.",
        "2. evidence_turns - номера реплик-доказательств из квадратных скобок [N] в транскрипте. "
        "Нумерация уже сделана по всему диалогу, не пересчитывай её. Указывай 1-4 самых важных номера.",
        "3. rationale - 1-2 коротких предложения ПО-РУССКИ, даже если диалог на казахском или английском. "
        "Если балл снижен, напиши конкретно, чего не хватило.",
        "4. Диалоги бывают на русском, казахском и английском. Язык сам по себе не влияет на оценку.",
        "5. Будь строгим, но справедливым: одинаковые ситуации оценивай одинаково.",
    ]
    return "\n".join(lines)


def value_schema(values):
    """Схема для поля value: true/false, числа шкалы и/или null."""
    options = []
    numbers = [v for v in values if type(v) == int]
    if numbers:
        options.append({"type": "integer", "enum": numbers})
    if any(v is True for v in values):
        options.append({"type": "boolean"})
    if any(v is None for v in values):
        options.append({"type": "null"})
    if len(options) == 1:
        return options[0]
    return {"anyOf": options}


def build_answer_schema():
    """JSON-схема ответа: модель обязана вернуть ровно такие поля."""
    properties = {}
    for c in CRITERIA:
        properties[c["id"]] = {
            "type": "object",
            "properties": {
                "rationale": {"type": "string"},
                "value": value_schema(c["values"]),
                "evidence_turns": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["rationale", "value", "evidence_turns"],
            "additionalProperties": False,
        }
    return {
        "type": "object",
        "properties": properties,
        "required": CRITERIA_IDS,
        "additionalProperties": False,
    }


client = None


def ask_model(dialog):
    """Отправить диалог модели и получить ответ-словарь. Возвращает (ответ, имя модели)."""
    global client
    if client is None:
        client = anthropic.Anthropic()   # ключ берётся из ANTHROPIC_API_KEY в .env

    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=build_system_prompt(),
        messages=[{"role": "user", "content": "Транскрипт диалога:\n\n" + format_transcript(dialog)}],
        # формат ответа: строго JSON по схеме; effort - насколько глубоко модель думает
        output_config={"effort": "medium",
                       "format": {"type": "json_schema", "schema": build_answer_schema()}},
        # если модель откажется отвечать, API само попробует запасную модель
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Модель отказалась оценивать диалог.")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Ответ модели обрезан (слишком длинный).")
    text = next(block.text for block in response.content if block.type == "text")
    return json.loads(text), response.model


# ---------------------------------------------------------------------------
# 3. Проверка ответа и расчёт балла
# ---------------------------------------------------------------------------

def check_answer(answer, turns_count):
    """Проверить ответ модели простым кодом. Возвращает список проблем (пустой = всё хорошо)."""
    problems = []
    for criterion_id in CRITERIA_IDS:
        item = answer.get(criterion_id)
        if not isinstance(item, dict):
            problems.append("нет критерия " + criterion_id)
            continue
        if not is_allowed_value(criterion_id, item.get("value")):
            problems.append("недопустимое значение %s = %r" % (criterion_id, item.get("value")))
        for n in item.get("evidence_turns", []):
            if not isinstance(n, int) or n < 1 or n > turns_count:
                problems.append("реплики №%s нет в диалоге (%s)" % (n, criterion_id))
    return problems


def build_result(dialog_id, answer):
    """Собрать запись для results.json из проверенного ответа модели."""
    criteria = []
    values = {}
    for criterion_id in CRITERIA_IDS:
        item = answer[criterion_id]
        values[criterion_id] = item["value"]
        criteria.append({
            "criterion_id": criterion_id,
            "value": item["value"],
            "rationale": item["rationale"].strip(),
            "evidence_turns": sorted(set(item["evidence_turns"])),
        })
    return {
        "conversation_id": dialog_id,
        "status": "ok",
        "criteria": criteria,
        "score": calc_score(values),   # балл считает код, не ИИ
    }


def error_result(dialog_id, message):
    """Запись для диалога, который не удалось оценить. Не ноль, а status = error."""
    return {"conversation_id": dialog_id, "status": "error",
            "criteria": [], "score": None, "error": message}


def evaluate_dialog(dialog, cache, cache_path):
    """Оценить один диалог. Возвращает (запись для results.json, имя модели или None)."""
    dialog_id = dialog["conversation_id"]
    turns_count = len(dialog["turns"])
    if turns_count == 0:
        return error_result(dialog_id, "Пустой транскрипт: нечего оценивать."), None

    # уже оценивали раньше? берём из кэша, модель не спрашиваем
    cached = cache.get(str(dialog_id))
    if cached and not check_answer(cached["answer"], turns_count):
        return build_result(dialog_id, cached["answer"]), cached["model"]

    last_error = "Не удалось получить ответ модели."
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            answer, model_name = ask_model(dialog)
            problems = check_answer(answer, turns_count)
            if not problems:
                with lock:
                    cache[str(dialog_id)] = {"answer": answer, "model": model_name}
                    save_json(cache_path, cache)    # прогресс сохраняется сразу
                return build_result(dialog_id, answer), model_name
            last_error = "Ответ модели не прошёл проверку: " + "; ".join(problems)
        except Exception as error:
            last_error = "Ошибка запроса к модели: %s" % error
        with lock:
            print("  [%s] попытка %d не удалась: %s" % (dialog_id, attempt, last_error))
        time.sleep(2 * attempt)
    return error_result(dialog_id, last_error), None


# ---------------------------------------------------------------------------
# 4. Сохранение
# ---------------------------------------------------------------------------

def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def prepare_audio(dialog):
    """Положить аудио звонка в web/data/audio/, чтобы сайт работал без интернета.
    Возвращает путь для сайта или None."""
    source = dialog["audio"]
    if not source:
        return None
    extension = os.path.splitext(str(source).split("?")[0])[1] or ".mp3"
    safe_name = re.sub(r"[^A-Za-z0-9_-]", "_", str(dialog["conversation_id"])) + extension
    target = os.path.join("web", "data", "audio", safe_name)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    try:
        if not os.path.exists(target):
            if str(source).startswith("http"):
                headers = {}
                if os.getenv("CQA_API_TOKEN"):
                    headers["Authorization"] = "Bearer " + os.getenv("CQA_API_TOKEN")
                response = requests.get(source, headers=headers, timeout=120)
                response.raise_for_status()
                with open(target, "wb") as f:
                    f.write(response.content)
            else:
                shutil.copy(source, target)
        return "data/audio/" + safe_name
    except Exception as error:
        print("  Не удалось получить аудио для", dialog["conversation_id"], "-", error)
        return None


def main():
    parser = argparse.ArgumentParser(description="Оценка диалогов операторов")
    parser.add_argument("--set", required=True, choices=["dev", "prod", "demo"])
    parser.add_argument("--limit", type=int, default=0, help="оценить только первые N диалогов (для пробы)")
    parser.add_argument("--workers", type=int, default=4, help="сколько диалогов оценивать одновременно")
    args = parser.parse_args()

    started = time.time()
    dialogs = load_dialogs(args.set)
    if args.limit:
        dialogs = dialogs[:args.limit]
    print("Диалогов к оценке:", len(dialogs))

    cache_path = os.path.join("cache", args.set + ".json")
    cache = load_json(cache_path, {})

    def process(dialog):
        result, model_name = evaluate_dialog(dialog, cache, cache_path)
        with lock:
            if result["status"] == "ok":
                print("  %-20s балл %6.2f  нарушение: %s" % (
                    dialog["conversation_id"], result["score"]["final_score"],
                    "ДА" if result["score"]["critical_flag"] else "нет"))
            else:
                print("  %-20s ОШИБКА: %s" % (dialog["conversation_id"], result["error"]))
        return result, model_name

    # оцениваем несколько диалогов одновременно; если один упал, остальные продолжают
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        outputs = list(pool.map(process, dialogs))
    results = [result for result, _ in outputs]
    models_used = sorted(set(name for _, name in outputs if name))

    # results.json в формате организаторов
    report = {
        "schema_version": "1.0",
        "team_name": TEAM_NAME,
        "model": {"mode": "external_api", "name": ", ".join(models_used) or MODEL},
        "results": results,
    }
    if args.set == "demo":
        results_path = os.path.join("data", "demo", "results.json")   # демо не смешиваем с настоящими
    else:
        results_path = os.path.join("results", args.set, "results.json")
    save_json(results_path, report)

    # данные для сайта: диалоги + оценки
    web_dialogs = []
    for dialog, result in zip(dialogs, results):
        web_dialogs.append({
            "conversation_id": dialog["conversation_id"],
            "channel": dialog["channel"],
            "language": dialog["language"],
            "audio": prepare_audio(dialog) if dialog["channel"] == "call" else None,
            "turns": dialog["turns"],
            "result": result,
        })
    save_json(os.path.join("web", "data", "dialogs_%s.json" % args.set),
              {"set": args.set, "model": report["model"], "dialogs": web_dialogs})

    # итоги прогона
    ok = [r for r in results if r["status"] == "ok"]
    errors = len(results) - len(ok)
    average = sum(r["score"]["final_score"] for r in ok) / len(ok) if ok else 0
    violations = sum(1 for r in ok if r["score"]["critical_flag"])
    print()
    print("Готово за %.1f сек." % (time.time() - started))
    print("Записей: %d, оценено: %d, ошибок: %d" % (len(results), len(ok), errors))
    print("Средний балл: %.2f, диалогов с грубым нарушением: %d" % (average, violations))
    print("Файл:", results_path)
    expected = EXPECTED_COUNT.get(args.set)
    if expected and not args.limit and len(results) != expected:
        print("ВНИМАНИЕ: ожидалось %d записей, получилось %d!" % (expected, len(results)))
    print("Проверка файла: python validate.py", results_path)


if __name__ == "__main__":
    main()
