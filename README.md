# CAN Tool

Python-CAN-Tool mit GUI: Vector VN1630A / VN7572 oder PEAK PCAN-USB Pro FD, DBC/CDD-Verwaltung,
zyklisches Senden/Empfangen, Trace, Testsequenzen, Liniengraph, Parquet-Logging, ISO-TP-Diagnose.

## Installation und Start
    pip install -r requirements.txt
    python main.py                    # GUI
    python main.py --project demo     # GUI mit Projekt
    python main.py --convert demo     # nur DBC/CDD -> JSON/YAML (ohne GUI)
    python -m unittest discover -s tests -v   # Kerntests (ohne Hardware/GUI)

Treiber: Vector -> "Vector XL Driver Library" + Hardware Config (Kanal-Index);
PEAK -> PCAN-Basic-Treiber (Kanal z. B. PCAN_USBBUS1). Das Demo-Projekt laeuft in der Simulation (ohne Hardware).

## Windows und Linux

Das Tool erkennt das Betriebssystem selbststaendig (`platform_info.py`, Anzeige in der Titelzeile). Funktionsumfang (Trace, Senden inkl. Rohframes, Testsequenzen, Graph, Logging, Diagnose, vFlash, Hilfe/PDF, Sprachen) ist auf beiden Systemen gleich; Projektordner sind austauschbar. Unterschiede liegen nur bei der Hardware-Anbindung:

| Hardware | Windows | Linux |
|---|---|---|
| Simulation | ja | ja |
| Vector VN1630A (USB) / VN7572 (PCIe-Karte) | ja (Vector XL Driver Library; beide Typ `vector`) | nein - XL-Treiber nur unter Windows; Geraet ggf. ueber SocketCAN einbinden |
| PEAK PCAN-USB (Pro/FD) | ja (PCAN-Basic) | ja (PCAN-Basic-Bibliothek `libpcanbasic` oder Kernel-Treiber als `can0` ueber SocketCAN) |
| SocketCAN | nein | ja (z. B. `sudo ip link set can0 up type can bitrate 500000`) |

Nicht nutzbare Eintraege sind im Interface-Dropdown markiert und gesperrt; **Hardware suchen** listet angeschlossene Schnittstellen. Start: `start_windows.bat` bzw. `./start_linux.sh` (legt beim ersten Start eine virtuelle Umgebung an). Unter Linux (X11) braucht Qt ab 6.5 die Bibliothek `libxcb-cursor0` (`sudo apt install libxcb-cursor0`); fehlt sie, meldet das Tool das mit Installationshinweis statt abzustuerzen. Projektnamen duerfen keine unter Windows reservierten Namen (CON, NUL, COM1 ...) sein. Die Konsole wird unter Windows auf UTF-8 gestellt. Hinweis: Hier wurde nur unter Linux (ohne Hardware, ohne GUI) getestet; die Windows-Pfade sind per Code-Review und Tests mit simulierter Betriebssystemkennung geprueft.

## Vector: "Vector XL library not found: vxlapi64"

