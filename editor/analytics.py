"""Read-only analysis of workspace data.

CSV, Excel, and SQLite become a table that can be profiled and aggregated.
PDF, Word, and images (through the OCR endpoint) become labeled fields for
invoices, receipts, and financial statements. Nothing here writes to the source.
"""
import csv
import io
import re
import sqlite3
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

from .fsops import FsError

MAX_ROWS = 80_000
MAX_COLS = 64
PREVIEW_ROWS = 25
RESULT_ROWS = 200
MAX_TEXT_BYTES = 32 * 1024 * 1024
TOP_N = 8

TABULAR_EXT = {".csv", ".tsv"}
EXCEL_EXT = {".xlsx", ".xlsm", ".xls"}
DB_EXT = {".sqlite", ".sqlite3", ".db"}
DOC_EXT = {".pdf", ".docx", ".doc"}

_NUM_RE = re.compile(r"\(?-?\d{1,3}(?:,\d{3})+(?:\.\d+)?\)?|\(?-?\d+(?:\.\d+)?\)?")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")

# Longer aliases first so "grand total" wins over "total".
_INVOICE_FIELDS = [
    ("invoice_no", "Invoice no.", [
        r"(?:invoice|receipt|bill)\s*(?:no|number|#)[:\s#]*([A-Za-z0-9][A-Za-z0-9\-\/]{1,30})",
        r"(?:เลขที่(?:ใบกำกับ|ใบเสร็จ|ใบแจ้งหนี้)?)[:\s]*([A-Za-z0-9][\w\-\/]{1,30})",
    ]),
    ("date", "Date", [
        r"(?:invoice\s*date|receipt\s*date|date|วันที่)[:\s]*(\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{2,4}|\d{4}-\d{2}-\d{2})",
    ]),
    ("subtotal", "Subtotal", [
        r"(?:sub\s*total|subtotal|ยอดก่อนภาษี)[:\s]*([0-9][0-9,]*(?:\.\d+)?)",
    ]),
    ("tax", "Tax", [
        r"(?:vat|tax|ภาษีมูลค่าเพิ่ม|ภาษี)[:\s]*([0-9][0-9,]*(?:\.\d+)?)",
    ]),
    ("total", "Total", [
        r"(?:grand\s*total|amount\s*due|balance\s*due|total\s*amount|รวมทั้งสิ้น|ยอดสุทธิ|ยอดรวมทั้งสิ้น)[:\s]*([0-9][0-9,]*(?:\.\d+)?)",
        r"(?<!sub)(?<!sub\s)\btotal\b[:\s]*([0-9][0-9,]*(?:\.\d+)?)",
    ]),
]
_FINANCIAL_LINES = [
    ("revenue", "Revenue", ["total revenue", "net revenue", "revenue", "net sales", "sales", "รายได้รวม", "รายได้"]),
    ("cost_of_sales", "Cost of sales", ["cost of goods sold", "cost of sales", "cost of goods", "ต้นทุนขาย"]),
    ("gross_profit", "Gross profit", ["gross profit", "กำไรขั้นต้น"]),
    ("operating_income", "Operating income", ["operating income", "operating profit", "กำไรจากการดำเนินงาน"]),
    ("net_income", "Net income", ["net income", "net profit", "profit for the year", "กำไรสุทธิ"]),
    ("total_assets", "Total assets", ["total assets", "สินทรัพย์รวม", "รวมสินทรัพย์"]),
    ("total_liabilities", "Total liabilities", ["total liabilities", "หนี้สินรวม", "รวมหนี้สิน"]),
    ("equity", "Equity", ["total equity", "shareholders' equity", "stockholders' equity", "ส่วนของผู้ถือหุ้น"]),
    ("cash", "Cash", ["cash and cash equivalents", "เงินสดและรายการเทียบเท่าเงินสด", "cash"]),
]
_INVOICE_MARKERS = ("invoice", "receipt", "tax invoice", "ใบเสร็จ", "ใบกำกับ", "ใบแจ้งหนี้")
_FINANCIAL_MARKERS = (
    "balance sheet", "income statement", "statement of financial", "profit or loss",
    "งบกำไร", "งบดุล", "งบแสดงฐานะ", "งบกำไรขาดทุน",
)


