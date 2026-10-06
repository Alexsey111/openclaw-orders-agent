# TOOLS.md — рабочие команды агента

Интерпретатор: **`python`**, НЕ `python3`. На этой машине имя `python3` перехватывает
заглушка Microsoft Store: она печатает `Python` и выходит с кодом 49, ничего не выполняя.
Все команды ниже запускай из корня workspace агента.

## 1. Проверить, что ядро живо (делать перед любым разбором)

```bash
python scripts/orders_ops.py health
```

Возвращает JSON: статус, активный LLM-провайдер, `is_live` (тратит ли деньги) и версию промпта.

## 2. Залить пробные заказы в ядро

```bash
python scripts/orders_ops.py ingest
```

Идемпотентно: заказы с уже известным `external_id` повторно не создаются.
Источник заказов — `data/orders-seed.json`.

## 3. Разобрать заказы (основной сценарий)

```bash
# только посмотреть, что уже посчитано (без обращения к модели)
python scripts/orders_ops.py triage

# запустить ИИ-анализ по заказам без анализа (тратит деньги, если ядро на реальной модели)
python scripts/orders_ops.py triage --analyze --only-new

# пересчитать все заказы заново
python scripts/orders_ops.py triage --analyze
```

Результат: `reports/latest-triage.md` — таблица (оценка, решение, нужен ли человек, причина)
плюс черновики откликов по тем заказам, где ядро рекомендовало `apply`.

## 4. Очередь ручной проверки

```bash
python scripts/orders_ops.py queue
```

## 5. Сводка по расписанию (эта же команда вызывается из cron-задачи)

```bash
python scripts/orders_ops.py summary
```

Результат: `reports/daily-summary.md` — новые заказы, очередь ручной проверки,
рекомендованные к отклику, метрики ядра.

## 6. Сквозная проверка сценариев

```bash
python scripts/orders_ops.py test
```

Прогоняет `tests/messages.json` через настоящее ядро и сверяет результат с ожиданием.
Пишет `tests/results.json`.

## Если ядро недоступно

Скрипт вернёт `{"ok": false, "error": "API недоступен по адресу ..."}` — это значит, что backend
ядра не запущен. Поднять его:

```bash
cd "D:/python projects/AI_Freelance_Copilot"
./.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8800
```

Не подменяй недоступное ядро выдуманными данными: сообщи владельцу, что ядро не отвечает.
