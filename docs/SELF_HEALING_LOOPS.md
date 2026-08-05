# Kyiv Monitor — Loop di monitoraggio e autoriparazione

## Obiettivo

Creare un sistema che rilevi rapidamente problemi, mantenga il servizio disponibile e, quando opportuno, coinvolga un agente AI per preparare una correzione verificabile.

La sicurezza dell'allerta viene prima dell'automazione. Nessun agente deve modificare autonomamente le regole di fusione dei trigger, i canali o le credenziali direttamente in produzione.

## Principio generale

```text
Segnale di problema
→ classificazione
→ azione automatica reversibile
→ verifica
→ diagnosi AI se il problema continua
→ pull request
→ test
→ approvazione/deploy
→ verifica post-deploy
```

## Loop 1 — Riavvio del processo

### Rilevazione

- container terminato;
- eccezione non gestita;
- assenza del processo;
- mancato heartbeat interno.

### Azione

Railway riavvia automaticamente il worker. Dopo l'avvio devono essere controllati:

- connessione Telegram;
- registrazione delle sorgenti corrette;
- stato dei due trigger;
- capacità di pubblicare nel gruppo previsto.

Se si verificano tre riavvii ravvicinati, il ciclo automatico deve fermarsi e generare un avviso, evitando un restart loop infinito.

## Loop 2 — Controllo dei trigger di allerta

### Segnali osservati

- ultimo successo UkraineAlarm;
- codici HTTP e frequenza dei `401`;
- ultimo stato esplicito ricevuto da `@kyiv_airraid_alert`;
- eventuale conflitto tra le due sorgenti.

### Regole

- Una sorgente valida in ALERT è sufficiente ad attivare ALERT.
- Se l'API non è valida, viene seguito Telegram.
- Se Telegram non ha ancora uno stato e l'API è valida, viene seguita l'API.
- In caso di conflitto viene mantenuto ALERT.
- La perdita di una sorgente genera un avviso, ma non interrompe il servizio se l'altra è valida.
- La perdita di entrambe genera un avviso critico immediato e conserva l'ultimo stato conosciuto.

Questo loop deve essere deterministico e non affidato a un modello linguistico.

## Loop 3 — Salute delle sorgenti Telegram

Per ciascuna sorgente registrare:

- timestamp dell'ultimo messaggio;
- ultimo message ID;
- numero di errori dell'handler;
- numero di messaggi filtrati e processati.

L'assenza di messaggi non è sempre un errore. Un avviso deve considerare il comportamento storico del canale e verificare prima la connessione Telethon.

Controlli automatici:

1. connessione Telegram attiva;
2. entity dei canali ancora risolvibili;
3. sessione non revocata;
4. handler registrati una sola volta;
5. nessun canale reale registrato quando `TEST_MODE=true`.

## Loop 4 — Controllo degli output

Verificare:

- esito delle chiamate `sendMessage` ed `editMessageText`;
- errori `429` e rispetto di `retry_after`;
- tempo tra messaggio sorgente e output;
- presenza di etichette interne come `[TEST_SOURCE:...]` o `[burst N/M]`;
- duplicati e loop generati dal bot.

Soglie suggerite:

- avviso se la latenza ALERT supera 15 secondi;
- avviso critico se tre messaggi ALERT consecutivi non vengono pubblicati;
- avviso se un'etichetta interna compare in un output.

## Loop 5 — Riepiloghi

Controllare che:

- in NORMAL venga eseguito un riepilogo ogni ora;
- in TEST il ciclo sia di 3 minuti;
- durante ALERT non vengano pubblicati riepiloghi;
- la pausa notturna venga rispettata;
- i recap numerici di `@Nashee_PPO` mantengano i numeri originali;
- un errore dell'API Anthropic non cancelli definitivamente i messaggi prima di un nuovo tentativo.

Miglioria consigliata: spostare i buffer da memoria a un archivio persistente, per esempio Redis o PostgreSQL, così un riavvio non perde il materiale del riepilogo.

## Loop 6 — Diagnosi tramite agente AI

Un webhook di Railway, Sentry o un monitor esterno può avviare un job che fornisce all'agente:

- commit attualmente distribuito;
- ultimi log rilevanti, con segreti rimossi;
- configurazione non sensibile;
- test falliti;
- differenza rispetto all'ultimo deployment stabile.

L'agente può:

1. classificare il problema;
2. riprodurlo in un ambiente isolato;
3. modificare il codice su un branch;
4. aggiungere un test di regressione;
5. aprire una pull request con diagnosi, rischio e rollback.

L'agente non deve:

- leggere o stampare token;
- cambiare chat ID o sorgenti senza approvazione;
- disabilitare la logica fail-safe;
- inviare messaggi di test in produzione;
- effettuare automaticamente un deploy ad alto rischio.

## Loop 7 — Test automatici prima del deploy

Test minimi:

- parsing di alert e cessato allarme Telegram;
- risposta API ACTIVE, CLEAR, `401`, timeout e JSON invalido;
- combinazioni API/Telegram concordi e in conflitto;
- conferma che il conflitto mantenga ALERT;
- isolamento totale di `TEST_MODE`;
- rimozione delle etichette `[TEST_SOURCE:...]` e `[burst N/M]`;
- deduplicazione degli output;
- filtri e riepiloghi numerici di `@Nashee_PPO`;
- sintassi Python e avvio del worker.

Il deploy deve essere bloccato se fallisce un test relativo a trigger, isolamento del test o destinazione Telegram.

## Loop 8 — Deployment e rollback

Strategia consigliata:

1. deploy in staging con `TEST_MODE=true`;
2. esecuzione della suite simulata nel solo gruppo di test;
3. controllo di latenza, burst, riepilogo, errore e riavvio;
4. promozione dello stesso commit in produzione;
5. verifica dei log e del messaggio di avvio;
6. rollback automatico se il processo non parte o non registra le sorgenti attese.

Non eseguire test con messaggi o allerte reali.

## Loop 9 — Escalation

Livelli suggeriti:

- **INFO**: singolo errore recuperato automaticamente.
- **WARNING**: una sorgente trigger indisponibile, ma l'altra funziona.
- **CRITICAL**: entrambe le sorgenti trigger indisponibili, impossibilità di inviare output o restart loop.

I WARNING possono essere inviati al proprietario in privato. I CRITICAL devono usare almeno due canali indipendenti, per esempio Telegram privato ed email/Sentry.

## Architettura tecnica suggerita

- Sentry o log drain Railway per raccolta errori.
- Healthcheck esterno ogni minuto.
- Endpoint o heartbeat che riporti stato del worker senza esporre segreti.
- GitHub Actions per test e pull request.
- Agente AI avviato tramite API soltanto dopo un evento qualificato.
- Redis/PostgreSQL per stato e buffer persistenti.
- Branch protection e approvazione obbligatoria per file critici.

## File critici da proteggere

Richiedere approvazione umana per modifiche che riguardano:

- logica di fusione API/Telegram;
- `TEST_MODE` e isolamento delle sorgenti;
- chat ID e nomi dei canali;
- messaggi di inizio/fine allerta;
- gestione delle credenziali;
- regole di deploy e rollback.

## Ordine di implementazione consigliato

1. Test unitari della logica dei due trigger.
2. Heartbeat e monitor esterno.
3. Avvisi per perdita di una o entrambe le sorgenti.
4. Persistenza dello stato e dei buffer.
5. Pipeline staging → test → produzione.
6. Agente AI che apre pull request.
7. Solo dopo sufficiente esperienza, automazione di correzioni a basso rischio.
