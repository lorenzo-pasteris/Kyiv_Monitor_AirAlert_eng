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

### 2026-08-11 — Attacco reale: test end-to-end fallito

- **Ambiente:** Telegram e Railway produzione durante un attacco reale.
- **Risultato osservato:** nel canale sono passati messaggi di raccolta fondi, ringraziamento e richiesta di supporto; il link `Join Kyiv News →` del cessato allarme riportava al canale invece che al gruppo; `@Nashee_PPO` non ha fornito copertura nei primi minuti.
- **Cause individuate:** il filtro pubblicità considerava operativo qualsiasi testo contenente parole come `балістика`; `SUMMARY_CHAT_LINK` era configurato con una destinazione errata; ALERT ascoltava una sola sorgente real-time.
- **Esito:** fallito.
- **Correzione richiesta:** filtro deterministico per donazioni/pagamenti/auguri, link del gruppo corretto, aggiunta di `@nebo_raketa` e deduplicazione cross-source.
- **Limite:** l'incidente dimostra i difetti reali; la correzione richiede test di regressione e verifica post-deploy separati.

### 2026-08-11 — Regressione locale dopo l'incidente

- **Ambiente:** checkout locale isolato.
- **Procedura:** compilazione di `monitor.py` e `tests/test_routing.py`, seguita dalla suite `unittest`.
- **Casi verificati:** routing casuale su 500 messaggi, copy del ciclo ALERT, filtro dei due testi reali di donazione/ringraziamento, esclusione dei post non tattici, accettazione dei follow-up brevi come `Київщина чисто`, registrazione di `@nebo_raketa` come feed solo ALERT, deduplicazione cross-source e scadenza della finestra dopo 180 secondi.
- **Risultato osservato:** 6 test eseguiti, 6 superati; nessun errore di sintassi.
- **Esito:** superato localmente.
- **Limite:** non dimostra ancora il comportamento del nuovo codice nel worker Railway.

### 2026-08-11 — Verifica reale del link e della seconda sorgente

- **Ambiente:** sessione Telethon del container Railway e Telegram Web di produzione.
- **Link:** l'href pubblicato da `Join Kyiv News →` è stato risolto tramite Telegram; l'invito corrisponde all'ID `SUMMARY_CHAT_ID` e al gruppo **Kyiv Hourly News 🇺🇦**.
- **Seconda sorgente:** `@nebo_raketa` è risolvibile dalla sessione di produzione come canale **Київський купол | Графіки**.
- **Esito:** superato per configurazione e accessibilità.
- **Limite:** non è stato pubblicato un falso allarme nel canale pubblico; la consegna e la deduplicazione reali richiedono il prossimo ALERT o un ambiente di test dedicato con due sorgenti.

### 2026-08-11 — Verifica post-deploy della correzione ALERT

- **Ambiente:** Railway produzione.
- **Commit applicativo finale:** `184164492a848ed4e7177ae52fe87f5713023bee`.
- **Deployment finale:** `2d20c537-83df-4d8d-a0b8-2d38b0a7f528` attivo con esito riuscito.
- **Log osservati:** database statistiche pronto; stato Telegram ricostruito `CLEAR`; sorgenti NORMAL `kievinfo_kyiv`, `shv_ukr`, `AMK_Mapping`, `Nashee_PPO`; feed ALERT `Nashee_PPO`, `nebo_raketa`; pianificazione oraria attiva in `Europe/Kyiv`.
- **Esito:** superato per build, avvio, risoluzione delle sorgenti e stabilità iniziale del worker.
- **Limite:** il filtro e la deduplicazione non sono stati provocati con messaggi falsi nel canale pubblico; la loro prova end-to-end definitiva avverrà al prossimo evento reale oppure in un test isolato a due sorgenti.

### 2026-08-11 — Separazione definitiva gruppo NORMAL e canale ALERT

