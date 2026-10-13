"""python -m troll_poly_bot --balance 100

Paper trading by default: real Polymarket data, simulated fills.

    --live            real account, DRY RUN: authenticates, reconciles, signs
                      every order the strategy wants and posts none of them
    --live --armed    posts real fill-or-kill orders. Requires
                      TPB_LIVE_ACK=I_UNDERSTAND_REAL_MONEY in the environment.

Credentials come from the environment (a local .env is loaded), never from
flags: TPB_POLY_PRIVATE_KEY (the signer), TPB_POLY_WALLET (the account wallet
shown on your profile) and optionally TPB_POLY_RELAYER_API_KEY(+_ADDRESS) so
the bot can redeem winners. The venue reports the wallet type itself.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

from .config import BotConfig
from .live import LiveBot


def _build_live_exchange(args, cfg: BotConfig):
    from dotenv import load_dotenv
    load_dotenv()
    logging.getLogger("httpx").setLevel(logging.WARNING)      # the SDK logs every request at INFO
    from .execution.paper import FeeModel
    from .execution.polymarket import (
        ACK_PHRASE, LiveCaps, LiveConfigError, LiveCredentials, PolymarketExchange,
    )
    from .signals.costs import FeeSchedule

    armed = bool(args.armed)
    if armed and os.getenv("TPB_LIVE_ACK", "").strip() != ACK_PHRASE:
        raise SystemExit(
            f"refusing to arm: set TPB_LIVE_ACK={ACK_PHRASE} in the environment (or .env) "
            "to confirm you want real orders placed. Without --armed this is a dry run.")
    try:
        creds = LiveCredentials.from_env()
        client = creds.build_client()
    except LiveConfigError as exc:
        raise SystemExit(f"live setup: {exc}") from exc
    except Exception as exc:                              # noqa: BLE001
        raise SystemExit(f"could not log in to the venue: {type(exc).__name__}: {exc}") from exc
    caps = LiveCaps(max_order_usdc=args.max_order_usdc, max_open_usdc=args.max_open_usdc,
                    max_daily_loss_usdc=args.max_daily_loss, max_orders_per_hour=args.max_orders_per_hour,
                    kill_file=args.kill_file)
    wallet = str(getattr(client, "wallet", "") or creds.wallet or "")
    # TPB_LIVE_ORDER_LANES: parallel threads that sign and POST fill-or-kill orders the moment
    # they are decided (0 = the original single worker; docs/latency_2026-09-29.md)
    try:
        lanes = max(0, min(8, int(os.getenv("TPB_LIVE_ORDER_LANES", "0") or 0)))
    except ValueError:
        lanes = 0
    exchange = PolymarketExchange(client, FeeModel.from_schedule(FeeSchedule()), bankroll=args.balance,
                                  caps=caps, armed=armed, wallet=wallet, can_redeem=creds.can_redeem,
                                  order_lanes=lanes)
    try:
        info = exchange.authenticate()
    except LiveConfigError as exc:
        raise SystemExit(f"live setup: {exc}") from exc
    except Exception as exc:                              # noqa: BLE001
        raise SystemExit(f"could not read the account from the venue: {type(exc).__name__}: {exc}") from exc
    cfg.mode = "live"
    cfg.live.armed = armed
    banner = "REAL MONEY -- ORDERS WILL BE POSTED" if armed else "DRY RUN -- orders are signed but never posted"
    allow = info["allowance"]
    allow_txt = "n/a" if allow is None else ("unlimited" if allow > 1e12 else f"{allow:.2f}")
    print("=" * 72)
    print(f"  LIVE ACCOUNT  {banner}")
    signer = str(getattr(client, "signer", "") or "")
    print(f"  wallet {info['address']}  type {info['wallet_type'] or '?'}  "
          f"signer ...{signer[-6:]}  api creds {creds.redacted()['api_creds']}")
    print(f"  venue balance {info['venue_balance']:.2f} USDC  allowance {allow_txt}  bankroll {info['bankroll']:.2f}")
    print(f"  caps: {caps.max_order_usdc:.2f}/order  {caps.max_open_usdc:.2f} open  "
          f"{caps.max_daily_loss_usdc:.2f} daily loss  {caps.max_orders_per_hour}/hour  kill file {caps.kill_file}")
    if creds.can_redeem:
        print("  redemption: automatic through your Relayer API key")
    else:
        print("  redemption: no Relayer API key in .env -> turn on Auto-Redeem in the Polymarket app, or add")
        print("              TPB_POLY_RELAYER_API_KEY and TPB_POLY_RELAYER_API_KEY_ADDRESS (Settings -> API Keys)")
    print("=" * 72)
    return exchange


def resolve_mode(args, env: dict | None = None) -> str:
    """Map the flags onto one deployment mode and enforce what it implies.

        paper       no real account
        shadow      --live, orders signed, never posted
        micro-live  --live --armed; caps clamped to MicroCaps whatever was asked
        live        --live --armed --mode live; refused unless graduation.assess()
                    passes or TPB_GRADUATION_OVERRIDE carries the override phrase

    The default for ``--live --armed`` is micro-live: real capital does not
    scale because a flag was typed, it scales because the evidence says so.
    """
    from .graduation import MicroCaps, assess, override_present

    env = os.environ if env is None else env
    mode = args.mode
    if mode is None:
        mode = "micro-live" if (args.live and args.armed) else "shadow" if args.live else "paper"
    if mode == "paper":
        if args.live or args.armed:
            raise SystemExit("--mode paper cannot be combined with --live/--armed")
    elif mode == "shadow":
        if args.armed:
            raise SystemExit("--mode shadow means orders are never posted; drop --armed")
        args.live = True
    elif mode == "micro-live":
        args.live = args.armed = True
        micro = MicroCaps.from_env(env)
        clamped = []
        for name, cap in (("max_order_usdc", micro.max_order_usdc), ("max_open_usdc", micro.max_open_usdc),
                          ("max_daily_loss", micro.max_daily_loss_usdc),
                          ("max_orders_per_hour", micro.max_orders_per_hour)):
            if getattr(args, name) > cap:
                setattr(args, name, cap)
                clamped.append(f"{name}={cap}")
        if clamped:
            logging.getLogger("live").warning("MICRO-LIVE caps applied: %s", ", ".join(clamped))
    elif mode == "live":
        args.live = args.armed = True
        verdict = assess(strategy=getattr(args, "strategy", None) or env.get("TPB_STRATEGY") or "analytic")
        if not verdict.ready and not override_present(env):
            raise SystemExit(
                "refusing --mode live: the graduation criteria are not met -> "
                + "; ".join(verdict.reasons)
                + f" (live epochs so far: {verdict.stats}). Run --mode micro-live, or set "
                  "TPB_GRADUATION_OVERRIDE=I_ACCEPT_UNPROVEN_EDGE to override explicitly.")
        if not verdict.ready:
            logging.getLogger("live").error("GRADUATION OVERRIDDEN by the operator: %s", "; ".join(verdict.reasons))
    args.mode = mode
    return mode


def check_spot_lag(strategy: str, mode: str, env: dict | None = None) -> None:
    """spot_lag is doubly gated: TPB_SPOT_LAG_ENABLED=1 for any mode, and
    TPB_SPOT_LAG_ALLOW_REAL=1 as well before it may place real orders. Turning
    it on for paper must never be enough to turn it on for money."""
    if strategy != "spot_lag":
        return
    env = os.environ if env is None else env
    on = lambda k: (env.get(k) or "").strip().lower() in ("1", "true", "yes")   # noqa: E731
    if not on("TPB_SPOT_LAG_ENABLED"):
        raise SystemExit("the spot_lag strategy is behind a feature flag: set TPB_SPOT_LAG_ENABLED=1")
    if mode in ("micro-live", "live") and not on("TPB_SPOT_LAG_ALLOW_REAL"):
        raise SystemExit(f"refusing spot_lag in {mode}: real orders need TPB_SPOT_LAG_ALLOW_REAL=1 as well "
                         "(an explicit decision; the strategy is not proven on real fills)")


def check_repriced_momentum(strategy: str, mode: str, env: dict | None = None) -> None:
    """repriced_momentum is doubly gated like spot_lag: TPB_REPRICED_MOM_ENABLED=1 for
    any mode, and TPB_REPRICED_MOM_ALLOW_REAL=1 as well before it may place real
    orders. Its research verdict is negative (docs/repriced_momentum_2026-09-24.md);
    paper is where it belongs."""
    if strategy != "repriced_momentum":
        return
    env = os.environ if env is None else env
    on = lambda k: (env.get(k) or "").strip().lower() in ("1", "true", "yes")   # noqa: E731
    if not on("TPB_REPRICED_MOM_ENABLED"):
        raise SystemExit("the repriced_momentum strategy is behind a feature flag: set TPB_REPRICED_MOM_ENABLED=1")
    if mode in ("micro-live", "live") and not on("TPB_REPRICED_MOM_ALLOW_REAL"):
        raise SystemExit(f"refusing repriced_momentum in {mode}: real orders need TPB_REPRICED_MOM_ALLOW_REAL=1 as "
                         "well (an explicit decision; the out-of-sample research found no edge)")


def check_cheapside(strategy: str, mode: str, env: dict | None = None) -> None:
    """cheapside is behind TPB_CHEAPSIDE_ENABLED=1 and has NO real-money switch at all:
    micro-live and live are refused whatever the environment says. It is a research
    strategy (docs/cheapside_2026-09-29.md) that exists to be watched on paper."""
    if strategy != "cheapside":
        return
    env = os.environ if env is None else env
    on = lambda k: (env.get(k) or "").strip().lower() in ("1", "true", "yes")   # noqa: E731
    if not on("TPB_CHEAPSIDE_ENABLED"):
        raise SystemExit("the cheapside strategy is behind a feature flag: set TPB_CHEAPSIDE_ENABLED=1")
    if mode in ("micro-live", "live", "shadow"):
        raise SystemExit(f"refusing cheapside in {mode}: this strategy is paper only and has no real-money switch")


def check_rising_recovery(strategy: str, mode: str, env: dict | None = None) -> None:
    """RISING-TOKEN RECOVERY (strategy/rising_recovery.py) is behind TPB_RTR_ENABLED=1 and has NO real-money switch:
    shadow, micro-live and live are refused whatever the environment says (the user's spec, 2026-10-09)."""
    if strategy != "rising_recovery":
        return
    env = os.environ if env is None else env
    on = lambda k: (env.get(k) or "").strip().lower() in ("1", "true", "yes")   # noqa: E731
    if not on("TPB_RTR_ENABLED"):
        raise SystemExit("the rising_recovery strategy is behind a feature flag: set TPB_RTR_ENABLED=1")
    if mode in ("micro-live", "live", "shadow"):
        raise SystemExit(f"refusing rising_recovery in {mode}: this strategy is paper only and has no real-money switch")


def check_book_momentum(strategy: str, mode: str, env: dict | None = None) -> None:
    """BOOK_MOMENTUM (strategy/book_momentum.py) is behind TPB_BOOKMOM_ENABLED=1 and has NO real-money switch: shadow,
    micro-live and live are refused whatever the environment says (the user's plan, 2026-10-09: paper first)."""
    if strategy != "book_momentum":
        return
    env = os.environ if env is None else env
    on = lambda k: (env.get(k) or "").strip().lower() in ("1", "true", "yes")   # noqa: E731
    if not on("TPB_BOOKMOM_ENABLED"):
        raise SystemExit("the book_momentum strategy is behind a feature flag: set TPB_BOOKMOM_ENABLED=1")
    if mode in ("micro-live", "live", "shadow"):
        raise SystemExit(f"refusing book_momentum in {mode}: this strategy is paper only and has no real-money switch")


def check_late_confidence(strategy: str, mode: str, env: dict | None = None) -> None:
    """LATE_CONFIDENCE (strategy/late_confidence.py) is behind TPB_LATECONF_ENABLED=1 and has NO real-money switch:
    shadow, micro-live and live are refused whatever the environment says (the user's spec, 2026-10-08: a paper bot)."""
    if strategy != "late_confidence":
        return
    env = os.environ if env is None else env
    on = lambda k: (env.get(k) or "").strip().lower() in ("1", "true", "yes")   # noqa: E731
    if not on("TPB_LATECONF_ENABLED"):
        raise SystemExit("the late_confidence strategy is behind a feature flag: set TPB_LATECONF_ENABLED=1")
    if mode in ("micro-live", "live", "shadow"):
        raise SystemExit(f"refusing late_confidence in {mode}: this strategy is paper only and has no real-money switch")


def check_confirmation_momentum(strategy: str, mode: str, env: dict | None = None) -> None:
    """BTC_CONFIRMATION_MOMENTUM (strategy/confirmation_momentum.py) is behind TPB_CM_ENABLED=1 and has NO
    real-money switch: shadow, micro-live and live are refused whatever the environment says (the user's brief,
    2026-10-06: paper first, live only after an explicit approval of the replay and paper results)."""
    if strategy != "confirmation_momentum":
        return
    env = os.environ if env is None else env
    on = lambda k: (env.get(k) or "").strip().lower() in ("1", "true", "yes")   # noqa: E731
    if not on("TPB_CM_ENABLED"):
        raise SystemExit("the confirmation_momentum strategy is behind a feature flag: set TPB_CM_ENABLED=1")
    if mode in ("micro-live", "live", "shadow"):
        raise SystemExit(f"refusing confirmation_momentum in {mode}: this strategy is paper only and has no real-money switch")


def check_preopen_maker(strategy: str, mode: str, env: dict | None = None) -> None:
    """PREOPEN_MAKER (strategy/preopen_maker.py) is behind TPB_POM_ENABLED=1 and has NO real-money switch: shadow,
    micro-live and live are refused whatever the environment says (the user's brief, 2026-10-06: paper first)."""
    if strategy != "preopen_maker":
        return
    env = os.environ if env is None else env
    on = lambda k: (env.get(k) or "").strip().lower() in ("1", "true", "yes")   # noqa: E731
    if not on("TPB_POM_ENABLED"):
        raise SystemExit("the preopen_maker strategy is behind a feature flag: set TPB_POM_ENABLED=1")
    if mode in ("micro-live", "live", "shadow"):
        raise SystemExit(f"refusing preopen_maker in {mode}: this strategy is paper only and has no real-money switch")


def check_copytrade(strategy: str, mode: str, env: dict | None = None) -> None:
    """COPYTRADE (strategy/copytrade.py) is behind TPB_COPY_ENABLED=1 and has NO real-money switch."""
    if strategy != "copytrade":
        return
    env = os.environ if env is None else env
    on = lambda k: (env.get(k) or "").strip().lower() in ("1", "true", "yes")   # noqa: E731
    if not on("TPB_COPY_ENABLED"):
        raise SystemExit("the copytrade strategy is behind a feature flag: set TPB_COPY_ENABLED=1")
    if mode in ("micro-live", "live", "shadow"):
        raise SystemExit(f"refusing copytrade in {mode}: this strategy is paper only and has no real-money switch")


def check_fastclip(strategy: str, mode: str, env: dict | None = None) -> None:
    """fastclip is behind TPB_FASTCLIP_ENABLED=1 and has NO real-money switch: micro-live, live
    and shadow are refused whatever the environment says (docs/fastclip_2026-09-30.md)."""
    if strategy != "fastclip":
        return
    env = os.environ if env is None else env
    if (env.get("TPB_FASTCLIP_ENABLED") or "").strip().lower() not in ("1", "true", "yes"):
        raise SystemExit("the fastclip strategy is behind a feature flag: set TPB_FASTCLIP_ENABLED=1")
    if mode in ("micro-live", "live", "shadow"):
        raise SystemExit(f"refusing fastclip in {mode}: this strategy is paper only and has no real-money switch")


def check_btc_hedge(strategy: str, mode: str, env: dict | None = None) -> None:
    """btc_trend_reversal_hedge is behind TPB_BTC_HEDGE_ENABLED=1 and has NO real-money switch:
    micro-live, live and shadow are refused whatever the environment says."""
    if strategy != "btc_trend_reversal_hedge":
        return
    env = os.environ if env is None else env
    if (env.get("TPB_BTC_HEDGE_ENABLED") or "").strip().lower() not in ("1", "true", "yes"):
        raise SystemExit("the btc_trend_reversal_hedge strategy is behind a feature flag: set TPB_BTC_HEDGE_ENABLED=1")
    if mode in ("micro-live", "live", "shadow"):
        raise SystemExit(f"refusing btc_trend_reversal_hedge in {mode}: paper only, no real-money switch")


def check_triangle(strategy: str, mode: str, env: dict | None = None) -> None:
    """triangle (one share per filled sudden-move triangle, held to settlement) is behind
    TPB_TRIANGLE_ENABLED=1 and has NO real-money switch: micro-live, live and shadow are refused
    whatever the environment says."""
    if strategy != "triangle":
        return
    env = os.environ if env is None else env
    on = lambda k: (env.get(k) or "").strip().lower() in ("1", "true", "yes")   # noqa: E731
    if not on("TPB_TRIANGLE_ENABLED"):
        raise SystemExit("the triangle strategy is behind a feature flag: set TPB_TRIANGLE_ENABLED=1")
    if mode not in ("micro-live", "live", "shadow"):
        return
    # the one exception (the user, 2026-10-05: "start the live bot in the same strategy, logic, parameters"):
    # pair-lock strategy R (TPB_PAIRLOCK_STRATEGY=R, whose orders are all fill-and-kill buys) may run in
    # shadow or micro-live when TPB_PAIRLOCK_ALLOW_REAL=1 is set as well; full live stays refused
    pairlock_r = (env.get("TPB_PAIRLOCK_STRATEGY") or "").strip().upper() == "R"
    # the swing recovery (the user's 2026-10-07 spec, strategy/pairlock_swing.py) is a PAPER strategy: no switch lifts it
    if pairlock_r and (on("TPB_PAIRLOCK_SWING_MODE") or on("TPB_PAIRLOCK_REC_SWING_MODE")):
        raise SystemExit(f"refusing pair-lock R swing mode in {mode}: the swing recovery strategy is paper only")
    if pairlock_r and mode in ("shadow", "micro-live") and on("TPB_PAIRLOCK_ALLOW_REAL"):
        return
    raise SystemExit(f"refusing triangle in {mode}: paper only, no real-money switch"
                     + (" (pair-lock R: set TPB_PAIRLOCK_ALLOW_REAL=1 for shadow / micro-live)" if pairlock_r else ""))


def main() -> None:
    # The order lanes are threads: an order handed to one waits for the GIL while the
    # event loop is busy, up to the switch interval (5 ms by default) at each turn.
    # 1 ms cuts that wait; the extra switching costs well under a percent of CPU.
    sys.setswitchinterval(0.001)
    ap = argparse.ArgumentParser(description="troll-poly-bot: paper by default, real account with --live")
    ap.add_argument("--balance", type=float, default=100.0,
                    help="paper: starting balance. live: the bankroll the risk caps scale to "
                         "(capped to what the account holds)")
    ap.add_argument("--assets", default="",
                    help="comma separated restriction, e.g. BTC,ETH. Default: every asset the venue lists")
    ap.add_argument("--exchanges", default="binance,bybit,coinbase",
                    help="spot sources for the composite price: binance, bybit, coinbase, kraken, plus the 2026-09-30 "
                         "candidates in feeds/sources.py (okx, okx_swap, bitstamp, binance_futures, bybit_linear, "
                         "deribit, hyperliquid, coinbase_adv, gemini, bitfinex)")
    ap.add_argument("--min-edge", type=float, default=None,
                    help="required NET edge after fees, slippage and the uncertainty charge")
    ap.add_argument("--blend", type=float, default=None,
                    help="weight on the market mid in the blended probability (default 0.5)")
    ap.add_argument("--take-profit", type=float, default=None,
                    help="sell early once the best bid is this far above our average "
                         "entry, in probability units (default 0.05). 0 disables")
    ap.add_argument("--stop-loss", type=float, default=None,
                    help="sell when the best bid falls this far BELOW our average entry, "
                         "in probability units (default 0.20). 0 disables")
    ap.add_argument("--keep-history", action="store_true",
                    help="append to data/live_trades.jsonl across restarts. Default is to "
                         "clear it, because a restart also resets the balance (always kept in --live)")
    ap.add_argument("--mode", choices=("paper", "shadow", "micro-live", "live"), default=None,
                    help="deployment mode. paper: simulated fills. shadow: realistic paper fills plus the "
                         "real account's path in dry run (reconciled, signed, never posted). micro-live: real orders at the MICRO caps (1 USDC/order, 5 open, "
                         "3 daily loss). live: real orders at the CLI caps, only after graduation "
                         "(graduation.py). Default: paper; --live maps to shadow; --live --armed maps to "
                         "micro-live")
    ap.add_argument("--scenario", choices=("optimistic", "realistic", "conservative", "stress", "venue_lag", "calibrated"),
                    default=None,
                    help="paper execution scenario (default realistic; TPB_EXECUTION_SCENARIO). venue_lag "
                         "matches against the local book 500 ms after arrival instead of the quote-survival rule")
    ap.add_argument("--strategy", choices=("analytic", "momentum_taker", "spot_lag", "repriced_momentum", "cheapside", "late_confidence", "book_momentum", "rising_recovery", "confirmation_momentum", "preopen_maker", "copytrade",
                                           "fastclip", "btc_trend_reversal_hedge", "triangle"),
                    default=None,
                    help="decision engine: analytic (default), the experimental momentum_taker "
                         "(TPB_MOMENTUM_*), the experimental spot_lag (needs TPB_SPOT_LAG_ENABLED=1; "
                         "knobs TPB_SPOT_LAG_*) or the experimental repriced_momentum (needs "
                         "TPB_REPRICED_MOM_ENABLED=1; knobs TPB_REPRICED_MOM_*)")
    ap.add_argument("--data-tag", default="",
                    help="suffix for the state, ledger, decision and forensics files so a second bot "
                         "(e.g. a shadow of another strategy) can run beside the first without sharing them")
    ap.add_argument("--live", action="store_true",
                    help="trade the real Polymarket account from .env credentials (DRY RUN unless --armed)")
    ap.add_argument("--armed", action="store_true",
                    help="with --live: actually post orders. Needs TPB_LIVE_ACK in the environment")
    ap.add_argument("--max-order-usdc", type=float, default=5.0, help="live cap per order (default 5)")
    ap.add_argument("--max-open-usdc", type=float, default=25.0, help="live cap on open notional (default 25)")
    ap.add_argument("--max-daily-loss", type=float, default=10.0,
                    help="live: halt for the rest of the run once realised losses reach this (default 10)")
    ap.add_argument("--max-orders-per-hour", type=int, default=60)
    ap.add_argument("--risk-daily-loss", type=float, default=None,
                    help="risk: halt entries once realised losses today reach this many USDC (default 10 per 100 of balance; "
                         "--max-daily-loss is the real-money cap only). Pair it with TPB_CONTROLS_IGNORE=risk.max_daily_loss_usdc")
    ap.add_argument("--max-drawdown", type=float, default=None,
                    help="risk: halt entries once equity is this many USDC below its peak (default 30 per 100 of balance). "
                         "Pair it with TPB_CONTROLS_IGNORE=risk.max_drawdown_usdc or the Settings page overrides it")
    ap.add_argument("--kill-file", default="data/KILL",
                    help="create this file and no new entry is sent, in every mode; <file>_ALL stops exits too")
    ap.add_argument("--log-level", default="INFO")
    ap.add_argument("--duration", type=float, default=0.0,
                    help="stop after this many seconds (0 = run until Ctrl+C)")
    args = ap.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = BotConfig()
    cfg.scale_risk_to_balance(args.balance)
    if args.max_drawdown is not None:
        cfg.risk.max_drawdown_usdc = args.max_drawdown
    if args.risk_daily_loss is not None:
        cfg.risk.max_daily_loss_usdc = args.risk_daily_loss
    resolve_mode(args)
    if args.scenario is not None:
        cfg.execution_scenario = args.scenario
    else:
        cfg.execution_scenario = os.getenv("TPB_EXECUTION_SCENARIO", cfg.execution_scenario)
    cfg.deploy_mode = args.mode
    cfg.engine.strategy = args.strategy or os.getenv("TPB_STRATEGY", cfg.engine.strategy)
    check_spot_lag(cfg.engine.strategy, args.mode)
    check_repriced_momentum(cfg.engine.strategy, args.mode)
    check_cheapside(cfg.engine.strategy, args.mode)
    check_late_confidence(cfg.engine.strategy, args.mode)
    check_book_momentum(cfg.engine.strategy, args.mode)
    check_rising_recovery(cfg.engine.strategy, args.mode)
    check_confirmation_momentum(cfg.engine.strategy, args.mode)
    check_preopen_maker(cfg.engine.strategy, args.mode)
    check_copytrade(cfg.engine.strategy, args.mode)
    check_fastclip(cfg.engine.strategy, args.mode)
    check_btc_hedge(cfg.engine.strategy, args.mode)
    check_triangle(cfg.engine.strategy, args.mode)
    if args.min_edge is not None:
        cfg.engine.min_net_edge = args.min_edge
    if args.blend is not None:
        cfg.engine.market_blend = min(max(args.blend, 0.0), 1.0)
    if args.take_profit is not None:
        cfg.engine.take_profit_enabled = args.take_profit > 0
        if args.take_profit > 0:
            cfg.engine.take_profit_delta = args.take_profit
    if args.stop_loss is not None:
        cfg.engine.stop_loss_enabled = args.stop_loss > 0
        if args.stop_loss > 0:
            cfg.engine.stop_loss_delta = args.stop_loss

    exchange = None
    dry_twin = None
    venue = None
    if args.live:
        venue = _build_live_exchange(args, cfg)
        # the risk caps scale to the bankroll the account can actually fund
        scenario, mode, momentum, spot_lag = cfg.execution_scenario, cfg.deploy_mode, cfg.momentum, cfg.spot_lag
        repriced, cheap, clip, hedge, tri = (cfg.repriced_momentum, cfg.cheapside, cfg.fastclip, cfg.btc_hedge,
                                             cfg.triangle)
        cm = cfg.confirmation_momentum
        pom = cfg.preopen_maker
        copy = cfg.copytrade
        cfg = BotConfig(engine=cfg.engine, mode="live")
        cfg.execution_scenario, cfg.deploy_mode, cfg.momentum, cfg.spot_lag = scenario, mode, momentum, spot_lag
        cfg.repriced_momentum, cfg.cheapside, cfg.fastclip, cfg.btc_hedge = repriced, cheap, clip, hedge
        cfg.triangle = tri
        cfg.confirmation_momentum = cm
        cfg.preopen_maker = pom
        cfg.copytrade = copy
        cfg.live.armed = bool(args.armed)
        cfg.scale_risk_to_balance(venue.bankroll)
        if args.max_drawdown is not None:
            cfg.risk.max_drawdown_usdc = args.max_drawdown
        if args.risk_daily_loss is not None:
            cfg.risk.max_daily_loss_usdc = args.risk_daily_loss
        if args.min_edge is not None:
            cfg.engine.min_net_edge = args.min_edge
        if args.mode == "shadow":
            # SHADOW: positions, exits and PnL come from the realistic paper
            # engine; the real-money adapter runs beside it in dry run and
            # takes a copy of every order through signing and the pre-post
            # checks, never posting one
            dry_twin = venue
            logging.getLogger("live").warning(
                "SHADOW  real decisions on the live book, realistic paper fills (scenario %s); every order "
                "also taken through the real-money path in DRY RUN -- nothing is ever posted", cfg.execution_scenario)
        else:
            exchange = venue
    elif args.armed:
        raise SystemExit("--armed only means something with --live")
    if args.live:
        cfg.state_interval_s = max(cfg.state_interval_s, 0.5)
    if args.mode == "micro-live":
        from .graduation import MicroCaps
        micro = MicroCaps.from_env()
        for line in micro.apply(cfg):
            logging.getLogger("live").warning("MICRO-LIVE  %s", line)
        logging.getLogger("live").warning(
            "MICRO-LIVE  smallest valid order every time (%.0f shares, >= %.2f USDC), %d concurrent, "
            "%s entry, spread cap %.2f, slippage cap %.2f", micro.min_shares, micro.min_notional_usdc,
            micro.max_concurrent_positions, "MAKER" if micro.maker_entry else "TAKER", micro.max_spread,
            micro.max_slippage)

    tag = f".{args.data_tag.strip()}" if args.data_tag.strip() else ""
    if cfg.engine.strategy != "analytic":
        logging.getLogger("live").warning("STRATEGY %s (experimental) %s", cfg.engine.strategy,
                                          cfg.momentum.as_dict() if cfg.engine.strategy == "momentum_taker" else
                                          cfg.spot_lag.as_dict() if cfg.engine.strategy == "spot_lag" else
                                          cfg.repriced_momentum.as_dict()
                                          if cfg.engine.strategy == "repriced_momentum" else "")
    bot = LiveBot(
        assets=tuple(a.strip().upper() for a in args.assets.split(",") if a.strip()),
        balance=venue.bankroll if venue is not None else args.balance,
        cfg=cfg,
        state_path=f"data/live_state{tag}.json",
        trade_log=f"data/live_trades{tag}.jsonl",
        exchanges=tuple(e.strip().lower() for e in args.exchanges.split(",") if e.strip()),
        reset_history=(not args.keep_history) and exchange is None,
        exchange=exchange,
        kill_file=args.kill_file,
        dry_run_twin=dry_twin,
    )

    async def runner() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, bot.stop)
            except NotImplementedError:
                pass                       # Windows: KeyboardInterrupt instead
        if args.duration > 0:
            loop.call_later(args.duration, bot.stop)
        await bot.run()

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        bot.stop()

    snap = bot.snapshot()
    print("\n" + "=" * 68)
    print(f"mode {snap['mode']}   final equity  ${snap['equity']:.2f}   pnl {snap['pnl']:+.2f}")
    print(f"assets {', '.join(snap['assets']) or 'none discovered'}")
    print(f"settled {snap['stats']['settled']}  wins {snap['stats']['wins']}  "
          f"losses {snap['stats']['losses']}  basis disagreements {snap['stats']['basis_disagreements']}")
    print(f"orders submitted {int(snap['orders'].get('orders_submitted', 0))}  "
          f"filled {int(snap['orders'].get('orders_filled', 0))}")
    top = sorted(snap["skips"].items(), key=lambda kv: -kv[1])[:8]
    print("why not trading: " + (", ".join(f"{k} {v}" for k, v in top) or "no evaluations"))
    print(f"evidence: {snap['evidence']}")
    if snap.get("live"):
        lv = snap["live"]
        print(f"account: venue balance {lv['venue_balance']}  pending redemption {lv['pending_redemption']}  "
              f"halted {lv['halted']}")
    print(f"state written to {bot.state_path}")


if __name__ == "__main__":
    sys.exit(main())
