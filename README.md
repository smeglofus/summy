# Symmy Task - ERP -> e-shop integrator

Synchronizacni mustek mezi ERP feedem v `erp_data.json` a fiktivnim e-shop API
(`https://api.fake-eshop.cz/v1`). Stack: Django, Celery, Redis, Postgres.

Fokus je na korektni sync semantice:

- transformace ERP dat do cisteho payloadu pro e-shop
- delta sync jen pro zmenene produkty
- retry na 429 / 5xx / network chyby
- quarantine misto tichych oprav vadnych dat
- singleton beh syncu bez prekryvu workeru
- recovery po padu mezi uspesnym create requestem a lokalnim DB zapisem

## Spusteni

```bash
docker compose up --build
```

Rucni sync bez Celery:

```bash
docker compose exec web python manage.py run_sync
```

Testy:

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
erp_data.json
    |
    v
loader -> transformer -> valid payloads
   |            |
   |            +-> QuarantinedProduct
   v
SyncService -> ProductSyncState -> EshopClient
                                   |
                                   +-> POST /products/
                                   +-> PATCH /products/{sku}/
```

### Moduly

| Soubor | Odpovednost |
|---|---|
| `integrator/loader.py` | Nacteni JSON feedu z disku. |
| `integrator/transformer.py` | Pure transformace a validace ERP zaznamu. |
| `integrator/models.py` | `ProductSyncState` a `QuarantinedProduct`. |
| `integrator/sync_lock.py` | Singleton lock pro cely sync. |
| `integrator/eshop_client.py` | HTTP klient, retry, rate limiting. |
| `integrator/sync_service.py` | Orchestrator load -> transform -> diff -> push. |
| `integrator/tasks.py` | Tenky Celery wrapper bez business logiky. |

## Klicova rozhodnuti

### Delta sync pres hash payloadu

ERP feed nema `updated_at`, proto se porovnava SHA-256 hash nad
`NormalizedProduct.to_payload()`.

- neni state -> novy produkt
- stejny hash -> skip
- jiny hash -> PATCH

`payload_hash` se uklada az po uspesne 2xx odpovedi.

### Singleton sync bez timeoutoveho locku

Produkce bezi na Postgresu, proto je cely sync chranen PostgreSQL advisory
lockem. Ten ma tri vyhody:

- nema TTL, takze neexpiruje uprostred dlouheho behu
- release je ownership-safe na stejnem DB spojeni
- pri padu procesu se lock uvolni automaticky zavrenim spojeni

V testech na SQLite se pouziva jen best-effort fallback pres Django cache,
protoze SQLite advisory lock nema.

### Recovery po padu mezi POST a DB write

Sync na nepopsanem `Idempotency-Key` nestavi korektnost celeho flow.

Pred prvnim `POST /products/` se do `ProductSyncState` ulozi lokalni marker
`create_in_progress=True`. Pokud worker spadne po uspesnem POSTu, ale pred
finalnim lokalnim zapisem hashe, dalsi beh:

1. uvidi `create_in_progress=True`
2. zkusi `PATCH /products/{sku}/`
3. pokud PATCH projde, jen dokonci lokalni stav bez druheho POSTu
4. pokud PATCH vrati 404, fallbackne na `POST /products/`

Tahle semantika je podlozena endpointem adresovanym podle SKU a nestoji na
nezdokumentovane podpore idempotency headeru.

### Quarantine je skutecne stav v DB

Vadna data se neopravuji potichu. Jdou do `QuarantinedProduct`.

- v aplikaci se existujici otevrena quarantine pro stejne SKU jen obnovi
- v DB je partial unique constraint: maximalne jedna otevrena quarantine na SKU
- pokud se SKU v dalsim feedu opravi, otevrena quarantine se uzavre pres
  `resolved_at`

### Retry a rate limit

`EshopClient`:

- drzi limit 5 req/s pres in-process limiter
- retryuje 429 s respektem k `Retry-After`
- retryuje transientni 5xx a network chyby s exponential backoff
- vraci specialni `RemoteProductMissingError` pro `PATCH 404`, aby sync mohl
  korektne udelat recovery create flow

Limit je garantovany pro jeden worker proces. `docker-compose.yml` proto pouziva
`celery ... --concurrency=1`.

## Validace ERP dat

| Pole | Pravidlo | Pri poruseni |
|---|---|---|
| `id` | non-empty string | quarantine `missing_sku` |
| `title` | non-empty string | quarantine `missing_title` |
| `price_vat_excl` | `Decimal > 0` | quarantine `missing_price` / `invalid_price_type` / `non_positive_price` |
| `stocks` | dict s aspon jednou integer hodnotou; `"N/A"` je povoleny sentinel | quarantine `no_known_stock` / `invalid_stock_type` |
| `attributes.color` | prazdne nebo chybejici -> `"N/A"` | fallback `"N/A"` |

Dulezite:

- `None` ve stocku je neplatna hodnota, ne "unknown"
- `float` stock je neplatny, i kdyz ma hodnotu `3.0`
- duplicitni SKU v jednom feedu: posledni zaznam vyhrava

## Konfigurace

Hlavni promenne z `.env.example`:

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
- quarantine resolve flow
- DB constraint na jednu otevrenou quarantine
- recovery po padu mezi remote success a lokalni finalizaci state

## Vedoma omezeni

Neco zustava zamerne mimo scope zadani:

- rate limiter je in-process, ne sdileny mezi vice worker procesy
- neresi se lifecycle "SKU zmizelo z ERP"
- neni implementovane alertovani nad quarantine
- fake API je porad jen mockovane v testech, ne realny endpoint
