import time
from typing import Dict, Any, Optional, List

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

# DexScreener endpoints
DEX_TOKEN = "https://api.dexscreener.com/latest/dex/tokens/{address}"
DEX_SEARCH = "https://api.dexscreener.com/latest/dex/search?q={q}"

app = FastAPI(title="Meme Scout Backend", version="0.2.1")

# CORS (чтобы расширение Chrome могло стучаться к бэку)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------ helpers ------------

async def fetch_json(url: str) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.json()


def pick_best_pair_from_list(
    pairs: List[Dict[str, Any]], prefer_chain: str = "sol"
) -> Optional[Dict[str, Any]]:
    """
    Берём лучшую (по ликвидности) пару, предпочитая указанный чейн.
    DexScreener может отдавать ключ chainId ("solana") или chain ("sol").
    """
    if not pairs:
        return None

    def liq_usd(p: Dict[str, Any]) -> float:
        liq = p.get("liquidity") or {}
        # иногда liquidity = {"usd": 12345}, иногда None
        return float((liq.get("usd") if isinstance(liq, dict) else 0) or 0)

    # сначала фильтруем предпочитаемый чейн (sol/solana)
    pref = [
        p
        for p in pairs
        if (p.get("chainId") or p.get("chain") or "").lower().startswith(
            prefer_chain.lower()
        )
    ]
    candidates = pref if pref else pairs
    candidates.sort(key=liq_usd, reverse=True)
    return candidates[0]


def compute_score(pair: Dict[str, Any]) -> Dict[str, Any]:
    """
    Простая эвристика 0–100 по базовым метрикам пары.
    """
    liq = ((pair.get("liquidity") or {}).get("usd") or 0) or 0
    tx5 = (pair.get("txns") or {}).get("m5") or {}
    buys = float(tx5.get("buys", 0) or 0)
    sells = float(tx5.get("sells", 0) or 0)
    total = buys + sells
    tpm = total / 5.0 if total > 0 else 0.0  # tx per minute (за 5 минут)
    buy_ratio = buys / total if total > 0 else 0.0
    pc = pair.get("priceChange") or {}
    m5 = pc.get("m5")
    h1 = pc.get("h1")
    boosted = bool(pair.get("boosts") or 0)
    created_ms = pair.get("pairCreatedAt") or 0
    age_min = max(0, (int(time.time() * 1000) - created_ms) / 60000) if created_ms else None

    s = 0
    reasons: List[str] = []

    # ликвидность
    if liq >= 25000:
        s += 15
        reasons.append("liq≥25k")
    elif liq >= 10000:
        s += 8
        reasons.append("liq≥10k")

    # активность
    if tpm >= 15:
        s += 10
        reasons.append("tx/min≥15")
    elif tpm >= 8:
        s += 6
        reasons.append("tx/min≥8")

    # соотношение покупок
    if buy_ratio >= 0.60:
        s += 10
        reasons.append("buy≥60%")
    elif buy_ratio >= 0.55:
        s += 6
        reasons.append("buy≥55%")

    # импульс
    if isinstance(m5, (int, float)) and m5 > 0:
        s += 5
        reasons.append("m5↑")
    if isinstance(h1, (int, float)) and h1 > 0:
        s += 5
        reasons.append("h1↑")

    if boosted:
        s += 5
        reasons.append("boosted")

    if age_min is not None and age_min <= 60:
        s += 5
        reasons.append("new≤60m")

    score_pct = max(0, min(100, int(round(s))))
    risk = "high" if score_pct < 40 else "mid" if score_pct < 70 else "low"

    return {
        "score": score_pct,
        "risk": risk,
        "reasons": reasons,
        "flags": {
            "hasWhales": False,  # зарезервировано под радар кошельков
            "fastMigration": bool(age_min is not None and age_min <= 10),
        },
        "metrics": {
            "liq": float(liq),
            "tpm": float(round(tpm, 2)),
            "buyRatio": float(round(buy_ratio, 3)),
            "m5": m5,
            "h1": h1,
            "ageMin": age_min,
        },
    }


# ------------ endpoints ------------

@app.get("/healthz")
async def healthz():
    return {"ok": True, "ts": int(time.time())}


@app.get("/score")
async def score(address: str = Query(...), chain: str = "sol"):
    """
    Оценка по mint-адресу.
    """
    data = await fetch_json(DEX_TOKEN.format(address=address))
    pair = pick_best_pair_from_list(data.get("pairs") or [], prefer_chain=chain)
    if not pair:
        raise HTTPException(status_code=404, detail="Token/pair not found")
    payload = compute_score(pair)
    payload["address"] = address
    payload["updatedAt"] = int(time.time())
    return payload


@app.get("/score/by-name")
async def score_by_name(q: str = Query(..., description="Ticker or name"), chain: str = "sol"):
    """
    Оценка по тикеру/имени.
    Отдаём лучшую по ликвидности пару, предпочитая указанный чейн (по умолчанию sol/solana).
    """
    data = await fetch_json(DEX_SEARCH.format(q=q))
    pairs = data.get("pairs") or []
    pair = pick_best_pair_from_list(pairs, prefer_chain=chain)
    if not pair:
        raise HTTPException(status_code=404, detail=f"No pairs found for query {q}")
    address = (
        (pair.get("baseToken") or {}).get("address")
        or (pair.get("quoteToken") or {}).get("address")
    )
    payload = compute_score(pair)
    payload["address"] = address
    payload["symbol"] = (pair.get("baseToken") or {}).get("symbol")
    payload["updatedAt"] = int(time.time())
    return payload


@app.get("/score/bulk")
async def score_bulk(addresses: str = Query(...)):
    """
    Пакетная оценка: /score/bulk?addresses=addr1,addr2,...
    """
    addrs = [a.strip() for a in addresses.split(",") if a.strip()]
    out: List[Dict[str, Any]] = []
    for a in addrs[:50]:
        try:
            data = await fetch_json(DEX_TOKEN.format(address=a))
            pair = pick_best_pair_from_list(data.get("pairs") or [])
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
