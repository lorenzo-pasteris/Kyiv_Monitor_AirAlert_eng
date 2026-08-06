# Kyiv Monitor — Architettura e funzionamento

## Regola obbligatoria di sincronizzazione

La documentazione è parte integrante del progetto. Ogni modifica a codice, architettura, canali, trigger, regole operative, variabili d'ambiente, configurazione Railway/GitHub, ambiente di test o strumenti deve aggiornare nello stesso lavoro i file pertinenti nella cartella `docs/`.

Una modifica non è considerata completa finché la documentazione non descrive fedelmente lo stato distribuito. Prima di ogni commit o deploy verificare sempre se devono essere aggiornati almeno `PROJECT_ARCHITECTURE.md` e `SELF_HEALING_LOOPS.md`.

## Scopo

Kyiv Monitor pubblica informazioni in inglese nel gruppo Telegram di produzione dedicato a Kyiv. Il sistema ha due modalità operative:

- **NORMAL**: raccoglie i messaggi e pubblica riepiloghi orari.
- **ALERT**: inoltra rapidamente, traducendoli, i messaggi di monitoraggio relativi all'allerta in corso.

Il servizio è eseguito come worker Python su Railway e il codice è conservato su GitHub.

## Componenti

- **Railway**: esecuzione continua, configurazione tramite variabili d'ambiente e log.
- **Telethon**: lettura dei canali e dei gruppi Telegram.
- **Telegram Bot API**: pubblicazione dei messaggi nel gruppo di destinazione.
- **`@kyiv_airraid_alert`**: unica sorgente dello stato di allerta per Kyiv città.
- **Anthropic API**: traduzione e produzione dei riepiloghi.
- **GitHub**: versionamento e origine dei deployment Railway.

Railway usa esclusivamente Watch Paths positivi per i file eseguibili: `/monitor.py` e `/requirements.txt` (eventuali duplicati sono innocui). I commit che modificano soltanto `docs/` devono risultare `Skipped` e non devono riavviare il worker. Non usare negazioni Gitignore in questa configurazione.

## Canali e destinazioni

### Produzione

Le sorgenti di contenuto sono:

- `@kievinfo_kyiv`: informazioni concrete sulla vita e sulle infrastrutture di Kyiv.
- `@AMK_Mapping`: sviluppi militari rilevanti per la guerra in Ucraina.
- `@Nashee_PPO`: monitoraggio della difesa aerea, messaggi in tempo reale e recap numerici degli attacchi.

`@monitorwarr` è stato rimosso completamente e non deve essere registrato o letto.

Il gruppo Telegram di produzione è configurato tramite `TARGET_CHAT_ID`. L'ID non deve essere inserito nel codice.

### Trigger dell'allerta

Lo stato di allerta proviene esclusivamente dal canale Telegram `@kyiv_airraid_alert`. Il canale non è una sorgente di contenuti: viene usato soltanto per determinare inizio e fine dell'allerta.

## Regole del trigger

- Un messaggio esplicito di allarme per Kyiv attiva immediatamente ALERT.
- Un messaggio esplicito di cessato allarme per Kyiv riporta il sistema in NORMAL.
- Messaggi ambigui o non riferiti a Kyiv non modificano lo stato conosciuto.
- UkraineAlarm API è stata rimossa dal codice e dalla configurazione perché restituiva `401 Unauthorized`.

All'avvio il worker legge gli ultimi messaggi di `@kyiv_airraid_alert` per ricostruire lo stato senza dover aspettare il prossimo evento.

## Modalità NORMAL

Il messaggio di avvio è volutamente breve e viene inviato esclusivamente alla chat operativa `OPS_CHAT_ID` quando il worker riparte, per esempio dopo un deployment. Non viene pubblicato nella chat di produzione:

```text
🟢 Kyiv Normal Monitor started
Mode: NORMAL (hourly summaries)
Night pause: 01:00–07:00 EET/EEST
```

Il messaggio di cessato allarme `ALL CLEAR — KYIV / Back to NORMAL mode` resta invece nella chat di produzione, perché descrive un reale cambio di stato operativo e non un riavvio tecnico.

In modalità NORMAL:

- i messaggi delle quattro sorgenti di contenuto vengono filtrati e conservati in memoria;
- ogni ora viene generato un riepilogo in inglese;
- se nessuna categoria contiene aggiornamenti rilevanti, il ciclo termina senza inviare messaggi Telegram; il completamento viene registrato nei log e non attiva il watchdog;
- in produzione il ciclo è allineato all'ora esatta di `Europe/Kyiv` (00 minuti), indipendentemente dall'orario dell'ultimo riavvio;
- i contenuti non pertinenti e la pubblicità vengono esclususi;
- tra le 01:00 e le 06:00, fuso Europe/Kyiv, i riepiloghi orari sono sospesi;
- al termine della pausa notturna viene prodotto un recap complessivo.

