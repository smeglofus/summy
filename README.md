# Symmy Task - ERP -> e-shop integrator

Synchronizace produktu z ERP feedu v `erp_data.json` do fiktivniho e-shop API
(`https://api.fake-eshop.cz/v1`). Stack: Django, Celery, Redis a Postgres.

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
                                                        +-> PATCH /products/{encoded_sku}/
```

| Soubor | Odpovednost |
|---|---|
| `integrator/loader.py` | Nacteni JSON feedu z disku. Chyby cteni a JSON parsovani vraci jako `ErpDataLoadError`. |
| `integrator/transformer.py` | Validace a normalizace ERP zaznamu. Invalidni zaznam zaloguje a preskoci. |
| `integrator/models.py` | `ProductSyncState` pro delta sync a recovery rozpracovaneho create vcetne `pending_payload_hash`. |
| `integrator/rate_limiter.py` | Redis sliding-window rate limit se sdilenym in-process fallbackem pro jeden proces. |
| `integrator/eshop_client.py` | HTTP klient, retry, URL encoding SKU a explicitni chyby pro `PATCH 404` a `POST 409`. |
| `integrator/sync_service.py` | Orchestrace load -> transform -> diff -> push. |
| `integrator/tasks.py` | Celery wrapper nad `SyncService.run()` s retry pro docasne API chyby. |

## Sync flow

Hash payloadu se pocita nad `NormalizedProduct.to_payload()`:

- `sku`
- `title`
- `price_with_vat`
- `total_stock`
- `color`

ERP feed nema `updated_at`, proto se delta urcuje pres SHA-256 hash payloadu.
`payload_hash` se uklada az po uspesne odpovedi e-shop API.

HTTP klient posila jen API kontrakt ze zadani: `X-Api-Key` a JSON payload.
Exactly-once/idempotency header neni predpokladany, proto recovery stoji na
lokalnim stavu `create_in_progress`, `pending_payload_hash`, PATCH-first recovery
a `409 -> PATCH` fallbacku.

Pro kazde SKU drzi sync jednoduchy per-SKU lock:

- na Postgresu opakovany `pg_try_advisory_lock` s timeoutem
- mimo Postgres procesovy `threading.Lock`, hlavne kvuli SQLite testum a lokalnimu behu

Lock neni globalni pro celou aplikaci. Serializuje jen zpracovani stejneho SKU.
Pokud se lock nepodari ziskat do `ERP_SYNC_SKU_LOCK_TIMEOUT_SECONDS`, produkt
selze jako retryable chyba a Celery muze sync zopakovat.

Sync je at-least-once. Recovery po nejistych create pokusech je best-effort:

1. Pokud lokalni state jeste nema potvrzeny remote produkt, sync ulozi
   `create_in_progress=True` a `pending_payload_hash=<aktualni hash>` pred prvnim POSTem.
2. Pokud dalsi beh najde `create_in_progress=True`, nejdriv zkusi
   `PATCH /products/{encoded_sku}/`.
3. Kdyz PATCH vrati 404, sync predpoklada, ze remote produkt neexistuje, a zkusi POST.
4. Kdyz POST vrati 409 conflict, sync predpoklada, ze remote produkt uz existuje, a prejde na PATCH.
5. Po uspesnem POST/PATCH se ulozi hash, `remote_exists=True`,
   `create_in_progress=False`, vynuluje se `pending_payload_hash`, ulozi se cas
   syncu a posledni HTTP status.

Bezny delta sync:

- stejny hash a `remote_exists=True` -> skip
- jiny hash a `remote_exists=True` -> PATCH
- PATCH 404 -> POST fallback
- POST 409 -> PATCH fallback

## Rate limiting

`EshopClient` vola limiter pred kazdym HTTP requestem. V Docker setupu pouziva
defaultni Django Redis cache (`REDIS_CACHE_URL=redis://redis:6379/2`) a sdileny
sliding-window limit, takze `ESHOP_RATE_LIMIT_PER_SEC=5` plati napric procesy,
ktere pouzivaji stejnou cache key.

Pokud cache backend neposkytuje Redis klienta nebo Redis selze, limiter prejde na
in-process fallback. Fallback je sdileny v ramci jednoho Python procesu podle
cache key/rate/per, proto chrani vice `EshopClient` instanci ve stejnem procesu.
Neni ale globalni garanci pro vice worker procesu nebo vice instanci aplikace.

## Celery semantics

Task `integrator.tasks.sync_erp_to_eshop` ma `acks_late=True` a
`reject_on_worker_lost=True`. Worker ma `worker_prefetch_multiplier=1`, aby si
jeden worker zbytecne nerezervoval vice sync tasku dopredu.

Kdyz worker zemre behem tasku, broker muze task dorucit znovu. Pokud k padu
doslo po remote side effectu a pred ulozenim hashe, recovery stoji na
`create_in_progress` a PATCH-first flow. Neni to exactly-once garance.

Task opakuje cely sync, pokud `SyncService.run()` vrati `retryable_failed > 0`.
To typicky znamena vycerpane retry po 429/5xx nebo sitove chybe. Jiz uspesne
produkty se pri dalsim behu preskoci pres ulozeny hash.

