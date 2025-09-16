
import os, time, math
from typing import List, Optional, Dict, Any
import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

DEX_URL = "https://api.dexscreener.com/latest/dex/tokens/{address}"

app = FastAPI(title="Meme Scout Backend", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

async def fetch_dex(address: str) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(DEX_URL.format(address=address))
        r.raise_for_status()
        return r.json()

def pick_best_pair(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    pairs = data.get("pairs") or []
    if not pairs: return None
    # Фокус на Solana; выбираем пару с наибольшей ликвидностью
    sol_pairs = [p for p in pairs if (p.get("chainId") or p.get("chain") or "").lower() in ("solana","sol")]
    if not sol_pairs:
        sol_pairs = pairs
    def liq_usd(p):
        liq = p.get("liquidity") or {}
        return float(liq.get("usd") or 0.0)
    sol_pairs.sort(key=liq_usd, reverse=True)
    return sol_pairs[0]

def compute_score(pair: Dict[str, Any]) -> Dict[str, Any]:
    liq = ((pair.get("liquidity") or {}).get("usd") or 0) or 0
    tx5 = (pair.get("txns") or {}).get("m5") or {}
    buys = float(tx5.get("buys", 0) or 0)
    sells = float(tx5.get("sells", 0) or 0)
    total = buys + sells
    tpm = total/5.0 if total>0 else 0.0
    buy_ratio = buys/total if total>0 else 0.0
    pc = pair.get("priceChange") or {}
    m5 = pc.get("m5")
    h1 = pc.get("h1")
    boosted = bool(pair.get("boosts") or 0)
    created_ms = pair.get("pairCreatedAt") or 0
    age_min = max(0, (int(time.time()*1000)-created_ms)/60000) if created_ms else None

    s = 0
    reasons = []
    if liq >= 25000: s+=15; reasons.append("liq≥25k")
    elif liq >= 10000: s+=8; reasons.append("liq≥10k")
    if tpm >= 15: s+=10; reasons.append("tx/min≥15")
    elif tpm >= 8: s+=6; reasons.append("tx/min≥8")
    if buy_ratio >= 0.6: s+=10; reasons.append("buy≥60%")
    elif buy_ratio >= 0.55: s+=6; reasons.append("buy≥55%")
    if isinstance(m5,(int,float)) and m5>0: s+=5; reasons.append("m5↑")
    if isinstance(h1,(int,float)) and h1>0: s+=5; reasons.append("h1↑")
    if boosted: s+=5; reasons.append("boosted")
    if age_min is not None and age_min <= 60: s+=5; reasons.append("new≤60m")

    score_pct = max(0, min(100, int(round(s))))
    risk = "high" if score_pct < 40 else "mid" if score_pct < 70 else "low"

    return {
        "score": score_pct,
        "risk": risk,
        "reasons": reasons,
        "flags": {
            "hasWhales": False,  # заполним позже, когда подключим радары кошельков
            "fastMigration": bool(age_min is not None and age_min <= 10)
        },
        "metrics": {
            "liq": float(liq),
            "tpm": float(round(tpm,2)),
            "buyRatio": float(round(buy_ratio,3)),
            "m5": m5,
            "h1": h1,
            "ageMin": age_min
        }
    }

@app.get("/score")
async def score(address: str = Query(..., description="Solana mint address"), chain: str = "sol"):
    try:
        data = await fetch_dex(address)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"DEX fetch failed: {e}")
    pair = pick_best_pair(data)
    if not pair:
        raise HTTPException(status_code=404, detail="Token/pair not found on DEX Screener")
    payload = compute_score(pair)
    payload["address"] = address
    payload["updatedAt"] = int(time.time())
    return payload

@app.get("/score/bulk")
async def score_bulk(addresses: str = Query(..., description="Comma-separated mint addresses")):
    addrs = [a.strip() for a in addresses.split(",") if a.strip()]
    out = []
    for a in addrs[:50]:  # safety cap
        try:
            data = await fetch_dex(a)
            pair = pick_best_pair(data)
            if not pair: 
                out.append({"address": a, "error": "not_found"})
                continue
            payload = compute_score(pair)
            payload["address"] = a
            payload["updatedAt"] = int(time.time())
            out.append(payload)
        except Exception as e:
            out.append({"address": a, "error": str(e)})
    return {"results": out}

@app.get("/healthz")
async def healthz():
    return {"ok": True, "ts": int(time.time())}
