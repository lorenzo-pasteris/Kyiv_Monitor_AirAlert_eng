# Kyiv Monitor — Registro permanente dei test

## Regola di aggiornamento

Questo file è il registro ufficiale delle verifiche del progetto. Ogni volta che il proprietario chiede esplicitamente un **test**, il lavoro non è completo finché questo file non viene aggiornato con:

- data e ambiente;
- obiettivo e procedura;
- risultato osservato;
- distinzione tra test locale, integrazione e prova end-to-end reale;
- identificativi utili, senza credenziali o token;
- limiti della prova ed eventuali problemi scoperti.

Un test non deve essere dichiarato riuscito soltanto perché il codice compila o una simulazione locale passa. Quando l'obiettivo riguarda Telegram o Railway, il risultato deve includere una conferma osservabile nell'ambiente reale oppure dichiarare esplicitamente che tale conferma manca.

## Test eseguiti

### 2026-08-10 — Compilazione Python

- **Ambiente:** checkout locale.
- **Procedura:** `python3 -m py_compile monitor.py` e compilazione del file di test.
- **Risultato:** superato; nessun errore sintattico.
- **Limite:** non verifica connettività, autorizzazioni o consegna Telegram.

### 2026-08-10 — Routing casuale canale/gruppo

- **Ambiente:** test automatico locale con Telegram simulato.
- **Procedura:** 500 messaggi con destinazione scelta casualmente tra ALERT e SUMMARY; confronto di ogni chiamata con l'ID atteso.
- **Risultato:** superato; nessun messaggio ha attraversato la destinazione sbagliata.
- **Copertura:** ALERT verso `TARGET_CHAT_ID`; SUMMARY verso `SUMMARY_CHAT_ID`.
- **Limite:** prova il routing applicativo ma non la consegna reale di Telegram.

### 2026-08-10 — Test dei testi di inizio e fine allerta

- **Ambiente:** test automatico locale.
- **Procedura:** simulazione di passaggio CLEAR → ALERT → CLEAR.
- **Risultato:** superato.
- **Verifiche:**
  - inizio: soltanto `AIR ALERT — KYIV`, senza `REAL-TIME mode`;
  - fine: `ALL CLEAR — KYIV` con link `Join Kyiv News →`;
  - assenza di `Back to NORMAL mode` nel nuovo output;
  - svuotamento dei buffer quando inizia ALERT.
- **Limite:** non modifica né verifica retroattivamente i vecchi post Telegram.

### 2026-08-10 — Deployment Railway della separazione

- **Ambiente:** Railway produzione.
- **Commit applicativo:** `8bff8cbd4beab5be7bb4392f79dc5197a5cd4f09`.
- **Risultato:** deployment riuscito; worker Online.
- **Log osservati:** database statistiche pronto, stato Telegram `CLEAR`, quattro sorgenti connesse e ciclo orario pianificato.

### 2026-08-10 — Ciclo pianificato delle 23:00

- **Ambiente:** Railway produzione.
- **Risultato osservato:** ciclo completato con `messages=0` e heartbeat inviato a Ops; nessun post pubblico.
- **Valutazione corretta:** questo **non è un test end-to-end valido della consegna dei riepiloghi**. Il worker era stato riavviato alle 22:59:10 e i buffer, conservati soltanto in memoria, erano stati azzerati. Il risultato non dimostra che nell'intera ora precedente non esistessero notizie rilevanti.
- **Problema rilevato:** i deployment e i riavvii perdono il materiale non ancora riepilogato.

### 2026-08-10 — Smoke test Telegram reale nel gruppo

- **Ambiente:** container Railway di produzione → Telegram Bot API → gruppo `SUMMARY_CHAT_ID`.
- **Procedura:** invio di un messaggio casuale direttamente con le stesse variabili e credenziali del worker.
- **Codice casuale:** `378948`.
- **Risposta Telegram:** HTTP `200`, `ok=true`, message ID `3`.
- **Verifica visiva:** messaggio presente nel gruppo oggi chiamato **Kyiv Hourly News 🇺🇦** (all'epoca **Kyiv Air 🚨 Alert Chat**) e assente dal canale delle allerte.
- **Risultato:** superato end-to-end per la capacità del bot di pubblicare nel gruppo corretto.

### 2026-08-10 — Migrazione manuale dello storico NORMAL

- **Ambiente:** Telegram produzione tramite la sessione Telethon del worker.
- **Inventario iniziale:** 163 post testuali nel canale; i candidati sono stati controllati tramite ID e prima riga.
- **Criterio:** spostati riepiloghi orari, recap notturni e vecchi messaggi tecnici `Kyiv Normal Monitor started`; conservati inizio allerta, aggiornamenti real-time e cessati allarme.
- **Operazione:** 89 post inoltrati cronologicamente nel gruppo e cancellati dal canale soltanto dopo conferma dell'inoltro.
- **Conferma automatica:** `FORWARDED 89`, `OK True`, `DELETED_SOURCE True`, `REMAINING_NORMAL_IDS []`.
- **Verifica visiva:** il gruppo mostra lo storico dei riepiloghi; nel canale restano soltanto messaggi del ciclo di allerta.
- **Recuperabilità:** i post cancellati dal canale non sono ripristinabili automaticamente; le copie inoltrate rimangono nel gruppo.

## Test futuri consigliati

1. ALERT reale o controllato: inizio, aggiornamento tradotto e cessato allarme nel canale; nessun riepilogo durante ALERT.
2. Riepilogo NORMAL con almeno un contenuto rilevante: consegna nel gruppo e assenza nel canale.
3. Ciclo NORMAL senza contenuti rilevanti: silenzio pubblico e heartbeat soltanto a Ops.
4. Pausa notturna 01:00–07:00 Europe/Kyiv e recap alla ripresa.
5. Riavvio con buffer persistente, quando verrà implementato, per dimostrare che i messaggi non vengono persi.
6. Collegamento `Join Kyiv News →` su un cessato allarme reale o controllato.

## Modello per le prossime registrazioni

```markdown
### YYYY-MM-DD — Nome del test

- **Ambiente:** locale / test Telegram / Railway produzione.
- **Obiettivo:**
- **Procedura:**
- **Risultato osservato:**
- **Esito:** superato / fallito / inconcludente.
- **Identificativi:** commit, deployment o message ID non sensibili.
- **Limiti o problemi scoperti:**
```