- **Ambiente:** checkout locale, Telegram produzione e sessione Telethon del worker.
- **Modifica sorgenti:** `@Nashee_PPO` rimosso completamente; `@nebo_raketa` configurato come unico feed ALERT; le sorgenti NORMAL diventano `kievinfo_kyiv`, `shv_ukr` e `AMK_Mapping`.
- **Test locali:** compilazione Python e 6 test `unittest` superati, inclusi feed ALERT unico, filtro contenuti, deduplicazione e routing casuale su 500 messaggi.
- **Separazione Telegram:** il gruppo **Kyiv Hourly News 🇺🇦** è stato scollegato dalla funzione Discussion del canale; verifica `linked_chat_id=None`.
- **Pulizia storico:** inventariati 130 messaggi; 35 copie automatiche ALERT avevano mittente e inoltro entrambi uguali al canale e coincidevano esattamente con l'insieme dei messaggi prefissati `🚨`, `🔴` o `✅`.
- **Operazione:** eliminate esclusivamente le 35 copie ALERT. Verifica successiva: 95 messaggi totali, zero messaggi ALERT e 91 messaggi NORMAL conservati.
- **Recuperabilità:** le copie eliminate dal gruppo non sono ripristinabili automaticamente, ma gli originali rimangono nel canale ALERT.
- **Commit applicativo:** `ed7cc9c914fb78830fd224630c3ae32deb4adf8e`.
- **Deployment Railway:** `377aee41-74ed-4e57-96ee-73802b28da98` attivo con esito riuscito.
- **Log post-deploy:** stato Telegram `CLEAR`; sorgenti NORMAL `kievinfo_kyiv`, `shv_ukr`, `AMK_Mapping`; unico feed ALERT `nebo_raketa`; pianificazione oraria attiva in `Europe/Kyiv`; nessuna registrazione di `Nashee_PPO`.
- **Esito:** superato per separazione Telegram, regressione locale e avvio della configurazione definitiva in produzione.

### 2026-08-11 — Persistenza SQLite e recupero della cronologia NORMAL

- **Ambiente:** checkout locale isolato con SQLite temporaneo e client Telegram simulato.
- **Obiettivo:** dimostrare che restart, eventi live mancati, duplicati e invii Telegram falliti non causino perdita dei messaggi NORMAL.
- **Procedura:** compilazione Python e suite `unittest`; inserimento dello stesso messaggio via listener live e cronologia; recupero incrementale tramite cursore; simulazione di invio fallito seguita da invio riuscito; ingresso in ALERT con materiale pendente e avanzamento dei cursori al cessato allarme.
- **Risultato osservato:** 9 test eseguiti e superati; chiave `(channel, message_id)` idempotente; messaggio rimasto `pending` dopo invio fallito e passato a `processed` soltanto dopo conferma; materiale pendente marcato `discarded` all'inizio di ALERT; cursori riallineati alle tre fonti alla ripresa di NORMAL.
- **Esito:** superato localmente.
- **Limite:** manca ancora la verifica post-deploy sul volume Railway reale e un ciclo orario con messaggi recuperati dalla cronologia reale.

### 2026-08-12 — Sostituzione del feed ALERT con Kyiv Alerts

- **Ambiente:** sessione Telethon del container Railway e checkout locale isolato.
- **Obiettivo:** configurare `@kyiv_alerts` come unica sorgente degli aggiornamenti durante ALERT ed escludere completamente `@nebo_raketa` e `@Nashee_PPO`.
- **Verifica reale della fonte:** la sessione Railway risolve `@kyiv_alerts` come canale broadcast **Kyiv Alerts**, ID Telegram `-1001520282656`, risulta iscritta e legge i messaggi recenti.
- **Test locali:** compilazione di `monitor.py` e `tests/test_routing.py`; suite `unittest` con controllo del feed unico, esclusione della fonte precedente e riconoscimento dei formati reali `тривога` e `Відбій` osservati nel canale.
- **Risultato osservato:** 10 test eseguiti e superati; nessun errore di sintassi.
- **Commit applicativo:** `3d80b93200cafcf58eac3792ed3b0324728c0905`.
- **Deployment Railway:** `c153286b-ec94-4eb2-99a1-5697df3359e9`, stato `Active` e worker `Online`.
- **Log post-deploy:** database SQLite pronto; stato Telegram `CLEAR`; sorgenti NORMAL invariate; unico feed ALERT `kyiv_alerts`; pianificazione oraria attiva in `Europe/Kyiv`; nessun errore di avvio.
- **Esito:** superato localmente e per configurazione/avvio in produzione.
- **Limite:** la consegna pubblica end-to-end deve essere confermata dal prossimo allarme reale; non è stato generato un falso allarme nel canale pubblico.

### 2026-08-12 — Collaudo end-to-end privato di Kyiv Alerts

