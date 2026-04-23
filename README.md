# Daily Economic Calendar WhatsApp Notifier

Cada día laborable a las **08:00 (hora Madrid)** recibes por WhatsApp los eventos
económicos de **alto impacto (3⭐)** del día, según el calendario de ForexFactory.

Todo corre gratis en GitHub Actions.

---

## Setup (≈ 10 minutos)

### 1. Activar CallMeBot para WhatsApp (gratis)

CallMeBot es un servicio gratuito que permite enviarte mensajes a ti mismo
por WhatsApp desde cualquier script, sin montar API oficial de Meta.

1. Guarda este contacto en tu móvil: **+34 644 51 95 23** (nombre: CallMeBot).
2. Desde tu WhatsApp, envíale exactamente este mensaje:
   ```
   I allow callmebot to send me messages
   ```
3. En unos minutos te responde con tu **API key**. Apúntala.

Si no responde rápido, reintenta pasados 2 min.
Docs: https://www.callmebot.com/blog/free-api-whatsapp-messages/

### 2. Crear repo en GitHub

1. Crea un repo **privado** en GitHub (puede llamarse `econ-calendar-notifier`).
2. Sube estos archivos respetando la estructura:
   ```
   econ-calendar-notifier/
   ├── fetch_and_notify.py
   ├── requirements.txt
   └── .github/
       └── workflows/
           └── daily-notify.yml
   ```

### 3. Configurar los secrets

En tu repo: **Settings → Secrets and variables → Actions → New repository secret**

Añade dos secrets:

| Nombre      | Valor                                                        |
|-------------|--------------------------------------------------------------|
| `WA_PHONE`  | Tu número con código de país **sin `+`** (ej: `34612345678`) |
| `WA_APIKEY` | La API key que te dio CallMeBot                              |

### 4. Probar manualmente

1. Ve a **Actions** en tu repo.
2. Selecciona el workflow **Daily Economic Calendar WhatsApp**.
3. Click en **Run workflow → Run workflow**.
4. En unos segundos deberías recibir un WhatsApp con los eventos de hoy
   (o un mensaje diciendo que no hay eventos 3⭐).

El trigger manual (`workflow_dispatch`) salta el check de hora y envía
inmediatamente. El cron automático solo envía a las 08:00.

---

## Cómo funciona

- **Fuente:** `https://nfs.faireconomy.media/ff_calendar_thisweek.json`
  (feed oficial no-oficial de ForexFactory, los mismos eventos y estrellas
  que ves en Investing.com)
- **Filtro:** `impact == "High"` → 3⭐
- **Horario:** Cron doble para cubrir CET/CEST. El script valida hora local
  `Europe/Madrid` y aborta si no son las 08:00. Esto maneja DST sin
  configuración manual dos veces al año.
- **Días:** Solo lunes a viernes. Para incluir fines de semana, pon
  `WEEKDAYS_ONLY = False` en el script.

## Personalización rápida

Todo está arriba de `fetch_and_notify.py`:

```python
LOCAL_TZ = ZoneInfo("Europe/Madrid")   # cambia la zona si hace falta
TARGET_HOUR = 8                         # cambia a la hora que quieras
WEEKDAYS_ONLY = True                    # False para incluir fin de semana
```

Si quieres filtrar por moneda (ej. solo USD + EUR), en
`filter_today_high_impact()` añade:

```python
if e.get("country") not in {"USD", "EUR"}:
    continue
```

## Troubleshooting

- **No llega el mensaje:** mira los logs en Actions, busca la línea
  `Response body:`. Si CallMeBot devuelve `APIKey is incorrect`, revisa el
  secret. Si dice `phone not activated`, reenvía el mensaje de activación.
- **GitHub Actions a veces se retrasa 10–15 min.** Es normal y conocido.
  El check `hour == 8` tolera retrasos de hasta 59 min.
- **"Request Denied" de ForexFactory:** el límite es 2 requests cada 5 min.
  Nosotros hacemos 1 al día, así que no debería pasar salvo que pruebes
  muchos runs manuales seguidos.
