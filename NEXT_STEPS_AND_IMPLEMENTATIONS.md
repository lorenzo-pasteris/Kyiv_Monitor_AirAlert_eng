# Kyiv Monitor — Next steps, implementazioni e migliorie

## Stabilizzazione proposta nel branch `codex/stabilize-monitor` (2026-08-12)

Questa revisione affronta i rischi immediati senza effettuare automaticamente il
deploy di produzione:

- ricostruzione obbligatoria dello stato Telegram all'avvio, con arresto fail-safe se
  non è possibile determinare ACTIVE/CLEAR;
- ripristino silenzioso di ALERT dopo restart, con notifica tecnica a Ops e senza
  duplicare il messaggio pubblico;
- transizioni serializzate e confermate da Telegram prima di modificare lo stato
  effettivo;
- allowlist `ADMIN_USER_IDS` per `/alert` e `/normal`;
- tracciamento dei task ALERT e scarto delle traduzioni appartenenti a una sessione
  ormai terminata;
- persistenza dello stato operativo minimo e dell'evento trigger canonico;
- self-check di SQLite e destinazioni Bot API prima dell'avvio operativo;
- WAL e timeout SQLite, limite esplicito alla dimensione dei bullet, correzione del
  cursore bootstrap quando la cronologia è interamente vecchia;
- estrazione del classificatore puro in `alert_rules.py`;
- CI GitHub, `.env.example`, README e nuovi test di regressione.

Restano attività di piattaforma non applicabili dal solo repository: abilitare branch
protection, richiedere il check CI, creare un servizio Railway staging separato e
disabilitare l'auto-deploy di branch non protetti.

## Scopo del documento

Questo documento raccoglie lo stato compreso del progetto, le discrepanze individuate tra documentazione e codice e una proposta ordinata per le prossime implementazioni.

Non rappresenta una modifica al sistema in produzione. Al momento della sua creazione non sono stati modificati codice, configurazione Railway, canali Telegram, credenziali o repository remoto.

Repository di riferimento: `lorenzo-pasteris/Kyiv_Monitor_AirAlert_eng`  
Branch esaminato: `main`  
Commit esaminato: `975c3cf` — `Send empty-summary heartbeats to Ops`

## Comprensione sintetica del progetto

Kyiv Monitor è un worker Python eseguito continuamente su Railway. Legge fonti Telegram in ucraino o russo e pubblica informazioni in inglese in un gruppo Telegram dedicato a Kyiv.

Il sistema ha due modalità operative:

- **NORMAL:** raccoglie i messaggi live e dalla cronologia delle fonti, li conserva nella coda SQLite persistente e produce riepiloghi categorizzati ogni ora.
- **ALERT:** interrompe i riepiloghi e pubblica rapidamente la traduzione dei nuovi messaggi di monitoraggio relativi all'allerta in corso.

Lo stato di allerta viene determinato esclusivamente dai messaggi espliciti di `@kyiv_airraid_alert`. L'API UkraineAlarm non è più utilizzata.

### Flusso principale

```text
Canali Telegram di contenuto
        ↓
     Telethon
        ↓
Coda SQLite persistente e cursori per fonte
        ↓
Anthropic: classificazione e sintesi strutturata
        ↓
Telegram Bot API
        ↓
Chat di produzione oppure chat Ops

@kyiv_airraid_alert
        ↓
Classificatore deterministico
        ↓
Stato NORMAL / ALERT
```

## Componenti attualmente presenti

- Worker Python avviato con `worker: python monitor.py`.
- Telethon per leggere i canali e gruppi Telegram.
- Telegram Bot API per inviare e modificare messaggi.
- Anthropic, modello `claude-haiku-4-5`, per traduzioni e riepiloghi.
- Structured Outputs con JSON Schema per i riepiloghi.
- Retry differenziati per errori di rete, `429`, `5xx` e limite token.
- Fallback deterministico con estratti originali quando Anthropic non è disponibile.
- Watchdog dei riepiloghi con tentativi a 62, 65, 67 e 70 minuti.
- Heartbeat verso `OPS_CHAT_ID` quando non esistono aggiornamenti rilevanti.
- Modalità test isolata dalla produzione.
- Archivio SQLite per statistiche, coda NORMAL e cursori Telegram per fonte.
- Deploy Railway originato da GitHub.

## Sorgenti e destinazioni

### Fonti di contenuto in NORMAL

1. `@kievinfo_kyiv`
2. `@shv_ukr`
3. `@AMK_Mapping`

