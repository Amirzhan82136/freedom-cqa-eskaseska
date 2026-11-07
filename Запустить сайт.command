#!/bin/bash
# Двойной клик по этому файлу запускает сайт и открывает его в браузере.
# Чтобы остановить сайт, закройте окно Терминала.

cd "$(dirname "$0")"

# если данных для сайта ещё нет, создаём демо-набор
if [ ! -f web/data/dialogs_demo.json ]; then
  python3 evaluate.py --set demo
  python3 evaluate.py --set demo-prod
fi

# если порт 8000 уже занят, берём следующий свободный
PORT=8000
while lsof -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; do
  PORT=$((PORT + 1))
done

echo ""
echo "Сайт работает: http://localhost:$PORT/web/"
echo "Чтобы остановить, закройте это окно."
echo ""

# через секунду открываем браузер (сервер к этому времени уже запустится)
(sleep 1 && open "http://localhost:$PORT/web/") &

python3 -m http.server $PORT
