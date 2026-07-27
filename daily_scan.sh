#!/bin/bash
# Раз в день (через cron) прогоняет сканер и шлёт системное уведомление с
# топ-результатом. cron запускает скрипты без графической сессии пользователя,
# поэтому DISPLAY/XDG_RUNTIME_DIR/DBUS_SESSION_BUS_ADDRESS нужно явно
# прописывать здесь — без этого notify-send молча ничего не покажет.
export DISPLAY=:0.0
export XDG_RUNTIME_DIR=/run/user/1000
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus

cd "$(dirname "$0")" || exit 1

FULL_OUTPUT=$(python3 scanner.py --preset mine --top 5 --invest 1000 2>&1)
{
    echo "=== $(date '+%Y-%m-%d %H:%M') ==="
    echo "$FULL_OUTPUT"
    echo
} >> daily_scan.log

# Короткая строка отдельным вызовом (--no-save — иначе снимок в history.db
# записался бы дважды за один прогон) — надёжнее, чем выковыривать текст из
# таблицы выше через grep/awk.
SUMMARY=$(python3 scanner.py --preset mine --notify-summary --no-save 2>&1 | tail -1)

notify-send --urgency=normal -i terminal "DeFi LP-сканер: mine-scan" "$SUMMARY

Полная таблица: daily_scan.log"
