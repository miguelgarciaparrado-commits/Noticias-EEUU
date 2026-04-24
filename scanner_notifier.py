"""
scanner_notifier.py - v2
-------------------------
Lee activos desde la tabla `assets`, alertas desde `rsi_alerts`,
calcula RSI con yfinance, y envía WhatsApp via CallMeBot.

Variables de entorno (GitHub Secrets):
  SUPABASE_URL
  SUPABASE_SERVICE_KEY   (NUNCA anon key)
  WA_APIKEY_DEFAULT      (fallback si el usuario no configuró el suyo)
"""
import os
import sys
import time
import requests
import pandas as pd
from datetime import datetime, timezone
from supabase import create_client
import yfinance as yf

SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_SERVICE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')
WA_APIKEY_DEFAULT = os.environ.get('WA_APIKEY_DEFAULT', '')

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    print('ERROR: faltan SUPABASE_URL o SUPABASE_SERVICE_KEY')
    sys.exit(1)

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

YF_TF = {
    '15m': ('15m', '5d'),
    '1h':  ('60m', '60d'),
    '4h':  ('60m', '60d'),
    '12h': ('60m', '180d'),   # se resamplea desde 1h
    '1d':  ('1d', '2y'),
    '1w':  ('1wk', '10y'),
}


def load_assets_map():
    resp = supabase.table('assets').select('*').eq('active', True).execute()
    return {a['symbol']: a for a in (resp.data or [])}


def fetch_candles(yf_symbol, timeframe):
    interval, period = YF_TF.get(timeframe, ('1d', '2y'))
    try:
        df = yf.download(yf_symbol, period=period, interval=interval,
                         progress=False, auto_adjust=False, threads=False)
    except Exception as e:
        print(f'[ERROR] yfinance {yf_symbol} {timeframe}: {e}')
        return None
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if timeframe == '4h':
        df = df.resample('4h').agg({
            'Open': 'first', 'High': 'max', 'Low': 'min',
            'Close': 'last', 'Volume': 'sum',
        }).dropna()
    elif timeframe == '12h':
        df = df.resample('12h').agg({
            'Open': 'first', 'High': 'max', 'Low': 'min',
            'Close': 'last', 'Volume': 'sum',
        }).dropna()
    return df


def compute_rsi(closes, period=14):
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    return 100 - 100 / (1 + rs)




def get_current_price(asset, fallback):
    """Intenta obtener el precio en tiempo real. Si falla, devuelve fallback."""
    try:
        ds = asset.get('data_source')
        if ds == 'binance':
            sym = asset.get('binance_symbol') or asset['symbol']
            r = requests.get(f'https://api.binance.com/api/v3/ticker/price?symbol={sym}', timeout=10)
            if r.status_code == 200:
                return float(r.json().get('price', fallback))
        # TwelveData y yfinance: no hay endpoint de precio instantáneo gratuito,
        # el fallback (cierre de la última vela) es el mejor dato disponible.
    except Exception as e:
        print(f'    [WARN] precio real-time falló: {e}')
    return fallback

def send_whatsapp(phone, apikey, message):
    url = 'https://api.callmebot.com/whatsapp.php'
    params = {'phone': phone, 'text': message, 'apikey': apikey}
    try:
        r = requests.get(url, params=params, timeout=20)
        if r.status_code != 200:
            print(f'[WA fail] {r.status_code}: {r.text[:150]}')
            return False
        return True
    except Exception as e:
        print(f'[WA error] {e}')
        return False




# ============================================================
# DETECCIÓN DE DIVERGENCIAS
# ============================================================
import numpy as np

