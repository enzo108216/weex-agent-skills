#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
MAPPING_PATH = ROOT / "references" / "mcp-capability-mapping.json"
BOUNDARY_PATH = ROOT / "references" / "mcp-boundary-contract-v2.json"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


LEGACY_ARGUMENTS = {
    "account.adjust_position_margin_trade": {"isolatedPositionId": "1", "amount": "1", "type": 1},
    "account.change_margin_mode_trade": {"symbol": "BTCUSDT", "marginType": "CROSSED"},
    "account.get_commission_rate": {"symbol": "BTCUSDT"},
    "account.get_single_position": {"symbol": "BTCUSDT"},
    "account.modify_auto_append_margin_trade": {"positionId": "1", "autoAppendMargin": True},
    "account.update_leverage_trade": {"symbol": "BTCUSDT", "crossLeverage": "5"},
    "market.get_depth_data": {"symbol": "BTCUSDT"},
    "market.get_funding_rate_history": {"symbol": "BTCUSDT"},
    "market.get_history_klines": {"symbol": "BTCUSDT", "interval": "1m"},
    "market.get_index_price_klines": {"symbol": "BTCUSDT", "interval": "1m"},
    "market.get_klines": {"symbol": "BTCUSDT", "interval": "1m"},
    "market.get_mark_price_klines": {"symbol": "BTCUSDT", "interval": "1m"},
    "market.get_open_interest": {"symbol": "BTCUSDT"},
    "market.get_recent_trades": {"symbol": "BTCUSDT"},
    "market.get_symbol_price": {"symbol": "BTCUSDT"},
    "sim.transaction.place_order": {
        "symbol": "BTCSUSDT",
        "side": "BUY",
        "positionSide": "LONG",
        "type": "MARKET",
        "quantity": "1",
        "newClientOrderId": "demo-1",
    },
    "spot.account.get_bill_records": {"after": 0, "before": 1700000001000},
    "spot.account.get_fund_bill_records": {"pageIndex": 1, "pageSize": 10},
    "spot.market.get_depth_data": {"symbol": "BTCUSDT"},
    "spot.market.get_k_line_data": {"symbol": "BTCUSDT", "interval": "1m"},
    "spot.market.get_ticker_info": {"symbols": ["BTCUSDT", "ETHUSDT"]},
    "spot.market.get_trade_data": {"symbol": "BTCUSDT"},
    "spot.order.bulk_cancel": {"orderIds": ["1"]},
    "spot.order.bulk_order": {
        "symbol": "BTCUSDT",
        "orderList": [
            {
                "side": "BUY",
                "type": "MARKET",
                "quantity": "1",
                "newClientOrderId": "spot-batch-1",
            }
        ],
    },
    "spot.order.cancel_order": {"orderId": "1"},
    "spot.order.cancel_symbol_orders": {"symbol": "BTCUSDT"},
    "spot.order.history_orders": {"symbol": "BTCUSDT"},
    "spot.order.order_details": {"orderId": "1"},
    "spot.order.place_order": {
        "symbol": "BTCUSDT",
        "side": "BUY",
        "type": "MARKET",
        "quantity": "1",
        "newClientOrderId": "spot-1",
    },
    "spot.order.transaction_details": {"symbol": "BTCUSDT"},
    "spot.tax.get_spot_account_record": {"coin": "USDT", "month": "2026-11"},
    "transaction.cancel_order": {"orderId": "1"},
    "transaction.cancel_orders_batch": {"orderIdList": ["1"]},
    "transaction.cancel_pending_order": {"orderId": "1"},
    "transaction.get_single_order_info": {"orderId": "1"},
    "transaction.modify_tp_sl_order": {"orderId": "1", "triggerPrice": "100"},
    "transaction.place_order": {
        "symbol": "BTCUSDT",
        "side": "BUY",
        "positionSide": "LONG",
        "type": "MARKET",
        "quantity": "1",
        "newClientOrderId": "futures-1",
    },
    "transaction.place_orders_batch": {
        "batchOrders": [
            {
                "symbol": "BTCUSDT",
                "side": "BUY",
                "positionSide": "LONG",
                "type": "MARKET",
                "quantity": "1",
                "newClientOrderId": "futures-batch-1",
            }
        ]
    },
    "transaction.place_pending_order": {
        "symbol": "BTCUSDT",
        "side": "SELL",
        "positionSide": "LONG",
        "type": "STOP_MARKET",
        "quantity": "1",
        "triggerPrice": "100",
        "clientAlgoId": "algo-1",
    },
    "transaction.place_tp_sl_order": {
        "symbol": "BTCUSDT",
        "clientAlgoId": "tp-sl-1",
        "planType": "TAKE_PROFIT",
        "triggerPrice": "100",
        "quantity": "1",
        "positionSide": "LONG",
    },
}


