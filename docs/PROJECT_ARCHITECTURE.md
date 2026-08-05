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
- **UkraineAlarm API**: prima sorgente dello stato di allerta per Kyiv città.
- **Anthropic API**: traduzione e produzione dei riepiloghi.
- **GitHub**: versionamento e origine dei deployment Railway.

## Canali e destinazioni

### Produzione

Le sorgenti di contenuto sono:

- `@kievinfo_kyiv`: informazioni concrete sulla vita e sulle infrastrutture di Kyiv.
- `@AMK_Mapping`: sviluppi militari rilevanti per la guerra in Ucraina.
- `@Nashee_PPO`: monitoraggio della difesa aerea, messaggi in tempo reale e recap numerici degli attacchi.

`@monitorwarr` è stato rimosso completamente e non deve essere registrato o letto.

Il gruppo Telegram di produzione è configurato tramite `TARGET_CHAT_ID`. L'ID non deve essere inserito nel codice.

### Trigger dell'allerta

Lo stato di allerta riceve sempre due input indipendenti:

1. UkraineAlarm API, regione Kyiv città (`UKRAINE_ALARM_REGION_ID`, valore attuale `31`).
2. Canale Telegram `@kyiv_airraid_alert`.

Entrambe le sorgenti vengono ascoltate continuamente. Il canale Telegram non è una sorgente di contenuti: viene usato soltanto per determinare inizio e fine dell'allerta.

## Regole di fusione dei due trigger

La priorità assoluta è evitare un falso cessato allarme o la mancata attivazione.

- Se una sola sorgente valida segnala **allarme**, il sistema entra immediatamente in ALERT.
- Se UkraineAlarm restituisce errori, incluso `401`, ma Telegram dispone di uno stato valido, viene seguito Telegram.
- Se Telegram non dispone ancora di uno stato valido ma l'API funziona, viene seguita l'API.
- Se entrambe concordano, viene applicato lo stato concordato.
- Se sono in conflitto, prevale **allarme**.
- Il cessato allarme viene applicato quando le sorgenti valide concordano, oppure quando è disponibile una sola sorgente valida e questa indica cessato allarme.
- Un errore API non modifica lo stato conosciuto.

All'avvio il worker legge gli ultimi messaggi di `@kyiv_airraid_alert` per ricostruire lo stato Telegram senza dover aspettare il prossimo evento.

## Modalità NORMAL

Il messaggio di avvio è volutamente breve:

```text
🟢 Kyiv Normal Monitor started
Mode: NORMAL (hourly summaries)
Night pause: 01:00–06:00 CET
```

In modalità NORMAL:

- i messaggi delle tre sorgenti di contenuto vengono filtrati e conservati in memoria;
- ogni ora viene generato un riepilogo in inglese;
- i contenuti non pertinenti e la pubblicità vengono esclususi;
- tra le 01:00 e le 06:00, fuso Europe/Rome, i riepiloghi orari sono sospesi;
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
OWNER_CHAT_ID
TELEGRAM_API_ID
TELEGRAM_API_HASH
TELEGRAM_SESSION
BOT_TOKEN
ANTHROPIC_API_KEY
UKRAINE_ALARM_API_KEY
UKRAINE_ALARM_REGION_ID
UKRAINE_ALARM_POLL_INTERVAL
```

## Protezioni operative

- Retry limitati per le richieste Telegram.
- Rispetto automatico di `retry_after` in caso di flood control.
- Una sola connessione HTTP condivisa.
- Serializzazione degli invii per evitare raffiche incontrollate.
- Conservazione dell'ultimo stato in caso di errore UkraineAlarm.
- Stato ALERT conservativo in caso di conflitto tra sorgenti.
- Deduplicazione dei messaggi simulati e protezione contro i loop del bot.

## Limiti noti

- UkraineAlarm ha restituito risposte intermittenti `401 Unauthorized`; per questo il trigger Telegram rimane sempre attivo come seconda sorgente.
- I buffer dei riepiloghi sono in memoria e vengono persi al riavvio del container.
- Lo stato ricostruito dal canale Telegram dipende dall'ultimo messaggio esplicito riconoscibile relativo a Kyiv.
- Il sistema è un'integrazione informativa e non sostituisce i sistemi ufficiali di protezione civile.

## Controlli dopo ogni deployment

1. Verificare che Railway mostri un solo deployment attivo.
2. Verificare che il log indichi `TEST_MODE` o produzione in modo coerente.
3. In produzione, controllare che le sorgenti di contenuto siano soltanto `kievinfo_kyiv`, `AMK_Mapping` e `Nashee_PPO`.
4. Controllare che `@kyiv_airraid_alert` sia registrato come trigger e non come contenuto.
5. Controllare la connessione UkraineAlarm e gli eventuali `401`.
6. Verificare il messaggio di avvio nel gruppo corretto.
7. Eseguire test funzionali esclusivamente nel gruppo di test.

