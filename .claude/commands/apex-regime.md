Führe folgende Analyse als quant-researcher aus:

python3 -c "
from core.db import get_connection
from research.train_hmm import get_current_regime, load_features, label_states
import pickle, os, numpy as np

conn = get_connection()
assets = ['SOLUSDT','XRPUSDT','BTCUSDT','LINKUSDT','AVAXUSDT','ADAUSDT']

print('## HMM Regime-Status')
for asset in assets:
    path = f'data/hmm_models/{asset}_hmm_py312.pkl'
    if not os.path.exists(path):
        print(f'{asset}: kein Modell')
        continue
    regime = get_current_regime(asset, conn)
    obj = pickle.load(open(path,'rb'))
    states = obj['model'].predict(obj['scaler'].transform(
        load_features(asset, conn, lookback_days=3)
    ))
    # Stabilität: wie lange im aktuellen Regime?
    streak = 1
    for i in range(len(states)-2, -1, -1):
        if states[i] == states[-1]: streak += 1
        else: break
    print(f'{asset}: {regime} (seit {streak} Bars stabil)')
"

Zeige auch: welche Strategien sind aktuell aktiv/blockiert basierend auf Regime.