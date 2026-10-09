# Storina · KS-STOR sniper

Monitora la disponibilità del Kimsufi **KS-STOR** (`24skstor01-v1`) e lo ordina automaticamente
tramite l'API ufficiale OVH appena torna disponibile e rispetta i requisiti:

- storage totale ≥ `MIN_STORAGE_TB`, letto dalle specifiche dei dischi nel catalogo OVH;
- canone mensile IVA inclusa ≤ `MAX_MONTHLY_PRICE` e primo pagamento (anteprima checkout) ≤ `MAX_FIRST_PAYMENT`;
- **un solo ordine**: dopo l'ordine scrive `/data/order.lock` e non ordina più finché il lock esiste.

Include una **web UI** (porta 8765) per inserire tutti i dati e seguire in diretta controlli,
disponibilità per datacenter, tentativi d'ordine e log.

Valori verificati sull'API il 9/10/2026: la configurazione 4×4 TB + 500 GB NVMe (16,5 TB) costa
23,99 € + IVA 22 % = **29,27 €/mese**, setup uguale → primo pagamento stimato **58,54 €**.

## Installazione su Unraid

L'immagine è pubblicata automaticamente su GitHub Container Registry a ogni push su `main`
(`ghcr.io/land2595/storina:latest`, linux/amd64).

1. Crea le cartelle (quella dei dati deve appartenere a `99:100`, l'utente del container):
   ```bash
   mkdir -p /mnt/user/appdata/ovh-ks-sniper /mnt/user/appdata/ovh-ks-sniper-compose && chown 99:100 /mnt/user/appdata/ovh-ks-sniper
   ```
2. Scarica `docker-compose.yml` e crea un `.env` (può restare quasi vuoto: tutto si configura dalla web UI):
   ```bash
   cd /mnt/user/appdata/ovh-ks-sniper-compose && wget -q https://raw.githubusercontent.com/land2595/storina/main/docker-compose.yml && wget -q -O .env https://raw.githubusercontent.com/land2595/storina/main/.env.example
   ```
   In alternativa usa il plugin *Docker Compose Manager* incollando i due file in uno stack.
3. Avvia:
   ```bash
   docker compose pull && docker compose up -d
   ```
4. Apri **http://IP-UNRAID:8765** e scegli la password della web UI al primo accesso
   (oppure impostala prima con `WEB_PASSWORD` nel `.env`).

Aggiornamento: `docker compose pull && docker compose up -d`.

## Configurazione dalla web UI

Scheda **Configurazione**:

1. Crea un'applicazione su https://eu.api.ovh.com/createApp/ e incolla **Application Key** e
   **Application Secret**, poi *Salva e applica*.
2. *Genera consumer key*: apri il link mostrato, accedi a OVH, scegli validità **Unlimited** e conferma.
   La chiave chiede solo i permessi necessari (GET/POST/DELETE `/order/cart*`, GET `/order/catalog/*`, GET `/me*`).
3. *Ho convalidato: verifica e applica*: controlla credenziali e metodo di pagamento.
4. Facoltativo: token e chat ID Telegram per le notifiche.

I valori salvati dalla UI finiscono in `/data/config.json` (permessi 600) e hanno la precedenza sul `.env`;
il badge accanto a ogni campo dice da dove arriva il valore e *ripristina* torna a quello del `.env`.
Le chiavi segrete salvate non vengono mai rimandate al browser.

**Metodo di pagamento**: il checkout usa `autoPayWithPreferredPaymentMethod`, quindi sull'account OVH
serve un metodo di pagamento **predefinito** valido (*Manager → Fatturazione → Metodi di pagamento*).

## Dry-run, test e ordine reale

- Si parte sempre in **dry-run**: al restock fa tutto (carrello, opzioni, configurazione, anteprima
  checkout con i prezzi reali) tranne il pagamento, e registra cosa avrebbe ordinato.
- **Avvia test** (Dashboard) esegue lo stesso flusso in un datacenter a scelta **anche senza stock**,
  senza mai fare il checkout: serve a verificare tutto prima di passare al reale.
- Per l'ordine reale disattiva *Dry-run* nella configurazione: la UI chiede di scrivere `ORDINA`.
  *Rinuncia al diritto di recesso* attiva (default) serve per la consegna immediata.

Dopo un ordine, o un checkout con esito incerto (es. timeout), la Dashboard mostra il lock e il
programma non ordina più: *Rimuovi lock* (con conferma `SBLOCCA`) lo riabilita. Dopo un errore
bloccante (pagamento, permessi, errori ripetuti) mostra il blocco: correggi e premi *Riprendi*.

## Riga di comando (facoltativa)

```bash
docker compose run --rm ovh-ks-sniper python -m app.main --check
docker compose run --rm ovh-ks-sniper python -m app.main --test-cart gra
```

## Dati in `/data`

`config.json` (configurazione dalla UI), `web_auth.json` (hash della password), `order.lock`,
`halt.json`, `attempts.jsonl` (storico tentativi), `logs/monitor.log` (a rotazione), `heartbeat`
(usato dall'healthcheck Docker).

## Note

- Il polling usa l'endpoint pubblico senza autenticazione; intervallo minimo forzato 20 s, backoff
  esponenziale su errori e HTTP 429.
- Il carrello viene creato e assegnato in anticipo e ricreato prima della scadenza.
- La web UI è pensata per la rete di casa: non esporla su Internet senza un reverse proxy con HTTPS.
- `24skstor012-v1` oggi non esiste né nel catalogo IT né nelle availabilities.