def source_kind(path):
    """tabular, database, document, image, or '' when this file is not a dataset."""
    ext = Path(str(path)).suffix.lower()
    if ext in TABULAR_EXT or ext in EXCEL_EXT:
        return "tabular"
    if ext in DB_EXT:
        return "database"
    if ext in DOC_EXT:
        return "document"
    from . import ocr
    if ext in ocr.IMAGE_SUFFIXES:
        return "image"
    return ""


def list_datasets(fs):
    groups = {"tabular": [], "database": [], "document": [], "image": []}
    for rel in fs.list_files(limit=4000):
        kind = source_kind(rel)
        if kind:
            groups[kind].append(rel)
    return {"datasets": groups}


def list_datasets_text(fs):
    groups = list_datasets(fs)["datasets"]
    if not any(groups.values()):
        return "No CSV, Excel, SQLite, PDF, Word, or image files in this workspace."
    labels = [
        ("tabular", "Tables (CSV, Excel)"),
        ("database", "SQLite"),
        ("document", "Documents (PDF, Word)"),
        ("image", "Images"),
    ]
    lines = []
    for key, title in labels:
        if groups[key]:
            lines.append(title + ":")
            lines.extend("  " + p for p in groups[key][:80])
    return "\n".join(lines)


def profile(fs, path, sheet=""):
    table = _load(fs, path, sheet)
    data = _profile_table(table)
    data["chart"] = _chart_from_profile(data)
    return data


def profile_text(fs, path, sheet=""):
    return _profile_text(profile(fs, path, sheet))


def query(fs, path, sheet="", filters=None, group_by=None, metrics=None, limit=50):
    table = _load(fs, path, sheet)
    rows = _apply_filters(table["rows"], table["columns"], filters or [])
    groups = _clean_names(group_by)
    aggs = _clean_metrics(metrics)
    limit = _clamp_limit(limit, 50)
    if groups or aggs:
        out_rows, columns = _aggregate(rows, groups, aggs)
        out_rows = out_rows[:limit]
    else:
        columns = table["columns"]
        out_rows = rows[:limit]
    title = _query_title(groups, aggs)
    return {
        "path": table["path"],
        "sheet": table["sheet"],
        "row_count": len(rows),
        "truncated": len(rows) > limit or table["truncated"],
        "columns": columns,
        "rows": out_rows,
        "chart": _chart_from_rows(columns, out_rows, title),
        "note": table["note"],
    }


def query_text(fs, path, sheet="", filters=None, group_by=None, metrics=None, limit=50):
    data = query(fs, path, sheet, filters, group_by, metrics, limit)
    lines = [f"File: {data['path']}", f"Matching rows: {data['row_count']}"]
    if data["sheet"]:
        lines.append(f"Sheet/table: {data['sheet']}")
    if data["note"]:
        lines.append(data["note"])
    lines.append(_markdown(data["columns"], data["rows"]))
    return "\n".join(lines)


def sql_query(fs, path, sql):
    kind = source_kind(path)
    if kind != "database":
        raise FsError("sql_query only reads a SQLite file (.sqlite, .sqlite3, .db).")
    statement = _safe_select(sql)
    file_path = fs.resolve(path)
    conn = _connect_ro(file_path)
    try:
        try:
            cur = conn.execute(statement)
        except sqlite3.Error as exc:
            raise FsError(f"SQLite error: {exc}")
        names = [d[0] for d in (cur.description or [])]
        fetched = cur.fetchmany(RESULT_ROWS + 1)
    finally:
        conn.close()
    truncated = len(fetched) > RESULT_ROWS
    rows = []
    for record in fetched[:RESULT_ROWS]:
        rows.append({names[i]: _json_value(record[i]) for i in range(len(names))})
    return {
        "path": path,
        "columns": names,
        "rows": rows,
        "truncated": truncated,
        "chart": _chart_from_rows(names, rows, "Query result"),
        "note": f"Showing {len(rows)} row(s)." + (" Further rows omitted." if truncated else ""),
    }