Ogni messaggio viene valutato trasversalmente rispetto alle quattro categorie, indipendentemente dalla fonte:

- `kyiv_city`
- `ukraine_national`
- `military`
- `air_defence`

Una fonte indica la provenienza, non determina rigidamente la categoria.

### Fonte dello stato di allerta

- `@kyiv_airraid_alert`

Questo canale deve essere usato soltanto come trigger e non come sorgente di contenuti.

### Destinazioni

- `TARGET_CHAT_ID`: gruppo di produzione.
- `TEST_CHAT_ID`: gruppo privato usato esclusivamente in modalità test.
- `OPS_CHAT_ID`: notifiche operative, heartbeat, fallback e problemi.
- `OWNER_CHAT_ID`: compatibilità/fallback quando `OPS_CHAT_ID` non è configurato.

## Comportamento operativo compreso

### Modalità NORMAL

- I messaggi delle tre fonti vengono ripuliti dalla pubblicità evidente e conservati idempotentemente nella coda persistente.
- In produzione il riepilogo è allineato all'ora piena nel fuso `Europe/Kyiv`.
- Anthropic valuta tutti i messaggi rispetto a tutte le categorie.
- Ogni categoria può contenere al massimo cinque bullet in inglese.
- I bullet devono mantenere ordine cronologico, località, orari, quantità e incertezza delle fonti.
- I record vengono marcati `processed` soltanto dopo la conferma dell'invio Telegram.
- Se non esistono aggiornamenti rilevanti, la produzione resta silenziosa e Ops riceve un heartbeat.
- Tra le 01:00 e le 07:00 non vengono pubblicati riepiloghi orari.
- Alle 07:00 viene prodotto un recap del materiale accumulato durante la notte.

### Modalità ALERT

- All'inizio dell'allerta i buffer NORMAL vengono svuotati.
- Viene pubblicato un messaggio unico di inizio allerta.
- Soltanto i nuovi messaggi di `@kievreal1` vengono processati.
- Il messaggio originale appare inizialmente come contenuto in traduzione e viene poi sostituito dalla versione inglese.
- Pubblicità e messaggi non pertinenti vengono scartati.
- Al cessato allarme viene pubblicato `ALL CLEAR — KYIV` e il sistema torna in NORMAL.

### Modalità TEST

Con `TEST_MODE=true`, soltanto `TEST_CHAT_ID` viene utilizzato come sorgente e destinazione. Gli handler dei canali reali non vengono registrati.

Comandi disponibili:

- `/test_start`
- `/test_message`
- `/test_burst N`
- `/test_end`
- `/test_summary`

Il riepilogo automatico viene eseguito ogni tre minuti. Il sistema esclude i propri output ed elimina marcatori interni come `[TEST_SOURCE:Nashee_PPO]` e `[burst N/M]`.

## Problemi e discrepanze individuati

### P0 — Riconciliazione dello stato di allerta dopo un riavvio

**Problema:** all'avvio in produzione il worker legge gli ultimi 20 messaggi di `@kyiv_airraid_alert` e assegna correttamente `telegram_alert_state`. Tuttavia, dopo aver individuato lo stato, non chiama `reconcile_alert_state()`.

**Rischio:** se il container si riavvia mentre Kyiv è già in allerta, `alert_active` parte da `False`. Il worker potrebbe quindi rimanere in NORMAL fino al prossimo messaggio esplicito del canale trigger.

**Discrepanza:** la documentazione dichiara che lo stato viene ricostruito all'avvio senza attendere un nuovo evento. Il codice ricostruisce il dato, ma non applica effettivamente la transizione.

**Implementazione proposta:**

1. Dopo la ricerca dell'ultimo messaggio esplicito, applicare lo stato tramite una funzione dedicata.
2. Distinguere una riconciliazione di avvio da una vera transizione in tempo reale.
3. Decidere esplicitamente se, durante un riavvio in allerta, debba essere pubblicato nuovamente il messaggio `AIR ALERT — KYIV` oppure soltanto una notifica tecnica a Ops.
4. Aggiungere test per avvio con stato ACTIVE, CLEAR e sconosciuto.
5. Aggiornare entrambi i documenti in `docs/` nello stesso intervento.

**Nota:** questa è la priorità tecnica più alta perché coinvolge direttamente l'affidabilità della modalità di sicurezza.

### P0 — Test automatici mancanti

