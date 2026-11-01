"""trace_logic.py - Qt-freie Logik der Trace-Tabelle im Aktualisierungsmodus.

Im Aktualisierungsmodus steht jede Botschaft (Richtung, Kanal, ID, Typ) nur **einmal** in der Tabelle: beim ersten
Auftreten wird eine Zeile angelegt, bei jedem weiteren Auftreten werden Zeitstempel, Daten und Signale dieser Zeile
aktualisiert und ein Zaehler erhoeht. Mit diff=True zeigt die Zeitspalte statt der absoluten Zeit
den Abstand zum vorigen Auftreten (Zykluszeit). Zeilenform: (Zeit, Richtung, Kanal, ID, Typ, Len, Daten, Botschaft, Signale,
Anzahl, Art).
"""
from __future__ import annotations

COUNT_COL = 9           # Anzeigespalte "Anzahl"
KIND_COL = 10           # Art ("rx" | "tx" | "error" | "isotp"), nicht angezeigt


def row_key(row: tuple) -> tuple:
    return (row[1], row[2], row[3], row[4])


class UpdateTable:
    def __init__(self, max_rows: int = 5000, diff: bool = False) -> None:
        self.rows: list[tuple] = []
        self.index: dict[tuple, int] = {}
        self.max_rows = max_rows
        self.diff = diff                      # True: Zeitspalte = Abstand zum vorigen Auftreten dieser Zeile
        self._last: dict[tuple, float] = {}

    def clear(self) -> None:
        self.rows.clear()
        self.index.clear()
        self._last.clear()

    def _time_text(self, key: tuple, row: tuple) -> str:
        """Zeitspalte: absolut oder (Diffzeit) Abstand zum vorigen Auftreten; beim ersten Auftreten 0."""
        if not self.diff:
            return row[0]
        now = float(row[0])
        prev = self._last.get(key)
        self._last[key] = now
        return f"{(0.0 if prev is None else now - prev):11.4f}"

    def apply(self, new_rows: list[tuple]) -> tuple[set[int], int]:
        """Zeilen einarbeiten. Rueckgabe: (Indizes aktualisierter Zeilen, Anzahl neu angehaengter Zeilen)."""
        changed: set[int] = set()
        added = 0
        for row in new_rows:
            key = row_key(row)
            i = self.index.get(key)
            if i is not None:
                count = int(self.rows[i][COUNT_COL] or 1) + 1
                row = (self._time_text(key, row),) + row[1:]
                self.rows[i] = row[:COUNT_COL] + (str(count),) + row[COUNT_COL + 1:]
                changed.add(i)
            elif len(self.rows) < self.max_rows:
                self.index[key] = len(self.rows)
                row = (self._time_text(key, row),) + row[1:]
                self.rows.append(row[:COUNT_COL] + ("1",) + row[COUNT_COL + 1:])
                added += 1
        return {i for i in changed if i < len(self.rows) - added}, added