def sql_text(fs, path, sql):
    data = sql_query(fs, path, sql)
    return f"File: {data['path']}\n{data['note']}\n" + _markdown(data["columns"], data["rows"])


def extract_document(fs, path):
    kind = source_kind(path)
    if kind not in ("document", "image"):
        raise FsError("extract_document reads a PDF, Word file, or image.")
    file_path = fs.resolve(path)
    if file_path.stat().st_size > 20 * 1024 * 1024:
        raise FsError("File is larger than 20 MB.")
    raw = file_path.read_bytes()
    text = _document_text(file_path.name, raw)
    data = extract_fields(text, path)
    data["path"] = path
    return data


def extract_text(fs, path):
    data = extract_document(fs, path)
    return _fields_text(data)


def extract_fields(text, path=""):
    """Labeled fields from invoice, receipt, or financial-statement text."""
    body = text or ""
    kind = _document_kind(body)
    fields = []
    if kind in ("invoice", "document"):
        fields.extend(_invoice_fields(body))
    if kind in ("financial", "document"):
        fields.extend(_financial_fields(body))
    # Prefer the stronger match when both sets found something.
    if kind == "document":
        if any(f["group"] == "financial" for f in fields) and not any(f["group"] == "invoice" for f in fields):
            kind = "financial"
        elif any(f["group"] == "invoice" for f in fields):
            kind = "invoice"
    excerpt = body.strip()
    if len(excerpt) > 4000:
        excerpt = excerpt[:4000] + "\n…(excerpt truncated)"
    return {"path": path, "document_kind": kind, "fields": fields, "excerpt": excerpt}


def field_note(text):
    """Short block appended to an attachment so the model can cite real fields."""
    data = extract_fields(text)
    if not data["fields"]:
        return ""
    return _fields_text(data)


def summarize_attachment(name, raw):
    """Profile an uploaded CSV or Excel file that is not in the workspace yet."""
    ext = Path(name).suffix.lower()
    if ext in TABULAR_EXT or ext == ".tsv":
        table = _table_from_csv(name, raw, ext == ".tsv")
    elif ext in EXCEL_EXT:
        table = _table_from_excel(name, raw, "")
    else:
        raise FsError(f"'{name}' is not a CSV or Excel file.")
    return _profile_text(_profile_table(table))


def open_file_note(fs, path):
    """Grounding text for the file the user currently has open. Does not call OCR."""
    kind = source_kind(path)
    if not kind:
        return ""
    if kind in ("document", "image"):
        return (
            f"The open file {path} is a {kind}. "
            "Call extract_document on it before quoting any invoice, receipt, or financial-statement figure."
        )
    try:
        return "Open file profile (computed from the file; cite these figures):\n" + profile_text(fs, path)
    except FsError as exc:
        return f"Could not profile {path}: {exc}"


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------
def _load(fs, path, sheet):
    kind = source_kind(path)
    if kind not in ("tabular", "database"):
        raise FsError("Profile and query read a CSV, Excel, or SQLite file.")
    file_path = fs.resolve(path)
    if not file_path.is_file():
        raise FsError(f"File not found: {path}")
    ext = file_path.suffix.lower()
    if ext in DB_EXT:
        return _table_from_sqlite(path, file_path, sheet)
    if file_path.stat().st_size > MAX_TEXT_BYTES and ext in TABULAR_EXT:
        raw = file_path.read_bytes()[:MAX_TEXT_BYTES]
        table = _table_from_csv(path, raw, ext == ".tsv")
        table["truncated"] = True
        table["note"] = (table["note"] + " " if table["note"] else "") + "File was cut at 32 MB."
        return table
    raw = file_path.read_bytes()
    if ext in TABULAR_EXT:
        return _table_from_csv(path, raw, ext == ".tsv")
    return _table_from_excel(path, raw, sheet)


