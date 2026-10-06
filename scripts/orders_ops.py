#!/usr/bin/env python3
"""Оператор входящих заказов: мост между агентом OpenClaw и API AI Freelance Copilot.

Агент не работает с базой напрямую — он вызывает этот скрипт, а скрипт ходит в HTTP API
(контракт описан в docs/API.md проекта ядра). Так границы остаются честными: бизнес-логика
и данные живут в ядре, агент только оркеструет и объясняет результат человеку.

Запуск (интерпретатор — `python`, НЕ `python3`: на Windows имя python3 перехватывает
заглушка Microsoft Store и скрипт не выполняется):

    python scripts/orders_ops.py health
    python scripts/orders_ops.py ingest
    python scripts/orders_ops.py triage --analyze
    python scripts/orders_ops.py queue
    python scripts/orders_ops.py summary
    python scripts/orders_ops.py test
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"
TESTS_DIR = ROOT / "tests"

SETTINGS_PATH = DATA_DIR / "settings.json"
SEED_PATH = DATA_DIR / "orders-seed.json"
PROFILE_PATH = DATA_DIR / "profile.json"
RULES_PATH = DATA_DIR / "rules.md"

TRIAGE_REPORT = REPORTS_DIR / "latest-triage.md"
SUMMARY_REPORT = REPORTS_DIR / "daily-summary.md"

DEFAULT_SETTINGS = {
    "api_base_url": "http://127.0.0.1:8800",
    "request_timeout_s": 30,
    "report_language": "ru",
    "max_orders_per_run": 25,
}


# --------------------------------------------------------------------------- #
# Транспорт
# --------------------------------------------------------------------------- #
class ApiError(RuntimeError):
    """Ошибка обращения к API ядра."""


def load_settings() -> dict:
    if SETTINGS_PATH.exists():
        try:
            return {**DEFAULT_SETTINGS, **json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))}
        except json.JSONDecodeError:
            pass
    return dict(DEFAULT_SETTINGS)


def read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class Api:
    """Минимальный HTTP-клиент ядра (stdlib, без внешних зависимостей)."""

    def __init__(self, base_url: str, timeout_s: float = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def call(self, method: str, path: str, payload: dict | None = None, query: dict | None = None):
        url = f"{self.base_url}{path}"
        if query:
            clean = {k: v for k, v in query.items() if v is not None}
            if clean:
                url = f"{url}?{urllib.parse.urlencode(clean)}"
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:  # 4xx/5xx с телом ответа
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            raise ApiError(f"HTTP {exc.code} на {method} {path}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ApiError(
                f"API недоступен по адресу {self.base_url} ({exc.reason}). "
                "Поднимите backend ядра: uvicorn app.main:app --host 127.0.0.1 --port 8800"
            ) from exc
        return json.loads(body) if body else None

    # ярлыки
    def health(self):
        return self.call("GET", "/api/v1/health")

    def orders(self, **query):
        return self.call("GET", "/api/v1/orders", query=query)

    def order_detail(self, order_id: int):
        return self.call("GET", f"/api/v1/orders/{order_id}")

    def analyze(self, order_id: int, profile_id: int | None = None):
        payload = {} if profile_id is None else {"profile_id": profile_id}
        return self.call("POST", f"/api/v1/orders/{order_id}/analyze", payload=payload)

    def review_queue(self, **query):
        return self.call("GET", "/api/v1/review-queue", query=query or {"review_status": "pending"})

    def metrics(self):
        return self.call("GET", "/api/v1/metrics")

    def create_order(self, payload: dict):
        return self.call("POST", "/api/v1/orders", payload=payload)


# --------------------------------------------------------------------------- #
# Команда health
# --------------------------------------------------------------------------- #
def cmd_health(api: Api) -> dict:
    data = api.health()
    provider = data.get("provider", {})
    return {
        "ok": True,
        "api": api.base_url,
        "status": data.get("status"),
        "database": data.get("database"),
        "llm_provider": provider.get("name"),
        "llm_model": provider.get("model"),
        "is_live": provider.get("is_live"),
        "prompt_version": provider.get("prompt_version"),
        "warning": provider.get("warning"),
    }


# --------------------------------------------------------------------------- #
# Команда ingest — залить пробные заказы в ядро (идемпотентно по external_id)
# --------------------------------------------------------------------------- #
def cmd_ingest(api: Api, *, limit: int | None = None) -> dict:
    seed = read_json(SEED_PATH, [])
    if not seed:
        return {"ok": False, "reason": f"нет файла с заказами {SEED_PATH.name}"}

    known = set()
    page = 1
    while True:
        chunk = api.orders(page=page, page_size=200)
        for item in chunk.get("items", []):
            if item.get("external_id"):
                known.add(item["external_id"])
        if page * chunk.get("page_size", 200) >= chunk.get("total", 0):
            break
        page += 1

    created, skipped = [], []
    for item in (seed[:limit] if limit else seed):
        payload = dict(item)
        if payload.get("external_id") in known:
            skipped.append(payload.get("external_id"))
            continue
        response = api.create_order(payload)
        created.append(
            {
                "external_id": payload.get("external_id"),
                "order_id": response.get("id"),
                "needs_review_at_input": response.get("needs_review"),
                "title": payload.get("title"),
            }
        )
    return {
        "ok": True,
        "created_count": len(created),
        "skipped_count": len(skipped),
        "skipped_external_ids": skipped,
        "created": created,
    }


# --------------------------------------------------------------------------- #
# Команда triage — прогон заказов через ИИ-анализ ядра
# --------------------------------------------------------------------------- #
def _collect_orders(api: Api, *, limit: int, status: str | None, only_without_analysis: bool) -> list:
    order_list, page = [], 1
    while len(order_list) < limit:
        chunk = api.orders(page=page, page_size=min(200, limit), status=status)
        order_list.extend(chunk.get("items", []))
        if page * chunk.get("page_size", 200) >= chunk.get("total", 0):
            break
        page += 1
    if only_without_analysis:
        order_list = [o for o in order_list if not o.get("latest_analysis")]
    return order_list[:limit]


def cmd_triage(api: Api, *, analyze: bool, limit: int, status: str | None, only_new: bool) -> dict:
    orders = _collect_orders(api, limit=limit, status=status, only_without_analysis=only_new)
    rows, errors = [], []
    for order in orders:
        entry = {
            "order_id": order["id"],
            "external_id": order.get("external_id"),
            "title": order.get("title"),
            "budget": _budget_text(order),
            "order_status": order.get("status"),
        }
        if analyze:
            try:
                outcome = api.analyze(order["id"])
                result = outcome.get("result", {})
                entry.update(
                    {
                        "analysis_id": outcome.get("analysis_id"),
                        "duration_ms": outcome.get("duration_ms"),
                        "provider": outcome.get("provider"),
                        "audit_status": outcome.get("audit_status"),
                        "match_score": result.get("match_score"),
                        "recommendation": result.get("recommendation"),
                        "needs_review": result.get("needs_review"),
                        "review_reason": result.get("review_reason"),
                        "draft_reply": result.get("draft_reply"),
                        "reason": result.get("reason"),
                    }
                )
            except ApiError as exc:
                entry["error"] = str(exc)
                errors.append(entry["order_id"])
        else:
            latest = order.get("latest_analysis") or {}
            entry.update(
                {
                    "analysis_id": latest.get("id"),
                    "match_score": latest.get("match_score"),
                    "recommendation": latest.get("recommendation"),
                    "needs_review": latest.get("needs_review"),
                    "review_status": latest.get("review_status"),
                }
            )
        rows.append(entry)

    report = _render_triage(rows, api.base_url, analyzed=analyze)
    write_text(TRIAGE_REPORT, report)
    return {
        "ok": True,
        "analyzed": analyze,
        "processed": len(rows),
        "errors": errors,
        "report": str(TRIAGE_REPORT.relative_to(ROOT)).replace("\\", "/"),
        "rows": rows,
    }


def _budget_text(order: dict) -> str:
    low, high, currency = order.get("budget_min"), order.get("budget_max"), order.get("currency", "RUB")
    if low and high:
        return f"{low:,.0f}-{high:,.0f} {currency}".replace(",", " ")
    if low or high:
        return f"от {low or high:,.0f} {currency}".replace(",", " ")
    return "не указан"


def _render_triage(rows: list[dict], base_url: str, *, analyzed: bool) -> str:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    head = (
        "# Разбор заказов\n\n"
        f"- Время: {stamp}\n"
        f"- Ядро: {base_url}\n"
        f"- Заказов в прогоне: {len(rows)}\n\n"
        "| ID | Заказ | Бюджет | Оценка | Решение | Нужен человек | Причина проверки |\n"
        "|---|---|---|---|---|---|---|\n"
    )
    verdict = {"apply": "откликаться", "review": "на проверку", "skip": "пропустить"}
    lines = []
    for row in rows:
        score = row.get("match_score")
        score_text = "—" if score is None else f"{score:.2f}"
        decision = verdict.get(row.get("recommendation") or "", "—")
        human = "да" if row.get("needs_review") else "нет"
        reason = (row.get("review_reason") or row.get("error") or "—").replace("|", "/")
        title = (row.get("title") or "")[:46].replace("|", "/")
        lines.append(
            f"| {row['order_id']} | {title} | {row.get('budget', '—')} | {score_text} | "
            f"{decision} | {human} | {reason} |"
        )
    tail = "\n"
    if analyzed:
        ready = [r for r in rows if r.get("recommendation") == "apply" and r.get("draft_reply")]
        tail += f"\n## Черновики откликов\n\nГотово черновиков: {len(ready)}\n\n"
        for row in ready:
            tail += f"### Заказ {row['order_id']} — {row.get('title')}\n\n{row['draft_reply']}\n\n"
    return head + "\n".join(lines) + "\n" + tail


# --------------------------------------------------------------------------- #
# Команда queue — что ждёт решения человека
# --------------------------------------------------------------------------- #
def cmd_queue(api: Api, *, limit: int) -> dict:
    queue = api.review_queue(review_status="pending", page_size=limit)
    items = [
        {
            "analysis_id": item["analysis_id"],
            "order_id": item["order_id"],
            "order_title": item["order_title"],
            "match_score": item.get("match_score"),
            "recommendation": item.get("recommendation"),
            "review_reason": item.get("review_reason"),
        }
        for item in queue.get("items", [])
    ]
    return {"ok": True, "pending_total": queue.get("total"), "items": items}


# --------------------------------------------------------------------------- #
# Команда summary — отчёт по расписанию
# --------------------------------------------------------------------------- #
def cmd_summary(api: Api, *, limit: int) -> dict:
    first = api.orders(page=1, page_size=200)
    all_orders = list(first.get("items", []))
    total_orders = first.get("total", len(all_orders))

    by_status: dict[str, int] = {}
    fresh, with_analysis = [], []
    for order in all_orders:
        by_status[order["status"]] = by_status.get(order["status"], 0) + 1
        if order["status"] == "new":
            fresh.append(order)
        if order.get("latest_analysis"):
            with_analysis.append(order)

    queue = api.review_queue(review_status="pending", page_size=200)
    queue_items = queue.get("items", [])
    # В ядре очередь --- это АНАЛИЗЫ, а не заказы: после повторного разбора на один заказ
    # может приходиться несколько записей. Считаем оба числа, чтобы не пугать владельца.
    queue_orders: dict[int, dict] = {}
    for item in queue_items:
        queue_orders.setdefault(item["order_id"], item)
    queue_by_order = list(queue_orders.values())
    to_apply = [
        o for o in with_analysis if (o["latest_analysis"] or {}).get("recommendation") == "apply"
    ]

    metrics, metrics_note = {}, None
    try:
        payload = api.metrics()
        metrics = {
            "json_valid_rate": payload.get("json_valid_rate"),
            "review_share": payload.get("review_share"),
            "analyses_total": payload.get("analyses_total"),
            "avg_analyze_duration_ms": payload.get("avg_analyze_duration_ms"),
        }
    except ApiError as exc:
        metrics_note = f"метрики недоступны: {exc}"

    health = api.health()
    provider = health.get("provider", {})

    report = _render_summary(
        base_url=api.base_url,
        total_orders=total_orders,
        by_status=by_status,
        fresh=fresh[:limit],
        queue_items=queue_by_order[:limit],
        queue_analyses_total=len(queue_items),
        to_apply=to_apply[:limit],
        metrics=metrics,
        metrics_note=metrics_note,
        provider=provider,
    )
    write_text(SUMMARY_REPORT, report)
    return {
        "ok": True,
        "report": str(SUMMARY_REPORT.relative_to(ROOT)).replace("\\", "/"),
        "total_orders": total_orders,
        "new_orders": len(fresh),
        "pending_review_orders": len(queue_by_order),
        "pending_review_analyses": len(queue_items),
        "recommended_to_apply": len(to_apply),
        "metrics": metrics,
        "metrics_note": metrics_note,
        "llm_provider": provider.get("name"),
        "llm_is_live": provider.get("is_live"),
    }


def _render_summary(
    *,
    base_url: str,
    total_orders: int,
    by_status: dict,
    fresh: list[dict],
    queue_items: list[dict],
    queue_analyses_total: int,
    to_apply: list[dict],
    metrics: dict,
    metrics_note: str | None,
    provider: dict,
) -> str:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    live = "да (тратит деньги)" if provider.get("is_live") else "нет (mock, офлайн)"

    def as_list(items: list[str]) -> str:
        return "\n".join(items) if items else "- (нет)"

    fresh_lines = as_list(
        [f"- #{o['id']} {o['title']} — бюджет: {_budget_text(o)}" for o in fresh]
    )
    queue_lines = as_list(
        [
            f"- анализ #{i['analysis_id']}, заказ #{i['order_id']} «{i['order_title']}» — "
            f"{i.get('review_reason') or 'причина не указана'}"
            for i in queue_items
        ]
    )
    apply_lines = as_list(
        [
            f"- #{o['id']} {o['title']} — оценка "
            f"{(o['latest_analysis'].get('match_score') or 0):.2f}"
            for o in to_apply
        ]
    )
    status_lines = as_list([f"- {name}: {count}" for name, count in sorted(by_status.items())])

    metrics_block = (
        f"- Доля корректных JSON: {metrics.get('json_valid_rate')}\n"
        f"- Доля ушедших на проверку: {metrics.get('review_share')}\n"
        f"- Всего анализов: {metrics.get('analyses_total')}\n"
        f"- Среднее время анализа, мс: {metrics.get('avg_analyze_duration_ms')}"
        if metrics
        else f"- {metrics_note or 'нет данных'}"
    )

    return f"""# Сводка по заказам

