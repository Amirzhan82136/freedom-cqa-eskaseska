#!/bin/bash
# Двойной клик: оценить набор DEV или PROD, проверить файл и сделать коммит.
# Перед запуском: ключ ИИ (GEMINI_API_KEY или ANTHROPIC_API_KEY) в .env и данные в data/dev/ или data/prod/
# (или адрес API организаторов CQA_API_URL в .env).

cd "$(dirname "$0")"

echo ""
read -p "Какой набор оценить? Напишите dev или prod и нажмите Enter: " SET
if [ "$SET" != "dev" ] && [ "$SET" != "prod" ]; then
  echo "Нужно написать dev или prod."
  read -p "Нажмите Enter, чтобы закрыть окно"
  exit 1
fi
mkdir -p "data/$SET"
GROUP_ID=$(echo "$SET" | tr a-z A-Z)
read -p "Значение group_id для API (Enter = $GROUP_ID; для PROD его сообщат организаторы): " ANSWER
if [ -n "$ANSWER" ]; then GROUP_ID="$ANSWER"; fi

echo ""
echo "=== Шаг 1 из 3. Проверяю, как прочитались диалоги ==="
if ! python3 evaluate.py --set "$SET" --group-id "$GROUP_ID" --check; then
  read -p "Нажмите Enter, чтобы закрыть окно"
  exit 1
fi
echo ""
read -p "Если всё выглядит правильно, нажмите Enter (отмена: закройте окно) "

echo ""
echo "=== Шаг 2 из 3. Оцениваю диалоги с помощью ИИ ==="
if ! python3 evaluate.py --set "$SET" --group-id "$GROUP_ID"; then
  read -p "Нажмите Enter, чтобы закрыть окно"
  exit 1
fi

echo ""
echo "=== Шаг 3 из 3. Проверяю results/$SET/results.json по правилам гайда ==="
if python3 validate.py "results/$SET/results.json"; then
  git add "results/$SET/results.json" "web/data/dialogs_$SET.json" web/data/audio cache 2>/dev/null
  if git commit -q -m "Результаты $SET" >/dev/null 2>&1; then
    echo "Коммит сделан."
  else
    echo "Коммит не нужен: эти результаты уже сохранены."
  fi
  echo ""
  echo "ГОТОВО. Осталось открыть GitHub Desktop и нажать Push origin."
else
  echo ""
  echo "Файл НЕ прошёл проверку, коммит не сделан. Запустите этот файл ещё раз:"
  echo "уже оценённые диалоги возьмутся из кэша, ИИ спросят только про упавшие."
fi
read -p "Нажмите Enter, чтобы закрыть окно"
