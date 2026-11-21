# BKHD Backup

**Versione 1.3.1~beta1** (anteprima; ultima stabile: 1.3.0) · © 2026 fabionet <fabionet666@gmail.com> · Licenza **GNU GPL v3 o successiva**

📘 Guida illustrata in PDF: [`docs/Guida-BKHD-Backup.pdf`](docs/Guida-BKHD-Backup.pdf)
(installata anche in `/usr/share/doc/bkhd-backup/` e apribile dal menu ☰ del programma).

Backup **settore per settore** di un intero disco (o di una partizione) **direttamente sul cloud**,
per Ubuntu, Debian e derivate (Mint, Pop!_OS, Zorin, elementary, MX...).

## Come funziona

```
Disco ──(lettura settori)──► pacchetto .bkhd in RAM (tmpfs) ──(upload, attesa fine)──► Cloud
                                     │
                                     └── cancellato dalla RAM dopo upload verificato ──► pacchetto successivo
```

1. L'utente sceglie una dimensione dei pacchetti tra **1,2 MB e 8 GB**: formati floppy (5¼" 1,2 MB, 3½" 1,44 MB,
   3½" ED 2,88 MB), 50 MB, 100 MB, CD (650/700 MB), 1, 2, 4 GB, DVD (4,7 GB), 8 GB oppure una misura personalizzata.
2. Il programma monta una cartella temporanea **in RAM** (tmpfs con `noswap` quando il kernel
   lo supporta, quindi i dati non finiscono neanche nello swap). **Su disco non viene scritto nulla.**
3. Legge il primo blocco di settori (es. 8 GB) nella cartella in RAM e crea `nome.part00001.bkhd`.
4. Carica il pacchetto sul cloud e **attende la fine dell'upload**, poi verifica la dimensione remota.
5. **Cancella il pacchetto dalla RAM**: da quel momento esiste solo sul cloud.
6. Aggiorna `manifest.json` sul cloud e ripete fino alla fine del disco.

Esempio: disco da 1 TB con pacchetti da 8 GB → 117 pacchetti `.bkhd`, mai più di uno alla volta in RAM.
Con i formati floppy un disco intero diventa centinaia di migliaia di pacchetti (molto lento): sono pensati per
partizioni e chiavette piccole. Oltre 10.000 pacchetti i file sul cloud vengono divisi in sottocartelle da 1000.

**Ripristino**: per ogni pacchetto → download in RAM → verifica SHA-256 → scrittura sul disco
nella posizione esatta → `fsync` → svuotamento della RAM → pacchetto successivo.

### Novità della versione 1.2
- **Destinazione su disco o cartella locale** (disco esterno, NAS montato): scrittura diretta dei pacchetti,
  senza RAM; rifiuta di salvare sul disco che si sta copiando.
- **Solo settori usati**: salta lo spazio libero dei file system (ext2/3/4 con `dumpe2fs`; NTFS, FAT, exFAT,
  Btrfs, XFS con `partclone`); partizioni montate, cifrate o sconosciute copiate per intero.
- **Password di protezione**: AES-256-GCM con chiave da scrypt; sigillo di autenticità per pacchetto,
  password mai salvata né passata sulla riga di comando.
- **Settori danneggiati**: «riprova e copia» oppure «salta subito»; pulsante **Controlla disco** (superficie + SMART).
- **Cerca backup interrotti** accanto al nome: elenco dei backup da riprendere e pulsante **Riprendi**;
  all'annullamento l'indice viene salvato, così la ripresa non ripete lavoro.
- **Pennetta avviabile BKHD Live**: Ubuntu 24.04 minimale con BKHD Backup, Firefox (repository Mozilla),
  rete LAN/Wi-Fi, GParted, GSmartControl, TestDisk, ddrescue, Memtest86+ e «Diagnosi rapida»;
  avvio BIOS e UEFI con Secure Boot. Creazione e scrittura su chiavetta (con verifica) dalla scheda dedicata.
- **Temi «nuvola di giorno» e «nuvola di notte»** (oppure automatico, come il desktop).

### Novità della versione 1.3
- **Immagine BKHD Live inclusa nel pacchetto** (`/usr/share/bkhd-backup/live/`): la pennetta si crea subito,
  senza scaricare nulla. La costruzione di un'immagine aggiornata resta disponibile come opzione avanzata.
