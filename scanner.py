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
"""
import argparse
import math
import sqlite3
from datetime import date, datetime
from pathlib import Path

import requests

POOLS_URL = "https://yields.llama.fi/pools"
DB_PATH = Path(__file__).parent / "history.db"


def fetch_pools() -> list[dict]:
    resp = requests.get(POOLS_URL, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "success":
        raise RuntimeError(f"DeFiLlama API вернул статус {data.get('status')!r}")
    return data["data"]


def daily_turnover_ratio(pool: dict) -> float | None:
    """Средний дневной оборот / TVL за последнюю неделю — устойчивее к разовым
    всплескам объёма за один день, чем volumeUsd1d."""
    tvl = pool.get("tvlUsd") or 0
    vol7d = pool.get("volumeUsd7d")
    if not tvl or vol7d is None:
        return None
    return (vol7d / 7) / tvl


def apy_spike_pct(pool: dict) -> float | None:
    """На сколько % текущий APY выше среднего за 30 дней — большое положительное
    значение обычно значит разовый всплеск (например памп объёма вчера), а не
    устойчивую доходность; отрицательное — текущий APY просел ниже своей нормы."""
    apy = pool.get("apy")
    mean30d = pool.get("apyMean30d")
    if apy is None or not mean30d:
        return None
    return (apy - mean30d) / mean30d * 100


def compute_scores(pools: list[dict]) -> None:
    """Проставляет pool['_score'] (0-100) каждому пулу в списке — нормализация
    только внутри ЭТОГО набора (после фильтров), не абсолютная шкала.

    Формула (веса выбраны просто и прозрачно, не подгонялись под данные):
      50% — место по обороту/TVL относительно остальных в выборке
      30% — стабильность = 1 - |отклонение APY от 30д-нормы|, ограничено [0,1]
      20% — размер TVL в лог-шкале относительно остальных в выборке
    """
    ratios = [p["_ratio"] for p in pools]
    tvls = [math.log10(p["tvlUsd"]) for p in pools]
    r_min, r_max = min(ratios), max(ratios)
    t_min, t_max = min(tvls), max(tvls)

    for p in pools:
        ratio_norm = (p["_ratio"] - r_min) / (r_max - r_min) if r_max > r_min else 0.5
        tvl_norm = (
            (math.log10(p["tvlUsd"]) - t_min) / (t_max - t_min) if t_max > t_min else 0.5
        )
        spike = p["_spike"]
        stability_norm = 1.0 - min(abs(spike) / 100, 1.0) if spike is not None else 0.5

        p["_score"] = round(
            100 * (0.5 * ratio_norm + 0.3 * stability_norm + 0.2 * tvl_norm), 1
        )


def save_snapshot(pools: list[dict], run_date: str) -> None:
    conn = sqlite3.connect(DB_PATH)
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
            turnover_ratio REAL,
            spike_pct REAL,
            predicted_class TEXT,
            score REAL,
            rank INTEGER,
            PRIMARY KEY (run_date, pool_id)
        )
        """
    )
    rows = [
        (
            run_date,
            p["pool"],
            p["chain"],
            p["project"],
            p["symbol"],
            p["tvlUsd"],
            p["apy"],
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
        (run_date, pool_id, chain, project, symbol, tvl_usd, apy, turnover_ratio,
         spike_pct, predicted_class, score, rank)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    conn.close()


def show_trend(search: str) -> None:
    """Показывает сохранённую историю по пулам, у кого project/symbol содержит
    search (без учёта регистра) — как менялись рейтинг/APY/score по дням."""
    if not DB_PATH.exists():
        print("История пуста — запусти сканер хотя бы раз без --trend, чтобы начать сохранять.")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT run_date, chain, project, symbol, tvl_usd, apy, turnover_ratio,
               spike_pct, predicted_class, score, rank
        FROM snapshots
        WHERE project LIKE ? OR symbol LIKE ?
        ORDER BY run_date ASC, rank ASC
        """,
        (f"%{search}%", f"%{search}%"),
    ).fetchall()
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


def main() -> None:
    parser = argparse.ArgumentParser(description="DeFi LP-сканер поверх DeFiLlama")
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
        help="Отсечь пулы, где текущий APY выше среднего за 30д больше чем на X%% "
        "(защита от разовых всплесков объёма, не устойчивой доходности)",
    )
    parser.add_argument(
        "--min-apy", type=float, default=0.5,
        help="Минимальный APY, %% (отсекает пулы с огромным оборотом, но APY около "
        "нуля — обычно артефакт данных или почти нулевая комиссия пула, не "
        "реальная возможность заработать; 0 чтобы отключить)",
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

    if args.trend:
        show_trend(args.trend)
        return

    pools = fetch_pools()

    filtered = []
    for p in pools:
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
        if (p.get("apy") or 0) < args.min_apy:
            continue

        ratio = daily_turnover_ratio(p)
        if ratio is None:
            continue

        spike = apy_spike_pct(p)
        if args.max_apy_spike is not None and spike is not None and spike > args.max_apy_spike:
            continue

        p["_ratio"] = ratio
        p["_spike"] = spike
        filtered.append(p)

    if not filtered:
        print("Ничего не прошло фильтры.")
        return

    compute_scores(filtered)
    filtered.sort(key=lambda p: p["_score"], reverse=True)
    top = filtered[: args.top]

    if not args.no_save:
        save_snapshot(filtered, date.today().isoformat())

    print(f"Рейтинг LP-пулов на {datetime.now():%Y-%m-%d %H:%M} "
          f"(из {len(filtered)} пулов после фильтров)\n")
    print(
        f"{'#':>3s} {'Сеть':10s} {'Проект':16s} {'Пара':20s} {'Score':>6s} "
        f"{'TVL':>13s} {'APY':>7s} {'30д':>6s} {'Прогноз':>10s}"
    )
    print("-" * 100)
    for rank, p in enumerate(top, start=1):
        spike = p["_spike"]
        spike_str = f"{spike:+.0f}%" if spike is not None else "?"
        pred = (p.get("predictions") or {}).get("predictedClass") or "?"
        print(
            f"{rank:>3d} {p['chain']:10.10s} {p['project']:16.16s} {p['symbol']:20.20s} "
            f"{p['_score']:>6.1f} ${p['tvlUsd']:>11,.0f} {p['apy']:>6.1f}% "
            f"{spike_str:>6s} {pred:>10s}"
        )

    print(
        "\nScore = 50% место по обороту/TVL + 30% стабильность APY (близость к "
        "30-дневной норме) + 20% размер TVL. Снимок сохранён в history.db "
        "(--no-save чтобы не писать, --trend 'подстрока' чтобы посмотреть историю)."
    )


if __name__ == "__main__":
    main()
