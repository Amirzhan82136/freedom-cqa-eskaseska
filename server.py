# Сервер сайта: показывает страницы и выполняет действия по кнопкам на сайте.
# Только стандартная библиотека Python, без фреймворков.
#
# Запуск:  python3 server.py   (откроется браузер с сайтом)
#
# Что умеет (адреса /api/...):
#   GET  /api/status     - есть ли ключ модели, идёт ли сейчас оценка набора
#   GET  /api/checklist  - рабочий чек-лист и чек-лист из гайда
#   POST /api/checklist  - сохранить чек-лист (добавить критерий, поменять вес)
#   POST /api/run        - скачать набор DEV/PROD из API организаторов и оценить его
#   POST /api/evaluate   - оценить один диалог прямо сейчас (вставленный текст или диалог из набора)

import json
import os
import re
import socket
import threading
import time
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import evaluate
from checklist import CRITERIA, load_active_checklist, save_active_checklist

# состояние фоновой оценки набора (показывается на сайте полосой прогресса)
job = {"running": False, "set": None, "done": 0, "total": 0, "errors": 0, "message": "", "summary": None}


def run_job(set_name):
    """Оценка целого набора в фоне, чтобы сайт не зависал."""
    def on_progress(done, total, errors):
        job.update(done=done, total=total, errors=errors, message="Оцениваю диалоги...")

    try:
        job["message"] = "Скачиваю диалоги..."
        job["summary"] = evaluate.run_set(set_name, on_progress=on_progress)
        job["message"] = "Готово"
    except SystemExit as error:          # понятные ошибки: нет ключа, нет данных
        job["message"] = str(error)
    except Exception as error:
        job["message"] = "Ошибка: %s" % error
    job["running"] = False


def parse_pasted(text):
    """Текст, вставленный на сайте, -> список реплик.
    Каждая строка вида «Оператор: Здравствуйте!». Строка без роли - продолжение предыдущей."""
    turns = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        found = re.match(r"^(?:\[\d+\]\s*)?([^:\[\]]{1,30}):\s*(.*)$", line)
        if found:
            turns.append({"role": found.group(1), "text": found.group(2)})
        elif turns:
            turns[-1]["text"] += " " + line
        else:
            turns.append({"role": "client", "text": line})
    return turns


def evaluate_live(body):
    """Оценить один диалог по рабочему чек-листу и добавить его в набор «Живая оценка»."""
    if not evaluate.has_key():
        raise ValueError(evaluate.no_key_message())
    criteria = load_active_checklist()

    if body.get("conversation_id"):
        # переоценка диалога из набора (например, после изменения чек-листа)
        source = evaluate.load_json(os.path.join("web", "data", "dialogs_%s.json" % body.get("set")), None)
        found = [d for d in (source or {}).get("dialogs", []) if str(d["conversation_id"]) == str(body["conversation_id"])]
        if not found:
            raise ValueError("Диалог не найден")
        dialog = dict(found[0])
    else:
        # новый диалог, вставленный текстом
        raw = {"conversation_id": "live-" + time.strftime("%m%d-%H%M%S"),
               "channel": body.get("channel", "chat"), "language": body.get("language", ""),
               "transcript": parse_pasted(body.get("text", ""))}
        dialog = evaluate.normalize_dialog(raw)
        if not dialog["turns"]:
            raise ValueError("Вставьте текст диалога: каждая реплика с новой строки, например «Оператор: Здравствуйте»")

    result, model_name = evaluate.evaluate_dialog(dialog, criteria=criteria)

    live_path = os.path.join("web", "data", "dialogs_live.json")
    live = evaluate.load_json(live_path, {"set": "live", "dialogs": []})
    live["model"] = {"mode": "external_api", "name": model_name or evaluate.MODEL}
    live["checklist"] = criteria
    live["dialogs"] = [d for d in live["dialogs"] if str(d["conversation_id"]) != str(dialog["conversation_id"])]
    live["dialogs"].insert(0, {
        "conversation_id": dialog["conversation_id"],
        "channel": dialog["channel"],
        "language": dialog["language"],
        "audio": dialog.get("audio") if str(dialog.get("audio", "")).startswith("data/") else None,
        "turns": dialog["turns"],
        "result": result,
    })
    evaluate.save_json(live_path, live)
    return {"conversation_id": dialog["conversation_id"], "status": result["status"], "error": result.get("error")}


def is_secret(file_path):
    """Файлы, которые сервер никому не отдаёт: .env с ключами и кэш ответов модели."""
    parts = os.path.normpath(file_path).split(os.sep)
    return ".env" in parts or "cache" in parts or ".git" in parts


class Handler(SimpleHTTPRequestHandler):
    """Отдаёт файлы проекта (сайт лежит в web/) и отвечает на запросы /api/..."""

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")   # чтобы браузер всегда брал свежие данные
        super().end_headers()

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self.send_response(302)
            self.send_header("Location", "/web/")
            self.end_headers()
        elif path == "/api/status":
            self.send_json({"server": True, "has_key": evaluate.has_key(),
                            "has_api": bool(os.getenv("CQA_API_URL")), "model": evaluate.MODEL, "job": job})
        elif path == "/api/checklist":
            self.send_json({"criteria": load_active_checklist(), "official": CRITERIA})
        elif is_secret(self.translate_path(self.path)):
            self.send_error(404)                       # ключи и кэш наружу не отдаём
        else:
            super().do_GET()

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self.send_json({"error": "Неверный JSON"}, 400)

        if path == "/api/checklist":
            errors = save_active_checklist(body.get("criteria", []))
            self.send_json({"ok": not errors, "errors": errors}, 200 if not errors else 400)
        elif path == "/api/run":
            set_name = body.get("set")
            if set_name not in ("dev", "prod"):
                return self.send_json({"error": "Набор должен быть dev или prod"}, 400)
            if job["running"]:
                return self.send_json({"error": "Уже идёт оценка набора " + str(job["set"])}, 409)
            job.update(running=True, set=set_name, done=0, total=0, errors=0, message="Запускаю...", summary=None)
            threading.Thread(target=run_job, args=(set_name,), daemon=True).start()
            self.send_json({"ok": True})
        elif path == "/api/evaluate":
            try:
                self.send_json(evaluate_live(body))
            except Exception as error:
                self.send_json({"error": str(error)}, 400)
        else:
            self.send_error(404)

    def log_message(self, *args):
        pass    # не засоряем консоль строчками про каждый файл


def free_port(start=8000):
    """Первый свободный порт, начиная с 8000."""
    port = start
    while True:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:   # порт никто не занял
                return port
        port += 1


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    port = free_port()
    url = "http://localhost:%d/web/" % port
    print("Сайт работает:", url)
    print("ИИ:", evaluate.PROVIDER, evaluate.MODEL, "- ключ", "есть" if evaluate.has_key() else "НЕТ (живая оценка не заработает)")
    print("Чтобы остановить, нажмите Ctrl + C или закройте окно.")
    threading.Timer(1, lambda: webbrowser.open(url)).start()
    # 127.0.0.1 - сайт доступен только с этого компьютера
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
