from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(
    prefix="/n8n",
    tags=["n8n"]
)

@router.post("/webhook")
async def n8n_webhook(request: Request):
    """
    Receives webhook events from n8n.
    Accepts any JSON payload and returns a simple acknowledgement.
    """
    try:
        payload = await request.json()
    except Exception:
        payload = None

    # Log or process the payload here if needed
    print("[n8n webhook] Received payload:", payload)

    return JSONResponse(
        content={"status": "ok", "received": payload},
        status_code=200
    )
