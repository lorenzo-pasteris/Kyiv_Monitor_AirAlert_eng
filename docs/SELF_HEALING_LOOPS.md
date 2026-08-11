# Kyiv Monitor — Loop di monitoraggio e autoriparazione

## Regola obbligatoria di sincronizzazione

Ogni modifica al codice, al progetto, agli strumenti, ai workflow di deployment o alle regole operative deve includere l'aggiornamento della documentazione pertinente nella cartella `docs/`. La documentazione deve essere aggiornata nello stesso lavoro e rappresentare sempre ciò che è realmente attivo in produzione e in test.

Un controllo di sincronizzazione documentale deve far parte della checklist di ogni pull request, commit operativo e intervento eseguito da un agente AI.

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
- stato del trigger `@kyiv_airraid_alert`;
- capacità di pubblicare le allerte nel canale `TARGET_CHAT_ID` e i riepiloghi nel gruppo `SUMMARY_CHAT_ID`.

Se si verificano tre riavvii ravvicinati, il ciclo automatico deve fermarsi e generare un avviso, evitando un restart loop infinito.

## Loop 2 — Controllo dei trigger di allerta

### Segnali osservati

- ultimo stato esplicito ricevuto da `@kyiv_airraid_alert`;
- tempo trascorso dall'ultimo aggiornamento riconoscibile;
- corretta registrazione dell'handler Telegram.

### Regole

- Un messaggio esplicito di allarme per Kyiv attiva ALERT.
- Un messaggio esplicito di cessato allarme per Kyiv attiva NORMAL.
- Messaggi ambigui non modificano lo stato conosciuto.
- Se il canale non è leggibile, viene inviato un avviso critico e viene conservato l'ultimo stato.
- UkraineAlarm API non fa più parte dell'architettura.

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
- rispetto della separazione delle destinazioni: ciclo ALERT verso `TARGET_CHAT_ID`, riepiloghi NORMAL verso `SUMMARY_CHAT_ID` e notifiche tecniche verso `OPS_CHAT_ID`;
- validità di `SUMMARY_CHAT_LINK` e presenza del link `Join Kyiv News →` nel cessato allarme;
- errori `429` e rispetto di `retry_after`;
- tempo tra messaggio sorgente e output;
- presenza di etichette interne come `[TEST_SOURCE:...]` o `[burst N/M]`;
- richieste di donazioni, numeri di carte, ringraziamenti o auguri pubblicati per errore;
- duplicati di `@nebo_raketa` e loop generati dal bot.

Soglie suggerite:

- avviso se la latenza ALERT supera 15 secondi;
- avviso critico se tre messaggi ALERT consecutivi non vengono pubblicati;
- avviso se un'etichetta interna compare in un output.

## Loop 5 — Riepiloghi

Controllare che:

- in NORMAL venga eseguito un riepilogo ogni ora;
- i riepiloghi vengano pubblicati esclusivamente nel gruppo `SUMMARY_CHAT_ID` e mai nel canale `TARGET_CHAT_ID`;
- il canale `TARGET_CHAT_ID` resti silenzioso in NORMAL;
- in TEST il ciclo sia di 3 minuti;
- durante ALERT non vengano pubblicati riepiloghi;
- la pausa notturna venga rispettata;
- Anthropic Structured Outputs imponga lo schema completo tramite `output_config.format`;
- HTTP, `stop_reason`, JSON, categorie, tipi e ID vengano validati in quest'ordine;
- il parser del primo JSON sia usato soltanto come fallback legacy, con log obbligatorio e validazione;
- il budget cresca 4000/6000/8000 soltanto per `stop_reason=max_tokens`;
- timeout, errori di trasporto, `429` e `5xx` ricevano backoff crescente e rispetto di `Retry-After`;
- `refusal`, errori 400 e schema invalido non vengano riprovati identici;
- dopo gli errori venga pubblicato un fallback con soli estratti originali e avviso di sintesi AI non disponibile;
- i buffer vengano cancellati soltanto dopo conferma dell'invio Telegram;
- un ciclo senza aggiornamenti rilevanti mantenga silenziosi sia il canale sia il gruppo dei riepiloghi, invii un heartbeat con ora a `OPS_CHAT_ID`, sia registrato come completato e non attivi falsamente il watchdog;
- il watchdog riprovi a 62, 65, 67 e 70 minuti dall'ultimo ciclo completato o riepilogo confermato;
- un'eccezione non arresti definitivamente il loop dei riepiloghi;
- le anomalie operative vengano inviate a `OPS_CHAT_ID`, con fallback a `OWNER_CHAT_ID`, mentre i dettagli ordinari restano nei log Railway.
- ogni avvio o riavvio tecnico del worker venga notificato a `OPS_CHAT_ID`; il messaggio di cessato allarme e ritorno a NORMAL resti nel canale di allerta.

Implementato: la coda NORMAL usa SQLite sul volume Railway `/data`, con inserimenti idempotenti, cursori Telegram per fonte, recupero dalla cronologia, stato `pending`/`processed`/`discarded` e retention di sette giorni. Il buffer in memoria resta soltanto come fallback quando SQLite non è disponibile.

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

Ogni esecuzione richiesta dal proprietario deve essere registrata in `docs/TEST_LOG.md` con ambiente, procedura, risultato osservato e limiti. I test locali e le prove end-to-end Telegram/Railway non sono equivalenti e devono essere indicati separatamente.

Test minimi:

- parsing di alert e cessato allarme Telegram;
- parsing Telegram di ACTIVE, CLEAR e messaggi ambigui;
- risposte Anthropic valide, duplicate, con testo aggiuntivo, troncate e non JSON;
- Structured Outputs, retry differenziati per errore, backoff/`Retry-After` e fallback di soli estratti originali;
- watchdog a 62/65/67/70 minuti e conservazione del buffer su errore di invio;
- isolamento totale di `TEST_MODE`;
- routing separato di allerte, riepiloghi e notifiche Ops;
- assenza di riepiloghi nel canale `TARGET_CHAT_ID`;
- testo minimale di inizio allerta e cessato allarme con link al gruppo;
- rimozione delle etichette `[TEST_SOURCE:...]` e `[burst N/M]`;
- deduplicazione degli output;
- filtro di donazioni, dettagli di pagamento, ringraziamenti e auguri anche quando il testo contiene parole di sicurezza;
- deduplicazione entro tre minuti senza eliminare aggiornamenti con località, quantità o direzioni nuove;
- registrazione di `@nebo_raketa` come unico feed ALERT e assenza completa di `@Nashee_PPO`;
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
