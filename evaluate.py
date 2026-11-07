# Главный скрипт: загрузка диалогов -> оценка ИИ -> проверка -> расчёт балла -> сохранение.
#
# Запуск:
#   python3 evaluate.py --set dev     -> results/dev/results.json
#   python3 evaluate.py --set prod    -> results/prod/results.json
#   python3 evaluate.py --set demo       -> демо-данные для сайта (без ключа, из кэша)
#   python3 evaluate.py --set demo-prod  -> пример PROD для сайта (тоже демо)

import argparse
import csv
import json
import os
import re
import shutil
import threading
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import anthropic
from google import genai
from google.genai import types
import requests
from dotenv import load_dotenv

from checklist import CRITERIA, CRITERIA_IDS, is_allowed_value
from scoring import calc_score

load_dotenv()  # читаем ключи из файла .env

TEAM_NAME = "EskaSeska"
# Какой ИИ использовать: gemini (бесплатный ключ Google) или claude (платный ключ Anthropic).
# Выбирается в .env строкой CQA_PROVIDER=gemini или CQA_PROVIDER=claude.
PROVIDER = os.getenv("CQA_PROVIDER", "gemini").lower()
KEY_NAMES = {"gemini": "GEMINI_API_KEY", "claude": "ANTHROPIC_API_KEY"}
# У бесплатного Gemini лимит около 20 запросов в день НА КАЖДУЮ модель. Поэтому моделей несколько:
# когда у первой кончился дневной лимит, берём следующую. В results.json попадут все использованные.
DEFAULT_MODELS = {"gemini": "gemini-3.6-flash,gemini-3.7-flash,gemini-3.5-flash,gemini-3.5-flash-lite", "claude": "claude-opus-5-5"}
MODELS = [m.strip() for m in (os.getenv("CQA_MODEL") or DEFAULT_MODELS[PROVIDER]).split(",") if m.strip()]
MODEL = MODELS[0]
exhausted_models = set()   # модели, у которых на сегодня кончился лимит
WORKERS = 2 if PROVIDER == "gemini" else 4   # у бесплатного Gemini лимит запросов в минуту
MAX_ATTEMPTS = 4          # сколько раз спрашиваем модель, если ответ плохой


def has_key():
    """Есть ли в .env ключ для выбранного ИИ."""
    return bool(os.getenv(KEY_NAMES[PROVIDER]))


def no_key_message():
    return "Нет ключа модели: впишите %s в файл .env и перезапустите программу" % KEY_NAMES[PROVIDER]
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
    """Достать список диалогов. API организаторов отвечает так:
    {"isSuccess": true, "data": {"records": [...]}}. Поддерживаем и просто список."""
    if isinstance(data, list):
        return data
    inner = first_of(data, ["records", "items", "conversations", "dialogs", "data", "results"])
    if inner is None:
        return [data]          # в файле один диалог
    return pick_list(inner)


def fetch_from_api(api_url, group_id):
    """Скачать диалоги из API организаторов (GET с параметром group_id).
    CQA_API_URL - ссылка «Открыть GET для DEV» из задания. Для контрольного набора
    организаторы сообщат своё значение group_id - его подставляем вместо DEV."""
    url = re.sub(r"group_id=[^&]*", "group_id=" + group_id, api_url)
    if "group_id=" not in url:
        url += "&group_id=" + group_id
    response = requests.get(url, timeout=120)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict) and data.get("isSuccess") is False:
        raise SystemExit("API организаторов вернуло ошибку: %s" % data.get("error"))
    return pick_list(data)


def read_csv_file(path):
    """CSV, где каждая строка - одна реплика: conversation_id, role, text, ...
    Реплики одного диалога собираем вместе в том порядке, как они идут в файле."""
    dialogs = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            dialog_id = first_of(row, ["conversation_id", "dialog_id", "id"])
            if dialog_id not in dialogs:
                dialogs[dialog_id] = {"conversation_id": dialog_id, "channel": row.get("channel", ""),
                                      "language": row.get("language", ""), "turns": []}
            dialogs[dialog_id]["turns"].append(row)
    return list(dialogs.values())


