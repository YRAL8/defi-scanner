#!/usr/bin/env python3
"""
DeFi LP-сканер: тянет данные с DeFiLlama (yields.llama.fi/pools, бесплатный
публичный API, без ключа) и строит единый рейтинг пулов (1-е, 2-е, ... место —
как в спорте), а не просто таблицу разрозненных цифр. Каждый запуск сохраняет
свой снимок в локальную SQLite-базу (history.db), чтобы можно было смотреть,
как рейтинг конкретного пула менялся день ото дня.

Рейтинг считается из трёх составляющих (см. compute_score):
  - оборот/TVL — главный драйвер реальной комиссионной доходности
  - стабильность — насколько текущий APY близок к своей 30-дневной норме
  - размер TVL — крупный пул надёжнее/сложнее манипулировать, чем крошечный
Веса и формула — осознанно простые и прозрачные, не "чёрный ящик".

Плюс дополнительные проверки "на доверие", которые раньше делались вручную:
возраст пула (сколько дней его вообще отслеживает DeFiLlama) и число
аудитов самого протокола (второй API-эндпоинт, не тот, что отдаёт пулы).
"""
import argparse
import math
import sqlite3
import statistics
import sys
import time
import random
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

POOLS_URL = "https://yields.llama.fi/pools"
PROTOCOLS_URL = "https://api.llama.fi/protocols"
CHART_URL_TMPL = "https://yields.llama.fi/chart/{}"
DB_PATH = Path(__file__).parent / "history.db"

CHART_SLEEP_SECONDS = 0.30
TODAY_REFRESH_TTL_SECONDS = 60 * 60  # не долбить API при двух прогонах подряд

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "defi-scanner (public DeFiLlama API)"})


def iso_day(ts: object) -> str | None:
    if not isinstance(ts, str) or len(ts) < 10:
        return None
    # timestamp в chart-эндпоинте: '2026-07-28T16:01:52.188Z'
    return ts[:10]


def required_days_window(days: int, end: date | None = None) -> tuple[str, str, set[str]]:
    end_d = end or date.today()
    start_d = end_d - timedelta(days=days - 1)
    start_s = start_d.isoformat()
    end_s = end_d.isoformat()
    req = {(start_d + timedelta(days=i)).isoformat() for i in range(days)}
    return start_s, end_s, req


def init_history_db(conn: sqlite3.Connection) -> None:
    """Создаёт/мигрирует историю. Идемпотентно."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            run_date TEXT NOT NULL,
            pool_id TEXT NOT NULL,
            chain TEXT,
            project TEXT,
            symbol TEXT,
            tvl_usd REAL,
            apy REAL,
            apy_base REAL,
            apy_base7d REAL,
            apy_base_median REAL,
            turnover_ratio REAL,
            spike_pct REAL,
            predicted_class TEXT,
            score REAL,
            rank INTEGER,
            PRIMARY KEY (run_date, pool_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pool_chart (
            pool_id TEXT NOT NULL,
            day TEXT NOT NULL,
            apy_base REAL,
            tvl_usd REAL,
            fetched_at INTEGER NOT NULL,
            PRIMARY KEY (pool_id, day)
        )
        """
    )
    cols = {
        r[1]
        for r in conn.execute("PRAGMA table_info(snapshots)").fetchall()
        if isinstance(r, (tuple, list)) and len(r) > 1
    }
    if "apy_base" not in cols:
        conn.execute("ALTER TABLE snapshots ADD COLUMN apy_base REAL")
    if "apy_base7d" not in cols:
        conn.execute("ALTER TABLE snapshots ADD COLUMN apy_base7d REAL")
    if "apy_base_median" not in cols:
        conn.execute("ALTER TABLE snapshots ADD COLUMN apy_base_median REAL")


def fetch_pool_chart(pool_id: str) -> list[dict]:
    # Чужой бесплатный API: без параллелизма, с повторами на 429/5xx.
    last_exc: Exception | None = None
    for attempt in range(6):
        try:
            resp = _SESSION.get(CHART_URL_TMPL.format(pool_id), timeout=30)
            if resp.status_code == 429:
                # экспоненциальный бэкофф с лёгким jitter
                wait = min(20.0, 0.8 * (2**attempt)) + random.random() * 0.3
                time.sleep(wait)
                continue
            if 500 <= resp.status_code < 600:
                wait = min(10.0, 0.5 * (2**attempt)) + random.random() * 0.2
                time.sleep(wait)
                continue
            resp.raise_for_status()
            payload = resp.json()
            if not isinstance(payload, dict) or payload.get("status") != "success":
                raise RuntimeError(
                    f"DeFiLlama API (chart) вернул неожиданный ответ: {payload!r:.200}"
                )
            data = payload.get("data")
            if not isinstance(data, list):
                raise RuntimeError("DeFiLlama API (chart): поле 'data' отсутствует или не список")
            return data
        except Exception as e:  # noqa: BLE001 - внешняя сеть/JSON/SQLite, не роняем прогон
            last_exc = e
            wait = min(6.0, 0.3 * (2**attempt)) + random.random() * 0.2
            time.sleep(wait)
            continue

    assert last_exc is not None
    raise last_exc


