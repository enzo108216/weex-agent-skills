#!/usr/bin/env python3
"""Small, fail-closed BTCUSDT futures moving-average example.

The example deliberately keeps production boundaries in subprocesses. It never
loads credentials or imports the Trader authorization state implementation.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from typing import Any, Callable, Sequence


BOT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = BOT_DIR.parents[1]
CONTRACT_CLI = SKILL_ROOT / "scripts" / "weex_contract_api.py"
AUTO_TRADE_CLI = SKILL_ROOT / "scripts" / "weex_auto_trade.py"
DEFAULT_CONFIG_PATH = BOT_DIR / "config.json"
ALLOWED_DIRECTIONS = {"both", "long_only", "short_only"}
INTERVAL_MILLISECONDS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
    "1w": 604_800_000,
}
MAX_KLINE_LIMIT = 1_000
FUTURE_TIMESTAMP_TOLERANCE_MS = 5_000
MAX_PRICE_AGE_MS = 30_000


class BotConfigError(ValueError):
    """Raised when a configuration would make the strategy ambiguous or unsafe."""


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise BotConfigError(f"{field} must be a decimal") from exc
    if not result.is_finite():
        raise BotConfigError(f"{field} must be finite")
    return result


def _optional_decimal(value: Any, field: str) -> Decimal | None:
    if value in (None, ""):
        return None
    return _decimal(value, field)


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise BotConfigError(f"{field} must be an integer")
    result = _decimal(value, field)
    if result != result.to_integral_value():
        raise BotConfigError(f"{field} must be an integer")
    return int(result)


@dataclass(frozen=True)
class BotConfig:
    symbol: str
    poll_interval_seconds: int
    direction: str
    margin_amount_usdt: Decimal
    kline_interval: str
    fast_ma_period: int
    slow_ma_period: int
    profile: str
    strategy_id: str
    authorization_id: str
    authorization_max_single_amount_u: Decimal | None
    authorization_max_total_amount_u: Decimal | None
    authorization_valid_hours: Decimal | None
    live_trading_enabled: bool

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "BotConfig":
        if not isinstance(raw, dict):
            raise BotConfigError("configuration must be a JSON object")
        symbol = str(raw.get("symbol", "")).strip().upper()
        direction = str(raw.get("direction", "")).strip().lower()
        interval = str(raw.get("kline_interval", "")).strip()
        poll_interval = _integer(raw.get("poll_interval_seconds"), "poll_interval_seconds")
        fast_period = _integer(raw.get("fast_ma_period"), "fast_ma_period")
        slow_period = _integer(raw.get("slow_ma_period"), "slow_ma_period")
        config = cls(
            symbol=symbol,
            poll_interval_seconds=poll_interval,
            direction=direction,
            margin_amount_usdt=_decimal(raw.get("margin_amount_usdt"), "margin_amount_usdt"),
            kline_interval=interval,
            fast_ma_period=fast_period,
            slow_ma_period=slow_period,
            profile=str(raw.get("profile", "")).strip(),
            strategy_id=str(raw.get("strategy_id", "")).strip(),
            authorization_id=str(raw.get("authorization_id", "")).strip(),
            authorization_max_single_amount_u=_optional_decimal(
                raw.get("authorization_max_single_amount_u"),
                "authorization_max_single_amount_u",
            ),
            authorization_max_total_amount_u=_optional_decimal(
                raw.get("authorization_max_total_amount_u"),
                "authorization_max_total_amount_u",
            ),
            authorization_valid_hours=_optional_decimal(
                raw.get("authorization_valid_hours"),
                "authorization_valid_hours",
            ),
            live_trading_enabled=raw.get("live_trading_enabled") is True,
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.symbol != "BTCUSDT":
            raise BotConfigError("symbol must be BTCUSDT")
        if self.poll_interval_seconds < 1:
            raise BotConfigError("poll_interval_seconds must be at least 1")
        if self.direction not in ALLOWED_DIRECTIONS:
            raise BotConfigError("direction must be both, long_only, or short_only")
        if self.margin_amount_usdt <= 0:
            raise BotConfigError("margin_amount_usdt must be greater than 0")
        if self.kline_interval not in INTERVAL_MILLISECONDS:
            raise BotConfigError("unsupported kline_interval")
        if self.fast_ma_period < 1 or self.slow_ma_period < 1:
            raise BotConfigError("moving-average periods must be positive")
        if self.fast_ma_period >= self.slow_ma_period:
            raise BotConfigError("fast_ma_period must be less than slow_ma_period")
        if self.slow_ma_period > MAX_KLINE_LIMIT:
            raise BotConfigError(f"slow_ma_period must not exceed {MAX_KLINE_LIMIT}")
        if (
            self.authorization_max_single_amount_u is not None
            and self.authorization_max_single_amount_u <= 0
        ):
            raise BotConfigError("authorization_max_single_amount_u must be greater than 0")
        if (
            self.authorization_max_total_amount_u is not None
            and self.authorization_max_total_amount_u <= 0
        ):
            raise BotConfigError("authorization_max_total_amount_u must be greater than 0")
        if (
            self.authorization_max_single_amount_u is not None
            and self.authorization_max_total_amount_u is not None
            and self.authorization_max_total_amount_u < self.authorization_max_single_amount_u
        ):
            raise BotConfigError(
                "authorization_max_total_amount_u must be greater than or equal to "
                "authorization_max_single_amount_u"
            )
        if self.authorization_valid_hours is not None:
            valid_seconds = self.authorization_valid_hours * Decimal(3600)
            if (
                self.authorization_valid_hours <= 0
                or self.authorization_valid_hours > 720
                or valid_seconds != valid_seconds.to_integral_value()
            ):
                raise BotConfigError(
                    "authorization_valid_hours must resolve to whole seconds between 0 and 720 hours"
                )

    def live_readiness_issues(self) -> list[str]:
        issues: list[str] = []
        if not self.live_trading_enabled:
            issues.append("live_trading_enabled")
        if not self.profile:
            issues.append("profile")
        if not self.strategy_id:
            issues.append("strategy_id")
        if not self.authorization_id:
            issues.append("authorization_id")
        if self.authorization_max_single_amount_u is None:
            issues.append("authorization_max_single_amount_u")
        if self.authorization_max_total_amount_u is None:
            issues.append("authorization_max_total_amount_u")
        if self.authorization_valid_hours is None:
            issues.append("authorization_valid_hours")
        return issues


def load_config(path: Path) -> BotConfig:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BotConfigError(f"configuration file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise BotConfigError(f"configuration is not valid JSON: {exc}") from exc
    return BotConfig.from_mapping(raw)


def moving_average_signal(closes: Sequence[Decimal], fast_period: int, slow_period: int) -> str:
    if fast_period < 1 or slow_period < 1 or fast_period >= slow_period:
        raise BotConfigError("moving-average periods are invalid")
    if len(closes) < slow_period:
        raise BotConfigError("not enough closes for slow moving average")
    fast = sum(closes[-fast_period:], Decimal("0")) / Decimal(fast_period)
    slow = sum(closes[-slow_period:], Decimal("0")) / Decimal(slow_period)
    if fast > slow:
        return "LONG"
    if fast < slow:
        return "SHORT"
    return "NEUTRAL"


def direction_allows(direction: str, signal: str) -> bool:
    return direction == "both" or (direction == "long_only" and signal == "LONG") or (
        direction == "short_only" and signal == "SHORT"
    )


def calculate_quantity(
    *,
    margin_amount_usdt: Decimal,
    leverage: Decimal,
    price: Decimal,
    contract_value: Decimal,
    quantity_precision: int,
    minimum: Decimal,
    maximum: Decimal,
) -> Decimal:
    if leverage <= 0 or price <= 0 or contract_value <= 0:
        raise BotConfigError("leverage, price, and contract value must be positive")
    if quantity_precision < 0:
        raise BotConfigError("quantity precision must not be negative")
    if minimum <= 0 or maximum <= 0 or minimum > maximum:
        raise BotConfigError("order quantity limits are invalid")
    raw = margin_amount_usdt * leverage / (price * max(Decimal("1"), contract_value))
    quantum = Decimal("1").scaleb(-quantity_precision)
    quantity = raw.quantize(quantum, rounding=ROUND_DOWN)
    if quantity < minimum:
        raise BotConfigError(f"calculated quantity {quantity} is below minimum {minimum}")
    if quantity > maximum:
        raise BotConfigError(f"calculated quantity {quantity} exceeds maximum {maximum}")
    return quantity


def _result_payload(stdout: str) -> dict[str, Any]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("WEEX CLI returned non-JSON output") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("WEEX CLI returned a non-object JSON value")
    return payload


class WeexCliGateway:
    def __init__(
        self,
        *,
        python_executable: str = sys.executable,
        contract_cli: Path = CONTRACT_CLI,
        auto_trade_cli: Path = AUTO_TRADE_CLI,
        run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self.python_executable = python_executable
        self.contract_cli = Path(contract_cli)
        self.auto_trade_cli = Path(auto_trade_cli)
        self.run = run or subprocess.run
        self.now_ms = now_ms or (lambda: time.time_ns() // 1_000_000)

    def ensure_authorization(self, config: BotConfig) -> dict[str, Any]:
        max_single = config.authorization_max_single_amount_u
        max_total = config.authorization_max_total_amount_u
        valid_hours = config.authorization_valid_hours
        if max_single is None or max_total is None or valid_hours is None:
            raise BotConfigError("authorization scope is incomplete")
        request_payload = {
            "profile": config.profile,
            "strategy_id": config.strategy_id,
            "trade_types": ["FUTURES"],
            "symbols": [config.symbol],
            "all_symbols": False,
            "max_single_amount": format(max_single, "f"),
            "max_total_amount": format(max_total, "f"),
            "valid_hours": format(valid_hours, "f"),
        }
        completed = self.run(
            [
                self.python_executable,
                str(self.auto_trade_cli),
                "ensure-authorization",
                "--input",
                "-",
            ],
            input=json.dumps(request_payload, separators=(",", ":")),
            text=True,
            capture_output=True,
            check=False,
        )
        if not completed.stdout.strip():
            raise RuntimeError(completed.stderr.strip() or "authorization CLI returned no JSON")
        return _result_payload(completed.stdout)

    def _contract_call(self, config: BotConfig, endpoint: str, query: dict[str, Any]) -> dict[str, Any]:
        completed = self.run(
            [
                self.python_executable,
                str(self.contract_cli),
                "--profile",
                config.profile,
                "call",
                "--endpoint",
                endpoint,
                "--query",
                json.dumps(query, separators=(",", ":")),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or f"contract CLI failed: {completed.returncode}")
        return _result_payload(completed.stdout)

    @staticmethod
    def _unwrap(payload: dict[str, Any]) -> Any:
        if payload.get("ok") is not True:
            detail = payload.get("result") or payload.get("error")
            if detail:
                raise RuntimeError(str(detail))
            raise RuntimeError("WEEX response does not explicitly report success")
        return payload.get("result", payload)

    def fetch_closes(self, config: BotConfig) -> list[Decimal]:
        payload = self._contract_call(
            config,
            "market.get_klines",
            {"symbol": config.symbol, "interval": config.kline_interval, "limit": config.slow_ma_period},
        )
        rows = self._unwrap(payload)
        if not isinstance(rows, list):
            raise RuntimeError("kline result must be an array")
        parsed: list[tuple[int, Decimal]] = []
        try:
            for row in rows:
                if not isinstance(row, list) or len(row) < 11:
                    raise RuntimeError("kline row has an unexpected shape")
                open_time = _integer(row[0], "kline open time")
                close_time = _integer(row[6], "kline close time")
                close = _decimal(row[4], "kline close price")
                if close_time < open_time or close <= 0:
                    raise RuntimeError("kline row has invalid time or price values")
                parsed.append((open_time, close))
        except (BotConfigError, InvalidOperation, TypeError, ValueError) as exc:
            raise RuntimeError("kline row contains an invalid time or close price") from exc
        parsed.sort(key=lambda item: item[0])
        open_times = [open_time for open_time, _ in parsed]
        if len(open_times) != len(set(open_times)):
            raise RuntimeError("kline rows contain duplicate open times")
        if len(parsed) < config.slow_ma_period:
            raise RuntimeError("kline result does not contain enough current rows")
        latest_age = self.now_ms() - open_times[-1]
        maximum_age = INTERVAL_MILLISECONDS[config.kline_interval] * 2
        if latest_age < -FUTURE_TIMESTAMP_TOLERANCE_MS or latest_age > maximum_age:
            raise RuntimeError("kline result is stale or future-dated")
        return [close for _, close in parsed]

    def fetch_positions(self, config: BotConfig) -> list[dict[str, Any]]:
        payload = self._contract_call(
            config, "account.get_single_position", {"symbol": config.symbol}
        )
        rows = self._unwrap(payload)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise RuntimeError("position result must be an array of objects")
        return rows

    def fetch_symbol_config(self, config: BotConfig) -> dict[str, Any]:
        payload = self._contract_call(
            config, "account.get_symbol_config", {"symbol": config.symbol}
        )
        rows = self._unwrap(payload)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise RuntimeError("symbol configuration result must be an array of objects")
        for row in rows:
            if str(row.get("symbol", "")).upper() == config.symbol:
                return row
        raise RuntimeError("BTCUSDT symbol configuration was not returned")

    def fetch_contract_info(self, config: BotConfig) -> dict[str, Any]:
        payload = self._contract_call(
            config, "market.get_contract_info", {"symbol": config.symbol}
        )
        result = self._unwrap(payload)
        rows = result.get("symbols", []) if isinstance(result, dict) else []
        for row in rows:
            if isinstance(row, dict) and str(row.get("symbol", "")).upper() == config.symbol:
                return row
        raise RuntimeError("BTCUSDT contract metadata was not returned")

    def fetch_price(self, config: BotConfig) -> Decimal:
        payload = self._contract_call(
            config, "market.get_symbol_price", {"symbol": config.symbol, "priceType": "MARK"}
        )
        result = self._unwrap(payload)
        if not isinstance(result, dict):
            raise RuntimeError("market price was not returned")
        if str(result.get("symbol", "")).upper() != config.symbol:
            raise RuntimeError("market price does not match BTCUSDT")
        try:
            price = _decimal(result.get("price"), "market price")
            timestamp = _integer(result.get("time"), "market price time")
        except BotConfigError as exc:
            raise RuntimeError("market price contains invalid fields") from exc
        if price <= 0:
            raise RuntimeError("market price must be positive")
        age = self.now_ms() - timestamp
        if age < -FUTURE_TIMESTAMP_TOLERANCE_MS or age > MAX_PRICE_AGE_MS:
            raise RuntimeError("market price is stale or future-dated")
        return price

    def submit_market_order(self, config: BotConfig, signal: str, quantity: Decimal) -> dict[str, Any]:
        order = {
            "symbol": config.symbol,
            "side": "BUY" if signal == "LONG" else "SELL",
            "positionSide": signal,
            "type": "MARKET",
            "quantity": format(quantity, "f"),
        }
        request_payload = {
            "profile": config.profile,
            "strategy_id": config.strategy_id,
            "authorization_id": config.authorization_id,
            "idempotency_key": f"btc-ma-{time.time_ns()}",
            "operation_key": "transaction.place_order",
            "orders": [order],
        }
        completed = self.run(
            [
                self.python_executable,
                str(self.auto_trade_cli),
                "submit-auto",
                "--input",
                "-",
                "--confirm-live",
            ],
            input=json.dumps(request_payload, separators=(",", ":")),
            text=True,
            capture_output=True,
            check=False,
        )
        if not completed.stdout.strip():
            raise RuntimeError(completed.stderr.strip() or "auto-trade CLI returned no JSON")
        return _result_payload(completed.stdout)


def _has_nonzero_position(positions: Sequence[dict[str, Any]], symbol: str) -> bool:
    for position in positions:
        if not isinstance(position, dict):
            raise RuntimeError("position row must be an object")
        if str(position.get("symbol", "")).upper() != symbol:
            raise RuntimeError("position row does not match BTCUSDT")
        if position.get("size") in (None, ""):
            raise RuntimeError("position size is missing")
        try:
            size = Decimal(str(position["size"]))
            if not size.is_finite():
                raise RuntimeError("position size must be finite")
            if abs(size) > 0:
                return True
        except (InvalidOperation, ValueError):
            raise RuntimeError("position size is not numeric")
    return False


def _require_authorization(config: BotConfig, gateway: Any) -> None:
    authorization = gateway.ensure_authorization(config)
    if (
        authorization.get("ok") is not True
        or authorization.get("status") != "ACTIVE"
        or authorization.get("authorization_id") != config.authorization_id
        or authorization.get("next_action") != "SUBMIT_ALLOWED"
    ):
        raise RuntimeError("authorization check did not permit automatic trading")


def run_bot(
    config: BotConfig,
    gateway: Any,
    *,
    confirm_live: bool = False,
    sleep_fn: Callable[[float], None] = time.sleep,
    max_cycles: int | None = None,
) -> int:
    issues = config.live_readiness_issues()
    if issues or not confirm_live:
        print(json.dumps({"status": "DISABLED", "issues": issues or ["--confirm-live"]}, ensure_ascii=False))
        return 1

    _require_authorization(config, gateway)

    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        cycles += 1
        closes = gateway.fetch_closes(config)
        signal = moving_average_signal(closes, config.fast_ma_period, config.slow_ma_period)
        if signal == "NEUTRAL" or not direction_allows(config.direction, signal):
            if max_cycles is not None and cycles >= max_cycles:
                return 0
            sleep_fn(config.poll_interval_seconds)
            continue
        _require_authorization(config, gateway)
        if _has_nonzero_position(gateway.fetch_positions(config), config.symbol):
            print(json.dumps({"status": "POSITION_EXISTS", "signal": signal}, ensure_ascii=False))
            if max_cycles is not None and cycles >= max_cycles:
                return 0
            sleep_fn(config.poll_interval_seconds)
            continue

        symbol = gateway.fetch_contract_info(config)
        account_symbol = gateway.fetch_symbol_config(config)
        if str(symbol.get("symbol", "")).upper() != config.symbol:
            raise BotConfigError("contract metadata does not match BTCUSDT")
        if str(account_symbol.get("symbol", "")).upper() != config.symbol:
            raise BotConfigError("account symbol configuration does not match BTCUSDT")
        margin_type = str(account_symbol.get("marginType", "")).upper()
        if margin_type not in {"CROSSED", "ISOLATED"}:
            raise BotConfigError("marginType must be CROSSED or ISOLATED")
        leverage_key = "crossLeverage" if margin_type == "CROSSED" else (
            "isolatedLongLeverage" if signal == "LONG" else "isolatedShortLeverage"
        )
        leverage = _decimal(account_symbol.get(leverage_key), leverage_key)
        contract_value = _decimal(symbol.get("contractVal"), "contractVal")
        quantity_precision = _integer(symbol.get("quantityPrecision"), "quantityPrecision")
        minimum = _decimal(symbol.get("minOrderSize"), "minOrderSize")
        maximum = min(
            _decimal(symbol.get("maxOrderSize"), "maxOrderSize"),
            _decimal(symbol.get("marketOpenLimitSize"), "marketOpenLimitSize"),
        )
        quantity = calculate_quantity(
            margin_amount_usdt=config.margin_amount_usdt,
            leverage=leverage,
            price=gateway.fetch_price(config),
            contract_value=contract_value,
            quantity_precision=quantity_precision,
            minimum=minimum,
            maximum=maximum,
        )
        result = gateway.submit_market_order(config, signal, quantity)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") and result.get("status") == "ACCEPTED" else 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline-configurable BTCUSDT moving-average example")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--check-config", action="store_true", help="Validate config without any network or account access")
    parser.add_argument("--confirm-live", action="store_true", help="Explicitly permit the configured real-trading path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.check_config:
            print(json.dumps({"status": "VALID", "config": str(args.config)}, ensure_ascii=False))
            return 0
        return run_bot(config, WeexCliGateway(), confirm_live=args.confirm_live)
    except (BotConfigError, RuntimeError, ValueError) as exc:
        print(json.dumps({"status": "ERROR", "message": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