def read_local_files(folder):
    """Прочитать все файлы с диалогами из папки data/<набор>/: .json, .jsonl или .csv."""
    dialogs = []
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if name == "results.json":
            continue
        if name.endswith(".json"):
            with open(path, encoding="utf-8-sig") as f:
                dialogs.extend(pick_list(json.load(f)))
        elif name.endswith(".jsonl"):
            with open(path, encoding="utf-8-sig") as f:
                dialogs.extend(json.loads(line) for line in f if line.strip())
        elif name.endswith(".csv"):
            dialogs.extend(read_csv_file(path))
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


def parse_moment(value):
    """'2026-11-03T15:49:13.19+00:00' -> дата и время. Иначе None."""
    if isinstance(value, str) and "T" in value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
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
    first_moment = None    # время первой реплики звонка (для времени вида 2026-11-03T15:49:13)
    for index, t in enumerate(turns_raw):
        if isinstance(t, str):
            t = {"text": t}
        time_value = first_of(t, ["start", "start_time", "startTime", "begin", "timestamp", "time", "ts"])
        start = to_seconds(time_value)
        moment = parse_moment(time_value)
        if moment:
            # у звонков время - дата и час; переводим в секунды от начала звонка
            if first_moment is None:
                first_moment = moment
            start = (moment - first_moment).total_seconds()
        turns.append({
            "n": index + 1,
            "role": normalize_role(first_of(t, ["role", "speaker", "author", "sender", "from", "participant", "type"], "")),
            "text": str(first_of(t, ["text", "content", "message", "body", "utterance"], "")),
            "start": start,
            "end": to_seconds(first_of(t, ["end", "end_time", "endTime", "stop"])),
            "time_label": time_value if isinstance(time_value, str) else None,
        })

    # если нет времени конца реплики, считаем, что она длится до начала следующей
    for i, turn in enumerate(turns):
        if turn["end"] is None and turn["start"] is not None:
            next_starts = [t["start"] for t in turns[i + 1:] if t["start"] is not None]
            turn["end"] = next_starts[0] if next_starts else turn["start"] + 3

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


def group_id_for(set_name, group_id=None):
    """Какой group_id спрашивать у API: явно заданный, из .env (CQA_GROUP_ID_PROD) или DEV/PROD."""
    if group_id:
        return group_id
    return os.getenv("CQA_GROUP_ID_" + set_name.upper()) or set_name.upper()


def load_dialogs(set_name, group_id=None):
    """Получить диалоги: из API (если задан CQA_API_URL) или из папки data/<набор>/."""
    api_url = os.getenv("CQA_API_URL")
    folder = os.path.join("data", set_name)
    if api_url and not set_name.startswith("demo"):
        print("Скачиваю диалоги из API организаторов (набор %s)" % set_name.upper())
        group_id = group_id_for(set_name, group_id)
        print("group_id =", group_id)
        raw = fetch_from_api(api_url, group_id)
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


