# AGENTS.md

Tento dokument je "ground truth" pro AI agenty (Claude Code, Cursor, …) pracující
na tomto repu. Stručná pravidla a invarianty, které by jinak agent musel
rekonstruovat z kódu pokaždé znovu.

## Stack & příkazy

- **Python 3.11**, Django 4.2+, Celery + Redis, Postgres 15.
- Vše běží přes Docker Compose:
  - `docker compose up --build` — spustí web + worker + beat + db + redis
  - `docker compose exec web pytest` — testy
  - `docker compose exec web python manage.py run_sync` — sync ručně
  - `docker compose exec web python manage.py makemigrations integrator` — po
    změně modelu
- Testy běží i lokálně bez Dockeru — `pytest` (SQLite in-memory přes
  `core.test_settings`).

## Architektonické invarianty

1. **`integrator/tasks.py` je tenký.** Celery task pouze deleguje na
   `SyncService.run()`. Žádná business logika v tasku.
2. **`transformer.py` je pure functions.** Nesahá na DB, nesahá na síť. Vstup
   `list[dict]`, výstup `(valid, quarantined)`. Snadno testovatelné bez fixtur.
3. **`eshop_client.py` neví nic o ERP.** Dostane payload (dict), pošle ho.
   Nezná `NormalizedProduct`, nezná hash, nezná `ProductSyncState`.
4. **Peníze jsou `Decimal`.** Nikdy `float`. Payload k API serializuje jako
   string s 2 desetinnými místy.
5. **Hash do `ProductSyncState.payload_hash` se ukládá až po 2xx odpovědi.**
   Jinak by se selhání už nezopakovala — kritická chyba.

## DO NOT

- **Nemockuj `requests.post` přímo** — používej `responses` jako v
  `test_eshop_client.py`. Mock na úrovni HTTP, ne na úrovni metody.
- **Nepřidávej business logiku do Celery tasku.**
- **Neopravuj data ze zdroje** (negativní cena → `abs()`, null cena → `0`,
  N/A sklad → `0`). Špatná data jdou do `QuarantinedProduct`, nikdy se neupravují.
- **Nepřidávej nové závislosti** bez vážného důvodu. Aktuálně vystačíme s tím,
  co je v `requirements.txt`.
- **Nezakládej PR bez testů.** Každá nová cesta v `transformer.py` nebo
  `eshop_client.py` má parametrized test.

## Mapa modulů

```
integrator/
├── loader.py            # json.load z disku
├── schemas.py           # dataclassy: NormalizedProduct, QuarantineRecord, ValidationError
├── transformer.py       # raw dict -> NormalizedProduct + reject reasons
├── models.py            # ProductSyncState, QuarantinedProduct
├── rate_limiter.py      # sliding-window in-process
├── eshop_client.py      # requests.Session + retry + rate limit
├── sync_service.py      # orchestrátor + stable_hash
├── tasks.py             # Celery task (thin)
├── management/commands/run_sync.py   # manuální spuštění
└── tests/               # pytest, responses pro HTTP
```

## Validační pravidla (viz README.md)

Když přidáš nový edge case do `transformer.py`, přidej i parametrized test do
`test_transformer.py` a aktualizuj tabulku v README.

## Pokud něco nesedí

Nesnaž se to "vyřešit" odbočkou. Když narazíš na rozhodnutí, které není
zapsané tady ani v README, **napiš to do PR description** — člověk to schválí
a do AGENTS.md to doplní příště.