### Regole per `@Nashee_PPO`

I riepiloghi devono dare priorità ai recap notturni o giornalieri contenenti quantità precise:

- numero totale di Shahed e altri UAV;
- missili da crociera, balistici e ipersonici;
- bombe guidate e altre tipologie dichiarate;
- intercettazioni, impatti e aree interessate;
- vittime e danni, soltanto quando esplicitamente riportati.

I numeri devono essere conservati separatamente. Il modello non deve inventare, sommare o combinare quantità non compatibili.

## Modalità ALERT

Quando lo stato effettivo diventa ALERT:

- i buffer dei riepiloghi vengono svuotati;
- viene pubblicato un unico messaggio di inizio allerta;
- vengono ignorati i contenuti di `@kievinfo_kyiv` e `@AMK_Mapping`;
- vengono elaborati esclusivamente i nuovi messaggi reali di `@Nashee_PPO`;
- ciascun messaggio viene tradotto in inglese e pubblicato dopo pochi secondi;
- pubblicità e contenuti non pertinenti vengono filtrati.

Quando lo stato effettivo torna CLEAR, il sistema pubblica il cessato allarme e ritorna in NORMAL.

## Ambiente di test

Il test usa un unico gruppo Telegram privato configurato tramite `TEST_CHAT_ID`.

Quando `TEST_MODE=true`:

- viene usato esclusivamente `TEST_CHAT_ID` come sorgente e destinazione;
- nessun handler viene registrato sui canali Telegram reali;
- non viene letta l'API UkraineAlarm;
- non viene inviato nulla al gruppo di produzione;
- i riepiloghi vengono eseguiti ogni 3 minuti invece che ogni ora.

Comandi disponibili:

- `/test_start`: attiva direttamente ALERT nel test.
- `/test_message`: genera un falso messaggio sorgente.
- `/test_burst N`: genera `N` falsi messaggi consecutivi.
- `/test_end`: termina direttamente l'allerta di test.
- `/test_summary`: forza un riepilogo quando il test è in NORMAL.

I messaggi simulati sono identificati da `[TEST_SOURCE:Nashee_PPO]`. Il marcatore e le etichette interne come `[burst 6/20]` vengono rimossi prima della traduzione. I messaggi prodotti dal bot sono esclusi esplicitamente dalla rielaborazione tramite sender ID e message ID.

Quando `TEST_MODE=false`, i comandi e gli handler di simulazione sono disabilitati e vengono usate esclusivamente le sorgenti reali.

## Variabili d'ambiente

Le credenziali non devono essere inserite nel repository. Railway deve contenere almeno:

```text
TEST_MODE
TEST_CHAT_ID
TARGET_CHAT_ID
OPS_CHAT_ID
OWNER_CHAT_ID
TELEGRAM_API_ID
TELEGRAM_API_HASH
TELEGRAM_SESSION
BOT_TOKEN
ANTHROPIC_API_KEY
```

`OPS_CHAT_ID` identifica il gruppo privato dedicato alle notifiche operative: errori dopo i retry, fallback AI, fallimenti di consegna, interventi del watchdog e silenzio anomalo delle sorgenti. Se non è configurato, viene usato `OWNER_CHAT_ID` per compatibilità. I dettagli ordinari restano esclusivamente nei log Railway; la chat di produzione non riceve messaggi “No relevant updates”.

## Protezioni operative

- Retry limitati per le richieste Telegram.
- Rispetto automatico di `retry_after` in caso di flood control.
- Una sola connessione HTTP condivisa.
- Serializzazione degli invii per evitare raffiche incontrollate.
- Deduplicazione dei messaggi simulati e protezione contro i loop del bot.

## Limiti noti

- UkraineAlarm è stato rimosso: il trigger è esclusivamente `@kyiv_airraid_alert`.
- I buffer dei riepiloghi sono in memoria e vengono persi al riavvio del container.
- Lo stato ricostruito dal canale Telegram dipende dall'ultimo messaggio esplicito riconoscibile relativo a Kyiv.
- Il sistema è un'integrazione informativa e non sostituisce i sistemi ufficiali di protezione civile.

## Controlli dopo ogni deployment

1. Verificare che Railway mostri un solo deployment attivo.
2. Verificare che il log indichi `TEST_MODE` o produzione in modo coerente.
3. In produzione, controllare che le sorgenti di contenuto siano soltanto `kievinfo_kyiv`, `AMK_Mapping` e `Nashee_PPO`.
4. Controllare che `@kyiv_airraid_alert` sia registrato come trigger e non come contenuto.
5. Verificare il messaggio di avvio nel gruppo corretto.
6. Eseguire test funzionali esclusivamente nel gruppo di test.