def build_system_prompt(criteria=CRITERIA):
    """Инструкция для ИИ: критерии, допустимые значения, правила."""
    lines = [
        "Ты - эксперт контроля качества (CQA) в брокерской компании Freedom Broker (Казахстан).",
        "Оцени работу ОПЕРАТОРА (живого сотрудника поддержки) в диалоге с клиентом по чек-листу.",
        "",
        "Как устроены диалоги:",
        "- Чаты часто начинаются с AI-бота (AIM) и системных событий («Эскалация на оператора»). "
        "Бот - не оператор: его ответы, ошибки и приветствия оператору НЕ засчитываются и НЕ ставятся в вину.",
        "- Звонки начинаются с голосового меню (IVR) и служебных событий. Текст звонков получен "
        "автоматическим распознаванием речи: в нём бывают искажённые слова, обрывы и повторы. "
        "Не наказывай оператора за ошибки распознавания, оценивай смысл сказанного.",
        "- Операторов в диалоге может быть несколько (перевод на другого специалиста) - оценивай их работу вместе.",
        "- Оператор может подключиться не сразу. Пауза до первого ответа оператора - это скорость ответа.",
        "",
        "Критерии и допустимые значения (null = N/A, критерий не применим к этому диалогу):",
    ]
    for c in criteria:
        values = ", ".join(json.dumps(v) for v in c["values"])
        lines.append("- %s (%s). Значения: %s. %s" % (c["id"], c["name"], values, c.get("description", "")))
    lines += [
        "",
        "Правила:",
        "1. Оценивай только реплики оператора. Реплики клиента, бота и события - это контекст.",
        "2. Не придумывай факты. Если в диалоге нет информации, чтобы уверенно оценить критерий, "
        "и N/A для него разрешён - ставь null. Если N/A не разрешён - оценивай по тому, что есть, "
        "и прямо напиши в rationale, чего не хватает.",
        "3. N/A (null) - не ноль и не ошибка. Ставь его, только если критерий не подходит к ситуации "
        "(например, общий вопрос не требует идентификации; клиент сразу ясно описал вопрос).",
        "4. Грубая ошибка (critical = true) - только при явном серьёзном нарушении, видном в репликах оператора. "
        "Сомнения и мелкие недочёты - это не грубая ошибка.",
        "5. evidence_turns - номера реплик-доказательств из квадратных скобок [N]. "
        "Нумерация уже сделана по всему диалогу, не пересчитывай её. Указывай 1-4 самых важных номера, "
        "по возможности - реплики оператора. Список никогда не пустой: если грубых ошибок нет (critical = false), "
        "укажи главные реплики оператора, которые ты проверил.",
        "6. rationale - 1-2 коротких предложения ПО-РУССКИ, даже если диалог на казахском или английском. "
        "Если балл снижен, напиши конкретно, чего не хватило.",
        "7. Язык диалога (русский, казахский, английский) сам по себе не влияет на оценку.",
        "8. Будь последовательным: одинаковые ситуации оценивай одинаково.",
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


def build_answer_schema(criteria=CRITERIA):
    """JSON-схема ответа: модель обязана вернуть ровно такие поля."""
    properties = {}
    for c in criteria:
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
        "required": [c["id"] for c in criteria],
        "additionalProperties": False,
    }


claude_client = None
gemini_client = None


def ask_model(dialog, criteria=CRITERIA):
    """Отправить диалог ИИ и получить ответ-словарь. Возвращает (ответ, имя модели)."""
    if PROVIDER == "gemini":
        return ask_gemini(dialog, criteria)
    return ask_claude(dialog, criteria)


def ask_gemini(dialog, criteria):
    """Google Gemini. Ключ берётся из GEMINI_API_KEY в .env."""
    global gemini_client
    if gemini_client is None:
        gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

    for model in MODELS:
        if model in exhausted_models:
            continue
        try:
            response = gemini_client.models.generate_content(
                model=model,
                contents="Транскрипт диалога:\n\n" + format_transcript(dialog),
                config=types.GenerateContentConfig(
                    system_instruction=build_system_prompt(criteria),
                    temperature=0,                          # одинаковый ответ на одинаковый диалог
                    response_mime_type="application/json",  # ответ строго в JSON по схеме
                    response_json_schema=build_answer_schema(criteria),
                ),
            )
        except Exception as error:
            if "PerDay" in str(error) or "per day" in str(error):
                exhausted_models.add(model)    # дневной лимит этой модели кончился - пробуем следующую
                print("  Дневной лимит модели %s исчерпан, переключаюсь на следующую" % model)
                continue
            raise
        if not response.text:
            raise RuntimeError("Пустой ответ модели.")
        return json.loads(response.text), response.model_version or model
    raise RuntimeError("Дневной лимит бесплатного Gemini исчерпан на всех моделях (%s). "
                       "Запустите оценку завтра или подключите платный ключ." % ", ".join(MODELS))