La documentazione definisce numerosi test minimi, ma nel repository esaminato non è presente una suite automatica.

Test prioritari:

- parsing di alert esplicito per Kyiv;
- parsing di cessato allarme;
- messaggi ambigui o relativi ad altre città;
- ricostruzione ACTIVE/CLEAR al riavvio;
- isolamento totale di `TEST_MODE`;
- impossibilità di inviare test a `TARGET_CHAT_ID`;
- conservazione dei buffer quando Telegram non conferma l'invio;
- eliminazione dei buffer dopo invio confermato;
- Structured Outputs validi e non validi;
- `max_tokens` con progressione 4000/6000/8000;
- errori `429`, `5xx`, timeout e `Retry-After`;
- fallback con soli estratti originali;
- watchdog a 62/65/67/70 minuti;
- rimozione dei marcatori interni;
- deduplicazione degli output del bot;
- conservazione corretta delle quantità nei recap di attacco.

**Implementazione proposta:** usare `pytest`, separando gradualmente la logica pura dalle integrazioni di rete. HTTP Anthropic e Telegram, orologio, Telethon e SQLite dovrebbero essere simulati nei test.

### P1 — Comandi manuali di produzione senza autorizzazione esplicita

Il worker accetta `/alert` e `/normal` nella chat di produzione, ma il codice non controlla esplicitamente l'identità dell'utente che invia il comando.

**Rischio:** un membro non autorizzato del gruppo potrebbe cambiare manualmente la modalità operativa.

**Implementazione proposta:**

- introdurre una variabile come `ADMIN_USER_IDS` o un unico `OWNER_USER_ID`;
- rifiutare e registrare i comandi provenienti da utenti non autorizzati;
- notificare a Ops i tentativi non autorizzati senza esporre dati inutili;
- aggiungere test di autorizzazione;
- documentare chiaramente precedenza e durata dell'override manuale.

### P1 — Persistenza dei buffer — implementato il 2026-08-11

La coda NORMAL è ora mantenuta nello stesso SQLite già presente sul volume Railway `/data`. Restart e deployment non eliminano i messaggi accumulati dall'ultimo riepilogo.

**Implementazione completata:**

- tabella `normal_messages` con identificatore Telegram, fonte, timestamp, testo pulito e stato `pending`, `processed` o `discarded`;
- tabella `source_cursors` con l'ultimo ID letto dalla cronologia per ciascuna fonte;
- inserimento idempotente tramite chiave primaria `(channel, message_id)`;
- recupero orario dei messaggi persi dal listener live;
- passaggio a `processed` soltanto dopo conferma Telegram;
- conservazione dei record completati per sette giorni;
- buffer in memoria mantenuto come fallback se SQLite non è disponibile.

### P1 — Stato operativo persistente

Oltre ai buffer, sarebbe utile persistere:

- ultimo stato esplicito del trigger;
- ID e data del messaggio che lo ha determinato;
- ultimo riepilogo completato;
- ultimo invio confermato;
- tentativi già eseguiti dal watchdog;
- ultima attività osservata per ciascuna fonte.

Questo renderebbe i riavvii più deterministici e ridurrebbe falsi retry o perdita di contesto.

### P1 — Health check per singola sorgente

Il controllo attuale usa `last_message_time`, quindi misura soltanto il silenzio complessivo di tutti i canali.

La specifica richiede invece dati separati per fonte:

- timestamp dell'ultimo messaggio;
- ultimo message ID;
- numero di messaggi ricevuti;
- numero di messaggi filtrati;
- numero di errori dell'handler;
- capacità di risolvere ancora l'entity Telegram.

**Implementazione proposta:** mantenere una struttura `source_health` per canale e applicare soglie diverse sulla base del comportamento atteso. L'assenza di messaggi non deve essere trattata automaticamente come guasto senza prima verificare connessione Telethon ed entity.

### P1 — Pipeline CI e protezione del deploy

Il repository non contiene GitHub Actions. La documentazione richiede invece che il deploy venga bloccato se falliscono test relativi a trigger, destinazioni o isolamento test.

**Implementazione proposta:**

1. Workflow GitHub Actions per sintassi, test e lint leggero.
2. Esecuzione automatica su pull request e push a branch protetti.
3. Branch protection su `main`.
4. Review obbligatoria per `monitor.py`, configurazione di deploy e documenti architetturali.
5. Deploy Railway soltanto dopo check verdi.
6. Verifica che modifiche esclusivamente documentali continuino a essere ignorate dai Watch Paths Railway.

