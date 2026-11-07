#!/bin/bash
# Двойной клик по этому файлу запускает сайт и открывает его в браузере.
# Чтобы остановить сайт, закройте окно Терминала.

cd "$(dirname "$0")"

# если данных для сайта ещё нет, создаём демо-набор
if [ ! -f web/data/dialogs_demo.json ]; then
  python3 evaluate.py --set demo
  python3 evaluate.py --set demo-prod
fi

# сервер сайта: показывает страницы и делает живую оценку ИИ (сам откроет браузер)
python3 server.py
