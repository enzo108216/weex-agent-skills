#!/usr/bin/env python3
"""Fail-closed BTCUSDT spot interval buyer.

The script only talks to WEEX through the bundled Spot and automatic-trade
facades. It never accepts or stores API credentials.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from typing import Any, Callable, Sequence


BOT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = BOT_DIR.parents[1]
SPOT_CLI = SKILL_ROOT / "scripts" / "weex_spot_api.py"
AUTO_TRADE_CLI = SKILL_ROOT / "scripts" / "weex_auto_trade.py"
DEFAULT_CONFIG_PATH = BOT_DIR / "spot_interval_config.json"
DEFAULT_LOG_PATH = BOT_DIR / "btc_spot_interval.jsonl"


class BotConfigError(ValueError):
    pass


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise BotConfigError(f"{field} must be a decimal") from exc
    if not result.is_finite():
        raise BotConfigError(f"{field} must be finite")
    return result


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
    quote_amount_usdt: Decimal
    profile: str
    strategy_id: str
    authorization_id: str
    authorization_max_single_amount_u: Decimal
    authorization_max_total_amount_u: Decimal
    authorization_valid_hours: Decimal
    live_trading_enabled: bool
    log_file: Path

    @classmethod
    def from_mapping(cls, raw: dict[str, Any], *, base_dir: Path = BOT_DIR) -> "BotConfig":
        if not isinstance(raw, dict):
            raise BotConfigError("configuration must be a JSON object")
        log_file = Path(str(raw.get("log_file", DEFAULT_LOG_PATH)))
        if not log_file.is_absolute():
            log_file = base_dir / log_file
        config = cls(
            symbol=str(raw.get("symbol", "")).strip().upper(),
            poll_interval_seconds=_integer(raw.get("poll_interval_seconds"), "poll_interval_seconds"),
            quote_amount_usdt=_decimal(raw.get("quote_amount_usdt"), "quote_amount_usdt"),
            profile=str(raw.get("profile", "")).strip(),
            strategy_id=str(raw.get("strategy_id", "")).strip(),
            authorization_id=str(raw.get("authorization_id", "")).strip(),
            authorization_max_single_amount_u=_decimal(
                raw.get("authorization_max_single_amount_u"),
                "authorization_max_single_amount_u",
            ),
            authorization_max_total_amount_u=_decimal(
                raw.get("authorization_max_total_amount_u"),
                "authorization_max_total_amount_u",
            ),
            authorization_valid_hours=_decimal(
                raw.get("authorization_valid_hours"),
                "authorization_valid_hours",
            ),
            live_trading_enabled=raw.get("live_trading_enabled") is True,
            log_file=log_file,
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.symbol != "BTCUSDT":
            raise BotConfigError("symbol must be BTCUSDT")
        if self.poll_interval_seconds < 60:
            raise BotConfigError("poll_interval_seconds must be at least 60")
        if self.quote_amount_usdt <= 0:
            raise BotConfigError("quote_amount_usdt must be greater than 0")
        if not self.profile or not self.strategy_id:
            raise BotConfigError("profile and strategy_id are required")
        if self.authorization_max_single_amount_u < self.quote_amount_usdt:
            raise BotConfigError("single authorization limit must cover quote_amount_usdt")
        if self.authorization_max_total_amount_u < self.authorization_max_single_amount_u:
            raise BotConfigError("total authorization limit must cover single authorization limit")
        if self.authorization_valid_hours <= 0 or self.authorization_valid_hours > 720:
            raise BotConfigError("authorization_valid_hours must be between 0 and 720")

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
        return issues


def load_config(path: Path) -> BotConfig:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BotConfigError(f"configuration file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise BotConfigError(f"configuration is not valid JSON: {exc}") from exc
    return BotConfig.from_mapping(raw, base_dir=path.parent)


def calculate_market_buy_quantity(
    *,
    quote_amount: Decimal,
    ask_price: Decimal,
    step_size: Decimal,
    minimum: Decimal,
    maximum: Decimal,
) -> Decimal:
    if quote_amount <= 0 or ask_price <= 0 or step_size <= 0:
        raise BotConfigError("quote amount, ask price, and step size must be positive")
    if minimum <= 0 or maximum <= 0 or minimum > maximum:
        raise BotConfigError("spot quantity limits are invalid")
    raw_quantity = quote_amount / ask_price
    units = (raw_quantity / step_size).to_integral_value(rounding=ROUND_DOWN)
    quantity = units * step_size
    if quantity < minimum:
        raise BotConfigError("calculated quantity is below the spot minimum")
    if quantity > maximum:
        raise BotConfigError("calculated quantity exceeds the spot maximum")
    return quantity


def _result_payload(stdout: str) -> dict[str, Any]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("WEEX CLI returned non-JSON output") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("WEEX CLI returned a non-object JSON value")
    return payload


def _unwrap(payload: dict[str, Any]) -> Any:
    if payload.get("ok") is not True:
        raise RuntimeError(str(payload.get("result") or payload.get("error") or "WEEX request failed"))
    return payload.get("result")


class JsonLog:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a", encoding="utf-8", buffering=1)

    def write(self, event: str, **fields: Any) -> None:
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event}
        record.update(fields)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        self.handle.write(line + "\n")
        print(line, flush=True)

    def close(self) -> None:
        self.handle.close()


class SpotIntervalGateway:
    def __init__(
        self,
        *,
        python_executable: str = sys.executable,
        spot_cli: Path = SPOT_CLI,
        auto_trade_cli: Path = AUTO_TRADE_CLI,
        run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.python_executable = python_executable
        self.spot_cli = Path(spot_cli)
        self.auto_trade_cli = Path(auto_trade_cli)
        self.run = run or subprocess.run

    def _spot_call(self, config: BotConfig, endpoint: str, query: dict[str, Any]) -> dict[str, Any]:
        completed = self.run(
            [
                self.python_executable,
                str(self.spot_cli),
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
            raise RuntimeError(completed.stderr.strip() or f"spot CLI failed: {completed.returncode}")
        return _result_payload(completed.stdout)

    def ensure_authorization(self, config: BotConfig) -> dict[str, Any]:
        request_payload = {
            "profile": config.profile,
            "strategy_id": config.strategy_id,
            "trade_types": ["SPOT"],
            "symbols": [config.symbol],
            "all_symbols": False,
            "max_single_amount": format(config.authorization_max_single_amount_u, "f"),
            "max_total_amount": format(config.authorization_max_total_amount_u, "f"),
            "valid_hours": format(config.authorization_valid_hours, "f"),
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

    def fetch_product(self, config: BotConfig) -> dict[str, Any]:
        result = _unwrap(
            self._spot_call(config, "spot.config.get_product_info", {"symbol": config.symbol})
        )
        rows = result.get("symbols", []) if isinstance(result, dict) else []
        for row in rows:
            if isinstance(row, dict) and str(row.get("symbol", "")).upper() == config.symbol:
                return row
        raise RuntimeError("BTCUSDT spot product metadata was not returned")

    def fetch_book_ticker(self, config: BotConfig) -> dict[str, Any]:
        result = _unwrap(
            self._spot_call(config, "spot.market.get_book_ticker", {"symbol": config.symbol})
        )
        if isinstance(result, list):
            result = result[0] if result else {}
        if not isinstance(result, dict) or str(result.get("symbol", "")).upper() != config.symbol:
            raise RuntimeError("BTCUSDT spot book ticker was not returned")
        return result

    def fetch_quote_balance(self, config: BotConfig) -> Decimal:
        result = _unwrap(self._spot_call(config, "spot.account.get_account_balance", {}))
        balances = result.get("balances", []) if isinstance(result, dict) else []
        for row in balances:
            if isinstance(row, dict) and str(row.get("asset", "")).upper() == "USDT":
                return _decimal(row.get("free"), "USDT free balance")
        raise RuntimeError("USDT free balance was not returned")

    def submit_market_buy(
        self, config: BotConfig, quantity: Decimal, cycle_id: int
    ) -> dict[str, Any]:
        client_order_id = f"btc-spot-{int(time.time() * 1000)}-{cycle_id}-{uuid.uuid4().hex[:8]}"
        request_payload = {
            "profile": config.profile,
            "strategy_id": config.strategy_id,
            "authorization_id": config.authorization_id,
            "idempotency_key": f"btc-spot-minute-{uuid.uuid4().hex}",
            "operation_key": "spot.order.place_order",
            "orders": [
                {
                    "symbol": config.symbol,
                    "side": "BUY",
                    "type": "MARKET",
                    "quantity": format(quantity, "f"),
                    "newClientOrderId": client_order_id,
                }
            ],
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


def _require_authorization(config: BotConfig, gateway: SpotIntervalGateway) -> dict[str, Any]:
    authorization = gateway.ensure_authorization(config)
    if (
        authorization.get("ok") is not True
        or authorization.get("status") != "ACTIVE"
        or authorization.get("authorization_id") != config.authorization_id
        or authorization.get("next_action") != "SUBMIT_ALLOWED"
    ):
        raise RuntimeError("SPOT authorization is not active for submission")
    return authorization


def _authorization_summary(authorization: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": authorization.get("status"),
        "next_action": authorization.get("next_action"),
        "remaining_amount_u": authorization.get("remaining_amount_u"),
        "expires_at": authorization.get("expires_at"),
    }


def run_bot(
    config: BotConfig,
    gateway: SpotIntervalGateway,
    *,
    confirm_live: bool = False,
    sleep_fn: Callable[[float], None] = time.sleep,
    max_cycles: int | None = None,
    logger: JsonLog | None = None,
) -> int:
    log = logger or JsonLog(config.log_file)
    try:
        issues = config.live_readiness_issues()
        if issues or not confirm_live:
            log.write("disabled", issues=issues or ["--confirm-live"])
            return 1

        authorization = _require_authorization(config, gateway)
        log.write("startup", symbol=config.symbol, interval_seconds=config.poll_interval_seconds,
                  quote_amount_usdt=format(config.quote_amount_usdt, "f"),
                  authorization=_authorization_summary(authorization))

        cycle = 0
        while max_cycles is None or cycle < max_cycles:
            cycle += 1
            try:
                authorization = _require_authorization(config, gateway)
                product = gateway.fetch_product(config)
                if str(product.get("status", "")).upper() != "TRADING" or product.get("enableTrade") is not True:
                    raise RuntimeError("BTCUSDT spot trading is not enabled")
                ticker = gateway.fetch_book_ticker(config)
                ask_price = _decimal(ticker.get("askPrice"), "ask price")
                balance = gateway.fetch_quote_balance(config)
                if balance < config.quote_amount_usdt:
                    raise RuntimeError("USDT free balance is below the configured order amount")
                quantity = calculate_market_buy_quantity(
                    quote_amount=config.quote_amount_usdt,
                    ask_price=ask_price,
                    step_size=_decimal(product.get("stepSize"), "stepSize"),
                    minimum=_decimal(product.get("minTradeAmount"), "minTradeAmount"),
                    maximum=min(
                        _decimal(product.get("maxTradeAmount"), "maxTradeAmount"),
                        _decimal(product.get("marketBuyLimitSize"), "marketBuyLimitSize"),
                    ),
                )
                log.write(
                    "order_prepared",
                    cycle=cycle,
                    ask_price=format(ask_price, "f"),
                    quote_balance=format(balance, "f"),
                    quantity=format(quantity, "f"),
                    estimated_quote=format(quantity * ask_price, "f"),
                    authorization=_authorization_summary(authorization),
                )
                result = gateway.submit_market_buy(config, quantity, cycle)
                status = result.get("status")
                log.write("order_result", cycle=cycle, status=status,
                          ok=result.get("ok") is True,
                          legs=[
                              {"status": leg.get("status")}
                              for leg in (result.get("legs") or [])
                              if isinstance(leg, dict)
                          ])
                print(json.dumps({"status": status, "cycle": cycle}, ensure_ascii=False), flush=True)
                if result.get("ok") is not True or status != "ACCEPTED":
                    return 2
            except Exception as exc:
                log.write("cycle_error", cycle=cycle, error=str(exc))
                print(json.dumps({"status": "ERROR", "cycle": cycle, "message": str(exc)}, ensure_ascii=False), flush=True)
                return 1
            if max_cycles is not None and cycle >= max_cycles:
                return 0
            sleep_fn(config.poll_interval_seconds)
        return 0
    finally:
        if logger is None:
            log.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BTCUSDT spot interval buyer")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--check-config", action="store_true")
    parser.add_argument("--confirm-live", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.check_config:
            print(json.dumps({"status": "VALID", "config": str(args.config)}, ensure_ascii=False))
            return 0
        return run_bot(config, SpotIntervalGateway(), confirm_live=args.confirm_live)
    except (BotConfigError, RuntimeError, ValueError) as exc:
        print(json.dumps({"status": "ERROR", "message": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
