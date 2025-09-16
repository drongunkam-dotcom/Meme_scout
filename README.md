Возвращает /score?address=<MINT> с предварительным скорингом токена на основе DexScreener (ликвидность, tx/min, buy-ratio, priceChange).

Локальный запуск (опционально)
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8080
# Откройте в браузере Cloud Shell: Preview on port 8080 → /healthz
