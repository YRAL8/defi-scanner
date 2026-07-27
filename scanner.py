#!/usr/bin/env python3
"""
DeFi LP-сканер: тянет данные с DeFiLlama (yields.llama.fi/pools, бесплатный
публичный API, без ключа) и ранжирует пулы по объёму торгов относительно TVL —
это то, что реально определяет доходность LP-позиции, а не сырой APY (тонкий
пул с большим APY часто временное явление, см. обсуждение стратегии для
orca-lp-bot). TVL, APY, объём, exposure (LP-пара vs стейкинг) и IL-риск —
всё уже готовые поля в ответе API, скрапинг сайтов не нужен.
"""
import argparse

import requests

POOLS_URL = "https://yields.llama.fi/pools"


def fetch_pools() -> list[dict]:
    resp = requests.get(POOLS_URL, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "success":
        raise RuntimeError(f"DeFiLlama API вернул статус {data.get('status')!r}")
    return data["data"]


def daily_turnover_ratio(pool: dict) -> float | None:
    """Средний дневной оборот / TVL за последнюю неделю — устойчивее к разовым
    всплескам объёма за один день, чем voumeUsd1d."""
    tvl = pool.get("tvlUsd") or 0
    vol7d = pool.get("volumeUsd7d")
    if not tvl or vol7d is None:
        return None
    return (vol7d / 7) / tvl


def main() -> None:
    parser = argparse.ArgumentParser(description="DeFi LP-сканер поверх DeFiLlama")
    parser.add_argument("--chain", default="Solana", help="Сеть (пусто/'' = все сети)")
    parser.add_argument("--min-tvl", type=float, default=1_000_000, help="Минимальный TVL, $")
    parser.add_argument("--top", type=int, default=20, help="Сколько строк показать")
    parser.add_argument(
        "--stablecoin", choices=["only", "exclude", "any"], default="any",
        help="only = только стейбл/стейбл пары, exclude = убрать их, any = не фильтровать",
    )
    parser.add_argument(
        "--exposure", choices=["single", "multi", "any"], default="multi",
        help="multi = LP-пара (по умолчанию), single = обычно стейкинг/лендинг одного токена",
    )
    args = parser.parse_args()

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

        ratio = daily_turnover_ratio(p)
        if ratio is None:
            continue
        p["_ratio"] = ratio
        filtered.append(p)

    filtered.sort(key=lambda p: p["_ratio"], reverse=True)

    print(
        f"{'Проект':16s} {'Пара':22s} {'TVL':>13s} {'APY':>7s} "
        f"{'об/TVL в день':>14s} {'IL':>4s}"
    )
    print("-" * 84)
    for p in filtered[: args.top]:
        print(
            f"{p['project']:16.16s} {p['symbol']:22.22s} "
            f"${p['tvlUsd']:>11,.0f} {p['apy']:>6.1f}% "
            f"{p['_ratio'] * 100:>13.1f}% {p.get('ilRisk', '?'):>4s}"
        )

    print(f"\nВсего пулов после фильтров: {len(filtered)} (показаны первые {args.top})")


if __name__ == "__main__":
    main()