### P1 — Distinzione tra specifica attuale e roadmap

`SELF_HEALING_LOOPS.md` mescola funzionalità presenti, requisiti futuri e architettura suggerita. Questo può far credere che alcune protezioni siano già attive.

Funzionalità descritte ma non osservate nel repository:

- arresto del restart loop dopo tre riavvii ravvicinati;
- Sentry o log drain;
- healthcheck esterno ogni minuto;
- endpoint di stato del worker;
- staging formalizzato;
- rollback automatico;
- agente AI che prepara branch e pull request;
- escalation CRITICAL su due canali indipendenti;
- branch protection;
- buffer persistenti.

**Implementazione documentale proposta:** etichettare chiaramente ogni sezione come `IMPLEMENTATO`, `PARZIALE` o `PIANIFICATO`.

### P2 — Incoerenza nell'elenco delle fonti

La prima sezione di `PROJECT_ARCHITECTURE.md` elenca tre fonti, ma più avanti il documento e il codice ne usano quattro, includendo `@shv_ukr`.

**Correzione proposta:** uniformare l'intero documento e mantenere una sola tabella canonica di fonti, ruoli e modalità operative.

### P2 — README mancante

La pagina principale GitHub non presenta una descrizione utilizzabile del progetto.

**README proposto:**

- scopo e avvertenza di sicurezza;
- diagramma sintetico;
- modalità NORMAL/ALERT/TEST;
- fonti e destinazioni senza ID sensibili;
- variabili d'ambiente richieste;
- avvio locale sicuro;
- esecuzione dei test;
- deployment Railway;
- collegamenti ai documenti dettagliati;
- procedura operativa e rollback.

### P2 — Identificatori dei messaggi nei riepiloghi

Gli ID temporanei usati per Anthropic sono costruiti come `canale:indice_buffer`. Sono sufficienti all'interno del singolo ciclo, ma non rappresentano l'identificatore Telegram originale.

**Miglioria proposta:** conservare il vero message ID Telegram e usarlo per deduplicazione, audit, persistenza e diagnostica.

### P2 — Monitoraggio della latenza ALERT

La documentazione suggerisce un warning oltre 15 secondi, ma non risulta una misura strutturata della latenza sorgente-output.

**Implementazione proposta:** registrare:

- timestamp evento Telethon;
- inizio traduzione;
- fine traduzione;
- conferma del primo invio;
- conferma della modifica del messaggio;
- esito finale.

Inviare warning a Ops quando vengono superate le soglie, evitando notifiche ripetitive.

### P2 — Gestione dei fallimenti consecutivi in ALERT

La documentazione richiede un avviso critico dopo tre messaggi ALERT consecutivi non pubblicati. Il comportamento non risulta implementato in modo esplicito.

**Implementazione proposta:** contatore consecutivo azzerato al primo successo, con alert Ops al raggiungimento della soglia e possibile escalation secondaria.

### P2 — Controllo dei marcatori interni negli output

I marcatori di test vengono rimossi in ingresso, ma sarebbe utile aggiungere anche una validazione finale prima dell'invio.

**Implementazione proposta:** bloccare o sanificare output contenenti:

- `[TEST_SOURCE:`
- `[burst N/M]`
- altre etichette interne definite dal sistema.

### P2 — Osservabilità e dati sensibili

Le statistiche SQLite possono contenere anteprime dei messaggi. Occorre decidere retention, accesso al volume e necessità effettiva del testo completo.

**Miglioria proposta:**

- minimizzare la lunghezza delle anteprime;
- configurare retention;
- evitare dati personali non necessari;
- non scrivere mai token o credenziali nei log;
- separare metriche aggregate e contenuto diagnostico.

## Migliorie architetturali successive

### Healthcheck esterno

Un worker senza server HTTP non offre naturalmente un endpoint. Possibili soluzioni:

- heartbeat periodico verso un servizio esterno;
- piccolo endpoint HTTP separato;
- monitor basato su log Railway;
- job esterno che controlla un timestamp persistente.

Il controllo dovrebbe verificare che il processo sia vivo, connesso a Telegram e capace di completare un ciclo, non soltanto che il container esista.

### Sentry o raccolta strutturata degli errori

Integrare una soluzione di error tracking per:

- eccezioni non previste;
- errori ripetuti Anthropic/Telegram;
- perdita della sessione Telethon;
- errori SQLite;
- restart ravvicinati;
- correlazione tra commit distribuito e regressione.