- **Ambiente:** codice del deployment Railway di produzione, eseguito una sola volta con `TEST_MODE=true` e destinazioni forzate a `TEST_CHAT_ID`.
- **Obiettivo:** verificare l'intera catena applicativa senza pubblicare un falso allarme nel canale pubblico: formato reale `@kyiv_alerts` → filtro ALERT → traduzione Anthropic → Telegram Bot API; in parallelo, notizia NORMAL → analisi oraria → gruppo privato di test.
- **Precondizioni confermate:** deployment `c153286b-ec94-4eb2-99a1-5697df3359e9` `Active`, worker `Online`, `TEST_CHAT_ID` configurato, stato iniziale `CLEAR`, unico feed ALERT dichiarato nei log `['kyiv_alerts']`.
- **Procedura ALERT:** passato a `handle_alert_message` un testo ucraino realistico su allerta UAV nella regione di Kyiv, con `source="kyiv_alerts"`; osservati `[ALERT ACCEPTED] @kyiv_alerts` e conferma positiva dell'invio Telegram dopo la traduzione.
- **Procedura NORMAL:** inserita una notizia realistica su una variazione temporanea della metropolitana di Kyiv; l'analisi Anthropic l'ha selezionata esclusivamente nella categoria `kyiv_city` e il riepilogo è stato consegnato alla destinazione SUMMARY di test.
- **Risultato osservato:** marker `KYIV_ALERTS_E2E_20260812_0026`; log `[SUMMARY COMPLETED] ... outcome=delivered messages=1`; risultato finale `E2E_RESULT True True True` per marker iniziale, riepilogo e marker finale. La traduzione ALERT è stata inviata fra i due marker dalla stessa funzione usata in produzione.
- **Conferma ALERT mirata:** seconda esecuzione privata con un messaggio realistico `Повітряна тривога у Києві`; risultato `ALERT_TELEGRAM_CONFIRMED True DELIVERIES 1`, quindi un solo messaggio tradotto è stato accettato dalla Telegram Bot API.
- **Regressione locale successiva:** compilazione di `monitor.py` e `tests/test_routing.py`; 10 test `unittest` eseguiti e superati, inclusi feed unico, formati reali, filtri, deduplicazione, persistenza SQLite e separazione casuale delle destinazioni.
- **Esito:** superato end-to-end nell'ambiente reale per Anthropic e Telegram, con isolamento completo nella chat privata di test.
- **Limite:** resta da osservare il prossimo allarme esterno reale per provare il listener Telethon e il ciclo pubblico senza simulazioni; il test non ha scritto nulla nel canale pubblico.

### 2026-08-12 — Staging PR 6 e compatibilità Anthropic Structured Outputs

- **Ambiente:** Railway `staging-pr-6`, environment `1f687c32-c70a-4d83-a19a-319f197ce772`, branch `codex/stabilize-monitor`, con `TEST_MODE=true` e output confinati a `TEST_CHAT_ID`.
- **Configurazione preliminare:** aggiunti esplicitamente `OWNER_CHAT_ID` e `ADMIN_USER_IDS` alle variabili Railway; nessun valore sensibile registrato in questo file.
- **CI iniziale:** GitHub Actions completata con successo sul candidato iniziale `cf84798f68f1cc86cdb7104284c1b8cb70bec7ea`.
- **Startup staging:** deployment `203190bb-5e9b-427d-92f8-7a3be51c690a` attivo; self-check SQLite e destinazioni Bot API superato; log `TEST_MODE enabled` e sorgenti Telegram reali non registrate.
- **Primo test end-to-end:** transizioni NORMAL → ALERT → NORMAL, traduzione dell'aggiornamento `@kyiv_alerts` e quattro consegne Telegram riuscite; il riepilogo ha tuttavia ricevuto HTTP 400 da Anthropic perché `maxItems` non è supportato nello schema JSON. Il fallback ha consegnato un risultato, ma ha classificato erroneamente la notizia metro come `military`.
- **Esito iniziale:** fallito; il fallback ha evitato la perdita della consegna ma ha rivelato una regressione reale di qualità.
- **Correzione:** rimossi dallo schema Anthropic `maxItems` e `maxLength`; il limite di cinque bullet da 180 caratteri resta applicato deterministicamente da `normalize_category_result`. Aggiunti due test di regressione. Commit `6178f6a82c0cc99627efd3e68fd7a356150f6f72` e `2b14256ad77101ca9a800145dcc19384676ec211`.
- **CI finale:** GitHub Actions completata con successo sul commit `2b14256ad77101ca9a800145dcc19384676ec211`; 18 test attesi.
- **Deployment corretto:** `2675985d-2dd9-43d5-ac7d-d4eb4c6c0bc8`, stato `Active`, worker `Online`, sempre con `TEST_MODE=true`.
- **Secondo test end-to-end:** marker `STAGING_PR6_E2E_20260812_0835`; transizioni confermate, traduzione ALERT inviata, analisi Anthropic completata senza fallback, notizia metro selezionata esclusivamente come `kyiv_city`, riepilogo consegnato.
- **Risultato finale:** `STAGING_E2E_FIXED_RESULT True True True True DELIVERIES 4`.
- **Esito:** superato dopo la correzione; nessun messaggio del test è stato inviato ai canali pubblici.
- **Limite:** la prova riguarda staging isolato; il merge e il deployment di produzione restano separati.