def _table_from_csv(path, raw, force_tab):
    text = _decode(raw)
    sample = text[:4096]
    first = sample.splitlines()[0] if sample.splitlines() else ""
    if force_tab or first.count("\t") > first.count(","):
        delimiter = "\t"
    elif first.count(";") > first.count(","):
        delimiter = ";"
    else:
        delimiter = ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    grid = []
    truncated = False
    for i, row in enumerate(reader):
        if i >= MAX_ROWS + 1:
            truncated = True
            break
        grid.append(row[:MAX_COLS])
    table = _grid_to_table(path, "", grid)
    table["truncated"] = table["truncated"] or truncated
    if truncated:
        table["note"] = f"First {MAX_ROWS} rows."
    return table


def _table_from_excel(path, raw, sheet):
    ext = Path(path).suffix.lower()
    if ext == ".xls":
        return _table_from_xls(path, raw, sheet)
    from .attachments import _guard_zip
    if ext in (".xlsx", ".xlsm"):
        _guard_zip(Path(path).name, raw)
    return _xlsx_table(path, raw, sheet)


def _xlsx_table(path, raw, sheet):
    import openpyxl
    try:
        wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True, read_only=True)
    except Exception as exc:
        raise FsError(f"Could not read '{path}': {exc}")
    title, names, grid, truncated = "", [], [], False
    try:
        names = list(wb.sheetnames)
        if not names:
            raise FsError("The workbook has no sheets.")
        chosen = sheet.strip() if isinstance(sheet, str) else ""
        if chosen and chosen not in names:
            raise FsError(f"No sheet named '{chosen}'. Sheets: {', '.join(names)}")
        ws = wb[chosen] if chosen else wb[names[0]]
        title = ws.title
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= MAX_ROWS:
                truncated = True
                break
            grid.append([_json_value(c) for c in list(row)[:MAX_COLS]])
    finally:
        wb.close()
    table = _grid_to_table(path, title, grid)
    table["sheets"] = names
    table["truncated"] = truncated
    table["note"] = "Values stored in the workbook (formulas without a saved result are empty)."
    if truncated:
        table["note"] += f" First {MAX_ROWS} rows."
    return table


def _table_from_xls(path, raw, sheet):
    import xlrd
    try:
        book = xlrd.open_workbook(file_contents=raw)
    except Exception as exc:
        raise FsError(f"Could not read '{path}': {exc}")
    names = book.sheet_names()
    if not names:
        raise FsError("The workbook has no sheets.")
    chosen = sheet.strip() if isinstance(sheet, str) else ""
    if chosen and chosen not in names:
        raise FsError(f"No sheet named '{chosen}'. Sheets: {', '.join(names)}")
    ws = book.sheet_by_name(chosen) if chosen else book.sheet_by_index(0)
    grid = []
    limit = min(ws.nrows, MAX_ROWS)
    for r in range(limit):
        vals = []
        for c in range(min(ws.ncols, MAX_COLS)):
            cell = ws.cell(r, c)
            value = cell.value
            if cell.ctype == xlrd.XL_CELL_DATE:
                try:
                    value = xlrd.xldate_as_datetime(cell.value, book.datemode)
                except Exception:
                    value = cell.value
            elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                value = bool(cell.value)
            elif cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                value = None
            vals.append(_json_value(value))
        grid.append(vals)
    table = _grid_to_table(path, ws.name, grid)
    table["sheets"] = names
    table["truncated"] = ws.nrows > MAX_ROWS
    table["note"] = "Values stored in the workbook."
    if table["truncated"]:
        table["note"] += f" First {MAX_ROWS} rows."
    return table


def _table_from_sqlite(path, file_path, sheet):
    conn = _connect_ro(file_path)
    try:
        tables = _sqlite_tables(conn)
        if not tables:
            raise FsError("This database has no tables.")
        chosen = sheet.strip() if isinstance(sheet, str) else ""
        if chosen and chosen not in tables:
            raise FsError(f"No table named '{chosen}'. Tables: {', '.join(tables)}")
        table_name = chosen or tables[0]
        try:
            cur = conn.execute(f"SELECT * FROM {_quote(table_name)} LIMIT ?", (MAX_ROWS + 1,))
        except sqlite3.Error as exc:
            raise FsError(f"SQLite error: {exc}")
        names = [d[0] for d in cur.description]
        fetched = cur.fetchall()
    finally:
        conn.close()
    truncated = len(fetched) > MAX_ROWS
    records = fetched[:MAX_ROWS]
    columns = _unique_headers(names)
    rows = []
    for record in records:
        rows.append({columns[i]: _json_value(record[i]) for i in range(len(columns))})
    return {
        "path": path,
        "sheet": table_name,
        "sheets": tables,
        "columns": columns,
        "rows": rows,
        "truncated": truncated,
        "note": f"Table {table_name}." + (f" First {MAX_ROWS} rows." if truncated else ""),
    }


