"""Use IBKR market scans as discovery before requesting per-stock history."""

from datetime import UTC, datetime
from xml.etree import ElementTree

SCAN_CODES = ("HIGH_VS_13W_HL", "HIGH_VS_26W_HL", "HIGH_VS_52W_HL", "TOP_PERC_GAIN")
LOCATION = "STK.US.MAJOR"
ROW_LIMIT = 50
MIN_PRICE = 3.0


async def shortlist(broker, instruments, out):
    # Both universe loaders enforce one eligible listing per symbol.
    by_symbol = {item.symbol: item for item in instruments}
    xml = await broker.scanner_parameters()
    (out / "scanner-parameters.xml").write_text(xml, encoding="utf-8")
    advertised = {node.text for node in ElementTree.fromstring(xml).iter("scanCode")}
    if not advertised.intersection(SCAN_CODES):
        raise ValueError("TWS did not advertise any configured US stock scan codes")

    scans, hits = [], {}
    for code in SCAN_CODES:
        if code not in advertised:
            scans.append({"code": code, "status": "unsupported"})
            continue
        rows = await broker.scanner(code, location=LOCATION, rows=ROW_LIMIT, min_price=MIN_PRICE)
        results = []
        for row in rows:
            contract = row.contractDetails.contract
            exchange = contract.primaryExchange or contract.exchange
            listing_exchange = "NASDAQ" if exchange == "ISLAND" else exchange
            item = by_symbol.get(contract.symbol)
            # SMART is a routing venue. Use the eligible listing's exchange for qualification.
            matched = (
                item is not None
                and contract.secType == "STK"
                and contract.currency == item.currency
                and listing_exchange in ("", "SMART", item.primary_exchange)
            )
            results.append(
                {
                    "symbol": contract.symbol,
                    "exchange": exchange,
                    "primary_exchange": contract.primaryExchange,
                    "con_id": contract.conId,
                    "rank": row.rank,
                    "instrument_key": item.key if matched else None,
                }
            )
            if matched:
                hits.setdefault(item.key, []).append({"code": code, "rank": row.rank})
        scans.append(
            {
                "code": code,
                "status": "complete",
                "observed_at": datetime.now(UTC).isoformat(),
                "at_row_limit": len(rows) >= ROW_LIMIT,
                "rows": results,
            }
        )

    return [item for item in instruments if item.key in hits], {
        "source": "ibkr_scanner",
        "location": LOCATION,
        "row_limit": ROW_LIMIT,
        "min_price": MIN_PRICE,
        "scans": scans,
        "hits": hits,
    }