def find_pivots(df, rsi_series, left=5, right=5):
    """Pivots combinados en precio y RSI con ventana left/right."""
    high_pivots, low_pivots = [], []
    n = len(df)
    rsi_aligned = rsi_series.reindex(df.index)
    for i in range(left, n - right):
        rsi_i = rsi_aligned.iloc[i]
        if np.isnan(rsi_i):
            continue
        is_high = True
        is_low = True
        for j in range(i - left, i + right + 1):
            if j == i:
                continue
            other_rsi = rsi_aligned.iloc[j]
            if np.isnan(other_rsi):
                is_high = False; is_low = False; break
            if df['High'].iloc[j] > df['High'].iloc[i] or other_rsi > rsi_i:
                is_high = False
            if df['Low'].iloc[j] < df['Low'].iloc[i] or other_rsi < rsi_i:
                is_low = False
        ts = int(df.index[i].timestamp())
        if is_high:
            high_pivots.append({'idx': i, 'time': ts, 'price': float(df['High'].iloc[i]), 'rsi': float(rsi_i)})
        if is_low:
            low_pivots.append({'idx': i, 'time': ts, 'price': float(df['Low'].iloc[i]), 'rsi': float(rsi_i)})
    return high_pivots, low_pivots


def detect_divergences(df, rsi_series):
    """Detecta divergencias (regulares y ocultas) entre pivots CONSECUTIVOS."""
    MIN_BETWEEN = 5
    MAX_BETWEEN = 60
    MIN_PRICE_DIFF = 0.003
    MIN_RSI_DIFF = 3
    REQUIRE_ZONE = True

    high_pivots, low_pivots = find_pivots(df, rsi_series, 5, 5)
    results = []

    # Bajistas (sobre pivots altos consecutivos)
    for i in range(1, len(high_pivots)):
        p1, p2 = high_pivots[i-1], high_pivots[i]
        dist = p2['idx'] - p1['idx']
        if dist < MIN_BETWEEN or dist > MAX_BETWEEN:
            continue
        price_diff = (p2['price'] - p1['price']) / p1['price']
        rsi_diff = p1['rsi'] - p2['rsi']
        if price_diff > MIN_PRICE_DIFF and rsi_diff > MIN_RSI_DIFF:
            if not REQUIRE_ZONE or p1['rsi'] > 55 or p2['rsi'] > 55:
                results.append({'type': 'bear', 'p1': p1, 'p2': p2})
        elif price_diff < -MIN_PRICE_DIFF and rsi_diff < -MIN_RSI_DIFF:
            results.append({'type': 'hidden_bear', 'p1': p1, 'p2': p2})

    # Alcistas (sobre pivots bajos consecutivos)
    for i in range(1, len(low_pivots)):
        p1, p2 = low_pivots[i-1], low_pivots[i]
        dist = p2['idx'] - p1['idx']
        if dist < MIN_BETWEEN or dist > MAX_BETWEEN:
            continue
        price_diff = (p2['price'] - p1['price']) / p1['price']
        rsi_diff = p2['rsi'] - p1['rsi']
        if price_diff < -MIN_PRICE_DIFF and rsi_diff > MIN_RSI_DIFF:
            if not REQUIRE_ZONE or p1['rsi'] < 45 or p2['rsi'] < 45:
                results.append({'type': 'bull', 'p1': p1, 'p2': p2})
        elif price_diff > MIN_PRICE_DIFF and rsi_diff < -MIN_RSI_DIFF:
            results.append({'type': 'hidden_bull', 'p1': p1, 'p2': p2})

    return results