HTTP klient sam retryuje:

- 429 s respektem k `Retry-After`
- transientni 5xx
- network chyby z `requests`

Statistiky syncu oddeluji:

- `invalid` - neplatne ERP zaznamy, netriggeruji Celery retry
- `failed` - produkty nebo load feedu, ktere se nepodarilo dokoncit
- `retryable_failed` - subset `failed`, kvuli kteremu se Celery task retryuje

Chyba cteni souboru z disku je retryable. Nevalidni JSON nebo JSON mimo top-level
array je chyba dat a retry celeho tasku netriggeruje.

## Validace ERP dat

| Pole | Pravidlo | Pri poruseni |
|---|---|---|
| `id` | trimovana non-empty string, max 64 znaku | invalid zaznam se loguje a preskoci (`missing_sku` / `sku_too_long`) |
| `title` | non-empty string | invalid zaznam se loguje a preskoci (`missing_title`) |
| `price_vat_excl` | finite `Decimal > 0` | invalid zaznam se loguje a preskoci (`missing_price` / `invalid_price_type` / `non_positive_price`) |
| `stocks` | dict s aspon jednou integer hodnotou `>= 0`; `"N/A"` je povoleny sentinel | invalid zaznam se loguje a preskoci (`no_known_stock` / `invalid_stock_type` / `negative_stock`) |
| `attributes.color` | prazdne nebo chybejici -> `"N/A"` | fallback `"N/A"` |

Dulezite:

- `None` ve stocku je neplatna hodnota, ne "unknown"
- `float` stock je neplatny, i kdyz ma hodnotu `3.0`
- `NaN` a nekonecne ceny jsou neplatne
- duplicitni SKU v jednom feedu: posledni validni zaznam vyhrava a sync zaloguje warning
- pozdejsi invalidni duplicita neprebije drivejsi validni zaznam se stejnym SKU

## Konfigurace

Hlavni promenne z `.env.example` nebo environmentu:

- `DJANGO_SECRET_KEY`
- `POSTGRES_HOST`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`
- `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`, `REDIS_CACHE_URL`
- `ERP_DATA_FILENAME`
- `ESHOP_API_BASE_URL`, `ESHOP_API_KEY`
- `ESHOP_RATE_LIMIT_PER_SEC`, `ESHOP_RATE_LIMIT_CACHE_KEY`
- `ESHOP_MAX_RETRIES`
- `ESHOP_REQUEST_TIMEOUT`
- `ERP_SYNC_INTERVAL_SECONDS`
- `ERP_SYNC_TASK_RETRY_DELAY_SECONDS`
- `ERP_SYNC_TASK_MAX_RETRIES`
- `VAT_RATE`
- `ERP_SYNC_SKU_LOCK_TIMEOUT_SECONDS`, `ERP_SYNC_SKU_LOCK_POLL_SECONDS`

## Testy

Test suite pokryva hlavne:

- validace a edge cases v `transformer.py`
- duplicate SKU warning pri pravidlu "last wins"
- loader chyby zapocitane do `failed`
- retry a HTTP chovani `EshopClient`
- ze `POST /products/` neposila nevyzadane extra headers mimo API kontrakt
- URL encoding SKU v `PATCH /products/{sku}/`
- delta sync create / update / skip
- recovery po padu mezi POST a ulozenim state
- PATCH 404 -> POST fallback
- POST 409 -> PATCH fallback
- timeout/network failure pri create, kde dalsi beh zkusi PATCH-first recovery
- zmenu ERP payloadu behem rozpracovane create recovery
- ulozeni hashe az po uspesne API odpovedi
- sdileny in-process fallback rate limiter a regrese, ze spici thread nedrzi lock
- Celery task delegujici na `SyncService.run()` a task options
- Celery retry pri `retryable_failed`, bez retry pri invalidnim ERP feedu
- stabilni vypocet advisory lock id bez Postgresu
- timeout in-process SKU locku jako retryable chyba

## Vedoma omezeni

- Exactly-once semantika neni garantovana. Sync je at-least-once a recovery po
  nejistem create je best-effort pres `create_in_progress`, PATCH-first retry,
  `POST 409 -> PATCH`, `PATCH 404 -> POST` a `pending_payload_hash`. Bez podpory
  idempotency na strane e-shop API nelze vyloucit vsechny duplicity po nejistem
  remote side effectu.
- Distribuovany rate limit plati jen pri sdilene Redis cache. Pri fallbacku na
  in-process limiter se limit vztahuje pouze na jeden proces.
- Per-SKU lock je postaveny na Postgres advisory locku a drzi se i pres HTTP
  call. Pro rozsah test tasku je to jednoduche a brani soubehu stejneho SKU,
  ale pri dlouhych API latencich muze blokovat dalsi sync stejneho SKU. SQLite
  fallback serializuje jen vlakna v jednom procesu a slouzi pro testy/lokalni beh.
- Sync neresi lifecycle produktu, ktere zmizi z ERP feedu.
- Fake e-shop API je v testech mockovane; repo neobsahuje kontrakt proti realnemu
  endpointu.