def ask_claude(dialog, criteria):
    """Anthropic Claude. Ключ берётся из ANTHROPIC_API_KEY в .env."""
    global claude_client
    if claude_client is None:
        claude_client = anthropic.Anthropic()

    response = claude_client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=build_system_prompt(criteria),
        messages=[{"role": "user", "content": "Транскрипт диалога:\n\n" + format_transcript(dialog)}],
        # формат ответа: строго JSON по схеме; effort - насколько глубоко модель думает
        output_config={"effort": "medium",
                       "format": {"type": "json_schema", "schema": build_answer_schema(criteria)}},
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

def check_answer(answer, turns_count, criteria=CRITERIA):
    """Проверить ответ модели простым кодом. Возвращает список проблем (пустой = всё хорошо)."""
    problems = []
    for c in criteria:
        criterion_id = c["id"]
        item = answer.get(criterion_id)
        if not isinstance(item, dict):
            problems.append("нет критерия " + criterion_id)
            continue
        if not is_allowed_value(criterion_id, item.get("value"), criteria):
            problems.append("недопустимое значение %s = %r" % (criterion_id, item.get("value")))
        if not str(item.get("rationale", "")).strip():
            problems.append("пустое объяснение (%s)" % criterion_id)
        turns = item.get("evidence_turns")
        if not isinstance(turns, list) or not turns:
            problems.append("нет номеров реплик-доказательств (%s)" % criterion_id)
            continue
        for n in turns:
            if not isinstance(n, int) or isinstance(n, bool) or n < 1 or n > turns_count:
                problems.append("реплики №%s нет в диалоге (%s)" % (n, criterion_id))
    return problems


def fill_critical_evidence(answer, dialog):
    """Если ИИ не нашёл грубых ошибок и не указал реплики, берём первые реплики оператора:
    именно их и проверяли на нарушения (гайд просит номера реплик у каждого критерия)."""
    item = answer.get("critical")
    if isinstance(item, dict) and item.get("value") is False and not item.get("evidence_turns"):
        item["evidence_turns"] = [t["n"] for t in dialog["turns"] if t["role"] == "operator"][:4] or [1]
    return answer


def build_result(dialog_id, answer, criteria=CRITERIA):
    """Собрать запись для results.json из проверенного ответа модели."""
    items = []
    values = {}
    for c in criteria:
        item = answer[c["id"]]
        values[c["id"]] = item["value"]
        items.append({
            "criterion_id": c["id"],
            "value": item["value"],
            "rationale": item["rationale"].strip(),
            "evidence_turns": sorted(set(item["evidence_turns"])),
        })
    return {
        "conversation_id": dialog_id,
        "status": "ok",
        "criteria": items,
        "score": calc_score(values, criteria),   # балл считает код, не ИИ
    }


def error_result(dialog_id, message):
    """Запись для диалога, который не удалось оценить. Не ноль, а status = error."""
    return {"conversation_id": dialog_id, "status": "error",
            "criteria": [], "score": None, "error": message}