### 2026-08-12 — Cambio feed ALERT a Real Kyiv e isolamento Telethon staging

- **Ambiente:** anteprima pubblica Telegram, GitHub Actions e Railway `staging-pr-6`.
- **Obiettivo:** sostituire l'unico feed operativo ALERT con `@kievreal1`, mantenere il canale generalista escluso in NORMAL e impedire a staging di aprire la sessione Telethon di produzione.
- **Verifica della fonte:** la pagina pubblica risolve `@kievreal1` come **Реальний Київ | Украина**; osservati formati reali su movimenti BPLА verso Kyiv/Brovary, attività PПО, esplosioni e cessato allarme. Osservata anche una notizia ferroviaria ordinaria usata come regressione negativa.
- **Modifica:** `ALERT_FEED_CHANNELS=['kievreal1']`; il filtro accetta soltanto aggiornamenti tattici durante ALERT e ignora completamente la fonte durante CLEAR.
- **Test automatici:** GitHub Actions run `31568036226`, 18 test superati; inclusi i formati reali di `@kievreal1` e il rifiuto della notizia ordinaria.
- **Problema scoperto:** il tentativo di verifica Telethon ha rivelato che la stessa `TELEGRAM_SESSION` era condivisa tra staging e produzione. Telegram ha invalidato la chiave con `AuthKeyDuplicatedError`; il deployment staging `06206837-5b55-4c6e-9099-76dc302938f1` e il deployment produzione esistente risultano in crash.
- **Correzione staging:** con `TEST_MODE=true`, il worker non apre più `TELEGRAM_SESSION`; i comandi interattivi richiedono una distinta `TEST_TELEGRAM_SESSION`. Deployment `bf4fea04-6274-4b1f-a549-e4c2c35e68ad` attivo e Online.
- **Esito:** superato per codice, CI, formati della fonte e isolamento staging; produzione bloccata.
- **Blocco produzione:** rigenerare una nuova `TELEGRAM_SESSION` e usarla esclusivamente in produzione prima del merge/deploy. Non è stato inviato alcun falso allarme pubblico.


### 2026-08-12 — Rigenerazione Telethon e deploy produzione `@kievreal1`

- **Ambiente:** Telegram, Railway produzione e GitHub `main`.
- **Obiettivo:** sostituire la sessione Telethon invalidata, completare il merge della PR 6 e attivare `@kievreal1` come unico feed degli aggiornamenti ALERT.
- **Sessione:** nuova autorizzazione completata tramite OTP; `TELEGRAM_SESSION` sostituita esclusivamente nelle variabili Railway di produzione. Il valore non è stato registrato nel repository o in questo file.
- **Recupero preliminare:** deployment `9cdc7d85-45de-46ba-a3e1-5455fb49ed9f` Active e worker Online; stato Telegram `CLEAR`; nessun nuovo `AuthKeyDuplicatedError`.
- **CI e merge:** GitHub Actions run `31568184712` superata con 18 test; PR 6 mergiata con squash commit `fe1e36c752d41f79253e181ef0b6476031520b32`.
- **Deployment finale:** `d71b7f27-f7d7-426b-b630-65ff35bef2b5`, stato Active, worker Online.
- **Log osservati:** SQLite pronto; stato Telegram caricato `CLEAR`; sorgenti NORMAL `kievinfo_kyiv`, `shv_ukr`, `AMK_Mapping`; trigger `@kyiv_airraid_alert`; unico feed ALERT `['kievreal1']`.
- **Esito:** superato per autorizzazione Telegram, CI, merge, avvio produzione e configurazione della sorgente.
- **Limite:** la ricezione e pubblicazione di un aggiornamento esterno di `@kievreal1` durante un allarme reale deve ancora essere osservata end-to-end; non è stato generato alcun falso allarme pubblico.

## Test futuri consigliati

1. ALERT reale o controllato: inizio, aggiornamento tradotto e cessato allarme nel canale; nessun riepilogo durante ALERT.
2. Riepilogo NORMAL con almeno un contenuto rilevante: consegna nel gruppo e assenza nel canale.
3. Ciclo NORMAL senza contenuti rilevanti: silenzio pubblico e heartbeat soltanto a Ops.
4. Pausa notturna 01:00–07:00 Europe/Kyiv e recap alla ripresa.
5. Riavvio Railway con almeno un record `pending`, per confermare sul volume reale che il messaggio sopravvive e viene riepilogato.
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