def _grid_to_table(path, sheet, grid):
    while grid and not any(_filled(c) for c in grid[-1]):
        grid.pop()
    if not grid:
        return {
            "path": path, "sheet": sheet, "sheets": [sheet] if sheet else [],
            "columns": [], "rows": [], "truncated": False, "note": "The sheet is empty.",
        }
    header, start = _header_row(grid[0])
    columns = _unique_headers(header)
    rows = []
    for raw in grid[start:]:
        if not any(_filled(c) for c in raw):
            continue
        item = {}
        for i, name in enumerate(columns):
            item[name] = _json_value(raw[i]) if i < len(raw) else None
        rows.append(item)
    return {
        "path": path, "sheet": sheet, "sheets": [sheet] if sheet else [],
        "columns": columns, "rows": rows, "truncated": False, "note": "",
    }


def _header_row(first):
    cells = [_json_value(c) for c in first]
    texts = [c for c in cells if isinstance(c, str) and c.strip()]
    numbers = [c for c in cells if isinstance(c, (int, float)) and not isinstance(c, bool)]
    if texts and len(texts) >= len(numbers):
        return [c if _filled(c) else "" for c in cells], 1
    return [""] * len(cells), 0


def _unique_headers(cells):
    used = set()
    names = []
    for i, cell in enumerate(cells):
        base = str(cell).strip().replace("\n", " ")[:80] if _filled(cell) else f"column_{i + 1}"
        base = base or f"column_{i + 1}"
        name, n = base, 2
        while name in used:
            name = f"{base}_{n}"
            n += 1
        used.add(name)
        names.append(name)
    return names or ["column_1"]


def _connect_ro(file_path):
    # Keep the drive colon so SQLite can open Windows paths in read-only mode.
    uri = "file:" + quote(file_path.resolve().as_posix(), safe="/:") + "?mode=ro"
    try:
        return sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise FsError(f"Could not open database: {exc}")


