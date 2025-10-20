from __future__ import annotations

from centrix.core.orders import add_order, clear_orders, list_orders


def test_orders_ring_buffer_behaviour() -> None:
    clear_orders()
    for idx in range(60):
        add_order({"source": "test", "symbol": f"SYM{idx}", "qty": idx, "px": idx * 0.5})

    orders = list_orders()
    assert len(orders) == 50
    assert orders[0]["symbol"] == "SYM59"
    assert orders[-1]["symbol"] == "SYM10"