def process_divergence_alerts(assets_map):
    """Escanea divergencia_alerts y envía WhatsApp para las divergencias nuevas."""
    div_alerts = supabase.table('divergence_alerts').select('*').eq('active', True).execute().data or []
    if not div_alerts:
        return
    print(f'\n{len(div_alerts)} alertas de divergencia activas')

    # Agrupar por (symbol, tf, period)
    groups = {}
    for a in div_alerts:
        key = (a['symbol'], a['timeframe'], a['rsi_period'])
        groups.setdefault(key, []).append(a)

    for (symbol, tf, period), group in groups.items():
        asset = assets_map.get(symbol)
        if not asset or not asset.get('yfinance_symbol'):
            continue
        yf_sym = asset['yfinance_symbol']
        print(f'  [DIV] {symbol} ({yf_sym}) · {tf} · RSI({period})')
        df = fetch_candles(yf_sym, tf)
        if df is None or len(df) < period + 10:
            print('    [SKIP] datos insuficientes')
            continue
        rsi = compute_rsi(df['Close'], period)
        divs = detect_divergences(df, rsi)
        if not divs:
            continue
        # Tomar solo las divergencias más recientes (p2 entre las últimas 3 velas)
        last_time = int(df.index[-1].timestamp())
        recent_cutoff_idx = len(df) - 3
        fresh_divs = [d for d in divs if d['p2']['idx'] >= recent_cutoff_idx]
        if not fresh_divs:
            continue
        print(f'    Divergencias frescas: {len(fresh_divs)}')

        for alert in group:
            user_id = alert['user_id']
            # Verificar que no se haya ya notificado
            for d in fresh_divs:
                # Dedup contra tabla detected_divergences
                existing = supabase.table('detected_divergences').select('id,notified').eq(
                    'user_id', user_id
                ).eq('symbol', symbol).eq('timeframe', tf).eq(
                    'rsi_period', period
                ).eq('divergence_type', d['type']).eq(
                    'pivot2_time', d['p2']['time']
                ).execute().data
                if existing and existing[0].get('notified'):
                    continue  # ya notificada

                # Perfil
                prof_resp = supabase.table('profiles').select(
                    'wa_phone, wa_apikey'
                ).eq('user_id', user_id).maybe_single().execute()
                profile = prof_resp.data if prof_resp else None
                if not profile or not profile.get('wa_phone'):
                    continue
                phone = profile['wa_phone']
                apikey = profile.get('wa_apikey') or WA_APIKEY_DEFAULT
                if not apikey:
                    continue

                # Precio actual
                price_now = get_current_price(asset, d['p2']['price'])
                decimals = asset.get('decimals') or 2
                display = asset.get('display_name') or symbol
                is_bull = d['type'] in ('bull', 'hidden_bull')
                icon = '📈' if is_bull else '📉'
                labels = {
                    'bull': 'ALCISTA regular',
                    'bear': 'BAJISTA regular',
                    'hidden_bull': 'ALCISTA oculta',
                    'hidden_bear': 'BAJISTA oculta',
                }
                type_word = labels.get(d['type'], d['type'].upper())

                msg = (
                    f"{icon} *Divergencia {type_word}*\n\n"
                    f"*{display}* · {tf} · RSI({period})\n\n"
                    f"Precio: {d['p1']['price']:,.{decimals}f} → {d['p2']['price']:,.{decimals}f}\n"
                    f"RSI: {d['p1']['rsi']:.1f} → {d['p2']['rsi']:.1f}\n\n"
                    f"Precio ahora: *{price_now:,.{decimals}f}*\n\n"
                    f"{datetime.now().strftime('%H:%M · %d %b')}"
                )

                if send_whatsapp(phone, apikey, msg):
                    print(f'    [OK] Div {d["type"]} -> {phone}')
                    try:
                        supabase.table('detected_divergences').upsert({
                            'user_id': user_id,
                            'symbol': symbol,
                            'timeframe': tf,
                            'rsi_period': period,
                            'divergence_type': d['type'],
                            'pivot1_time': d['p1']['time'],
                            'pivot1_price': d['p1']['price'],
                            'pivot1_rsi': d['p1']['rsi'],
                            'pivot2_time': d['p2']['time'],
                            'pivot2_price': d['p2']['price'],
                            'pivot2_rsi': d['p2']['rsi'],
                            'notified': True,
                        }, on_conflict='user_id,symbol,timeframe,rsi_period,divergence_type,pivot2_time').execute()
                        supabase.table('divergence_alerts').update({
                            'last_triggered_at': datetime.now(timezone.utc).isoformat(),
                            'trigger_count': (alert.get('trigger_count') or 0) + 1,
                        }).eq('id', alert['id']).execute()
                    except Exception as e:
                        print(f'    [WARN] {e}')
                    time.sleep(7)


