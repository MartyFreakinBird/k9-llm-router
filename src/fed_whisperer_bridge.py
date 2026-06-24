import asyncio

async def enrich_trading_request(payload: dict) -> dict:
    """
    Stub function for Fed Whisperer enrichment.
    Returns the payload unchanged.
    """
    return payload

async def start_fed_whisperer_poll_loop(interval_seconds: int = 30):
    """
    Stub async poll loop for Fed Whisperer.
    Logs a heartbeat every interval.
    """
    print(f"[fed-whisperer] Poll loop started (interval={interval_seconds}s)")
    while True:
        await asyncio.sleep(interval_seconds)
        print("[fed-whisperer] Heartbeat")