def _sqlite_tables(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def _quote(name):
    return '"' + str(name).replace('"', '""') + '"'


def _safe_select(sql):
    text = (sql or "").strip()
    if not text:
        raise FsError("SQL is empty.")
    body = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    body = re.sub(r"--[^\n]*", " ", body).strip()
    if body.endswith(";"):
        body = body[:-1].strip()
    if ";" in body:
        raise FsError("Only one SELECT statement is allowed.")
    head = body.lstrip("(").strip().lower()
    if not (head.startswith("select") or head.startswith("with")):
        raise FsError("Only a read-only SELECT is allowed.")
    if _writes(body):
        raise FsError("That statement is not read-only.")
    return body


def _writes(sql):
    stripped = re.sub(r"'([^']|'')*'", "''", sql)
    stripped = re.sub(r'"([^"]|"")*"', '""', stripped)
    words = set(re.findall(r"[A-Za-z_]+", stripped.lower()))
    blocked = {
        "insert", "update", "delete", "drop", "alter", "attach", "detach",
        "create", "vacuum", "reindex", "pragma", "grant", "revoke",
    }
    return bool(words & blocked)


# ---------------------------------------------------------------------------
# profile, filter, aggregate
# ---------------------------------------------------------------------------
def _profile_table(table):
    columns = []
    for name in table["columns"]:
        values = [row.get(name) for row in table["rows"]]
        columns.append(_column_stats(name, values))
    preview = table["rows"][:PREVIEW_ROWS]
    return {
        "path": table["path"],
        "source_kind": "database" if source_kind(table["path"]) == "database" else "tabular",
        "sheet": table["sheet"],
        "sheets": table["sheets"],
        "row_count": len(table["rows"]),
        "truncated": table["truncated"],
        "note": table["note"],
        "columns": columns,
        "preview_columns": table["columns"],
        "preview": preview,
    }


def _column_stats(name, values):
    filled = [v for v in values if _filled(v)]
    nulls = len(values) - len(filled)
    numbers = [_num(v) for v in filled]
    numeric = [n for n in numbers if n is not None]
    ratio = (len(numeric) / len(filled)) if filled else 0
    dates = sum(1 for v in filled if isinstance(v, str) and _DATE_RE.match(v.strip()))
    if filled and ratio >= 0.8:
        kind = "number"
    elif filled and dates / len(filled) >= 0.8:
        kind = "date"
    else:
        kind = "text"
    stats = {
        "name": name,
        "kind": kind,
        "non_null": len(filled),
        "nulls": nulls,
        "distinct": _distinct_count(filled),
    }
    if kind == "number" and numeric:
        stats["min"] = _round_num(min(numeric))
        stats["max"] = _round_num(max(numeric))
        stats["sum"] = _round_num(sum(numeric))
        stats["mean"] = _round_num(sum(numeric) / len(numeric))
    else:
        counts = Counter(str(v) for v in filled)
        stats["top"] = [{"value": v, "count": n} for v, n in counts.most_common(TOP_N)]
    return stats


def _distinct_count(values):
    seen = set()
    for value in values:
        seen.add(str(value))
        if len(seen) > 500:
            return "500+"
    return len(seen)


def _chart_from_profile(data):
    best = None
    for col in data["columns"]:
        top = col.get("top") or []
        if 2 <= len(top) <= 12 and (best is None or len(top) < len(best.get("top") or [])):
            # Prefer a compact category column over a near-unique text column.
            if isinstance(col["distinct"], int) and col["distinct"] <= 12:
                best = col
    if not best:
        return None
    return {
        "title": f"Rows by {best['name']}",
        "labels": [item["value"] for item in best["top"]],
        "values": [item["count"] for item in best["top"]],
    }


def _apply_filters(rows, columns, filters):
    if not isinstance(filters, list):
        raise FsError("filters must be a list.")
    out = rows
    for item in filters:
        if not isinstance(item, dict):
            raise FsError("Each filter needs column, op, and value.")
        column = str(item.get("column") or "")
        op = str(item.get("op") or "eq").lower()
        if column not in columns:
            raise FsError(f"No column named '{column}'.")
        if op not in ("eq", "ne", "contains", "gt", "gte", "lt", "lte"):
            raise FsError(f"Unknown filter op '{op}'.")
        value = item.get("value")
        out = [row for row in out if _match(row.get(column), op, value)]
    return out


def _match(cell, op, expected):
    if op == "contains":
        return str(expected).lower() in str(cell if cell is not None else "").lower()
    left, right = _num(cell), _num(expected)
    if left is not None and right is not None and op in ("eq", "ne", "gt", "gte", "lt", "lte"):
        if op == "eq":
            return left == right
        if op == "ne":
            return left != right
        if op == "gt":
            return left > right
        if op == "gte":
            return left >= right
        if op == "lt":
            return left < right
        return left <= right
    a = str(cell if cell is not None else "").casefold()
    b = str(expected if expected is not None else "").casefold()
    if op == "eq":
        return a == b
    if op == "ne":
        return a != b
    return False


def _clean_names(names):
    if not names:
        return []
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, list):
        raise FsError("group_by must be a list of column names.")
    return [str(n) for n in names if str(n).strip()]


def _clean_metrics(metrics):
    if not metrics:
        return []
    if not isinstance(metrics, list):
        raise FsError("metrics must be a list.")
    out = []
    for item in metrics:
        if isinstance(item, str):
            item = {"fn": item}
        if not isinstance(item, dict):
            raise FsError("Each metric needs fn and, except for count, column.")
        fn = str(item.get("fn") or "sum").lower()
        if fn == "mean":
            fn = "avg"
        if fn not in ("sum", "count", "avg", "min", "max"):
            raise FsError(f"Unknown metric '{fn}'.")
        out.append({"fn": fn, "column": str(item.get("column") or "")})
    return out


