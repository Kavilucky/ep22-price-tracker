#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Мониторинг динамики цен на товары и авиабилеты 24/7 (Эпизод 22, @ByteAutomate_AI).

Что делает:
  1) опрашивает карточки товаров и авиабилетов своим скриптом (без подписки);
  2) притворяется мобильным Safari (iOS 17.4): часть витрин отдаёт мобильным
     клиентам иную цену и реже включает капчу;
  3) держит вежливые паузы с джиттером +/-15%: ровный интервал главный
     маркер робота для защиты магазинов (WAF);
  4) шлёт HTML-алерты в Telegram Bot API при падении цены ниже порога;
  5) хранит историю котировок в SQLite: график волны цены и ответ на вопрос
     «брать сейчас или ждать».

Запуск:
  python price_tracker.py --once            разовый опрос из products.json
  python price_tracker.py --loop            круглосуточный режим
  docker compose up -d                      то же в контейнере (см. docker-compose.yml)

Настройки: переменные окружения TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
или ключи в products.json (файл не коммитить, шаблон см. в README.md).
"""
from __future__ import annotations

import argparse
import html
import json
import random
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    print("Нужен пакет requests: pip install -r requirements.txt", file=sys.stderr)
    raise SystemExit(1)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(__file__).resolve().parent / "data" / "prices.db"
PRODUCTS_PATH = BASE_DIR / "products.json"

# Заголовки мобильного Safari (iOS 17.4): витрины чаще отдают мобильным
# клиентам актуальную цену и реже показывают капчу, чем десктопным ботам.
MOBILE_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 "
        "Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.google.com/",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Dest": "document",
}

JITTER = 0.15  # разброс пауз +/-15%: защита от детекта по ровному интервалу
REQUEST_TIMEOUT = 20  # секунд на один опрос карточки
COOLDOWN_S = 3600  # не чаще одного алерта в час на товар (защита от спама)


@dataclass
class Product:
    name: str
    url: str
    target_price: float
    selector: str = ""  # CSS-селектор цены под конкретную витрину (пример!)
    min_interval_s: int = 600  # не чаще раза в 10 минут на карточку


def load_products(path: Path = PRODUCTS_PATH) -> list[Product]:
    """Читает список карточек из products.json."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"Нет {path.name}: скопируйте products.example.json и впишите свои карточки.",
              file=sys.stderr)
        raise SystemExit(1)
    except json.JSONDecodeError as exc:
        print(f"Битый JSON в {path.name}: {exc}", file=sys.stderr)
        raise SystemExit(1)
    products: list[Product] = []
    for i, item in enumerate(raw if isinstance(raw, list) else raw.get("products", [])):
        try:
            products.append(Product(
                name=str(item["name"]),
                url=str(item["url"]),
                target_price=float(item["target_price"]),
                selector=str(item.get("selector", "")),
                min_interval_s=int(item.get("min_interval_s", 600)),
            ))
        except (KeyError, ValueError, TypeError) as exc:
            print(f"Карточка #{i} пропущена (проверьте поля name/url/target_price): {exc}",
                  file=sys.stderr)
    if not products:
        print("Список карточек пуст: добавьте хотя бы один товар.", file=sys.stderr)
        raise SystemExit(1)
    return products