def evaluate_dialog(dialog, cache=None, cache_path=None, criteria=CRITERIA):
    """Оценить один диалог. Возвращает (запись для results.json, имя модели или None).
    cache - уже полученные ответы модели (для живой оценки на сайте кэш не используется)."""
    dialog_id = dialog["conversation_id"]
    turns_count = len(dialog["turns"])
    if turns_count == 0:
        return error_result(dialog_id, "Пустой транскрипт: нечего оценивать."), None

    # уже оценивали раньше? берём из кэша, модель не спрашиваем
    cached = cache.get(str(dialog_id)) if cache is not None else None
    if cached and not check_answer(cached["answer"], turns_count, criteria):
        return build_result(dialog_id, cached["answer"], criteria), cached["model"]

    last_error = "Не удалось получить ответ модели."
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            answer, model_name = ask_model(dialog, criteria)
            answer = fill_critical_evidence(answer, dialog)
            problems = check_answer(answer, turns_count, criteria)
            if not problems:
                if cache is not None:
                    with lock:
                        cache[str(dialog_id)] = {"answer": answer, "model": model_name}
                        save_json(cache_path, cache)    # прогресс сохраняется сразу
                return build_result(dialog_id, answer, criteria), model_name
            last_error = "Ответ модели не прошёл проверку: " + "; ".join(problems)
        except Exception as error:
            last_error = "Ошибка запроса к модели: %s" % error
        with lock:
            print("  [%s] попытка %d не удалась: %s" % (dialog_id, attempt, last_error[:200]))
        if "Дневной лимит" in last_error:
            break                       # ждать бесполезно: лимит обнулится только завтра
        if any(code in last_error for code in ["429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE"]):
            time.sleep(30 * attempt)    # лимит бесплатного тарифа или модель перегружена - ждём подольше
        else:
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
                if "text/html" in response.headers.get("Content-Type", ""):
                    # организаторы отдают страницу с плеером; настоящий mp3 - в <source src="...">
                    found = re.search(r'<source[^>]+src="([^"]+)"', response.text)
                    if not found:
                        raise RuntimeError("в странице нет ссылки на аудио")
                    response = requests.get(found.group(1).replace("&amp;", "&"), timeout=300)
                    response.raise_for_status()
                with open(target, "wb") as f:
                    f.write(response.content)
            else:
                shutil.copy(source, target)
        return "data/audio/" + safe_name
    except Exception as error:
        print("  Не удалось получить аудио для", dialog["conversation_id"], "-", error)
        return None


def check_input(set_name, group_id=None):
    """Проверка входных данных: показать, как скрипт понял каждый диалог.
    Запускать сразу после получения данных, чтобы до оценки увидеть проблемы с форматом."""
    dialogs = load_dialogs(set_name, group_id)
    print("Найдено диалогов:", len(dialogs))
    if not dialogs:
        raise SystemExit("Диалогов нет: положите файлы в data/%s/ или впишите CQA_API_URL в .env" % set_name)
    problems = 0
    for d in dialogs:
        roles = {}
        for t in d["turns"]:
            roles[t["role"]] = roles.get(t["role"], 0) + 1
        roles_text = ", ".join("%s: %d" % (ROLE_NAMES[r], n) for r, n in roles.items())
        print("  %-24s %-5s %s  реплик: %-3d (%s)" % (
            d["conversation_id"], d["channel"], d["language"], len(d["turns"]), roles_text))
        if not d["conversation_id"]:
            print("     ПРОБЛЕМА: нет conversation_id")
            problems += 1
        if not d["turns"]:
            print("     ПРОБЛЕМА: не нашёл реплики (транскрипт)")
            problems += 1
        elif "operator" not in roles:
            print("     ПРОБЛЕМА: не нашёл реплик оператора - проверьте, как в данных называется роль")
            problems += 1
    expected = EXPECTED_COUNT.get(set_name)
    if expected and len(dialogs) != expected:
        print("ВНИМАНИЕ: по гайду в %s должно быть %d диалогов, а найдено %d" % (set_name, expected, len(dialogs)))
        problems += 1
    if dialogs:
        print("\nПример: так ИИ увидит первый диалог:\n")
        print(format_transcript(dialogs[0])[:1500])
    print("\nПроблем найдено:", problems)