def _aggregate(rows, groups, metrics):
    if not metrics:
        metrics = [{"fn": "count", "column": ""}]
    buckets = {}
    for row in rows:
        key = tuple(row.get(name) for name in groups)
        buckets.setdefault(key, []).append(row)
    out = []
    for key, group_rows in buckets.items():
        item = {groups[i]: _json_value(key[i]) for i in range(len(groups))}
        for metric in metrics:
            label = metric["fn"] if not metric["column"] else f"{metric['fn']}_{metric['column']}"
            item[label] = _metric_value(group_rows, metric)
        out.append(item)
    value_key = None
    if out:
        for metric in metrics:
            label = metric["fn"] if not metric["column"] else f"{metric['fn']}_{metric['column']}"
            if metric["fn"] in ("sum", "count", "avg"):
                value_key = label
                break
    if value_key:
        out.sort(key=lambda r: _num(r.get(value_key)) or 0, reverse=True)
    columns = list(groups)
    if out:
        for key in out[0]:
            if key not in columns:
                columns.append(key)
    return out, columns


def _metric_value(rows, metric):
    if metric["fn"] == "count" and not metric["column"]:
        return len(rows)
    values = [_num(row.get(metric["column"])) for row in rows]
    values = [n for n in values if n is not None]
    if metric["fn"] == "count":
        return len(values)
    if not values:
        return None
    if metric["fn"] == "sum":
        return _round_num(sum(values))
    if metric["fn"] == "avg":
        return _round_num(sum(values) / len(values))
    if metric["fn"] == "min":
        return _round_num(min(values))
    return _round_num(max(values))


def _query_title(groups, metrics):
    if not metrics:
        return "Rows"
    metric = metrics[0]
    what = metric["fn"] if not metric["column"] else f"{metric['fn']} of {metric['column']}"
    if groups:
        return f"{what} by {', '.join(groups)}"
    return what


def _chart_from_rows(columns, rows, title):
    if len(columns) < 2 or not rows or len(rows) > 12:
        return None
    label_key, value_key = columns[0], columns[-1]
    labels, values = [], []
    for row in rows:
        number = _num(row.get(value_key))
        if number is None:
            return None
        label = row.get(label_key)
        labels.append("" if label is None else str(label))
        values.append(_round_num(number))
    return {"title": title, "labels": labels, "values": values}


def _profile_text(data):
    lines = [f"File: {data['path']}", f"Rows: {data['row_count']}"]
    if data.get("sheet"):
        lines.append(f"Sheet/table: {data['sheet']}")
    if data.get("sheets") and len(data["sheets"]) > 1:
        lines.append("Also available: " + ", ".join(str(s) for s in data["sheets"]))
    if data.get("note"):
        lines.append(data["note"])
    if data.get("truncated"):
        lines.append("The profile does not include every row.")
    lines.append("Columns:")
    for col in data["columns"]:
        bits = [f"- {col['name']} ({col['kind']})", f"nulls {col['nulls']}", f"distinct {col['distinct']}"]
        if col["kind"] == "number":
            bits.append(f"min {col.get('min')} max {col.get('max')} sum {col.get('sum')} mean {col.get('mean')}")
        elif col.get("top"):
            top = ", ".join(f"{item['value']} ({item['count']})" for item in col["top"][:5])
            bits.append("top " + top)
        lines.append(" ".join(bits))
    lines.append("Preview:")
    lines.append(_markdown(data["preview_columns"], data["preview"]))
    return "\n".join(lines)


def _markdown(columns, rows):
    if not columns:
        return "(no columns)"
    head = " | ".join(str(c) for c in columns)
    body = []
    for row in rows[:40]:
        body.append(" | ".join(_cell_text(row.get(c)) for c in columns))
    text = head + "\n" + "\n".join(body)
    if len(rows) > 40:
        text += f"\n…({len(rows) - 40} more rows)"
    return text