class McpRequestDescriptionIntegrationTests(unittest.TestCase):
    def test_spot_bill_request_mapping_preserves_time_bound_fields(self) -> None:
        from weex_mcp_read_compat import _map_arguments

        mapping = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))
        entry = next(
            item for item in mapping["capabilities"]
            if item["legacy_key"] == "spot.account.get_bill_records"
        )

        self.assertEqual(
            _map_arguments(entry, query={}, body={"after": 0, "before": 1700000001000}),
            {
                "market": "spot",
                "trading_mode": "live",
                "bill_type": "account",
                "after": "0",
                "before": "1700000001000",
            },
        )

    def test_all_51_reads_and_21_writes_match_mcp_routes(self) -> None:
        try:
            from weex_mcp import __version__
            from weex_mcp.local import describe_request
            from weex_mcp.settings import Settings
        except ModuleNotFoundError:
            self.skipTest("verified weex-mcp package is not installed")
        except ImportError as exc:
            self.fail(f"installed weex-mcp lacks the request-description API: {exc}")

        import weex_mcp_read_compat as read_compat
        import weex_mcp_trade_compat as trade_compat

        mapping = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))
        boundary = json.loads(BOUNDARY_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            __version__,
            mapping["verified_mcp_candidate"]["package_version"],
        )
        write_operations = trade_compat._build_trade_operations(mapping, boundary)
        self.assertEqual(len(write_operations), 21)
        self.assertEqual(len({operation["direct_tool"] for operation in write_operations.values()}), 14)
        descriptions = {}
        selected = [
            entry
            for entry in mapping["capabilities"]
            if entry["product"] in {"contract", "spot"}
        ]
        self.assertEqual(
            sum(entry["target"]["variant"] == "read" for entry in selected), 51,
        )
        self.assertEqual(len(selected), 51 + len(write_operations))
        for entry in selected:
            with self.subTest(legacy_key=entry["legacy_key"]):
                supplied = LEGACY_ARGUMENTS.get(entry["legacy_key"], {})
                query = supplied if entry["request_transport"] == "query" else {}
                body = supplied if entry["request_transport"] == "body" else {}
                if entry["target"]["variant"] == "read":
                    name = entry["target"]["tool"]
                    arguments = read_compat._map_arguments(entry, query=query, body=body)
                else:
                    operation = write_operations[(entry["product"], entry["legacy_key"])]
                    name = operation["direct_tool"]
                    arguments = trade_compat._map_trade_arguments(
                        operation,
                        query=query,
                        body=body,
                        client_id_factory=lambda: "stable-description-id",
                    )
                description = describe_request(
                    settings=Settings(trade_enabled=True), name=name, arguments=arguments,
                )
                parsed = urlsplit(description["url"])
                self.assertEqual(description["method"], entry["method"])
                self.assertEqual(parsed.path, entry["path"])
                self.assertEqual(
                    f"{parsed.scheme}://{parsed.netloc}",
                    (
                        "https://api-spot.weex.com"
                        if entry["product"] == "spot"
                        else "https://api-contract.weex.com"
                    ),
                )
                descriptions[entry["legacy_key"]] = description

        self.assertEqual(
            descriptions["spot.market.get_ticker_info"]["query"],
            {"symbols": '["BTCUSDT","ETHUSDT"]'},
        )
        self.assertEqual(
            descriptions["spot.account.get_bill_records"]["body"],
            {"after": 0, "before": 1700000001000},
        )
        self.assertEqual(
            descriptions["spot.account.get_fund_bill_records"]["body"],
            {"pageIndex": 1, "pageSize": 10},
        )
        self.assertEqual(
            descriptions["spot.tax.get_spot_account_record"]["body"],
            {"coin": "USDT", "month": "2026-11"},
        )
        self.assertEqual(
            descriptions["account.update_leverage_trade"]["body"]["crossLeverage"],
            "5",
        )
        self.assertEqual(
            descriptions["transaction.place_tp_sl_order"]["body"]["planType"],
            "TAKE_PROFIT",
        )


if __name__ == "__main__":
    unittest.main()
