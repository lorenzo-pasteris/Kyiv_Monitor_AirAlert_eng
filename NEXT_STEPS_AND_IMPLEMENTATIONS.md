# Kyiv Monitor — next steps

Questo documento contiene soltanto lavoro futuro ancora utile. La descrizione del sistema è nel
`README.md` e in `docs/PROJECT_ARCHITECTURE.md`; le verifiche già eseguite sono in
`docs/TEST_LOG.md`.

## Baseline attuale

- Un worker Railway legge Telegram con Telethon e pubblica tramite Telegram Bot API.
- `@kyiv_airraid_alert` determina lo stato NORMAL/ALERT.
- In ALERT, `@kyiv_alerts` alimenta traduzione e pubblicazione a bassa latenza.
- In NORMAL, SQLite conserva messaggi e cursori prima del riepilogo Anthropic.
- GitHub Actions esegue compilazione e suite `unittest`.
- Il worker usa una sola replica; SQLite resta quindi la scelta più semplice.

## Priorità operative

### P0 — Verifica end-to-end degli edit Telegram

Il listener gestisce sia nuovi messaggi sia modifiche di `@kyiv_alerts`. Prima del merge in
produzione verificare in staging questa sequenza:

1. messaggio iniziale non tattico;
2. modifica dello stesso ID con informazione tattica;
3. una sola traduzione pubblicata;
4. cursore avanzato soltanto dopo consegna Telegram confermata;
5. nessuna ripubblicazione dopo riconnessione Telethon.

La suite automatica deve mantenere un test singolo che copra questa regressione.

### P0 — Protezioni di deploy

- Richiedere CI verde e review prima del merge su `main`.
- Mantenere una sola replica del worker: due processi con la stessa `TELEGRAM_SESSION` possono
  invalidare la sessione.
- Usare un servizio Railway staging separato con `TEST_MODE=true`.
- Conservare il deployment precedente per rollback durante il primo ciclo ALERT/CLEAR reale.

### P1 — Segnali operativi utili

- Conservare log per transizioni, recuperi, filtri, errori e consegne.
- Non registrare ogni poll vuoto: aggiunge rumore senza aiutare il debug.
- Aggiungere metriche o logging strutturato soltanto se i log Railway non permettono più di
  diagnosticare un incidente concreto.

## Riduzione progressiva di `monitor.py`

`monitor.py` è grande, ma una riscrittura completa aumenterebbe il rischio. La divisione deve
avvenire durante modifiche reali, un confine alla volta, mantenendo comportamento e test.

Ordine consigliato:

1. **`storage.py`** — spostare schema SQLite, operazioni su messaggi, cursori e stato quando sarà
   necessaria la prossima modifica alla persistenza.
2. **`summary_pipeline.py`** — spostare classificazione Anthropic, fallback e costruzione del
   riepilogo quando cambierà il formato dei summary.
3. **`alert_pipeline.py`** — spostare filtri, deduplicazione e consegna ALERT solo dopo che il
   percorso condiviso per nuovi messaggi, edit e backfill sarà stabile in produzione.
4. Lasciare in **`monitor.py`** configurazione, registrazione degli handler e avvio dei loop.

Per ogni estrazione:

- un solo modulo per PR;
- nessun cambiamento funzionale nello stesso passaggio;
- nessuna nuova dipendenza;
- suite esistente verde prima e dopo;
- cancellare i wrapper che non aggiungono comportamento.

Non creare package, classi base o dependency injection finché semplici funzioni e parametri
coprono il caso reale.

## Decisioni da non anticipare

### Database

Mantenere SQLite finché esiste una sola replica e il volume Railway è persistente. Valutare
PostgreSQL soltanto se compare almeno una di queste necessità:

- più worker devono condividere stato e cursori;
- lock o tempi di scrittura SQLite causano incidenti misurabili;
- serve interrogare operativamente i dati da un altro servizio;
- volume o retention superano concretamente il singolo database locale.

### Astrazione dei feed

Mantenere feed e canali come configurazione più funzioni. Introdurre un'interfaccia o una classe
soltanto quando almeno due feed richiedono comportamenti realmente diversi, per esempio parsing,
checkpoint, autenticazione o retry incompatibili. Un secondo nome di canale non è sufficiente.

### Nuova infrastruttura

Non aggiungere code esterne, cache, ORM, scheduler o framework di test senza un limite osservato
che la libreria standard e le dipendenze attuali non risolvano.

## Criterio di completamento

Una modifica è pronta quando:

- compilazione e suite automatica passano;
- nessun output TEST può raggiungere le chat di produzione;
- consegne fallite non consumano messaggi o cursori;
- edit e replay non causano perdite o duplicati;
- documentazione e log descrivono il comportamento effettivo, non piani già conclusi.
