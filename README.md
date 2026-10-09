# ovh-ks-sniper

Monitora la disponibilità del Kimsufi **KS-STOR** (`24skstor01-v1`) e lo ordina automaticamente
tramite l'API ufficiale OVH appena torna disponibile e rispetta i requisiti:

- storage totale ≥ `MIN_STORAGE_TB`, letto dalle specifiche dei dischi nel catalogo OVH;
- canone mensile IVA inclusa ≤ `MAX_MONTHLY_PRICE` e primo pagamento (anteprima checkout) ≤ `MAX_FIRST_PAYMENT`;
- **un solo ordine**: dopo l'ordine scrive `/data/order.lock` e non ordina più finché il file esiste.

Valori verificati sull'API il 9/10/2026: la configurazione 4×4 TB + 500 GB NVMe (16,5 TB) costa
23,99 € + IVA 22 % = **29,27 €/mese**, setup uguale → primo pagamento stimato **58,54 €**. Le
varianti 4×6 TB, 6×8 TB e 6×12 TB costano di più e vengono scartate con i limiti di default.

## 1. Chiavi API

1. Vai su https://eu.api.ovh.com/createApp/, accedi e crea un'applicazione: ottieni
   **Application Key** e **Application Secret**.
2. `cp .env.example .env` e inseriscile in `OVH_APPLICATION_KEY` / `OVH_APPLICATION_SECRET`.
3. Genera la consumer key con i soli permessi necessari
   (GET/POST/DELETE `/order/cart*`, GET `/order/catalog/*`, GET `/me*`):
   ```bash
   docker compose run --rm ovh-ks-sniper python create_consumer_key.py
   ```
   Apri l'URL stampato, scegli validità **Unlimited**, conferma e copia la chiave in `OVH_CONSUMER_KEY`.

## 2. Metodo di pagamento (obbligatorio)

Il checkout usa `autoPayWithPreferredPaymentMethod`: sull'account OVH deve esserci un metodo di
pagamento **predefinito** valido (carta o SEPA) in *Manager → Fatturazione → Metodi di pagamento*.
All'avvio il programma lo verifica; in modalità live, se manca, si ferma.

## 3. Test in dry-run (default)

```bash
docker compose pull
docker compose run --rm ovh-ks-sniper python -m app.main --check
```
Mostra ogni combinazione con storage, prezzi IVA inclusa, esito dei requisiti e disponibilità, e verifica
credenziali e metodo di pagamento.

```bash
docker compose run --rm ovh-ks-sniper python -m app.main --test-cart gra
```
Esegue il flusso completo (carrello, opzioni, configurazione, anteprima checkout con i prezzi reali)
**anche senza stock**, senza mai fare il checkout. Poi avvia il servizio con `DRY_RUN=true`:

```bash
docker compose up -d
docker compose logs -f
```
Al primo restock vedrai `[DRY-RUN] Avrei ordinato: ...` (e la notifica Telegram, se configurata).

## 4. Passaggio all'ordine reale

1. Assicurati che `--check` e `--test-cart` siano andati a buon fine.
2. Nel `.env` imposta `DRY_RUN=false` (e lascia `WAIVE_RETRACTATION=true` per la consegna immediata:
   rinunci al diritto di recesso di 14 giorni).
3. `docker compose up -d` (ricrea il container con la nuova configurazione).

Dopo un ordine (o un checkout dall'esito incerto, es. timeout) il programma scrive `/data/order.lock`
e resta in pausa. Per un nuovo ordine elimina il file. Dopo un errore bloccante (pagamento, permessi,
errori ripetuti) scrive `/data/halt.json`: correggi il problema ed elimina il file per riprendere.

## 5. Unraid

L'immagine è pubblicata automaticamente su GitHub Container Registry a ogni push su `main`
(`ghcr.io/land2595/storina:latest`, amd64 e arm64): non serve clonare il repository né compilare.

1. Crea la cartella con i permessi giusti **prima** del primo avvio (se la crea Docker è di root e il
   container, che non gira come root, non può scriverci):
   ```bash
   mkdir -p /mnt/user/appdata/ovh-ks-sniper && chown 99:100 /mnt/user/appdata/ovh-ks-sniper
   ```
2. Metti `docker-compose.yml` e `.env` (copiato da `.env.example`) in una cartella, ad es.
   `/mnt/user/appdata/ovh-ks-sniper-compose/`, oppure crea uno stack con il plugin *Docker Compose Manager*.
3. Avvia:
   ```bash
   cd /mnt/user/appdata/ovh-ks-sniper-compose && docker compose pull && docker compose up -d
   ```
4. Aggiornamento dopo una nuova versione: `docker compose pull && docker compose up -d`.
5. Se il pacchetto su GitHub è privato, una volta sola: `docker login ghcr.io -u land2595` con un
   *Personal access token (classic)* con permesso `read:packages` come password.
6. Lock, stato e log (`logs/monitor.log`, a rotazione) finiscono in `/mnt/user/appdata/ovh-ks-sniper`.
   Il container gira come `99:100` (nobody:users). Lo stato *healthy/unhealthy* nella scheda Docker
   deriva dal file `heartbeat`, aggiornato ogni 15 s.

## Note

- Il polling usa l'endpoint pubblico senza autenticazione; intervallo minimo forzato 20 s, backoff
  esponenziale su errori e HTTP 429.
- Il carrello viene creato e assegnato in anticipo e ricreato prima della scadenza, così al restock
  restano solo aggiunta del server, opzioni, configurazione, anteprima e checkout.
- `24skstor012-v1` oggi non esiste né nel catalogo IT né nelle availabilities: se compare, aggiungilo a `PLAN_CODES`.
