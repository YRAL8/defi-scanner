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
import sys
from datetime import date, datetime
from pathlib import Path

import requests

POOLS_URL = "https://yields.llama.fi/pools"
PROTOCOLS_URL = "https://api.llama.fi/protocols"
DB_PATH = Path(__file__).parent / "history.db"


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
        "0.01%, у честных пулов выборки 0.1-7x (у эталона Orca ~0.9x). "
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
        "Колонка 'Комис.' в таблице показывает apyBase всегда, независимо "
        "от этого флага",
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
    if args.beat:
        beat_pool = parse_beat(args.beat, pools)
        beat_apy = effective_apy(beat_pool, args.fee_only)
        if beat_apy is None:
            raise SystemExit(
                f"У эталона {args.beat!r} нет apyBase, а --fee-only включён — "
                f"сравнивать не с чем. Убери --fee-only или выбери другой эталон."
            )
        apy_kind = "только комиссии (apyBase)" if args.fee_only else "полный APY"
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
        if beat_apy is not None:
            p["_vs_beat"] = eff_apy - beat_apy
        filtered.append(p)

    if not filtered:
        print("Ничего не прошло фильтры доверия.")
        return

    compute_scores(filtered)
    if beat_apy is not None:
        # Внутри уже доверенного набора — сортировка по отрыву от эталона, а не
        # по общему score: тут важнее конкретно "выше/ближе к моему APY", а не
        # оборот/TVL сами по себе.
        filtered.sort(key=lambda p: p["_vs_beat"], reverse=True)
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
        f"{'TVL':>12s} {'APY':>7s} {'Комис.':>7s} {'30д':>6s} {'Возр':>5s} {'IL':>4s} {'Ауд':>4s} {'Прогноз':>9s}"
    )
    print("-" * (123 + (11 if beat_apy is not None else 0)))
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
        vs_str = f"{p['_vs_beat']:>+9.1f}% " if beat_apy is not None else ""
        # apyBase — сколько из APY реально комиссии, а не токен-поощрения
        # протокола; показываем ВСЕГДА, независимо от --fee-only, чтобы разрыв
        # был виден даже когда фильтры считаются по полному apy.
        apy_base = p.get("apyBase")
        apy_base_str = f"{apy_base:>6.1f}%" if isinstance(apy_base, (int, float)) else "     ?"
        print(
            f"{rank:>3d} {p['chain']:9.9s} {p['project']:16.16s} {p['symbol']:18.18s} "
            f"{vs_str}{p['_score']:>6.1f} ${p['tvlUsd']:>10,.0f} {p['apy']:>6.1f}% "
            f"{apy_base_str} {spike_str:>6s} {age_days:>4d}д {il_risk:>4s} "
            f"{p['_audits']:>4d} {pred:>9s}"
        )

    print(
        "\nScore = 50% место по обороту/TVL + 30% стабильность APY (близость к "
        "30-дневной норме) + 20% размер TVL."
    )
    print("Комис. = apyBase — сколько из APY реально торговые комиссии, а не "
          "токен-поощрения протокола (apyReward = APY - Комис.). Без --fee-only "
          "фильтры/сортировка считаются по полному APY — эта колонка просто "
          "показывает разрыв, ничего не отсекая сама по себе.")
    print("Возр = сколько дней DeFiLlama вообще отслеживает этот пул. "
          "Ауд = число аудитов ПРОТОКОЛА по данным DeFiLlama — 0 может значить "
          "'не занесено в базу', а не 'точно не проверялся' (например у Orca и "
          "Raydium тут 0, хотя оба реально аудировались).")
    print("Снимок сохранён в history.db (--no-save чтобы не писать, "
          "--trend 'подстрока' — история, --beat 'Сеть/project/Пара' — сравнить со своим).")

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


if __name__ == "__main__":
    main()