- Время формирования: {stamp}
- Ядро: {base_url}
- Всего заказов в системе: {total_orders}
- Новых без анализа: {len(fresh)}
- Заказов ждут решения человека: {len(queue_items)} (записей в очереди: {queue_analyses_total})
- Рекомендовано к отклику: {len(to_apply)}
- LLM-провайдер: {provider.get('name')} / {provider.get('model')}
- Реальная модель (тратит деньги): {live}

## Новые заказы без анализа

{fresh_lines}

## Очередь ручной проверки

{queue_lines}

## Рекомендовано к отклику

{apply_lines}

## Заказы по статусам

{status_lines}

## Метрики ядра

{metrics_block}

---

Сводка сформирована скриптом `scripts/orders_ops.py` по данным API ядра.
Решение по каждому заказу принимает человек: агент только собирает картину и готовит черновики.
"""


# --------------------------------------------------------------------------- #
# Команда test — сквозная проверка на реальном ядре
# --------------------------------------------------------------------------- #
def cmd_test(api: Api) -> dict:
    cases = read_json(TESTS_DIR / "messages.json", [])
    results = []
    for case in cases:
        external_id = case["external_id"]
        try:
            created = cmd_ingest(api, limit=None)
            order_id = None
            for item in created.get("created", []):
                if item["external_id"] == external_id:
                    order_id = item["order_id"]
            if order_id is None:
                listing = api.orders(page=1, page_size=200)
                for item in listing.get("items", []):
                    if item.get("external_id") == external_id:
                        order_id = item["id"]
                        break
            if order_id is None:
                results.append({**case, "status": "failed", "why": "заказ не найден после ingest"})
                continue
            outcome = api.analyze(order_id)
            result = outcome.get("result", {})
            expected = case.get("expect", {})
            checks = {
                "recommendation": result.get("recommendation") == expected.get("recommendation"),
                "needs_review": bool(result.get("needs_review")) == bool(expected.get("needs_review")),
                "has_draft": bool(result.get("draft_reply")) == bool(expected.get("draft_reply")),
            }
            ok = all(checks.values())
            results.append(
                {
                    **case,
                    "order_id": order_id,
                    "status": "passed" if ok else "failed",
                    "checks": checks,
                    "got": {
                        "recommendation": result.get("recommendation"),
                        "needs_review": result.get("needs_review"),
                        "has_draft": bool(result.get("draft_reply")),
                        "match_score": result.get("match_score"),
                        "review_reason": result.get("review_reason"),
                    },
                }
            )
        except ApiError as exc:
            results.append({**case, "status": "failed", "why": str(exc)})

    passed = sum(1 for item in results if item["status"] == "passed")
    summary = {
        "status": "passed" if passed == len(results) and results else "failed",
        "passed": passed,
        "total": len(results),
        "api": api.base_url,
        "results": results,
    }
    write_text(TESTS_DIR / "results.json", json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> int:
    settings = load_settings()
    parser = argparse.ArgumentParser(description="Оператор входящих заказов (мост к ядру AI Freelance Copilot)")
    parser.add_argument("--base-url", default=settings["api_base_url"], help="адрес API ядра")
    parser.add_argument(
        "--timeout", type=float, default=settings["request_timeout_s"], help="таймаут запроса, с"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("health", help="проверить доступность ядра и активную модель")

    ingest = sub.add_parser("ingest", help="залить пробные заказы из data/orders-seed.json")
    ingest.add_argument("--limit", type=int, default=None)

    triage = sub.add_parser("triage", help="разобрать заказы (при --analyze запустить ИИ-анализ)")
    triage.add_argument("--analyze", action="store_true", help="запустить ИИ-анализ по каждому заказу")
    triage.add_argument("--limit", type=int, default=settings["max_orders_per_run"])
    triage.add_argument("--status", default=None, help="фильтр статуса заказа")
    triage.add_argument(
        "--only-new",
        action="store_true",
        help="брать только заказы без анализа (иначе пересчитываются все)",
    )

    queue = sub.add_parser("queue", help="очередь ручной проверки")
    queue.add_argument("--limit", type=int, default=50)

    summary = sub.add_parser("summary", help="сводка по заказам в reports/daily-summary.md")
    summary.add_argument("--limit", type=int, default=10)

    sub.add_parser("test", help="сквозная проверка сценариев на реальном ядре")

    args = parser.parse_args()
    api = Api(args.base_url, args.timeout)

    try:
        if args.command == "health":
            payload = cmd_health(api)
        elif args.command == "ingest":
            payload = cmd_ingest(api, limit=args.limit)
        elif args.command == "triage":
            payload = cmd_triage(
                api, analyze=args.analyze, limit=args.limit, status=args.status, only_new=args.only_new
            )
        elif args.command == "queue":
            payload = cmd_queue(api, limit=args.limit)
        elif args.command == "summary":
            payload = cmd_summary(api, limit=args.limit)
        else:
            payload = cmd_test(api)
    except ApiError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok", payload.get("status") == "passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