def run_set(set_name, limit=0, workers=WORKERS, on_progress=None, group_id=None):
    """Оценить весь набор: загрузка -> ИИ -> проверка -> балл -> results.json и данные для сайта.
    on_progress(готово, всего, ошибок) вызывается после каждого диалога (для полосы прогресса на сайте).
    Возвращает словарь с итогами."""
    if not set_name.startswith("demo") and not has_key():
        raise SystemExit(no_key_message())

    started = time.time()
    dialogs = load_dialogs(set_name, group_id)
    if not dialogs:
        raise SystemExit("Не найдено ни одного диалога. Проверьте папку data/%s/ или CQA_API_URL в .env" % set_name)
    if limit:
        dialogs = dialogs[:limit]
    print("Диалогов к оценке:", len(dialogs))

    cache_path = os.path.join("cache", set_name + ".json")
    cache = load_json(cache_path, {})
    progress = {"done": 0, "errors": 0}

    def process(dialog):
        # официальные наборы всегда оцениваются по чек-листу из гайда (CRITERIA)
        result, model_name = evaluate_dialog(dialog, cache, cache_path)
        with lock:
            progress["done"] += 1
            if result["status"] == "ok":
                print("  %-20s балл %6.2f  нарушение: %s" % (
                    dialog["conversation_id"], result["score"]["final_score"],
                    "ДА" if result["score"]["critical_flag"] else "нет"))
            else:
                progress["errors"] += 1
                print("  %-20s ОШИБКА: %s" % (dialog["conversation_id"], result["error"]))
            if on_progress:
                on_progress(progress["done"], len(dialogs), progress["errors"])
        return result, model_name

    # оцениваем несколько диалогов одновременно; если один упал, остальные продолжают
    with ThreadPoolExecutor(max_workers=workers) as pool:
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
    if set_name.startswith("demo"):
        results_path = os.path.join("data", set_name, "results.json")   # демо не смешиваем с настоящими
    else:
        results_path = os.path.join("results", set_name, "results.json")
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
    save_json(os.path.join("web", "data", "dialogs_%s.json" % set_name),
              {"set": set_name, "model": report["model"], "checklist": CRITERIA, "dialogs": web_dialogs})

    # итоги прогона
    ok = [r for r in results if r["status"] == "ok"]
    summary = {
        "records": len(results),
        "ok": len(ok),
        "errors": len(results) - len(ok),
        "average": round(sum(r["score"]["final_score"] for r in ok) / len(ok), 2) if ok else 0,
        "violations": sum(1 for r in ok if r["score"]["critical_flag"]),
        "seconds": round(time.time() - started, 1),
        "path": results_path,
    }
    print()
    print("Готово за %.1f сек." % summary["seconds"])
    print("Записей: %d, оценено: %d, ошибок: %d" % (summary["records"], summary["ok"], summary["errors"]))
    print("Средний балл: %.2f, диалогов с грубым нарушением: %d" % (summary["average"], summary["violations"]))
    print("Файл:", results_path)
    expected = EXPECTED_COUNT.get(set_name)
    if expected and not limit and len(results) != expected:
        print("ВНИМАНИЕ: ожидалось %d записей, получилось %d!" % (expected, len(results)))
    print("Проверка файла: python3 validate.py", results_path)
    return summary


def main():
    parser = argparse.ArgumentParser(description="Оценка диалогов операторов")
    parser.add_argument("--set", required=True, choices=["dev", "prod", "demo", "demo-prod"])
    parser.add_argument("--limit", type=int, default=0, help="оценить только первые N диалогов (для пробы)")
    parser.add_argument("--workers", type=int, default=WORKERS, help="сколько диалогов оценивать одновременно")
    parser.add_argument("--check", action="store_true",
                        help="только прочитать диалоги и показать, как они поняты (без ИИ и без ключа)")
    parser.add_argument("--group-id", help="group_id для API организаторов (для PROD его сообщат организаторы)")
    args = parser.parse_args()

    if args.check:
        check_input(args.set, args.group_id)
    else:
        run_set(args.set, args.limit, args.workers, group_id=args.group_id)


if __name__ == "__main__":
    main()
