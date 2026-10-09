"""عیب‌یابی اتصال:  python -m roshd_bot.main --diag"""
from __future__ import annotations

import asyncio
import os
import socket
import urllib.request

import httpx

from .config import load_config

HOST = "tapi.bale.ai"


async def _try(label, **kw):
    try:
        async with httpx.AsyncClient(timeout=12, **kw) as c:
            r = await c.get(f"https://{HOST}/")
        print(f"  [OK ] {label}: HTTP {r.status_code}")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  [FAIL] {label}: {type(e).__name__}: {e}")
        return False


async def run() -> None:
    cfg = load_config()
    print("1) Proxy settings seen by Python")
    env = {k: v for k, v in os.environ.items() if k.lower() in ("http_proxy", "https_proxy", "all_proxy")}
    print("  env vars     :", env or "none")
    print("  system/registry:", urllib.request.getproxies() or "none")
    print(f"2) DNS for {HOST}")
    try:
        print("  ", sorted({a[4][0] for a in socket.getaddrinfo(HOST, 443)}))
    except OSError as e:
        print("  [FAIL] DNS:", e)
    print("3) Raw TCP to port 443")
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(HOST, 443), 10)
        w.close()
        print("  [OK ] TCP connected")
    except Exception as e:  # noqa: BLE001
        print(f"  [FAIL] TCP: {type(e).__name__}: {e}")
    print("4) HTTPS request")
    direct = await _try("direct (no proxy)", trust_env=False)
    system = await _try("via system proxy + env settings (trust_env)", trust_env=True)
    custom = None
    if cfg.bale_proxy:
        custom = await _try(f"via BALE_PROXY={cfg.bale_proxy}", proxy=cfg.bale_proxy, trust_env=False)
    print("\nRecommendation:")
    if direct:
        print("  Direct works -> keep USE_SYSTEM_PROXY=0 and BALE_PROXY empty (default).")
    elif system:
        print("  Only the system proxy/env settings work -> set USE_SYSTEM_PROXY=1 in .env")
    elif custom:
        print("  Only BALE_PROXY works -> keep it in .env")
    else:
        print("  Nothing works: check internet/VPN/firewall, or try another network (phone hotspot).")


if __name__ == "__main__":
    asyncio.run(run())
