"""
TradingView Webhook Bot – entrypoint.
Run: python main.py
"""
import uvicorn

from config import settings

if __name__ == "__main__":
    uvicorn.run(
        "server:app",
        host=settings.host,
        port=settings.port,
        proxy_headers=True,
        forwarded_allow_ips=settings.trusted_proxy_ips,
        reload=False,
    )