## Aggiornamento operativo: riepiloghi e sorgenti

Il ciclo NORMAL usa quattro sorgenti di contenuto:

- `@kievinfo_kyiv` per disservizi e infrastrutture di Kyiv;
- `@shv_ukr` per sviluppi politici, economici, diplomatici e nazionali ucraini;
- `@AMK_Mapping` per analisi militare della guerra Russia-Ucraina;
- `@Nashee_PPO` per monitoraggio della difesa aerea e riepiloghi numerici degli attacchi.

Il fuso orario operativo è `Europe/Kyiv`: gli output mostrano automaticamente `EEST` in estate e `EET` in inverno. La pausa notturna 01:00–07:00 segue lo stesso fuso.

Prima dell'analisi viene acquisita un'istantanea del buffer. Anthropic Structured Outputs (`output_config.format` con JSON Schema) è la difesa primaria: impone tutte le categorie e i tipi previsti. Dopo la risposta vengono controllati HTTP, `stop_reason`, JSON, categorie, tipi e ID. Il parser del primo oggetto JSON rimane soltanto come fallback legacy: ogni utilizzo produce un log esplicito e il risultato viene sottoposto alla stessa validazione rigorosa. Il budget cresce da 4000 a 6000 e 8000 token esclusivamente quando `stop_reason=max_tokens`; `refusal`, errori 400 e output strutturalmente invalidi non vengono riprovati identici. Timeout, errori di trasporto, `429` e `5xx` ricevono fino a tre tentativi con attesa crescente e rispetto di `Retry-After`. Se l'analisi non è disponibile, l'output di emergenza contiene soltanto brevi estratti originali con fonte esplicita e avverte che non si tratta di una sintesi AI.

I messaggi vengono rimossi soltanto dopo la conferma dell'invio Telegram. Se la consegna non è confermata, il buffer resta intatto. Il watchdog riprova un riepilogo mancante a 62, 65, 67 e 70 minuti dall'ultimo invio confermato. Il loop dei riepiloghi intercetta le eccezioni e continua a funzionare.

I log registrano buffer, tentativi AI, uso del fallback, risultato per sorgente e conferma di consegna.



## Classificazione trasversale e statistiche per fonte

Le sorgenti indicano esclusivamente la provenienza dei messaggi. Non esiste più una categoria
assegnata rigidamente a ciascun canale: a ogni ciclo NORMAL, ciascun messaggio acquisito da
`@kievinfo_kyiv`, `@shv_ukr`, `@AMK_Mapping` e `@Nashee_PPO` viene valutato contro tutte
le categorie:

- `kyiv_city`: disservizi, infrastrutture e conseguenze concrete sulla vita di Kyiv;
- `ukraine_national`: politica, economia, diplomazia e sviluppi nazionali ucraini;
- `military`: sviluppi militari e analisi della guerra Russia-Ucraina;
- `air_defence`: droni, missili, aviazione, intercettazioni, impatti e recap degli attacchi.

Un messaggio può appartenere a più categorie quando è realmente pertinente. Pubblicità,
clickbait e materiale non pertinente vengono scartati; i filtri preventivi specifici per canale
non vengono più applicati.

Ogni riepilogo scrive nei log Railway:

- `[SUMMARY INPUT]`: messaggi ricevuti per fonte;
- `[CATEGORY STATS]`: messaggi validi per ciascuna combinazione categoria × fonte;
- `[CATEGORY ITEM]`: anteprima e provenienza dei messaggi selezionati;
- `[CATEGORY ANALYSIS ERROR]`: errore del classificatore, con conservazione completa dei buffer.

Le stesse statistiche vengono archiviate in SQLite nella posizione indicata da
`CATEGORY_STATS_DB_PATH`, valore predefinito
`/data/kyiv_monitor_category_stats.sqlite3`. Per conservarle attraverso restart e deployment,
Railway deve montare un volume persistente su `/data`. Le tabelle sono
`hourly_category_stats` e `hourly_classifications`.


## Pianificazione oraria e ordine cronologico

In produzione i riepiloghi sono ancorati alle ore piene di `Europe/Kyiv`, non all'istante
di avvio del container. Dopo un restart, il primo ciclo viene pianificato per la successiva
ora piena EET/EEST e i successivi continuano alle ore 12:00, 13:00 e così via.

La pausa notturna è 01:00–07:00 EET/EEST. Il riepilogo accumulato durante la pausa viene
pubblicato alle 07:00. All'interno di ogni categoria, il modello deve ordinare i bullet dal
messaggio/evento più vecchio al più recente.