# ---------------------------------------------------------------------------
# documents
# ---------------------------------------------------------------------------
def _document_text(name, raw):
    from . import ocr
    from .attachments import _read_pdf, _read_word
    ext = Path(name).suffix.lower()
    if ext in ocr.IMAGE_SUFFIXES:
        if not ocr.endpoint():
            raise FsError("Set the OCR endpoint with the gear icon to read this image.")
        return ocr.recognize_bytes(raw, name).strip()
    if ext == ".pdf":
        return _read_pdf(name, raw)
    if ext in (".docx", ".doc"):
        return _read_word(name, raw)
    raise FsError("This file is not a PDF, Word document, or image.")


def _document_kind(text):
    low = text.lower()
    if any(marker in low for marker in _FINANCIAL_MARKERS):
        return "financial"
    if any(marker in low for marker in _INVOICE_MARKERS):
        return "invoice"
    return "document"


def _invoice_fields(text):
    fields = []
    for key, label, patterns in _INVOICE_FIELDS:
        match, line = _search_patterns(text, patterns)
        if not match:
            continue
        raw = match.group(1).strip()
        number = _num(raw) if key in ("subtotal", "tax", "total") else None
        fields.append({
            "name": key, "label": label, "value": _round_num(number) if number is not None else raw,
            "line": line[:180], "group": "invoice",
        })
    vendor = re.search(r"(?:vendor|seller|billed by|from|ผู้ขาย|ร้าน)[:\s]+(.{2,80})", text, re.I)
    if vendor:
        value = vendor.group(1).strip().splitlines()[0][:80]
        fields.insert(0, {
            "name": "vendor", "label": "Vendor", "value": value,
            "line": vendor.group(0)[:180], "group": "invoice",
        })
    return fields


def _financial_fields(text):
    fields = []
    seen = set()
    for key, label, aliases in _FINANCIAL_LINES:
        found = _line_amount(text, aliases)
        if not found or key in seen:
            continue
        seen.add(key)
        raw, line = found
        number = _num(raw)
        fields.append({
            "name": key, "label": label,
            "value": _round_num(number) if number is not None else raw,
            "line": line[:180], "group": "financial",
        })
    return fields


def _search_patterns(text, patterns):
    for line in text.splitlines():
        for pattern in patterns:
            match = re.search(pattern, line, re.I)
            if match:
                return match, line.strip()
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            line = text[max(0, match.start() - 40):match.end() + 40].splitlines()
            return match, " ".join(part.strip() for part in line if part.strip())[:180]
    return None, ""


def _line_amount(text, aliases):
    aliases = sorted(aliases, key=len, reverse=True)
    for line in text.splitlines():
        low = line.lower()
        for alias in aliases:
            idx = low.find(alias.lower())
            if idx < 0:
                continue
            if alias.lower() == "cash" and "cash flow" in low[idx:idx + 12]:
                continue
            if alias.lower() in ("sales", "revenue") and "cost of" in low:
                continue
            after = line[idx + len(alias):]
            nums = _NUM_RE.findall(after) or _NUM_RE.findall(line)
            if nums:
                return nums[0], line.strip()
    return None


def _fields_text(data):
    lines = [f"Document: {data.get('path') or '(attachment)'}", f"Kind: {data.get('document_kind') or 'document'}"]
    if not data.get("fields"):
        lines.append("No invoice, receipt, or financial-statement fields were found.")
    else:
        lines.append("Fields (quote these; do not invent others):")
        for field in data["fields"]:
            lines.append(f"- {field['label']}: {field['value']}  | source: {field['line']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# values
# ---------------------------------------------------------------------------
def _decode(raw):
    for enc in ("utf-8-sig", "utf-8", "cp874", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _filled(value):
    return value is not None and str(value).strip() != ""


def _num(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("฿", "").replace("$", "").replace("€", "").replace(",", "").replace(" ", "")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    try:
        return float(text)
    except ValueError:
        return None


def _round_num(value):
    if value is None:
        return None
    if abs(value - round(value)) < 1e-6:
        return int(round(value))
    return round(float(value), 4)


def _json_value(value):
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        return _round_num(value)
    if isinstance(value, int):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value if isinstance(value, str) else str(value)


def _cell_text(value):
    if value is None:
        return ""
    return str(value).replace("\n", " ")


def _clamp_limit(limit, default):
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, RESULT_ROWS))