def run():
    print(f'--- Scanner notifier · {datetime.now(timezone.utc).isoformat()} ---')

    assets_map = load_assets_map()
    if not assets_map:
        print('No hay activos. Fin.')
        return
    print(f'{len(assets_map)} activos en catálogo')

    alerts = supabase.table('rsi_alerts').select('*').eq('active', True).execute().data or []
    if not alerts:
        print('Sin alertas activas.')
        return
    print(f'{len(alerts)} alertas activas')

    groups = {}
    for a in alerts:
        key = (a['symbol'], a['timeframe'], a['rsi_period'])
        groups.setdefault(key, []).append(a)

    for (symbol, tf, period), group_alerts in groups.items():
        asset = assets_map.get(symbol)
        if not asset:
            print(f'  [SKIP] {symbol} no está en tabla assets')
            continue
        yf_sym = asset.get('yfinance_symbol')
        if not yf_sym:
            print(f'  [SKIP] {symbol} sin yfinance_symbol')
            continue

        print(f'  {symbol} ({yf_sym}) · {tf} · RSI({period}) — {len(group_alerts)} alertas')
        df = fetch_candles(yf_sym, tf)
        if df is None or len(df) < period + 2:
            print('    [SKIP] datos insuficientes')
            continue

        rsi = compute_rsi(df['Close'], period).dropna()
        if len(rsi) < 2:
            continue
        last_rsi = float(rsi.iloc[-1])
        prev_rsi = float(rsi.iloc[-2])
        last_price = float(df['Close'].iloc[-1])
        last_time = int(df.index[-1].timestamp())

        for alert in group_alerts:
            level = float(alert['level'])
            cond = alert['condition']
            fired = (
                cond == 'cross_up' and prev_rsi < level <= last_rsi
            ) or (
                cond == 'cross_down' and prev_rsi > level >= last_rsi
            )
            if not fired:
                continue
            if alert.get('last_candle_time') == last_time:
                continue

            prof_resp = supabase.table('profiles').select('wa_phone, wa_apikey') \
                .eq('user_id', alert['user_id']).maybe_single().execute()
            profile = prof_resp.data if prof_resp else None
            if not profile or not profile.get('wa_phone'):
                print(f'    [SKIP] user {alert["user_id"]} sin teléfono')
                continue

            phone = profile['wa_phone']
            apikey = profile.get('wa_apikey') or WA_APIKEY_DEFAULT
            if not apikey:
                print('    [SKIP] sin APIKEY')
                continue

            icon = '📈' if cond == 'cross_up' else '📉'
            dir_word = 'cruzó AL ALZA' if cond == 'cross_up' else 'cruzó A LA BAJA'
            label = alert.get('label') or ''
            decimals = asset.get('decimals') or 2
            display = asset.get('display_name') or symbol
            # Precio en tiempo real en el momento del envío
            current_price = get_current_price(asset, last_price)
            pretty_price = f'{current_price:,.{decimals}f}'
            pretty_close = f'{last_price:,.{decimals}f}'
            msg = (
                f'{icon} *Alerta RSI*\n\n'
                f'*{display}* · {tf} · RSI({period})\n'
                f'RSI {dir_word} {level:.1f}\n'
                f'RSI actual: *{last_rsi:.1f}*\n'
                f'Precio ahora: *{pretty_price}*\n'
                f'Cierre vela: {pretty_close}'
            )
            if label:
                msg += f'\n\n_{label}_'
            msg += f'\n\n{datetime.now().strftime("%H:%M · %d %b")}'

            if send_whatsapp(phone, apikey, msg):
                print(f'    [OK] #{alert["id"]} -> {phone}')
                try:
                    supabase.table('alert_history').insert({
                        'alert_id': alert['id'], 'rsi_value': last_rsi,
                        'price': current_price, 'symbol': symbol, 'timeframe': tf,
                    }).execute()
                    supabase.table('rsi_alerts').update({
                        'last_triggered_at': datetime.now(timezone.utc).isoformat(),
                        'last_candle_time': last_time,
                        'trigger_count': (alert.get('trigger_count') or 0) + 1,
                    }).eq('id', alert['id']).execute()
                except Exception as e:
                    print(f'    [WARN] {e}')
            else:
                print(f'    [FAIL] #{alert["id"]}')

            time.sleep(7)

    # Procesar alertas de divergencias
    try:
        process_divergence_alerts(assets_map)
    except Exception as e:
        print(f'[ERROR divergencias] {e}')

    print('--- Fin ---')


if __name__ == '__main__':
    run()
