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
    return df


def compute_rsi(closes, period=14):
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    return 100 - 100 / (1 + rs)


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
            pretty_price = f'{last_price:,.{decimals}f}'
            msg = (
                f'{icon} *Alerta RSI*\n\n'
                f'*{display}* · {tf} · RSI({period})\n'
                f'RSI {dir_word} {int(level)}\n'
                f'RSI actual: *{last_rsi:.1f}*\n'
                f'Precio: *{pretty_price}*'
            )
            if label:
                msg += f'\n\n_{label}_'
            msg += f'\n\n{datetime.now().strftime("%H:%M · %d %b")}'

            if send_whatsapp(phone, apikey, msg):
                print(f'    [OK] #{alert["id"]} -> {phone}')
                try:
                    supabase.table('alert_history').insert({
                        'alert_id': alert['id'], 'rsi_value': last_rsi,
                        'price': last_price, 'symbol': symbol, 'timeframe': tf,
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

    print('--- Fin ---')


if __name__ == '__main__':
    run()