Die Vector-Treiberbibliothek fehlt (Windows). Loesung: **Vector Driver Setup** bzw. **XL Driver Library** von vector.com installieren (legt `vxlapi64.dll` in `C:\Windows\System32` ab) und das Tool neu starten; alternativ die DLL in den Tool-Ordner oder nach `drivers\` kopieren (wird automatisch gefunden). 32-bit-Python sucht `vxlapi.dll` - 64-bit-Python verwenden. Danach in der *Vector Hardware Config* pruefen, dass der VN1630A erscheint und kein anderes Programm (CANalyzer/CANoe/vFlash) den Kanal belegt. Das Tool zeigt diese Hinweise bei fehlender DLL selbst an. Unter Linux gibt es keinen XL-Treiber (siehe Tabelle oben).

## Vector: "Channel 1 of application CANalyzer is not assigned to any interface"

python-can nutzt ohne App-Name die Vector Hardware Config (Anwendung "CANalyzer"); ist dort kein Kanal zugewiesen, scheitert das. Das Tool spricht das Geraet deshalb jetzt zuerst **direkt ueber die Seriennummer** an (Feld *Kanal* = Nummer am Geraet, **0 = CH1**, 1 = CH2 ...; virtuelle Vector-Kanaele werden uebersprungen), dann ueber den globalen Hardware-Index und zuletzt ueber die Hardware Config. Bei einem Fehler werden alle Versuche und die vom Treiber gemeldeten Vector-Kanaele (Name, Seriennummer) angezeigt; *Hardware suchen* zeigt dieselbe Liste. Das Feld *App-Name* leer lassen, ausser die Zuweisung soll bewusst aus der Hardware Config kommen.

## Verbunden, aber keine Empfangsdaten (Vector)

Die Statuszeile zeigt, welches Geraet/Kanal geoeffnet wurde (z. B. `Vector CH1 [VN1630A Channel 1 (S/N ...) ueber Seriennummer]`). Kommt 3 s nach dem Verbinden kein Frame an, erscheint eine Checkliste. Haeufigste Ursache: **Kanalnummer** - im Feld *Kanal* ist **0 = CH1**, 1 = CH2; steht dort 1, liest das Tool den zweiten, unbeschalteten Kanal ohne Fehlermeldung. Weitere Punkte: gleiche Bitrate, mindestens ein sendender Knoten (PCAN-View sendet?), Terminierung 2 x 120 Ohm nur an den Busenden mit kurzem Y-Abzweig, CAN-H/CAN-L und Masse, kein anderes Programm auf dem Kanal, CAN FD nur bei FD-Bus.

## Trace: Aktualisierungsmodus

Der Haken *Aktualisieren* in der Trace-Leiste schaltet um: ohne Haken werden alle Frames scrollend hintereinander geschrieben (wie bisher); mit Haken steht jede Botschaft (Richtung, Kanal, ID) nur **einmal** in der Tabelle (Reihenfolge des ersten Auftretens). Bei erneutem Auftreten werden Zeit, Daten und Signale dieser Zeile aktualisiert und die neue Spalte *Anzahl* erhoeht. Beim Umschalten wird die Tabelle geleert; Autoscroll gilt nur im scrollenden Modus. Zusaetzlich zeigt der Haken *Diffzeit* (nur im Aktualisierungsmodus) in der Zeitspalte den Abstand zum vorigen Auftreten derselben Botschaft (Zykluszeit); beim ersten Auftreten 0. (Logik in `trace_logic.py` getestet; die GUI selbst nicht.)

## XCP (Rubrik XCP)

A2L-Dateien (`projects/<name>/a2l/*.a2l`) werden wie DBC/CDD nach `cache/<name>.a2l.json|yaml` konvertiert (Import ueber *Projekt*, oder `python main.py --convert demo`). Die Rubrik **XCP** verbindet per XCP on CAN (Master-/Slave-ID aus der A2L vorbelegt, aenderbar), zeigt Messgroessen und Parameter, liest die Beobachtungsliste einmal oder zyklisch (SHORT_UPLOAD-Polling) und schreibt Parameter (SET_MTA + DOWNLOAD). Unterstuetzt: skalare Datentypen, Umrechnung IDENTICAL/LINEAR/RAT_FUNC (ohne quadratische Terme)/TAB_VERB, BIT_MASK beim Lesen. **Nicht enthalten:** DAQ/STIM, Seed&Key, Kalibrierseiten, Flash. In der Simulation antwortet ein simulierter XCP-Slave auf Basis der A2L (Demo: `projects/demo/a2l/demo.a2l`, IDs 600/601). Mit echten Steuergeraeten und realen A2L-Dateien nicht getestet; die GUI ebenfalls nicht.

## Struktur
    can_tool/
    ├── main.py              Start / Kommandozeile
    ├── gui.py               Rubriken: Projekt, Daten, Trace, Testsequenz, Graph, Logging, Diagnose, Hilfe
    ├── can_manager.py       Backends (python-can, Simulation), Sendeliste (zyklisch), Empfang, SignalStore
    ├── dbc_manager.py       DBC-Parser, Konvertierung nach JSON/YAML, Encode/Decode
    ├── cdd_manager.py       CDD -> JSON/YAML (siehe Einschraenkung)
    ├── iso_tp_manager.py    ISO-TP (Classic + FD), UDS-Request/Response, Diagnose-Definition, virtuelles Steuergeraet
    ├── sequence_runner.py   Testsequenzen (JSON) laden/speichern/ausfuehren
    ├── parquet_logger.py    Parquet-Logging mit Filter
    ├── help_content.py      Inhalte der Rubrik "Hilfe" (Kapitel 1-3, Anhaenge)
    ├── project_manager.py   Projektverwaltung
    └── projects/<name>/     project.json, dbc/, cdd/, sequences/, cache/ (konvertiert), logs/ (Parquet)

## Ablauf
1. Projekt waehlen/anlegen, DBC/CDD importieren. Beim Oeffnen werden DBC/CDD automatisch nach
   `cache/*.dbc.json|yaml` bzw. `cache/*.cdd.json|yaml` gewandelt (nur bei geaenderter Quelle). Zur Laufzeit wird nur der Cache gelesen.
2. Rubrik "Daten": Botschaften aus der DBC nach TX/RX uebernehmen (Doppelklick = TX). Signalwerte,
   Zyklus und zyklisches Senden je Botschaft; Haken "Auswahl" bestimmt, welche Signale fuer Graph/Logging gelten.
3. Trace, Graph, Logging nutzen die Auswahl. Logging: eigener Filter (Signale, nur bei Aenderung, Mindestabstand).
4. Testsequenz: genau eine zur Zeit im Editor; Speichern/Laden in `sequences/`.
   Schritttypen: set_signal, enable_tx, set_cycle, send_once, wait, wait_for, check, diag, diag_raw, loop, comment.
   Im Befehls-Dropdown zeigt ein Tooltip je Befehl Format, Beschreibung der Elemente und ein Beispiel
   (Quelle: `STEP_HELP` in sequence_runner.py, auch Grundlage von Hilfe-Anhang A).
5. Diagnose: Services aus der CDD, Roh-Requests, Tester Present, Antwortdekodierung.
6. Hilfe: Kapitel zu Projekterzeugung, Bedatung der Dateien vor dem Start und jeder Rubrik (mit Suche).

## PEAK PCAN-USB (Pro / Pro FD)
Interface `PEAK PCAN-USB`, Kanal `PCAN_USBBUS1` oder einfach `1` (zweiter Kanal: `2`); leer = Kanal 1. CAN FD nur bei FD-Geraeten (PCAN-USB Pro FD) aktivieren, beim PCAN-USB Pro (ohne FD) Haken aus lassen. Bitrate muss zum Bus passen (z. B. 500 kbit/s). Der Trace funktioniert auch ohne DBC/CDD (dann ohne Namen/Signale). Schlaegt das Oeffnen fehl, erscheint die Original-Fehlermeldung mit Hinweis.

## Sprachen und Dark Mode
Am rechten Rand der Rubrikzeile stehen ein Haken **Dark Mode** und ein Dropdown **Sprache** (Deutsch, English, Francais, Espanol, Chinesisch (vereinfacht), Thai). Beides wirkt sofort, auch auf Tooltips der Befehle und die Hilfe, und wird in `~/.can_tool/settings.json` gespeichert.
- Jede Sprache liegt in einer eigenen Datei `lang/<code>.yaml` (Abschnitte `meta`, `ui`, `core`, `steps`, `help`). Deutsch (`de`) ist Primaer- und Rueckfallsprache.
- Neue Sprache: `lang/de.yaml` kopieren, uebersetzen, `python tools/check_lang.py <code>` ausfuehren; sie erscheint danach automatisch im Dropdown.
- Kommandozeile: `python main.py --lang en --list`.
- Die Uebersetzungen wurden maschinell erzeugt und nicht von Muttersprachlern geprueft.

## Diagnose: vFlash-Pack (Reiter vFlash)
Laedt ein vFlash-Pack (`.vflashpack`/`.zip`), zeigt die Interpretation (Allgemein, Daten, Ablauf, Hinweise) und fuehrt den Ablauf mit dem **eigenen UDS-Flashablauf** ueber die gewaehlte Hardware aus (Vector, PEAK, SocketCAN, Simulation) - die Software vFlash wird nicht benoetigt. Ein Fortschrittsfenster zeigt Gesamtfortschritt, Status je Schritt, aktuellen Datenblock mit Datenrate und Protokoll; vor dem Start gibt es eine Sicherheitsabfrage, waehrend des Flashens ist Abbrechen zwischen zwei Transfers moeglich.
- Vollstaendig unterstuetzt: ZIP mit `flashpack.json|yaml` (Manifest) + HEX/S-Record/BIN-Dateien (Format: Hilfe, Anhang C; Vorlage: `projects/demo/flash/demo.vflashpack`, erzeugt mit `python tools/make_demo_flashpack.py`).
- **Echte vFlash-Packs:** deren internes Format ist nicht oeffentlich dokumentiert. Das Tool findet HEX/S19/BIN-Dateien und liest das Projekt-XML heuristisch (Name, CAN-IDs); Ablauf, Adressen und Seed&Key muessen meist ueber ein Manifest ergaenzt werden. Herstellerspezifische Seed&Key-DLLs, ODX-F/PDX und Signaturen werden nicht ausgewertet; aus Paketen wird nie Code ausgefuehrt. Ohne eigenen Ablauf gilt ein generischer UDS-Standardablauf (nicht herstellerspezifisch). Nicht an echter Hardware getestet - zuerst mit der Simulation ueben.
- Test in der Simulation: Projekt `demo`, Hardware Simulation, verbinden, Reiter vFlash, `projects/demo/flash/demo.vflashpack` laden, Flash starten (die simulierte ECU nimmt Daten an, Security-Key = Seed XOR 0xFFFFFFFF).

## Trace: Farbmodus
Der Button **Farbig** (Rubrik Trace, umschaltbar) faerbt die Zeilen: Rx mittelblau, Tx mittelgruen, fehlerhafte Botschaften (Bus-Fehlerframes, Typ `ERR`) rot, ISO-TP-Botschaften (Diagnose-Request-/Response-ID aus der Diagnose-Konfiguration, auch 0x7DF) mittelviolett. Ausgeschaltet bleibt die Darstellung wie bisher. Error-Frames werden nur im Trace angezeigt (Logging, Signale und Sequenzen ignorieren sie).

## vFlash-Pack (Rubrik Diagnose, Reiter vFlash)
Laedt ein vFlash-Pack (`.vflashpack`/`.zip`), zeigt die Interpretation (ECU, IDs, Seed&Key, Datenbloecke, Ablauf, Hinweise) und fuehrt den Ablauf nach Sicherheitsabfrage mit dem eigenen UDS-Flashablauf (0x10, 0x28, 0x27, 0x31, 0x34/0x36/0x37, 0x11) ueber die in Projekt gewaehlte Hardware aus (Vector, PEAK, SocketCAN, Simulation) - die Software vFlash wird nicht benoetigt. Ein Fortschrittsfenster zeigt Gesamtfortschritt, Status je Schritt, den laufenden Datenblock mit Datenrate und das Protokoll; "Abbrechen" stoppt zwischen zwei Datenbloecken.
- Vollstaendig unterstuetzt: Pakete mit `flashpack.json`/`.yaml` (Manifest, siehe Hilfe Anhang C) plus HEX-/S-Record-/BIN-Dateien. Beispiel: `projects/demo/flash/demo.vflashpack` (Simulation mit virtuellem Steuergeraet; erzeugt mit `python tools/make_demo_flashpack.py`).
- Echte vFlash-Packs ohne Manifest: Das Format ist nicht oeffentlich dokumentiert und wurde hier nicht gegen echte Packs getestet. Datendateien werden gefunden, Namen/CAN-IDs heuristisch gelesen; Ablauf, Adressen und Seed&Key sind meist ueber ein Manifest zu ergaenzen. Herstellerspezifische Seed&Key-DLLs werden nicht ausgefuehrt (nur xor/add/constant); aus Paketen wird nie Code ausgefuehrt.
- Ohne eigenen Ablauf gilt ein generischer UDS-Standardablauf (nicht herstellerspezifisch). **Flashen ueberschreibt Steuergeraete-Speicher - zuerst an Simulation/Bench testen.**

## Hilfe, Abbildungen und PDF-Handbuch
- Die Rubrik **Hilfe** enthaelt je Rubrik eine kommentierte Abbildung mit nummerierten Erklaerungen (Texte in `lang/*.yaml`, Schluessel `help.shot.<rubrik>.<nr>`; Bilder in `help_images/<rubrik>_<sprache>.png`).
- Die Abbildungen sind schematische Nachbauten der Oberflaeche, erzeugt mit `python tools/make_screenshots.py [sprachen]` (benoetigt playwright + Chromium) aus den Texten der Sprachdateien; bei neuen Sprachen oder geaenderten Texten neu erzeugen.
- Button **Handbuch als PDF...** (Rubrik Hilfe) oder `python main.py --manual handbuch.pdf`: schreibt die Hilfe in allen verfuegbaren Sprachen als A4-PDF (Deckblatt mit Programmname und Ausgabedatum, je Sprache alle Kapitel mit Abbildungen) und bietet sie zum Speichern an. Umsetzung: `manual_builder.py` (Qt `QTextDocument` + `QPdfWriter`; Schriften fuer Thai/Chinesisch kommen vom System).
- `docs/CAN_Tool_Handbuch_Beispiel.pdf` ist ein mit Chromium erzeugtes Beispiel; die Qt-Ausgabe sieht im Detail etwas anders aus (u. a. ohne Seitenzahlen).

## Bekannte Grenzen
* DBC: kein Extended Multiplexing, keine Umgebungsvariablen. CAN-FD-Botschaften ueber VFrameFormat oder DLC > 8.
* CDD: vollstaendig nur das einfache `<DIAGDATA>`-XML (siehe projects/demo/cdd/diag.cdd). Echte CANdelaStudio-CDDs werden
  nur heuristisch gelesen -> `cache/*.cdd.json` pruefen/ergaenzen (Services, Request-Bytes, Antwortparameter, Adressen).
* PEAK + CAN FD: festes Bit-Timing (80 MHz, 500k/2M); andere Werte unter `hardware.extra` in project.json.
* Zyklisches Senden: Zeitgenauigkeit im Bereich weniger ms (Software-Timer), nicht hardware-getriggert.

## Logging: aufgeloeste Signale und Graph-Modi

Ist eine DBC (CAN), A2L (XCP) oder CDD (UDS) eingebunden, schreibt die Parquet-Datei die Signale **aufgeloest**: `timestamp, direction, message, signal, value` (physikalisch) plus `source` (CAN/XCP/UDS), `can_id`, `raw` (Rohwert), `raw_hex` (Bitmuster, z. B. `0x1F40`), `text` (Wertetabelle), `unit` und `message_hex` (Frame als Hex). XCP-Werte (gelesen in der Rubrik XCP) und UDS-Antwortparameter (Rubrik Diagnose) werden waehrend des Loggings mitgeschrieben. Die Rohdatei enthaelt zusaetzlich die Spalte `signals` (z. B. `RPM=0x1F40/2000 rpm; Gear=0x3/3 [D]`). Der **Graph** hat einen Schalter *1 Graph / n Graphen* (je Signal ein Graph, gemeinsame Zeitachse); die Auswahl ist nach Botschaft gruppiert (Haken an der Botschaft waehlt alle Signale). Eine Normieren-Checkbox gibt es nicht mehr. Die Hilfe (Kapitel 3.5) beschreibt beide Darstellungen mit Screenshots in allen 6 Sprachen. (Parquet mangels pyarrow nur per Stub, GUI nicht getestet.)

## Logging: Rohdaten

Der Haken *Rohdaten (Hex)* schreibt zusaetzlich `<Name>_raw.parquet` mit **allen** Frames des Busses (auch ohne DBC, inkl. Fehlerframes, unabhaengig von der Signalauswahl). Spalten: `timestamp, direction, channel, can_id, extended, fd, brs, error, dlc, data (binary), data_hex, message`. Das Feld *Rohdaten-IDs* begrenzt die Frames (hex, z. B. `100, 7E0-7EF`; leer = alle). Mit aktivem Haken ist Logging auch ohne Signalauswahl moeglich. Die Parquet-Dateien selbst konnten hier mangels pyarrow nicht erzeugt werden (Logik per Stub getestet).

## PDF-Handbuch

Der PDF-Druck verwendet schmale Raender (8 mm, `manual_builder.PDF_MARGIN_MM`) und entsprechend breitere Abbildungen.

## Re-Init HW

Im Bereich *Projekt* schliesst der Button **Re-Init HW** (neben *Verbinden*) die Schnittstelle und oeffnet sie mit denselben Einstellungen neu - hilfreich, wenn die Hardware haengt. Die Sendeliste bleibt erhalten, ein laufendes Logging wird beendet; eine nicht reagierende Treiber-Schliessung wird nach 3 s abgebrochen.

## Rohframes senden (Trace und Testsequenz)

- **Trace**: Unter der Optionsleiste steht eine Sende-Zeile mit ID (hex, Standard 100), DLC (8), Daten (`00 01 02 03 04 05 06 07`), Zyklus (1000 ms), Haken *Zyklisch* und Button *Senden*. Ohne Haken wird einmal gesendet, mit Haken zyklisch bis zum erneuten Klick (*Stopp*). IDs ueber 7FF gehen als Extended-Frame, DLC ueber 8 als CAN FD.
- **Testsequenz**: Befehl `send_raw`, z. B. `{"type": "send_raw", "id": "0x100", "data": "00 01 02 03 04 05 06 07", "dlc": 8, "cyclic": true, "cycle_ms": 1000}`. `cyclic: false` sendet einmal; `cyclic: true, enabled: false` beendet das zyklische Senden dieser ID.


## Trace: selbst definierte Sendebotschaften

Der Button *Selbst definiert* in der Sende-Zeile oeffnet ein Fenster mit beliebig vielen Sendebotschaften (ID hex, DLC, Daten hex, Zyklus [ms], zyklisch). Rechtsklick: Hinzufuegen, Aendern, Loeschen, Senden/Stopp. Die Liste steht im Projekt (`custom_tx`). GUI nicht getestet.

## GUI Builder

Neue Rubrik *GUI Builder*: eigene Anzeige aus **LED** (leuchtet ueber einstellbarer Schwelle), **Textfeld** (rein oder mit Signalwert/Einheit) und **Zeiger** (180-Grad-Skala, automatische Skala mit runden Grenzen oder per Rechtsklick manuell min/max) fuer ausgewaehlte Signale; nur Ansicht, keine Werteeingabe. Elemente per Maus verschieben/skalieren (Haken *Bearbeiten*), Rechtsklick fuer Einstellungen. Speicherung im Projekt (`builder`), Logik in `builder_logic.py` (getestet), Oberflaeche ungetestet. Hilfe Kapitel 3.11.

Hilfe/Handbuch: Kapitel 3.3 (Trace) beschreibt jetzt das Multi-Senden eigener Botschaften Schritt fuer Schritt (Fenster- und Dialog-Screenshot), Kapitel 3.11 (GUI Builder) Schnellstart, Signalverknuepfung, LED/Textfeld-Einstellungen und Zeigerskala (automatisch/manuell) mit drei Screenshots, jeweils in allen 6 Sprachen.

## GUI Builder: Erweiterungen

Neue Elemente **Hintergrund** (Farbwahl, Ebene zwischen Standardhintergrund und Funktionselementen) und **Bild** (Bildauswahl, automatische Skalierung auf den Rahmen unter Beibehaltung des Seitenverhaeltnisses; Bild wird nach `images/` im Projekt kopiert). **LED**: digital (Wert 0/1, je Farbe) oder analog (Farbe fuer kleiner/gleich/groesser x). **Zeiger**: Ueberschrift ein/aus und Bereichsanzeige (Skala faerbt sich kleiner/gleich/groesser x). Alte Projekte (LED mit Schwelle) werden automatisch uebernommen. Hilfe Kapitel 3.11 mit neuem Screenshot.

Aenderung: Die **Bereichsanzeige** des Zeigers hat jetzt drei Bereiche (Bereich 1 bis 3), je mit Minimum, Maximum, Farbe und Ein/Aus nach der Regel `min <= x < max` (Rechtsklick auf den Zeiger, Haken *Bereichsanzeige*, dann *Bereich 1...3*). Teile ausserhalb der Skala werden abgeschnitten; min muss kleiner als max sein.

Aenderung: Die **LED** verwendet dieselbe Bereichseinstellung wie der Zeiger: Modus *digital* (Wert 0/1) oder *Bereiche* (Bereich 1 bis 3 mit je Ein/Aus, min, max, Farbe nach `min <= x < max`; erster passender Bereich bestimmt die Farbe, sonst grau). Der bisherige analoge Modus (kleiner/gleich/groesser x) entfaellt; vorhandene Projekte werden in Bereiche umgesetzt.

Aenderung: Der **Zeiger** kann optional einen zweiten (blau, Minimum) und dritten Zeiger (orange, Maximum) zeigen: Rechtsklick > *Minimum-Zeiger* / *Maximum-Zeiger* (Werte seit Beobachtungsbeginn, *Min/Max zuruecksetzen*; Signalwechsel setzt zurueck; nur die Haken werden im Projekt gespeichert).