def ensure_pool_chart_cached(
    conn: sqlite3.Connection,
    pool_id: str,
    median_days: int,
) -> bool:
    """Гарантирует, что в pool_chart есть данные за нужное окно (N дней),
    при необходимости тянет chart-эндпоинт и upsert-ит только нужные дни.

    Возвращает True, если был сетевой запрос (для паузы между запросами).
    """
    start_day, end_day, req_days = required_days_window(median_days)
    now = int(time.time())

    rows = conn.execute(
        """
        SELECT day, fetched_at
        FROM pool_chart
        WHERE pool_id = ?
          AND day >= ?
          AND day <= ?
        """,
        (pool_id, start_day, end_day),
    ).fetchall()
    seen = {r[0] for r in rows if r and isinstance(r[0], str)}
    fetched_at_by_day = {r[0]: int(r[1] or 0) for r in rows if r and isinstance(r[0], str)}

    missing = req_days - seen
    today = date.today().isoformat()
    today_stale = today in fetched_at_by_day and (now - fetched_at_by_day[today]) > TODAY_REFRESH_TTL_SECONDS

    if not missing and not today_stale:
        return False

    chart = fetch_pool_chart(pool_id)

    # На случай нескольких точек в день — берём последнюю по timestamp (они идут по времени).
    by_day: dict[str, dict] = {}
    for item in chart:
        if not isinstance(item, dict):
            continue
        d = iso_day(item.get("timestamp"))
        if not d or d < start_day or d > end_day:
            continue
        by_day[d] = item

    to_upsert_days = set(missing)
    if today_stale:
        to_upsert_days.add(today)

    to_insert = []
    for d in sorted(to_upsert_days):
        item = by_day.get(d)
        if not item:
            continue
        apy_base = item.get("apyBase")
        tvl_usd = item.get("tvlUsd")
        to_insert.append(
            (
                pool_id,
                d,
                float(apy_base) if isinstance(apy_base, (int, float)) else None,
                float(tvl_usd) if isinstance(tvl_usd, (int, float)) else None,
                now,
            )
        )

    if to_insert:
        conn.executemany(
            """
            INSERT OR REPLACE INTO pool_chart (pool_id, day, apy_base, tvl_usd, fetched_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            to_insert,
        )
        conn.commit()
    return True


def apy_base_median_from_cache(
    conn: sqlite3.Connection,
    pool_id: str,
    median_days: int,
) -> tuple[float | None, int]:
    start_day, end_day, _ = required_days_window(median_days)
    vals = [
        r[0]
        for r in conn.execute(
            """
            SELECT apy_base
            FROM pool_chart
            WHERE pool_id = ?
              AND day >= ?
              AND day <= ?
              AND apy_base IS NOT NULL
            """,
            (pool_id, start_day, end_day),
        ).fetchall()
        if r and isinstance(r[0], (int, float))
    ]
    if len(vals) < (median_days / 2):
        return None, len(vals)
    return float(statistics.median(vals)), len(vals)


def fetch_pools() -> list[dict]:
    resp = requests.get(POOLS_URL, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or data.get("status") != "success":
        raise RuntimeError(f"DeFiLlama API (пулы) вернул неожиданный ответ: {data!r:.200}")
    pools = data.get("data")
    if not isinstance(pools, list):
        raise RuntimeError("DeFiLlama API (пулы): поле 'data' отсутствует или не список")
    return pools


def fetch_protocol_audits() -> dict[str, int]:
    """slug -> число аудитов по данным DeFiLlama. 0/отсутствие в этом словаре не
    значит "не аудирован" — это может просто значить, что DeFiLlama не занесла
    данные (например у Orca и Raydium тут 0, хотя оба реально проверялись) —
    это сигнал "не проверено по этим данным", а не "точно небезопасно"."""
    resp = requests.get(PROTOCOLS_URL, timeout=30)
    resp.raise_for_status()
    protocols = resp.json()
    if not isinstance(protocols, list):
        raise RuntimeError("DeFiLlama API (протоколы) вернул не список — не могу прочитать аудиты")
    result = {}
    for p in protocols:
        if not isinstance(p, dict):
            continue
        slug = p.get("slug")
        if not slug:
            continue
        try:
            result[slug] = int(p.get("audits") or 0)
        except (TypeError, ValueError):
            result[slug] = 0
    return result


def is_usable_pool(p: dict) -> bool:
    """Базовая проверка данных пула — независимо от пользовательских фильтров
    (--min-tvl 0 и т.п.), пул с нулевым/отсутствующим/нечисловым TVL или без
    project/symbol нельзя ни оценить, ни осмысленно показать. Раньше такие
    записи могли проползти дальше и уронить math.log10() или форматирование
    строки на None (найдено независимым аудитом, 2026-07-27)."""
    tvl = p.get("tvlUsd")
    if not isinstance(tvl, (int, float)) or tvl <= 0:
        return False
    apy = p.get("apy")
    if not isinstance(apy, (int, float)):
        return False
    if not p.get("project") or not p.get("symbol") or not p.get("chain"):
        return False
    return True


def daily_turnover_ratio(pool: dict) -> float | None:
    """Средний дневной оборот / TVL за последнюю неделю — устойчивее к разовым
    всплескам объёма за один день, чем volumeUsd1d."""
    tvl = pool.get("tvlUsd") or 0
    vol7d = pool.get("volumeUsd7d")
    if not tvl or vol7d is None:
        return None
    return (vol7d / 7) / tvl


def apy_spike_pct(pool: dict, apy: float, fee_only: bool) -> float | None:
    """На сколько % текущий APY отклоняется от своей нормы — большое положительное
    значение обычно значит разовый всплеск (например памп объёма вчера), а не
    устойчивую доходность; отрицательное — текущий APY просел ниже своей нормы.
    apy передаётся явно (не всегда p['apy'] — см. effective_apy).

    Норма берётся той же природы, что и apy: при fee_only сравниваем apyBase с
    apyBase7d (тоже комиссионная норма, за 7 дней — у DeFiLlama нет
    комиссионной нормы за 30 дней, это ограничение данных), иначе — с
    apyMean30d (норма ПОЛНОГО apy). Раньше при fee_only числитель был apyBase,
    а знаменатель — норма ПОЛНОГО apy, то есть сравнивались разные шкалы: пул с
    большой долей reward автоматически получал огромное "отклонение" комиссий
    от чужой нормы, даже если сами комиссии были стабильны (найдено вторым
    независимым аудитом логики, 2026-07-27)."""
    norm = pool.get("apyBase7d") if fee_only else pool.get("apyMean30d")
    if apy is None or not norm:
        return None
    return (apy - norm) / norm * 100


def effective_apy(pool: dict, fee_only: bool) -> float | None:
    """Какой APY считать "настоящим" для фильтров/сравнения/сортировки: полный
    (apy — комиссии + токен-поощрения от протокола) или только комиссионный
    (apyBase). У Orca весь доход — комиссии (apyBase == apy, apyReward == 0);
    у некоторых пулов на других сетях значительная часть "доходности" — это
    временные токен-эмиссии, которые протокол может урезать в любой момент,
    и по сути другое качество дохода, а не то же самое (найдено независимым
    аудитом логики отбора, 2026-07-27 — например Aerodrome USDC-AERO
    показывал 110.5% общего APY, из них только 54.8% — реальные комиссии).

    При fee_only=True и отсутствующем apyBase возвращает None (пул исключают
    выше по стеку), а НЕ полный apy — раньше был тихий fallback на apy, из-за
    которого чисто эмиссионные пулы (Aerodrome v1 / Velodrome v2 — там по
    дизайну протокола комиссии LP-пула уходят ve-держателям токена
    управления, а LP получает ТОЛЬКО эмиссию, apyBase там в принципе
    отсутствует) проходили под видом честных fee-пулов — найдено вторым
    независимым аудитом логики, 2026-07-27."""
    if fee_only:
        base = pool.get("apyBase")
        return base if isinstance(base, (int, float)) else None
    return pool.get("apy") or 0


def compute_scores(pools: list[dict]) -> None:
    """Проставляет pool['_score'] (0-100) каждому пулу в списке — нормализация
    только внутри ЭТОГО набора (после фильтров), не абсолютная шкала.

    Формула (веса выбраны просто и прозрачно, не подгонялись под данные):
      50% — место по обороту/TVL относительно остальных в выборке
      30% — стабильность = 1 - |отклонение APY от 30д-нормы|, ограничено [0,1]
      20% — размер TVL в лог-шкале относительно остальных в выборке
    """
    # ratio может быть None (нет volumeUsd7d — не отсекаем такие пулы совсем,
    # см. фильтр выше), поэтому для min/max берём только реальные числа; сами
    # None получат нейтральные 0.5 в цикле ниже, как и spike=None.
    known_ratios = [p["_ratio"] for p in pools if p["_ratio"] is not None]
    tvls = [math.log10(p["tvlUsd"]) for p in pools]
    r_min, r_max = (min(known_ratios), max(known_ratios)) if known_ratios else (0, 0)
    t_min, t_max = min(tvls), max(tvls)

    for p in pools:
        if p["_ratio"] is not None and r_max > r_min:
            ratio_norm = (p["_ratio"] - r_min) / (r_max - r_min)
        else:
            ratio_norm = 0.5
        tvl_norm = (
            (math.log10(p["tvlUsd"]) - t_min) / (t_max - t_min) if t_max > t_min else 0.5
        )
        spike = p["_spike"]
        stability_norm = 1.0 - min(abs(spike) / 100, 1.0) if spike is not None else 0.5

        p["_score"] = round(
            100 * (0.5 * ratio_norm + 0.3 * stability_norm + 0.2 * tvl_norm), 1
        )


def save_snapshot(pools: list[dict], run_date: str) -> None:
    # try/finally вокруг всего — раньше conn.close() вызывался только на
    # "счастливом пути"; если бы executemany() или commit() упали (например
    # из-за неожиданного типа поля), соединение осталось бы висеть открытым
    # (найдено независимым аудитом, 2026-07-27).
    conn = sqlite3.connect(DB_PATH)
    try:
        init_history_db(conn)
        rows = [
            (
                run_date,
                p["pool"],
                p["chain"],
                p["project"],
                p["symbol"],
                p["tvlUsd"],
                p["apy"],
                p.get("apyBase") if isinstance(p.get("apyBase"), (int, float)) else None,
                p.get("apyBase7d") if isinstance(p.get("apyBase7d"), (int, float)) else None,
                p.get("_apy_base_median") if isinstance(p.get("_apy_base_median"), (int, float)) else None,
                p["_ratio"],
                p["_spike"],
                (p.get("predictions") or {}).get("predictedClass"),
                p["_score"],
                rank,
            )
            for rank, p in enumerate(pools, start=1)
        ]
        conn.executemany(
            """
            INSERT OR REPLACE INTO snapshots
            (run_date, pool_id, chain, project, symbol, tvl_usd, apy, apy_base, apy_base7d, apy_base_median,
             turnover_ratio, spike_pct, predicted_class, score, rank)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def show_trend(search: str) -> None:
    """Показывает сохранённую историю по пулам, у кого project/symbol содержит
    search (без учёта регистра) — как менялись рейтинг/APY/score по дням."""
    if not DB_PATH.exists():
        print("История пуста — запусти сканер хотя бы раз без --trend, чтобы начать сохранять.")
        return

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT run_date, chain, project, symbol, tvl_usd, apy, turnover_ratio,
                   spike_pct, predicted_class, score, rank
            FROM snapshots
            WHERE project LIKE ? COLLATE NOCASE
               OR symbol LIKE ? COLLATE NOCASE
            ORDER BY run_date ASC, rank ASC
            """,
            (f"%{search}%", f"%{search}%"),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print(f"Ничего не найдено по '{search}' в сохранённой истории.")
        return

    print(f"{'Дата':10s} {'Сеть':10s} {'Проект':16s} {'Пара':18s} {'Место':>6s} "
          f"{'Score':>6s} {'APY':>7s} {'TVL':>13s}")
    print("-" * 90)
    for r in rows:
        print(
            f"{r['run_date']:10s} {r['chain']:10.10s} {r['project']:16.16s} "
            f"{r['symbol']:18.18s} {r['rank']:>6d} {r['score']:>6.1f} "
            f"{r['apy']:>6.1f}% ${r['tvl_usd']:>11,.0f}"
        )


def parse_beat(spec: str, pools: list[dict]) -> dict:
    """Разбирает 'Сеть/project/Пара' и находит этот пул в общем (нефильтрованном)
    списке — чтобы сравнение было по его apy независимо от того, прошёл бы он
    сам текущие фильтры (--min-tvl и т.п.) или нет.

    По chain/project/symbol может совпасть НЕСКОЛЬКО пулов (разные fee-тиры
    того же протокола/пары) — например у Orca SOL-USDC в данных 9 разных
    пулов, TVL от $16k до $26M. Раньше брался первый по порядку ответа API —
    порядок API нигде не гарантирован, так что при следующем запуске первым
    мог бы прийти совсем другой (мелкий) тир, и весь рейтинг "кто обгоняет
    мой пул" молча уехал бы относительно другого эталона без единого
    предупреждения (найдено вторым независимым аудитом логики,
    2026-07-27). Теперь берём детерминированно — пул с максимальным TVL среди
    совпадений, и явно печатаем сколько было совпадений и TVL выбранного."""
    parts = spec.split("/", 2)
    if len(parts) != 3:
        raise SystemExit(
            f"--beat ожидает 'Сеть/project/Пара', например 'Solana/orca-dex/SOL-USDC', получено {spec!r}"
        )
    chain, project, symbol = parts
    matches = [
        p for p in pools
        if p.get("chain") == chain and p.get("project") == project and p.get("symbol") == symbol
    ]
    if not matches:
        raise SystemExit(f"Пул {spec!r} не найден в данных DeFiLlama — проверь написание.")
    chosen = max(matches, key=lambda p: p.get("tvlUsd") or 0)
    if len(matches) > 1:
        print(
            f"Внимание: {len(matches)} пулов с адресом {spec!r} (разные fee-тиры/"
            f"инстансы) — беру с максимальным TVL: ${chosen.get('tvlUsd') or 0:,.0f}"
        )
    return chosen


# Именованные наборы флагов — чтобы не запоминать длинные строки. Preset задаёт
# сеть/--beat/возраст/TVL напрямую (перекрывает эти же флаги, если они тоже
# указаны в командной строке — простое правило вместо путаницы "что главнее").
PRESETS = {
    "mine": {
        "chain": "",
        "beat": "Solana/orca-dex/SOL-USDC",
        "min_age_days": 300,
        # Было $1M против эталона в $26M — оставляло ~треть верхних строк
        # мемкоин-парами на $1-3M (WSOL-USELESS, GIGA-WSOL и т.п.). Поднято
        # до $3M — середина диапазона 3-5M, предложенного вторым независимым
        # аудитом логики (2026-07-27) после проверки на живых
        # данных: на $1M мусора много, на $5M выборка уже слишком узкая.
        "min_tvl": 3_000_000,
        "max_apy": 150,
        "max_apy_spike": 50,
        "fee_only": True,
    },
    "safe": {
        "chain": "",
        "stablecoin": "only",
        "min_age_days": 500,
        "min_tvl": 5_000_000,
    },
    "explore": {
        "chain": "",
        "min_tvl": 200_000,
        "min_age_days": 0,
    },
}


def main() -> None:
    parser = argparse.ArgumentParser(description="DeFi LP-сканер поверх DeFiLlama")
    parser.add_argument(
        "--preset", choices=sorted(PRESETS), default=None,
        help="Готовый набор фильтров вместо ручных флагов: "
        "mine = что обгоняет мой Orca SOL-USDC (не мусор, не новодел); "
        "safe = только крупные стейбл/стейбл пары; "
        "explore = вообще всё, без ограничений",
    )
    parser.add_argument("--chain", default="Solana", help="Сеть (пусто/'' = все сети)")
    parser.add_argument("--min-tvl", type=float, default=1_000_000, help="Минимальный TVL, $")
    parser.add_argument("--top", type=int, default=15, help="Сколько мест показать")
    parser.add_argument(
        "--stablecoin", choices=["only", "exclude", "any"], default="any",
        help="only = только стейбл/стейбл пары, exclude = убрать их, any = не фильтровать",
    )
    parser.add_argument(
        "--exposure", choices=["single", "multi", "any"], default="multi",
        help="multi = LP-пара (по умолчанию), single = обычно стейкинг/лендинг одного токена",
    )
    parser.add_argument(
        "--max-apy-spike", type=float, default=None,
        help="Отсечь пулы, где текущий APY отклоняется от своей 30-дневной нормы "
        "больше чем на X%% В ЛЮБУЮ СТОРОНУ — и разовый всплеск вверх (не "
        "устойчивая доходность), и просадка вниз (доходность реально ухудшилась) "
        "одинаково означают нестабильность",
    )
    parser.add_argument(
        "--min-apy", type=float, default=0.5,
        help="Минимальный APY, %% (отсекает пулы с огромным оборотом, но APY около "
        "нуля — обычно артефакт данных или почти нулевая комиссия пула, не "
        "реальная возможность заработать; 0 чтобы отключить)",
    )
    parser.add_argument(
        "--max-apy", type=float, default=300,
        help="Максимальный APY, %% — фильтр доверия, не просто красивое число: "
        "APY в тысячи процентов почти всегда значит сломанные/манипулируемые "
        "данные, а не реальную доходность (пример: пул с 34000%% APY и при этом "
        "просевший на 85%% от своей 30-дневной нормы — то есть его 'норма' была бы "
        "ещё на порядок безумнее). Отсекается ДО сортировки по --beat, чтобы такой "
        "мусор не мог оказаться на первом месте только из-за огромного номинала; "
        "0 чтобы отключить",
    )
    parser.add_argument(
        "--min-age-days", type=int, default=0,
        help="Минимальный возраст пула в днях (поле 'count' в DeFiLlama — сколько "
        "дней его вообще отслеживают). 0 = не фильтровать",
    )
    parser.add_argument(
        "--max-turnover-ratio", type=float, default=10,
        help="Максимальный дневной оборот относительно TVL, в разах (10 = не "
        "больше 10x TVL в день). Выше обычно значит wash-trading/накрутку "
        "объёма, а не реальный рынок — найдено независимым аудитом логики "
        "на живом примере: QUQ-USDT показывал 41x TVL в день при комиссии "
        "0.01%%, у честных пулов выборки 0.1-7x (у эталона Orca ~0.9x). "
        "0 чтобы отключить",
    )
    parser.add_argument(
        "--beat", metavar="Сеть/project/Пара", default=None,
        help="Показать только пулы с APY выше, чем у указанного (например "
        "'Solana/orca-dex/SOL-USDC') — прямое сравнение со своим пулом",
    )
    parser.add_argument(
        "--fee-only", action="store_true",
        help="Все фильтры/сортировка/--beat считаются по apyBase (только "
        "торговые комиссии), а не по общему apy (комиссии + токен-поощрения "
        "протокола). Честнее сравнивать с Orca, где весь доход — комиссии: "
        "иначе пул может выглядеть 'обгоняет', хотя половина его APY — "
        "временная эмиссия токена, которую могут срезать в любой момент. "
        "Колонка 'Ком/день' в таблице показывает apyBase всегда, независимо "
        "от этого флага",
    )
    parser.add_argument(
        "--rank-by", choices=["median", "week", "day"], default="median",
        help="Как ранжировать комиссионный APY в режиме --fee-only: week = по "
        "apyBase7d (если есть), day = по apyBase, median = медиана apyBase по "
        "последним N дням из chart-эндпоинта (см. --median-days). "
        "Игнорируется без --fee-only.",
    )
    parser.add_argument(
        "--median-days", type=int, default=30, metavar="N",
        help="Сколько последних дней брать для медианы комиссионного APY (apyBase) "
        "из chart-эндпоинта. Используется только в --fee-only и --rank-by median.",
    )
    parser.add_argument(
        "--invest", type=float, default=None, metavar="СУММА",
        help="Показать простой расчёт, сколько бы эта сумма ($) заработала за "
        "год/месяц на каждом из показанных пулов (просто СУММА * APY, без "
        "сложных процентов и без учёта комиссий за вход/выход/газ)",
    )
    parser.add_argument(
        "--notify-summary", action="store_true",
        help="Вместо обычной таблицы — одна короткая строка про пул №1 "
        "(для системных уведомлений/cron, где длинная таблица не влезает)",
    )
    parser.add_argument(
        "--no-save", action="store_true", help="Не сохранять этот запуск в историю"
    )
    parser.add_argument(
        "--trend", metavar="ПОИСК", help="Показать сохранённую историю по пулу "
        "(подстрока в названии проекта или паре, например 'orca' или 'SOL-USDC') "
        "вместо обычного рейтинга"
    )
    args = parser.parse_args()

    if args.preset:
        # Раньше пресет перезаписывал флаги безусловно — нельзя было взять
        # --preset mine и подкрутить один порог поверх (например --min-tvl),
        # не переписывая всё вручную (найдено независимым аудитом логики,
        # 2026-07-27). Теперь пресет применяется только к тем флагам,
        # которые пользователь НЕ указал явно сам в командной строке.
        raw_args = sys.argv[1:]
        applied = {}
        for key, value in PRESETS[args.preset].items():
            flag = "--" + key.replace("_", "-")
            explicitly_set = any(a == flag or a.startswith(flag + "=") for a in raw_args)
            if explicitly_set:
                continue
            setattr(args, key, value)
            applied[key] = value
        print(f"Пресет '{args.preset}': {applied}"
              + (" (часть значений пресета переопределена явными флагами)"
                 if len(applied) < len(PRESETS[args.preset]) else "")
              + "\n")

    if args.trend:
        show_trend(args.trend)
        return

    pools = fetch_pools()
    audits_by_slug = fetch_protocol_audits()

    beat_apy = None
    rank_by = args.rank_by
    median_days = max(int(args.median_days or 30), 1)
    if args.fee_only and rank_by == "median" and median_days < 2:
        raise SystemExit("--median-days должен быть ≥ 2, иначе медиана бессмысленна.")

    conn = None
    if args.fee_only:
        conn = sqlite3.connect(DB_PATH)
        init_history_db(conn)

    if args.beat:
        beat_pool = parse_beat(args.beat, pools)
        beat_day = effective_apy(beat_pool, args.fee_only)
        beat_week = beat_pool.get("apyBase7d")
        if args.fee_only and beat_day is None:
            raise SystemExit(
                f"У эталона {args.beat!r} нет apyBase, а --fee-only включён — "
                f"сравнивать не с чем. Убери --fee-only или выбери другой эталон."
            )
        if args.fee_only and rank_by == "median":
            try:
                assert conn is not None
                did_fetch = ensure_pool_chart_cached(conn, beat_pool["pool"], median_days)
                if did_fetch:
                    time.sleep(CHART_SLEEP_SECONDS)
                beat_med, beat_cnt = apy_base_median_from_cache(conn, beat_pool["pool"], median_days)
            except Exception as e:
                raise SystemExit(f"Не смог посчитать медиану для эталона {args.beat!r}: {e}")
            if beat_med is None:
                raise SystemExit(
                    f"У эталона {args.beat!r} мало данных для медианы: {beat_cnt} дней из {median_days}."
                )
            beat_apy = beat_med
        elif args.fee_only and rank_by == "week":
            if isinstance(beat_week, (int, float)):
                beat_apy = float(beat_week)
            else:
                print(
                    f"Предупреждение: у эталона {args.beat!r} нет apyBase7d, поэтому "
                    f"сравнение 'неделя против недели' невозможно. Откатываюсь на "
                    f"сравнение по дневным комиссиям (apyBase) для ВСЕХ в этом запуске "
                    f"(эквивалентно --rank-by day).\n"
                )
                rank_by = "day"
                beat_apy = beat_day
        else:
            beat_apy = beat_day

        if beat_apy is None:
            # Возможен только когда args.fee_only=False и у пула нет apy/нечисловой apy
            raise SystemExit(
                f"У эталона {args.beat!r} нет APY — сравнивать не с чем. "
                f"Выбери другой эталон."
            )
        apy_kind = (
            (f"медиана комиссий за {median_days}д (apyBase)" if (args.fee_only and rank_by == "median") else
             ("комиссии за 7д (apyBase7d)" if (args.fee_only and rank_by == "week") else
              ("только комиссии (apyBase)" if args.fee_only else "полный APY")))
        )
        print(
            f"Сравниваю с {args.beat}: {apy_kind}={beat_apy:.1f}%. Порядок такой: сначала "
            f"фильтры доверия (возраст/TVL/и т.п.) отсеивают мусор, ПОТОМ среди "
            f"оставшихся сортирую по разнице с этим значением — сверху то, что "
            f"обгоняет сильнее всего, ниже — то, что близко, но пока хуже. Ничего не "
            f"скрываю, просто ранжирую.\n"
        )

    # Порядок фильтров — намеренно "доверие сначала": возраст/TVL/минимальный APY
    # прежде, чем вообще сравнивать с эталоном. Пул моложе --min-age-days или с
    # тонким TVL отсекается независимо от того, насколько высокий у него APY.
    # Пулы, отсечённые ТОЛЬКО потолком max_apy, но при этом выглядящие стабильно
    # (маленький spike относительно своей же нормы) — не выкидываем молча,
    # показываем отдельным коротким списком "посмотреть глазами" после
    # основного рейтинга. Найдено вторым независимым аудитом логики
    # (2026-07-27): потолок 150% отрезал живой стабильный
    # кандидат (Aerodrome USDC-CBBTC, apyBase 239%, spike всего -5%) вместе с
    # реальным мусором — потолок нужен, но не должен прятать пограничные случаи.
    borderline_high_apy = []
    low_median_data = []

    filtered = []
    for p in pools:
        if not is_usable_pool(p):
            continue
        if args.chain and p.get("chain") != args.chain:
            continue
        if (p.get("tvlUsd") or 0) < args.min_tvl:
            continue
        if args.exposure != "any" and p.get("exposure") != args.exposure:
            continue
        if args.stablecoin == "only" and not p.get("stablecoin"):
            continue
        if args.stablecoin == "exclude" and p.get("stablecoin"):
            continue

        eff_apy = effective_apy(p, args.fee_only)
        if eff_apy is None:
            continue
        if eff_apy < args.min_apy:
            continue
        if args.max_apy and eff_apy > args.max_apy:
            borderline_spike = apy_spike_pct(p, eff_apy, args.fee_only)
            if borderline_spike is not None and abs(borderline_spike) <= 25:
                p["_eff_apy"] = eff_apy
                p["_spike"] = borderline_spike
                borderline_high_apy.append(p)
            continue
        if args.min_age_days and (p.get("count") or 0) < args.min_age_days:
            continue

        # Раньше отсутствие volumeUsd7d значило "выкинуть пул полностью" — но это
        # исключает не только реальный мусор, а и легитимные крупные LP-пары
        # (Convex-обёртки над Curve, GMX-перпетуалы, Morpho — механика дохода не
        # через своп-объём, поэтому DeFiLlama просто не даёт эту метрику; найдено
        # независимым аудитом логики, 2026-07-27: 162 крупных/старых
        # exposure=multi пула теряются только по этой причине). ratio=None
        # теперь просто идёт дальше с нейтральной оценкой в score, не отсекается.
        ratio = daily_turnover_ratio(p)
        if args.max_turnover_ratio and ratio is not None and ratio > args.max_turnover_ratio:
            continue

        spike = apy_spike_pct(p, eff_apy, args.fee_only)
        # abs(), не просто spike — раньше резался только всплеск ВВЕРХ от своей
        # 30-дневной нормы, а провал ВНИЗ (доходность реально просела) спокойно
        # проходил. Поймано на живом примере: Aerodrome WETH-USDC просел с 51.6%
        # до 25.2% (-48% от нормы) и прошёл фильтр только потому, что -48 не
        # больше +50 (найдено независимым аудитом логики, 2026-07-27).
        if args.max_apy_spike is not None and spike is not None and abs(spike) > args.max_apy_spike:
            continue

        p["_ratio"] = ratio
        p["_spike"] = spike
        p["_eff_apy"] = eff_apy
        p["_audits"] = audits_by_slug.get(p.get("project"), 0)

        # Медиана комиссий по истории (chart) — считаем для всех fee_only, чтобы
        # показывать рядом с day/week и (при rank_by=median) ранжировать по ней.
        if args.fee_only:
            try:
                assert conn is not None
                did_fetch = ensure_pool_chart_cached(conn, p["pool"], median_days)
                if did_fetch:
                    time.sleep(CHART_SLEEP_SECONDS)
                med, cnt = apy_base_median_from_cache(conn, p["pool"], median_days)
                p["_apy_base_median"] = med
                p["_apy_base_median_days"] = cnt
            except Exception as e:
                print(f"Предупреждение: не смог загрузить chart для пула {p.get('pool')}: {e}", file=sys.stderr)
                p["_apy_base_median"] = None
                p["_apy_base_median_days"] = 0

            if rank_by == "median" and p["_apy_base_median"] is None:
                low_median_data.append(p)
                continue

        if beat_apy is not None:
            if args.fee_only and rank_by == "median":
                metric = p.get("_apy_base_median")
            elif args.fee_only and rank_by == "week":
                metric = p.get("apyBase7d") if isinstance(p.get("apyBase7d"), (int, float)) else None
            elif args.fee_only and rank_by == "day":
                metric = eff_apy
            else:
                metric = eff_apy
            p["_vs_beat"] = (float(metric) - beat_apy) if isinstance(metric, (int, float)) else None

        filtered.append(p)

    if not filtered:
        print("Ничего не прошло фильтры доверия.")
        return

    compute_scores(filtered)
    if beat_apy is not None:
        # Внутри уже доверенного набора — сортировка по отрыву от эталона, а не
        # по общему score: тут важнее конкретно "выше/ближе к моему APY", а не
        # оборот/TVL сами по себе.
        filtered.sort(
            key=lambda p: (
                p.get("_vs_beat") is None,
                -(float(p["_vs_beat"]) if isinstance(p.get("_vs_beat"), (int, float)) else 0.0),
            )
        )
    else:
        filtered.sort(key=lambda p: p["_score"], reverse=True)
    top = filtered[: args.top]

    if not args.no_save:
        save_snapshot(filtered, date.today().isoformat())

    if args.notify_summary:
        if not top:
            print("DeFi-сканер: сегодня пусто, ничего не прошло фильтры.")
        else:
            p = top[0]
            vs = f", {p['_vs_beat']:+.0f}% к эталону" if beat_apy is not None else ""
            print(
                f"#1 {p['project']}/{p['symbol']}: {p['_eff_apy']:.0f}% комиссий"
                f"{vs} (TVL ${p['tvlUsd']:,.0f})"
            )
        return

    print(f"Рейтинг LP-пулов на {datetime.now():%Y-%m-%d %H:%M} "
          f"(из {len(filtered)} пулов после фильтров доверия)\n")
    vs_col = f"{'vs эталон':>10s} " if beat_apy is not None else ""
    print(
        f"{'#':>3s} {'Сеть':9s} {'Проект':16s} {'Пара':18s} {vs_col}{'Score':>6s} "
        f"{'TVL':>12s} {'APY':>7s} {'Ком/день':>8s} {'Ком/нед':>8s} {'Мед':>8s} {'д/мед':>6s} {'30д':>6s} "
        f"{'Возр':>5s} {'IL':>4s} {'Ауд':>4s} {'Прогноз':>9s}"
    )
    print("-" * (148 + (11 if beat_apy is not None else 0)))
    for rank, p in enumerate(top, start=1):
        spike = p["_spike"]
        spike_str = f"{spike:+.0f}%" if spike is not None else "?"
        pred = (p.get("predictions") or {}).get("predictedClass") or "?"
        age_days = p.get("count") or 0
        # .get(key, default) подставляет default только если КЛЮЧА нет — если
        # ilRisk присутствует, но равен null (Python None), .get вернёт None, а
        # не '?', и формат :>4s упадёт. Нужно "или", а не второй аргумент .get()
        # (найдено независимым аудитом, 2026-07-27).
        il_risk = p.get("ilRisk") or "?"
        if beat_apy is not None:
            vs_val = p.get("_vs_beat")
            vs_str = f"{vs_val:>+9.1f}% " if isinstance(vs_val, (int, float)) else f"{'?':>10s} "
        else:
            vs_str = ""
        # apyBase — сколько из APY реально комиссии, а не токен-поощрения
        # протокола; показываем ВСЕГДА, независимо от --fee-only, чтобы разрыв
        # был виден даже когда фильтры считаются по полному apy.
        apy_base = p.get("apyBase")
        apy_base_str = f"{apy_base:>6.1f}%" if isinstance(apy_base, (int, float)) else "      ?"
        apy_base7d = p.get("apyBase7d")
        apy_base7d_str = f"{apy_base7d:>6.1f}%" if isinstance(apy_base7d, (int, float)) else "      ?"
        med = p.get("_apy_base_median")
        med_str = f"{med:>6.1f}%" if isinstance(med, (int, float)) else "      ?"
        # Отношение "сегодня / медиана" — главный индикатор всплеска, ради которого
        # вся эта затея. Около 1.0 — доходность настоящая; заметно выше — сегодня
        # повезло с объёмом (так мы 27.07 выбрали Aerodrome по 62.4% при реальных
        # ~21-23%); заметно ниже — доходность затухает. Считаем сами, а не заставляем
        # человека делить в уме две соседние колонки.
        if isinstance(apy_base, (int, float)) and isinstance(med, (int, float)) and med > 0.05:
            ratio = apy_base / med
            ratio_str = f"{ratio:>5.1f}x" if ratio < 100 else "  >99x"
        elif isinstance(apy_base, (int, float)) and apy_base > 0.05:
            # медиана ~нулевая, а сегодня доходность есть — всплеск на пустом месте
            ratio_str = "    ∞"
        else:
            ratio_str = "    ?"
        print(
            f"{rank:>3d} {p['chain']:9.9s} {p['project']:16.16s} {p['symbol']:18.18s} "
            f"{vs_str}{p['_score']:>6.1f} ${p['tvlUsd']:>10,.0f} {p['apy']:>6.1f}% "
            f"{apy_base_str} {apy_base7d_str} {med_str} {ratio_str} {spike_str:>6s} "
            f"{age_days:>4d}д {il_risk:>4s} {p['_audits']:>4d} {pred:>9s}"
        )

    print(
        "\nScore = 50% место по обороту/TVL + 30% стабильность APY (близость к "
        "30-дневной норме) + 20% размер TVL."
    )
    print("д/мед = во сколько раз сегодняшняя доходность выше своей же медианы. "
          "Около 1.0 — доходность настоящая. Заметно больше — сегодня просто повезло с "
          "объёмом торгов, завтра так не будет (по такому пику 27.07 был выбран пул "
          "Aerodrome с 62.4%, реально дающий ~21-23%). Заметно меньше 1.0 — доходность "
          "затухает. '∞' — медиана почти нулевая, то есть всплеск на ровном месте.")
    print("Ком/день = apyBase, Ком/нед = apyBase7d — сколько из APY реально торговые "
          "комиссии (apyReward = APY - apyBase). Мед = медиана apyBase по chart-истории "
          f"за последние {median_days}д (если --fee-only). Без --fee-only фильтры/сортировка "
          "считаются по полному APY — эти колонки просто показывают разрыв, ничего "
          "не отсекая сами по себе.")
    print("Возр = сколько дней DeFiLlama вообще отслеживает этот пул. "
          "Ауд = число аудитов ПРОТОКОЛА по данным DeFiLlama — 0 может значить "
          "'не занесено в базу', а не 'точно не проверялся' (например у Orca и "
          "Raydium тут 0, хотя оба реально аудировались).")
    # Раньше эта строка печаталась безусловно — то есть при --no-save инструмент
    # сообщал о сохранении, которого не было. Врать о собственных действиях нельзя.
    if args.no_save:
        print("Снимок НЕ сохранён (--no-save). "
              "--trend 'подстрока' — история, --beat 'Сеть/project/Пара' — сравнить со своим.")
    else:
        print("Снимок сохранён в history.db (--no-save чтобы не писать, "
              "--trend 'подстрока' — история, --beat 'Сеть/project/Пара' — сравнить со своим).")

    if args.fee_only and low_median_data:
        print(
            f"\nМало данных для медианы (нужно ≥ {median_days/2:.0f} дней из {median_days}) — "
            "не в рейтинге при --rank-by median:\n"
        )
        for p in sorted(low_median_data, key=lambda x: x.get("apyBase") or 0, reverse=True)[:15]:
            apy_base = p.get("apyBase")
            apy_base_str = f"{apy_base:.1f}%" if isinstance(apy_base, (int, float)) else "?"
            have = int(p.get("_apy_base_median_days") or 0)
            print(
                f"  {p['chain']:9.9s} {p['project']:16.16s} {p['symbol']:18.18s} "
                f"Ком/день={apy_base_str:>6s} дней={have:>2d}/{median_days} TVL=${p['tvlUsd']:,.0f}"
            )

    if borderline_high_apy:
        print(
            f"\nОтсечены потолком --max-apy={args.max_apy}%, но выглядят стабильно "
            f"(|отклонение от нормы| ≤25%) — не в рейтинге и не в score, просто "
            f"посмотреть глазами, вдруг зря отсечены:\n"
        )
        for p in sorted(borderline_high_apy, key=lambda x: x["_eff_apy"]):
            print(
                f"  {p['chain']:9.9s} {p['project']:16.16s} {p['symbol']:18.18s} "
                f"APY={p['_eff_apy']:.1f}% TVL=${p['tvlUsd']:,.0f} "
                f"отклонение={p['_spike']:+.0f}%"
            )

    if args.invest:
        basis = "только комиссии, apyBase" if args.fee_only else "полный APY"
        print(f"\nПростой расчёт для ${args.invest:,.0f} ({basis}; сумма * APY, без "
              f"сложных процентов, без комиссий за вход/выход/газ — только ориентир):\n")
        print(f"{'#':>3s} {'Проект':16s} {'Пара':18s} {'APY':>7s} {'в год':>12s} {'в месяц':>10s}")
        print("-" * 70)
        for rank, p in enumerate(top, start=1):
            per_year = args.invest * p["_eff_apy"] / 100
            per_month = per_year / 12
            print(
                f"{rank:>3d} {p['project']:16.16s} {p['symbol']:18.18s} "
                f"{p['_eff_apy']:>6.1f}% ${per_year:>10,.0f} ${per_month:>8,.0f}"
            )

    if conn is not None:
        conn.close()


if __name__ == "__main__":
    main()
