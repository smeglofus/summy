# Symmy Tasker — ERP → e-shop integrator

Synchronizační můstek mezi ERP systémem (soubor `erp_data.json`) a fiktivním
e-shop API (`https://api.fake-eshop.cz/v1`). Django + Celery + Redis + Postgres.

Čte produkty z ERP, validuje je a přepočítá DPH, posílá jen změněné produkty
(delta sync přes hash payloadu). Rate-limited 5 req/s s retry na 429 / 5xx,
invalidní data jdou do karantény místo tichých oprav.

## Spuštění

```bash
docker compose up --build
```

Při startu `web` služby se automaticky aplikují migrace. `beat` plánuje
periodickou synchronizaci (default každých 5 min, viz `ERP_SYNC_INTERVAL_SECONDS`).

Ruční spuštění syncu (in-process, bez Celery — vhodné pro debug):

```bash
docker compose exec web python manage.py run_sync
```

Vyvolání Celery tasku z Django shellu:

```bash
docker compose exec web python manage.py shell
>>> from integrator.tasks import sync_erp_to_eshop
>>> sync_erp_to_eshop.delay()
```

> **Poznámka k fake API:** výchozí `ESHOP_API_BASE_URL=https://api.fake-eshop.cz/v1`
> je záměrně fiktivní endpoint ze zadání — bez přepsání na reálný nebo lokálně
> mockovaný endpoint bude skutečný sync request selhávat. Chování API je v
> testech ověřeno přes HTTP-level mocky (`responses`).

## Testy

```bash
docker compose exec web pytest
```

Testy běží proti SQLite in-memory (viz `core/test_settings.py`), takže je
možné je pustit i lokálně bez Postgresu:

```bash
pip install -r requirements.txt
pytest
```

## Architektura

```
ERP (erp_data.json)
        |
        v
   loader  -> transformer (validate + dedupe + VAT + sum stocks)
        |              \
        |               +--> QuarantinedProduct  (invalid records)
        v
   sync_service -> hash compare -> ProductSyncState (delta)
        |
        v
   eshop_client (rate limit 5/s, retry on 429 / 5xx)
        |
        v
   POST /products/   (new)     PATCH /products/{sku}/   (changed)
```

### Vrstvy (`integrator/`)

| Soubor | Odpovědnost |
|---|---|
| `loader.py` | Čte `erp_data.json` z disku. |
| `schemas.py` | `NormalizedProduct`, `QuarantineRecord`, `ValidationError`. |
| `transformer.py` | Pure funkce: validace + transformace + dedup. |
| `models.py` | `ProductSyncState` (delta sync), `QuarantinedProduct` (rejecty). |
| `rate_limiter.py` | Sliding-window limiter (in-process, single worker). |
| `eshop_client.py` | HTTP klient, rate limit, retry s respektem k `Retry-After`. |
| `sync_service.py` | Orchestrátor: load → transform → diff přes hash → push. |
| `tasks.py` | Tenký Celery task — pouze deleguje na `SyncService`. |
| `management/commands/run_sync.py` | Synchronní spuštění syncu. |

### Klíčová rozhodnutí

**Delta sync přes SHA-256 hash payloadu.** ERP data nemají `updated_at`, takže
nejjednodušší robustní řešení je porovnání kanonického JSON hashe se stavem v DB:

- není v DB → `POST /products/`
- v DB, hash se shoduje → skip
- v DB, hash se liší → `PATCH /products/{sku}/`

Hash se ukládá **až po úspěšné odpovědi** — jinak by se selhání už nezopakovala.

**Rate limit token-window v paměti procesu.** Zadání mluví o jednom workeru,
in-process limiter je tedy dostatečný a má nulové závislosti. Při scale-outu
přejít na Redis-backend (rozhraní `RateLimiter` zůstává stejné).

**Retry respektuje `Retry-After`.** Pokud server pošle hodnotu jako header
(integer/sekundy), použije se. Jinak fallback na exponenciální backoff.

**Quarantine, ne self-healing.** Záznamy s `null` / zápornou cenou, s neplatným
stock payloadem nebo bez známých skladů se neopravují potichu, ale jdou do
`QuarantinedProduct` k revizi. Opakovaný výskyt stejného SKU pouze obnoví
existující otevřený záznam (`last_seen_at`, `raw_payload`, `reason`) místo
zakládání duplicit. Při dalším syncu, pokud ERP pošle SKU opravené, se
quarantine sám uzavře (`resolved_at`). Důvod: nikdy si nepřebírat
zodpovědnost za chyby zdroje — e-shop nesmí dostat produkt za -150 Kč.

**Decimal pro peníze.** `12400.50 * 1.21` se v `float` zaokrouhlí jinak než v
`Decimal`. Cena se ukládá jako string ve formátu s 2 desetinnými místy.

### Pravidla validace

| Pole | Pravidlo | Při porušení |
|---|---|---|
| `id` | non-empty string | quarantine `missing_sku` |
| `title` | non-empty string | quarantine `missing_title` |
| `price_vat_excl` | `Decimal > 0` | quarantine `missing_price` / `invalid_price_type` / `non_positive_price` |
| `stocks` | dict s ≥ 1 integer hodnotou; sentinel `"N/A"` se ignoruje | quarantine `no_known_stock` / `invalid_stock_type` |
| `attributes.color` | string nebo nic | fallback `"N/A"` |

Duplicity SKU: poslední záznam vítězí.

## Konfigurace

Viz `.env.example`. Klíčové proměnné:

- `ESHOP_API_BASE_URL`, `ESHOP_API_KEY`
- `ESHOP_RATE_LIMIT_PER_SEC` (default 5)
- `ESHOP_MAX_RETRIES` (default 5)
- `ERP_SYNC_INTERVAL_SECONDS` (default 300)
- `VAT_RATE` (default 0.21)

## Vědomá zjednodušení (out of scope pro tohle zadání)

Vědomě zúžené, ne přehlédnuté. V reálném nasazení by se řešilo:

- **Sklady se sčítají bez whitelistu.** Externí nebo servisní sklad by neměl
  jít do prodejného stocku — řešilo by se přes konfiguraci povolených poboček.
- **Lifecycle „SKU zmizelo z ERP" se neřeší.** Sync aktuálně jen vytváří a
  updatuje. Produkčně by se hodila archivace / unpublish / `stock=0` pro
  produkty, které z feedu vypadly.
- **Duplicitní SKU = „last wins" tiše.** Pohodlné, ale produktově je to
  datový incident — chtělo by to alert, ne spoléhat na pořadí záznamů.
- **`color = "N/A"` jako sentinel v payloadu.** V reálu by se buď pole
  neposílalo, nebo poslalo `null` — záleží na kontraktu cílového API.
- **Jedna globální VAT sazba.** Pro vícekategoriovou nabídku by se hodila
  daňová třída na úrovni produktu (ERP feed ji ale dnes nenese).

## Poznámky k produkčnímu nasazení

Infra/provozní část, která by se řešila při nasazení mimo demo:

- **Distribuovaný lock** přes Redis, pokud má běžet více workerů paralelně.
- **Redis-backed rate limiter** ze stejného důvodu.
- **Alerting** nad `QuarantinedProduct` (Slack webhook nebo dashboard) místo
  jen `last_seen_at`.
- **Sentry / strukturované logy** místo plain stdlib loggeru.