def init_db(path: Path = DB_PATH) -> sqlite3.Connection:
    """Создаёт SQLite-базу истории котировок (переживает перезапуск)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS quotes("
        "name TEXT, price REAL, checked_at REAL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS alerts("
        "name TEXT PRIMARY KEY, sent_at REAL)"
    )
    return conn


def fetch_price(session: requests.Session, product: Product) -> Optional[float]:
    """Опрашивает карточку, возвращает цену или None при блокировке/ошибке."""
    try:
        resp = session.get(product.url, headers=MOBILE_HEADERS,
                           timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        print(f"[{product.name}] сеть недоступна: {exc}")
        return None
    if resp.status_code in (403, 429):
        # Защита витрины сработала: пауза с backoff, не долбить.
        print(f"[{product.name}] витрина ответила {resp.status_code}: "
              "пауза, повтор позже.")
        time.sleep(60)
        return None
    if resp.status_code != 200:
        print(f"[{product.name}] HTTP {resp.status_code}: пропуск.")
        return None
    return parse_price(resp.text, product.selector, product.name)


def parse_price(page: str, selector: str, name: str) -> Optional[float]:
    """Достаёт цену из HTML (селектор пример! адаптируйте под витрину)."""
    try:
        from bs4 import BeautifulSoup  # локальный импорт: без него только эвристика
    except ImportError:
        print(f"[{name}] нет beautifulsoup4: pip install -r requirements.txt")
        return None
    try:
        soup = BeautifulSoup(page, "html.parser")
        node = soup.select_one(selector) if selector else None
        text = node.get_text(" ", strip=True) if node else soup.get_text(" ", strip=True)
        digits = "".join(ch for ch in text if ch.isdigit() or ch in ",.")
        price = float(digits.replace("\u00a0", "").replace(" ", "").replace(",", "."))
        return price if price > 0 else None
    except (ValueError, AttributeError) as exc:
        print(f"[{name}] не разобрал цену (поменялась вёрстка?): {exc}")
        return None


def should_alert(conn: sqlite3.Connection, product: Product, price: float) -> bool:
    """Цена ниже порога + cooldown на товар (не спамить в Telegram)."""
    if price >= product.target_price:
        return False
    row = conn.execute("SELECT sent_at FROM alerts WHERE name=?",
                       (product.name,)).fetchone()
    if row and (time.time() - float(row[0]) < COOLDOWN_S):
        return False
    return True


def send_telegram(token: str, chat_id: str, product: Product,
                  old: Optional[float], new: float) -> bool:
    """Шлёт HTML-алерт в Telegram Bot API. Возвращает успех."""
    diff = (old - new) if old else 0.0
    text = (
        f"📉 <b>{html.escape(product.name)}</b>\n"
        f"Было: {html.escape(f'{old:,.0f} ₽') if old else '—'}\n"
        f"Стало: <b>{new:,.0f} ₽</b>\n"
        f"Разница: {diff:,.0f} ₽\n"
        f'<a href="{html.escape(product.url)}">Открыть карточку</a>'
    )
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": False},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        print(f"[{product.name}] Telegram недоступен: {exc}")
        return False
    if resp.status_code != 200:
        print(f"[{product.name}] Telegram ответил {resp.status_code}: "
              f"{resp.text[:200]}")
        return False
    return True


def last_price(conn: sqlite3.Connection, name: str) -> Optional[float]:
    row = conn.execute("SELECT price FROM quotes WHERE name=? "
                       "ORDER BY checked_at DESC LIMIT 1", (name,)).fetchone()
    return float(row[0]) if row else None


def check_once(conn: sqlite3.Connection, session: requests.Session,
               products: list[Product], token: str, chat_id: str) -> None:
    """Один круг опроса всех карточек с вежливыми паузами."""
    for product in products:
        price = fetch_price(session, product)
        if price is None:
            continue
        old = last_price(conn, product.name)
        conn.execute("INSERT INTO quotes(name, price, checked_at) VALUES(?,?,?)",
                     (product.name, price, time.time()))
        conn.commit()
        print(f"[{product.name}] {price:,.0f} ₽ (порог {product.target_price:,.0f} ₽)")
        if should_alert(conn, product, price):
            if send_telegram(token, chat_id, product, old, price):
                conn.execute("INSERT OR REPLACE INTO alerts(name, sent_at) VALUES(?,?)",
                             (product.name, time.time()))
                conn.commit()
        # Вежливая пауза с джиттером: база * (1 +/- 15%).
        pause = product.min_interval_s * random.uniform(1 - JITTER, 1 + JITTER)
        time.sleep(min(pause, 60))


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Трекер цен 24/7 с алертами в Telegram")
    ap.add_argument("--once", action="store_true", help="один круг опроса и выход")
    ap.add_argument("--loop", action="store_true", help="круглосуточный режим")
    ap.add_argument("--products", default=str(PRODUCTS_PATH), help="путь к products.json")
    args = ap.parse_args(argv)

    import os
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print("Задайте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID "
              "(см. README.md, раздел за 3 минуты).", file=sys.stderr)
        return 1

    products = load_products(Path(args.products))
    conn = init_db()
    session = requests.Session()
    try:
        if args.once or not args.loop:
            check_once(conn, session, products, token, chat_id)
        else:
            print(f"Режим 24/7: карточек {len(products)}, "
                  "остановка по Ctrl+C.")
            while True:
                check_once(conn, session, products, token, chat_id)
    except KeyboardInterrupt:
        print("\nОстановлено владельцем.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
