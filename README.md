# Symmy Task - ERP -> e-shop integrator

Synchronizacni mustek mezi ERP feedem v `erp_data.json` a fiktivnim e-shop API
(`https://api.fake-eshop.cz/v1`). Stack: Django, Celery, Redis, Postgres.

Reseni drzi rozsah zadani:

- nacteni ERP dat z JSON souboru
- transformace na payload pro e-shop
- delta sync pres hash payloadu
- rate limit 5 req/s a retry na 429 / 5xx / network chyby
- Celery task jako tenky wrapper nad sync sluzbou

## Spusteni

```bash
docker compose up --build
```

Rucni sync bez Celery:

```bash
docker compose exec web python manage.py run_sync
```

Testy v Dockeru:

```bash
docker compose exec web pytest
```

Lokalne bez Dockeru:

```bash
pip install -r requirements.txt
pytest
```

## Architektura

```text
erp_data.json -> loader -> transformer -> sync_service -> eshop_client
                                                        |
                                                        +-> POST /products/
                                                        +-> PATCH /products/{sku}/
```

### Moduly

| Soubor | Odpovednost |
|---|---|
| `integrator/loader.py` | Nacteni JSON feedu z disku. |
| `integrator/transformer.py` | Pure transformace a validace ERP zaznamu. Invalidni zaznam zaloguje a preskoci. |
| `integrator/models.py` | `ProductSyncState` pro delta sync. |
| `integrator/rate_limiter.py` | In-process sliding-window rate limit. |
| `integrator/eshop_client.py` | HTTP klient, retry, rate limiting. |
| `integrator/sync_service.py` | Orchestrator load -> transform -> diff -> push. |
| `integrator/tasks.py` | Tenky Celery wrapper bez business logiky. |

## Klicova rozhodnuti

### Delta sync pres hash payloadu

ERP feed nema `updated_at`, proto se porovnava SHA-256 hash nad
`NormalizedProduct.to_payload()`.

- neni state nebo `remote_exists=False` -> `POST /products/`
- stejny hash a `remote_exists=True` -> skip
- jiny hash a `remote_exists=True` -> `PATCH /products/{sku}/`

`payload_hash` se uklada az po uspesne 2xx odpovedi. Pokud API request selze,
produkt zustane pro dalsi beh znovu synchronizovatelny.

### Retry a rate limit

`EshopClient`:

- drzi limit 5 req/s pres in-process limiter
- retryuje 429 s respektem k `Retry-After`
- retryuje transientni 5xx a network chyby s exponential backoff
- zvedne `EshopError` pro permanentni chyby, vcetne `PATCH 404`

Rate limit plati pro jeden worker proces. `docker-compose.yml` proto pouziva
`celery ... --concurrency=1`.

## Validace ERP dat

| Pole | Pravidlo | Pri poruseni |
|---|---|---|
| `id` | non-empty string | invalid zaznam se loguje a preskoci (`missing_sku`) |
| `title` | non-empty string | invalid zaznam se loguje a preskoci (`missing_title`) |
| `price_vat_excl` | `Decimal > 0` | invalid zaznam se loguje a preskoci (`missing_price` / `invalid_price_type` / `non_positive_price`) |
| `stocks` | dict s aspon jednou integer hodnotou; `"N/A"` je povoleny sentinel | invalid zaznam se loguje a preskoci (`no_known_stock` / `invalid_stock_type`) |
| `attributes.color` | prazdne nebo chybejici -> `"N/A"` | fallback `"N/A"` |

Dulezite:

- `None` ve stocku je neplatna hodnota, ne "unknown"
- `float` stock je neplatny, i kdyz ma hodnotu `3.0`
- duplicitni SKU v jednom feedu: posledni zaznam vyhrava

## Konfigurace

Hlavni promenne z `.env.example`:

- `DJANGO_SECRET_KEY`
- `POSTGRES_HOST`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`
- `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`
- `ERP_DATA_FILENAME`
- `ESHOP_API_BASE_URL`, `ESHOP_API_KEY`
- `ESHOP_RATE_LIMIT_PER_SEC`
- `ESHOP_MAX_RETRIES`
- `ESHOP_REQUEST_TIMEOUT`
- `ERP_SYNC_INTERVAL_SECONDS`
- `VAT_RATE`

## Testy

Test suite pokryva hlavne:

- parametrizovane edge cases v `transformer.py`
- retry a HTTP chovani `EshopClient` pres `responses`
- delta sync create / update / skip
- ulozeni hashe az po uspesne API odpovedi
- rate limiter vcetne regrese, ze spici thread nedrzi lock
- Celery task delegujici na `SyncService.run()`

## Vedoma omezeni

Neco zustava zamerne mimo scope zadani:

- rate limiter je in-process, ne sdileny mezi vice worker procesy
- neresi se lifecycle "SKU zmizelo z ERP"
- fake API je porad jen mockovane v testech, ne realny endpoint
