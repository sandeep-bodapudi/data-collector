"""Sheets stored in the database: read, import, save and Merge & Dedupe (PRD 5.7-5.10)."""
import csv
import io
import json
import os
import re
import tempfile
import uuid
from collections import OrderedDict

from openpyxl import load_workbook

from .excel import write_workbook

_CACHE: "OrderedDict[str, tuple[list[str], list[dict]]]" = OrderedDict()
_CACHE_SIZE = 8
MAX_IMPORT_ROWS = 200_000


def forget(sheet_id: str):
    _CACHE.pop(sheet_id, None)


def _xlsx_rows(data: bytes) -> tuple[list[str], list[dict]]:
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb["Data"] if "Data" in wb.sheetnames else wb.worksheets[0]
    it = ws.iter_rows(values_only=True)
    header = next(it, None) or []
    columns, seen = [], set()
    for i, h in enumerate(header):  # blank or repeated headers get a unique name
        name = str(h).strip() if h not in (None, "") else f"Column {i + 1}"
        while name in seen:
            name += " (2)"
        seen.add(name)
        columns.append(name)
    rows = []
    for values in it:
        if values is None or all(v in (None, "") for v in values):
            continue
        rows.append({c: ("" if v is None else v) for c, v in zip(columns, values)})
        if len(rows) >= MAX_IMPORT_ROWS:
            break
    wb.close()
    return columns, rows


def read(sheet) -> tuple[list[str], list[dict]]:
    if sheet.id in _CACHE:
        _CACHE.move_to_end(sheet.id)
        return _CACHE[sheet.id]
    columns, rows = _xlsx_rows(sheet.file_data)
    _CACHE[sheet.id] = (columns, rows)
    while len(_CACHE) > _CACHE_SIZE:
        _CACHE.popitem(last=False)
    return columns, rows


def parse_upload(filename: str, data: bytes) -> tuple[list[str], list[dict]]:
    ext = os.path.splitext(filename.lower())[1]
    if ext in (".xlsx", ".xlsm"):
        try:
            columns, rows = _xlsx_rows(data)
        except Exception:
            raise ValueError("This Excel file could not be read. Save it as .xlsx and try again.")
    elif ext in (".csv", ".tsv", ".txt"):
        text = data.decode("utf-8-sig", errors="replace")
        dialect = "excel-tab" if ext == ".tsv" or text.split("\n", 1)[0].count("\t") > text.split("\n", 1)[0].count(",") else "excel"
        reader = csv.reader(io.StringIO(text), dialect=dialect)
        header = next(reader, [])
        columns = [h.strip() or f"Column {i + 1}" for i, h in enumerate(header)]
        rows = [dict(zip(columns, r)) for r in reader if any(c.strip() for c in r)][:MAX_IMPORT_ROWS]
    elif ext == ".json":
        try:
            items = json.loads(data.decode("utf-8-sig"))
        except ValueError:
            raise ValueError("This JSON file is not valid.")
        if isinstance(items, dict):
            items = next((v for v in items.values() if isinstance(v, list)), [items])
        rows = [i for i in items if isinstance(i, dict)][:MAX_IMPORT_ROWS]
        columns = list(dict.fromkeys(k for r in rows for k in r))
        rows = [{c: (json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v) for c, v in r.items()} for r in rows]
    else:
        raise ValueError("Use an .xlsx, .csv, .tsv or .json file.")
    if not columns or not rows:
        raise ValueError("The file has no rows to import.")
    return columns, rows


def save(name: str, columns: list[str], rows: list[dict], owner_id: int, source: str, info: dict | None = None, run_id=None):
    from models import Sheet, db
    fd, path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    try:
        write_workbook(path, columns, rows, info or {})
        with open(path, "rb") as f:
            data = f.read()
    finally:
        os.remove(path)
    s = Sheet(id=str(uuid.uuid4()), name=name, source=source, run_id=run_id, row_count=len(rows),
              columns_json=json.dumps(columns), file_data=data, owner_id=owner_id)
    db.session.add(s)
    db.session.commit()
    return s


# ---------------------------------------------------------------- Merge & Dedupe

def _norm(value, column: str, match: dict) -> str:
    v = "" if value is None else str(value)
    if match.get("trim", True):
        v = re.sub(r"\s+", " ", v).strip()
    if match.get("ignore_case", True):
        v = v.lower()
    if match.get("smart", True):
        col = column.lower()
        if "phone" in col or "mobile" in col:
            v = ";".join(sorted({re.sub(r"\D", "", p)[-10:] for p in re.split(r"[;,/]", v) if re.sub(r"\D", "", p)}))
        elif "url" in col or "website" in col or "link" in col or "domain" in col:
            v = re.sub(r"^https?://(www\.)?", "", v).rstrip("/")
        elif "email" in col:
            v = ";".join(sorted({e.strip() for e in re.split(r"[;,\s]+", v) if e.strip()}))
    if match.get("ignore_punct"):
        v = re.sub(r"[^\w;]+", "", v)
    return v


def _filled(row: dict) -> int:
    return sum(1 for v in row.values() if str(v).strip() not in ("", "None"))


class MergeResult:
    def __init__(self, columns, rows, removed_rows, rows_in, reasons):
        self.columns, self.rows, self.removed_rows, self.rows_in, self.reasons = columns, rows, removed_rows, rows_in, reasons

    @property
    def removed(self):
        return len(self.removed_rows)

    def summary(self):
        return {"rows_in": self.rows_in, "removed": self.removed, "rows_out": len(self.rows),
                "columns": self.columns,
                "sample_removed": [{"row": r, "why": why} for r, why in zip(self.removed_rows[:15], self.reasons[:15])]}


def merge(chosen_sheets, keys: list[str], match: dict, keep: str = "first", fill_empty: bool = False) -> MergeResult:
    columns, all_rows = [], []
    for s in chosen_sheets:
        cols, rows = read(s)
        for c in cols:
            if c not in columns:
                columns.append(c)
        label = os.path.splitext(s.name)[0]
        all_rows += [{**r, "Source Sheet": label} for r in rows]
    if len(chosen_sheets) > 1:
        columns.append("Source Sheet")
    missing = [k for k in keys if k not in columns]
    if missing:
        raise ValueError(f"Column not found: {', '.join(missing)}")
    if not keys:
        return MergeResult(columns, all_rows, [], len(all_rows), [])

    groups: "OrderedDict[tuple, list[int]]" = OrderedDict()
    unique_rows = []
    for i, row in enumerate(all_rows):
        key = tuple(_norm(row.get(k, ""), k, match) for k in keys)
        if not any(key):  # rows with no value in the key columns are never treated as duplicates
            unique_rows.append(i)
            continue
        groups.setdefault(key, []).append(i)

    kept, removed, reasons = {}, [], []
    for key, idxs in groups.items():
        if keep == "last":
            winner = idxs[-1]
        elif keep == "most_complete":
            winner = max(idxs, key=lambda i: (_filled(all_rows[i]), -i))
        else:
            winner = idxs[0]
        row = dict(all_rows[winner])
        for i in idxs:
            if i == winner:
                continue
            removed.append(all_rows[i])
            reasons.append(f"Same {', '.join(keys)} as a kept row")
            if fill_empty:
                for c, v in all_rows[i].items():
                    if str(row.get(c, "")).strip() == "" and str(v).strip() != "":
                        row[c] = v
        kept[winner] = row
    order = sorted(list(kept) + unique_rows)
    rows = [kept.get(i, all_rows[i]) for i in order]
    return MergeResult(columns, rows, removed, len(all_rows), reasons)