- Scheda **«Pennetta e DVD»**: «Salva immagine ISO…», pennetta avviabile (con spazio facoltativo
  **BKHD-DATI** per i backup, montato e proposto da solo in BKHD Live), formattazione di una chiavetta per i soli
  backup (exFAT, ext4, FAT32, NTFS), «Usa come destinazione dei backup», «Espelli» e **masterizzazione su DVD**
  con verifica (l'immagine non entra in un CD).
- Nuovi comandi: `usb-format`, `disc-burn`, `live-write --data-fs exfat|ext4`.

### Novità della versione 1.3.1 (anteprima beta1)
- Immagine BKHD Live ricostruita con i pacchetti di Ubuntu 24.04 aggiornati (Firefox 160) e con la guida PDF
  aggiornata (nella 1.3.0 la live conteneva la versione precedente della guida).
- È un'anteprima: avvio collaudato in macchina virtuale (BIOS e UEFI con Secure Boot), non ancora su pennette
  e DVD reali.

### Sicurezza
- Password di amministratore richiesta a ogni operazione sui dischi (`auth_admin`, nessuna autorizzazione ricordata).
- Cartella in RAM accessibile solo a root; rclone (eseguito con l'utente) usa `rcat`/`cat` a flusso.
- Destinazioni locali gestite con descrittori `O_NOFOLLOW`/`O_EXCL`; pacchetti di root non modificabili dall'utente;
  verifica e scrittura del ripristino dallo stesso descrittore (copia privata in memoria se il file system non
  protegge i file, es. FAT/exFAT).
- Manifest e indici validati (tipi, limiti, posizioni); nei backup protetti sono firmati con HMAC e il sigillo
  AES-GCM copre backup, numero, posizione, lunghezza e formato di ogni pacchetto.
- Pennetta: cartella di lavoro di root «bloccata» con un bind mount, firme di debootstrap obbligatorie,
  chiave Mozilla verificata, ISO consegnata all'utente senza seguire collegamenti.

### Funzioni aggiuntive
- **Zoom dell'interfaccia** dall'80% al 200% (menu ☰ oppure <kbd>Ctrl</kbd> + <kbd>+</kbd> / <kbd>−</kbd> / <kbd>0</kbd>),
  ricordato ai successivi avvii.
- **Compressione zstd** facoltativa: il backup resta settore per settore, ma lo spazio vuoto del disco
  occupa quasi zero sul cloud (nel test, 1,5 GB → 300 MB).
- **Ripresa** di un backup interrotto (rete caduta, PC spento): riparte dal primo pacchetto mancante.
- **Riprova automatica** degli upload/download falliti (10 tentativi con attesa crescente).
- **Settori illeggibili**: vengono riletti uno a uno, salvati come zeri e annotati nel manifest.
- **Verifica integrità** di un backup senza scrivere nulla.
- Ripristino su **disco** oppure su **file immagine** (.img).
- Impedisce la sospensione del PC durante l'operazione.
- Protezioni: rifiuta di ripristinare su dischi montati/in uso o più piccoli del backup; per confermare
  bisogna scrivere il nome del dispositivo.

## Cloud supportati

Il trasferimento usa [rclone](https://rclone.org), quindi funziona con oltre 70 servizi: Google Drive,
OneDrive, Dropbox, Mega, pCloud, Box, Amazon S3, Backblaze B2, Wasabi, Cloudflare R2, WebDAV
(Nextcloud), SFTP, FTP... Per **cifrare** i backup basta configurare un remote rclone di tipo `crypt`
sopra il cloud scelto e selezionarlo in BKHD Backup.

## Installazione

```bash
sudo apt install rclone zstd python3-gi gir1.2-gtk-3.0
./packaging/build-deb.sh
sudo apt install ./dist/bkhd-backup_*_all.deb
```

Poi apri **BKHD Backup** dal menu delle applicazioni. Al primo avvio premi **«Configura cloud…»**:
si apre `rclone config` in un terminale per collegare il tuo cloud (operazione da fare una volta sola).

> La versione di rclone nei repository di Debian/Ubuntu può essere vecchia. Per i cloud più recenti
> puoi installare l'ultima versione dal sito ufficiale (https://rclone.org/install/).

## Uso da riga di comando (anche su server senza interfaccia grafica)

```bash
sudo bkhd-engine backup --source /dev/sda --remote gdrive: --path BKHD --name pc-casa \
     --chunk-size-mib 8192 --compression zstd
sudo bkhd-engine list    --remote gdrive: --path BKHD
sudo bkhd-engine backup --source /dev/sdc --remote gdrive: --path BKHD --name chiavetta --chunk-size 1440K
sudo bkhd-engine restore --remote gdrive: --path BKHD/pc-casa --verify-only
sudo bkhd-engine restore --remote gdrive: --path BKHD/pc-casa --target /dev/sdb --yes
```

Altri comandi: `sudo bkhd-engine scan --source /dev/sda` (controllo settori e SMART),
`sudo bkhd-engine live-build --output ~/BKHD-Live/bkhd-live.iso`,
`sudo bkhd-engine live-write --image ~/BKHD-Live/bkhd-live.iso --target /dev/sdX --yes`.

Opzioni utili: `--remote /media/utente/Disco` (destinazione locale), `--used-only`, `--encrypt`,
`--bad-sectors retry|skip`, `--chunk-size` (es. `1200K`, `1440K`, `2880K`, `50M`, `100M`, `4G`), `--resume` (riprende un backup interrotto), `--from-chunk N` (riprende un ripristino),
`--retries N`, `--overwrite`.

## RAM necessaria

Dimensione del pacchetto + circa 580 MB (quindi circa 580 MB con i formati floppy). Con pacchetti da 8 GB servono quindi almeno ~9 GB di RAM
libera; l'interfaccia mostra la RAM disponibile e blocca l'avvio se non basta.

## Consigli importanti

- Per copiare **il disco di sistema** (quello da cui è avviato il PC) in modo coerente, avvia il PC
  da una **chiavetta live di Ubuntu**, installa lì BKHD Backup ed esegui il backup da lì.
  Lo stesso vale per il ripristino del disco di sistema.
- Fai periodicamente una **«Solo verifica integrità»** dei backup importanti.
- Se ripristini su un disco GPT più grande dell'originale: `sudo sgdisk -e /dev/sdX`.

## Formato dei file

Cartella sul cloud: `<cartella>/<nome-backup>/`
- `manifest.json` – disco di origine (modello, seriale, dimensione, settore, tabella partizioni),
  dimensione pacchetti, compressione, numero di pacchetti salvati.
- `index/index-NNNNN.json` – per ogni gruppo di 1000 pacchetti: dimensione e codici SHA-256
  (i backup della versione 1.0, con l'indice dentro il manifest, restano leggibili).
- `<nome>.part00001.bkhd`, `...part00002.bkhd`, ... – intestazione di 4096 byte
  (`BKHD\x01\n` + JSON con indice, offset, lunghezza, checksum) seguita dai dati del disco
  (grezzi oppure compressi zstd).

## Struttura del progetto

| File | Descrizione |
|---|---|
| `src/bkhd_engine.py` | motore di backup/ripristino (gira come root tramite pkexec, rclone gira come utente) |
| `src/bkhd_gui.py` | interfaccia grafica GTK 3 |
| `live/bkhd-live-build.sh` | costruisce l'immagine ISO di BKHD Live (debootstrap, squashfs, GRUB BIOS/UEFI firmato) |
| `tests/gui_features.py` | test automatico di destinazione locale, password, ripresa e ripristino dall'interfaccia |
| `tests/qemu_live_test.py` | collaudo di BKHD Live in QEMU (BIOS e UEFI con Secure Boot) |
| `data/` | lanciatore .desktop, icona (SVG; il pacchetto include le versioni PNG da 16 a 512 px), regola polkit |
| `docs/guida.html` | sorgente della guida illustrata |
| `docs/make_screenshots.py` | genera le schermate della guida pilotando l'interfaccia (dischi d'esempio, cloud simulato) |
| `docs/build_guide.py` | crea `docs/Guida-BKHD-Backup.pdf` con Chromium/Chrome |
| `LICENSE` | testo completo della GNU GPL v3 |
| `packaging/build-deb.sh` | crea il pacchetto .deb |
| `tests/run_tests.sh` | test end-to-end con un cloud simulato (non serve root) |
| `tests/gui_smoke.py` | test automatico dell'interfaccia |

## Rigenerare la guida PDF

```bash
GTK_THEME=Yaru python3 docs/make_screenshots.py /tmp/bkhd-schermate   # richiede sessione grafica X11
python3 docs/build_guide.py
```

## Autore e licenza

Copyright © 2026 **fabionet** — fabionet666@gmail.com

Questo programma è software libero: puoi redistribuirlo e/o modificarlo secondo i termini della
GNU General Public License pubblicata dalla Free Software Foundation, versione 3 della licenza o
(a tua scelta) qualsiasi versione successiva. È distribuito nella speranza che sia utile, ma
SENZA ALCUNA GARANZIA. Vedi il file [`LICENSE`](LICENSE).
