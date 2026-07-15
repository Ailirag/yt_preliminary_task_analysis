@echo off
chcp 65001 >nul
rem === Запуск ИИ-анализатора по расписанию (Планировщик Windows) ===
rem Регистрация (пример, 2 раза в день):
rem   schtasks /Create /TN "TrackerAI-bugs" /SC DAILY /ST 07:30 /TR "\"D:\Claude\yandex tracker task_bug analyzer\scripts\run_scheduled.cmd\""
rem Запускать от пользователя, у которого заданы переменные окружения (YATRACKER_*, ZAI_API_KEY, ...).

cd /d "%~dp0.."

if exist "work\.lock" (
    echo [%date% %time%] Прогон уже идёт (work\.lock) — выход. >> "journal\scheduler.log"
    exit /b 1
)
if not exist "work" mkdir "work"
if not exist "journal" mkdir "journal"
type nul > "work\.lock"

echo [%date% %time%] === Запуск analyzer bugs === >> "journal\scheduler.log"
uv run analyzer bugs --live >> "journal\scheduler.log" 2>&1
echo [%date% %time%] === Код завершения: %errorlevel% === >> "journal\scheduler.log"

del "work\.lock"
exit /b 0
