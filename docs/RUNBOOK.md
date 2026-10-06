# Запуск и автоматизация

## Что должно быть поднято

1. **Ядро** — AI Freelance Copilot (обязательно; без него агент ничего не разберёт):

```bash
cd "D:/python projects/AI_Freelance_Copilot"
./.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8800
```

Первые 5 заказов заливаются один раз: `python scripts/orders_ops.py ingest` (команда идемпотентна).

2. **OpenClaw gateway** — работает как задача Windows с триггером «при входе в систему»:

```bash
openclaw gateway status      # Running + порт 18789
openclaw gateway restart     # если агент не виден в дашборде
```

## Автоматический запуск: почему по входу в систему, а не по часам

Владелец выключает компьютер между сессиями, поэтому задача по расписанию (cron) срабатывает
вхолостую — в момент срабатывания машина выключена. Запуск, привязанный ко входу в систему,
срабатывает ровно тогда, когда владелец реально начинает работу.

**Технически это возможно и проверено:** у OpenClaw нет собственного триггера «при старте системы»,
но он и не нужен — gateway уже запускается задачей Windows `OpenClaw Gateway` с триггером
`MSFT_TaskLogonTrigger` (создан при установке). Достаточно, чтобы после старта gateway сработала
наша автоматизация. Способы:

- **основной** — cron-задача OpenClaw (`openclaw cron add`) для самого агента: сводка по заказам;
- **резервный** — если автоматизация OpenClaw недоступна (например, плагин ломает CLI),
  задача Windows с триггером «при входе в систему» вызывает скрипт агента напрямую.

### Основная автоматизация: сводка по заказам

```bash
openclaw cron add \
  --agent orders-operator \
  --name "Сводка по заказам" \
  --description "Новые заказы и очередь ручной проверки" \
  --cron "0 8 * * *" \
  --tz Asia/Krasnoyarsk \
  --session isolated \
  --message "Собери сводку по заказам: новые заказы и очередь ручной проверки. Запусти python scripts/orders_ops.py summary и верни краткий отчёт." \
  --timeout 600000
```

Здесь `--cron "0 8 * * *"` — ежедневно в 8:00 по местному времени (Алтай, UTC+07:00)
как запасной вариант; основной запуск — по входу в систему (см. ниже, флаг `--at` + задача Windows).

Проверить параметры под свою версию OpenClaw: `openclaw cron add --help`.

### Как сделать запуск «при входе в систему»

Cron OpenClaw умеет только календарное расписание, поэтому привязка к входу в систему делается
задачей Windows, которая при логоне запускает одноразовую автоматизацию:

```bash
# создать одноразовую задачу (выполнится при следующем запуске)
openclaw cron add --agent orders-operator --name "Сводка при входе" \
  --at 0m --session isolated \
  --message "Собери сводку по заказам: python scripts/orders_ops.py summary"
```

```powershell
# задача Windows: при входе в систему запустить сводку и выйти
schtasks /Create /TN "Orders Agent Summary (logon)" /SC ONLOGON ^
  /TR "cmd /c cd /d \"D:\python projects\openclaw-orders-agent\" && python scripts\orders_ops.py summary" /F
```

Запустить прямо сейчас, не дожидаясь перезагрузки:

```powershell
schtasks /Run /TN "Orders Agent Summary (logon)"
```

Из git-bash `schtasks` требует `MSYS2_ARG_CONV_EXCL='*'`, иначе MSYS перепишет `/Run` и `/TN`
как пути. Проще выполнить команду в PowerShell.

## Ручной запуск (для проверки и скриншотов)

```bash
cd "D:/python projects/openclaw-orders-agent"
python scripts/orders_ops.py health
python scripts/orders_ops.py ingest
python scripts/orders_ops.py triage --analyze --only-new
python scripts/orders_ops.py summary
python scripts/orders_ops.py test
```

## Проверка, что автоматизация сработала

```bash
openclaw cron list                 # статус и последний запуск задачи
openclaw cron runs <job-id>        # история запусков
```

Результат работы — файл `reports/daily-summary.md` с датой и временем формирования.