### Staging reale

Definire un ambiente Railway separato con:

- `TEST_MODE=true`;
- `TEST_CHAT_ID` dedicato;
- credenziali separate quando possibile;
- stesso commit candidato alla produzione;
- nessun accesso agli handler reali;
- promozione soltanto dopo test funzionali.

### Rollback

Documentare e, quando sicuro, automatizzare:

- identificazione dell'ultimo deployment stabile;
- condizioni che richiedono rollback;
- procedura Railway;
- verifica post-rollback;
- notifica a Ops;
- conservazione dei log e della causa del fallimento.

### Agente AI per diagnosi e pull request

Questa fase dovrebbe arrivare soltanto dopo test, CI e staging affidabili.

L'agente potrebbe ricevere:

- commit distribuito;
- log privati già privati dei segreti;
- test falliti;
- configurazione non sensibile;
- differenza rispetto all'ultimo commit stabile.

L'agente dovrebbe poter preparare un branch, aggiungere un test di regressione e aprire una PR, ma non modificare autonomamente trigger, chat, credenziali o produzione.

## Ordine di lavoro consigliato

### Fase 1 — Sicurezza e regressioni immediate

1. Scrivere test della logica dei trigger.
2. Correggere la riconciliazione dello stato all'avvio.
3. Aggiungere autorizzazione ai comandi `/alert` e `/normal`.
4. Verificare isolamento di `TEST_MODE` e destinazioni Telegram.
5. Correggere la documentazione nello stesso intervento.

### Fase 2 — Affidabilità dei dati

1. Persistenza dei buffer.
2. Memorizzazione dei veri message ID Telegram.
3. Persistenza dello stato operativo minimo.
4. Deduplicazione idempotente dopo restart.
5. Test dei fallimenti Telegram e dei retry.

### Fase 3 — Osservabilità

1. Salute per singola fonte.
2. Metriche di latenza ALERT.
3. Contatore dei fallimenti consecutivi.
4. Healthcheck esterno.
5. Error tracking strutturato.

### Fase 4 — Qualità e deployment

1. GitHub Actions.
2. Branch protection.
3. Staging Railway.
4. Checklist automatizzata pre-deploy.
5. Procedura di rollback verificata.

### Fase 5 — Automazione avanzata

1. Trigger qualificati per diagnosi AI.
2. Riproduzione isolata.
3. Test di regressione generato o assistito.
4. Pull request automatica.
5. Approvazione umana obbligatoria.

## Checklist per la prossima sessione

- [ ] Confermare che il repository e il branch siano ancora gli stessi.
- [ ] Leggere gli eventuali commit successivi a `975c3cf`.
- [ ] Controllare se Railway sta eseguendo lo stesso commit.
- [ ] Verificare se esistono modifiche locali o non pubblicate.
- [ ] Partire dal test della riconciliazione dello stato al riavvio.
- [ ] Concordare il comportamento desiderato se il worker riparte durante un'allerta già attiva.
- [ ] Decidere chi può usare `/alert` e `/normal`.
- [ ] Non eseguire test nel gruppo di produzione.
- [ ] Aggiornare sempre `PROJECT_ARCHITECTURE.md` e `SELF_HEALING_LOOPS.md` insieme al codice pertinente.
- [ ] Eseguire test e verifica sintattica prima di qualsiasi deploy.
- [ ] Verificare log, sorgenti, trigger, destinazione e modalità dopo il deploy.

## Decisione necessaria prima della prima modifica

La prima domanda da risolvere è:

> Se Railway riavvia il worker mentre `@kyiv_airraid_alert` indica che l'allerta è già attiva, il sistema deve pubblicare nuovamente `AIR ALERT — KYIV`, deve inviare soltanto una notifica tecnica a Ops oppure deve entrare silenziosamente in ALERT?

La scelta influenza l'implementazione della riconciliazione iniziale e i relativi test. L'opzione più prudente da valutare è entrare immediatamente in ALERT e inviare a Ops una notifica tecnica di stato ripristinato, evitando duplicati pubblici non necessari.

## Regola di sicurezza per gli interventi futuri

Il sistema è informativo ma tratta eventi di sicurezza reali. Le modifiche a trigger, modalità test, chat ID, canali, messaggi di allerta, credenziali e deploy devono essere considerate ad alto rischio. Devono essere accompagnate da test, aggiornamento documentale, verifica in ambiente isolato e approvazione prima della produzione.
