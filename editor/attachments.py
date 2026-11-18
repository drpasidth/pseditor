"""Turn chat attachments into something a model can read.

Images are resized and sent as image blocks (vision). PDF, Excel and Word files are
turned into text that is inserted into the user message. The original bytes are not
written into the workspace.
"""
import base64
import importlib
import io
import re
import zipfile
from pathlib import Path

from .fsops import FsError

MAX_FILES = 4
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_UNZIPPED = 30 * 1024 * 1024
MAX_IMAGE_SIDE = 1568
MAX_IMAGE_OUT = 4 * 1024 * 1024
PER_FILE_CHARS = 60_000
TOTAL_CHARS = 120_000
MAX_SHEETS = 8
MAX_ROWS = 200
MAX_COLS = 30
MAX_PDF_PAGES = 40

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

_EXT_KIND = {
    "png": "image", "jpg": "image", "jpeg": "image", "gif": "image", "webp": "image", "bmp": "image",
    "pdf": "pdf",
    "xlsx": "excel", "xlsm": "excel", "xls": "excel",
    "csv": "csv", "tsv": "csv",
    "docx": "word", "doc": "word",
}
_MIME_KIND = {
    "image/png": "image", "image/jpeg": "image", "image/jpg": "image", "image/gif": "image",
    "image/webp": "image", "image/bmp": "image",
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "excel",
    "application/vnd.ms-excel": "excel",
    "application/vnd.ms-excel.sheet.macroenabled.12": "excel",
    "text/csv": "csv",
    "text/tab-separated-values": "csv",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "word",
    "application/msword": "word",
}
_KIND_LABEL = {"image": "image", "pdf": "PDF", "excel": "Excel", "csv": "CSV", "word": "Word"}
_UNSUPPORTED = (
    "Attach an image (png, jpg, gif, webp, bmp), a PDF, an Excel file (xlsx, xlsm, xls), "
    "a CSV file (csv, tsv), or a Word file (docx, doc)."
)


class Prepared:
    """Result of preparing one chat turn's attachments."""

    def __init__(self, text, images, meta):
        self.text = text        # extracted document text, wrapped in <attachments>
        self.images = images    # [{"name","mime","data"}] base64, no data: prefix
        self.meta = meta        # [{"name","mime","kind"}] safe to show in the UI

    def __bool__(self):
        return bool(self.meta)


def prepare_attachments(items, analytics=False):
    """Validate and read attachments. Raises FsError with a message for the user.

    In analytics mode, CSV and Excel attachments are profiled (totals and a short
    preview) instead of dumping the sheet, and PDF/Word text gets labeled fields.
    """
    if not items:
        return Prepared("", [], [])
    if not isinstance(items, list):
        raise FsError("attachments must be a list.")
    if len(items) > MAX_FILES:
        raise FsError(f"You can attach at most {MAX_FILES} files at once.")

    blocks, images, meta = [], [], []
    total = 0
    for item in items:
        name, kind, raw = _decode(item)
        total += len(raw)
        if total > MAX_TOTAL_BYTES:
            raise FsError(f"Attachments are larger than {MAX_TOTAL_BYTES // (1024 * 1024)} MB in total.")
        if kind == "image":
            mime, b64 = _read_image(name, raw)
            images.append({"name": name, "mime": mime, "data": b64})
            meta.append({"name": name, "mime": mime, "kind": "image"})
            blocks.append(f"### {name} (image)\nThe user attached this image. It is included for you to look at.")
        elif kind == "pdf":
            text = _document_text(_clip(_read_pdf(name, raw), name), analytics)
            meta.append({"name": name, "mime": "application/pdf", "kind": "pdf"})
            blocks.append(f"### {name} (PDF)\n{text}")
        elif kind == "excel":
            text = _tabular_text(name, raw, "excel", analytics)
            meta.append({"name": name, "mime": "application/vnd.ms-excel", "kind": "excel"})
            blocks.append(f"### {name} (Excel)\n{text}")
        elif kind == "csv":
            text = _tabular_text(name, raw, "csv", analytics)
            meta.append({"name": name, "mime": "text/csv", "kind": "csv"})
            blocks.append(f"### {name} (CSV)\n{text}")
        else:
            text = _document_text(_clip(_read_word(name, raw), name), analytics)
            meta.append({"name": name, "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                         "kind": "word"})
            blocks.append(f"### {name} (Word)\n{text}")

    body = (
        "<attachments>\n"
        "The user attached these files. Use them when answering.\n\n"
        + "\n\n".join(blocks)
        + "\n</attachments>"
    )
    if len(body) > TOTAL_CHARS:
        body = body[:TOTAL_CHARS] + "\n\n…(further attachment text omitted)\n</attachments>"
    return Prepared(body, images, meta)


def _tabular_text(name, raw, kind, analytics):
    if analytics:
        from .analytics import summarize_attachment
        return summarize_attachment(name, raw)
    if kind == "excel":
        return _clip(_read_excel(name, raw), name)
    return _clip(_decode_text(raw), name)


def _document_text(text, analytics):
    if not analytics or not text:
        return text
    from .analytics import field_note
    note = field_note(text)
    return (note + "\n\n" + text).strip() if note else text


def _decode_text(raw):
    for enc in ("utf-8-sig", "utf-8", "cp874", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# decoding
# ---------------------------------------------------------------------------
def _decode(item):
    if not isinstance(item, dict):
        raise FsError("Each attachment must be an object with name and data.")
    name = _clean_name(item.get("name"))
    mime = str(item.get("mime") or "").split(";")[0].strip().lower()
    kind = _EXT_KIND.get(Path(name).suffix.lower().lstrip(".")) or _MIME_KIND.get(mime)
    if not kind:
        raise FsError(f"'{name}' is not a supported attachment. {_UNSUPPORTED}")
    raw = _b64(item.get("data"), name)
    if len(raw) > MAX_FILE_BYTES:
        raise FsError(f"'{name}' is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")
    if not raw:
        raise FsError(f"'{name}' is empty.")
    return name, kind, raw


def _clean_name(raw):
    name = Path(str(raw or "file")).name
    name = re.sub(r"[\x00-\x1f]+", "", name).strip()
    return (name or "file")[:180]


def _b64(value, name):
    if not isinstance(value, str) or not value.strip():
        raise FsError(f"'{name}' has no file data.")
    data = value.strip()
    if data.startswith("data:"):
        comma = data.find(",")
        if comma < 0:
            raise FsError(f"'{name}' is not valid base64.")
        data = data[comma + 1:]
    try:
        return base64.b64decode(data, validate=False)
    except Exception:
        raise FsError(f"'{name}' is not valid base64.")


def _import(module, package):
    try:
        return importlib.import_module(module)
    except ImportError:
        raise FsError(f"Reading this file needs the '{package}' package. Install it with: pip install {package}")


def _clip(text, name):
    text = (text or "").strip()
    if len(text) <= PER_FILE_CHARS:
        return text or f"(No text could be read from {name}.)"
    return text[:PER_FILE_CHARS] + f"\n\n…(truncated; {name} is longer than this)"


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).replace("\r", " ").replace("\n", " ").replace("|", "\\|").strip()


def _markdown(rows):
    """rows is a list of lists of strings. Returns a markdown table, or '' if everything is blank."""
    width = 0
    for row in rows:
        for i, cell in enumerate(row):
            if cell:
                width = max(width, i + 1)
    if not width:
        return ""
    kept = []
    for row in rows:
        padded = (row + [""] * width)[:width]
        if any(c.strip() for c in padded):
            kept.append(padded)
    if not kept:
        return ""

    def line(row):
        return "| " + " | ".join(row) + " |"

    out = [line(kept[0]), "| " + " | ".join("---" for _ in range(width)) + " |"]
    out.extend(line(r) for r in kept[1:])
    return "\n".join(out)


def _guard_zip(name, data):
    buf = io.BytesIO(data)
    if not zipfile.is_zipfile(buf):
        raise FsError(f"'{name}' is not a valid Office file.")
    buf.seek(0)
    total = 0
    with zipfile.ZipFile(buf) as zf:
        for info in zf.infolist():
            total += info.file_size
            if total > MAX_UNZIPPED:
                raise FsError(f"'{name}' expands to more than {MAX_UNZIPPED // (1024 * 1024)} MB, so it was not opened.")


# ---------------------------------------------------------------------------
# images
# ---------------------------------------------------------------------------
def _read_image(name, data):
    image_mod = _import("PIL.Image", "pillow")
    image_mod.MAX_IMAGE_PIXELS = 40_000_000
    try:
        im = image_mod.open(io.BytesIO(data))
        im.load()
    except image_mod.DecompressionBombError:
        raise FsError(f"'{name}' is too large an image to open.")
    except Exception as exc:
        raise FsError(f"'{name}' is not a readable image ({exc}).")
    if getattr(im, "n_frames", 1) > 1:
        im.seek(0)
    im.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
    has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
    if has_alpha:
        im = im.convert("RGBA")
        png = io.BytesIO()
        im.save(png, format="PNG", optimize=True)
        if png.tell() <= MAX_IMAGE_OUT:
            return "image/png", base64.b64encode(png.getvalue()).decode("ascii")
        bg = image_mod.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.getchannel("A"))
        im = bg
    elif im.mode != "RGB":
        im = im.convert("RGB")
    for quality, side in ((85, MAX_IMAGE_SIDE), (70, MAX_IMAGE_SIDE), (60, 1024)):
        frame = im
        if side < MAX_IMAGE_SIDE:
            frame = im.copy()
            frame.thumbnail((side, side))
        buf = io.BytesIO()
        frame.save(buf, format="JPEG", quality=quality, optimize=True)
        if buf.tell() <= MAX_IMAGE_OUT:
            return "image/jpeg", base64.b64encode(buf.getvalue()).decode("ascii")
    raise FsError(f"'{name}' is still too large after compressing it.")


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def _read_pdf(name, data):
    pypdf = _import("pypdf", "pypdf")
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                opened = reader.decrypt("")
            except Exception:
                opened = 0
            if not opened:
                raise FsError(f"'{name}' is password-protected. Remove the password and attach it again.")
        pages = []
        total = len(reader.pages)
        for i, page in enumerate(reader.pages[:MAX_PDF_PAGES]):
            text = (page.extract_text() or "").strip()
            if text:
                pages.append(f"## Page {i + 1}\n{text}")
    except FsError:
        raise
    except Exception as exc:
        raise FsError(f"Could not read '{name}': {exc}")
    if not pages:
        return "(No extractable text. If this PDF is a scan, attach pictures of the pages instead.)"
    body = "\n\n".join(pages)
    if total > MAX_PDF_PAGES:
        body += f"\n\n(Further pages omitted; {total - MAX_PDF_PAGES} more.)"
    return body


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------
def _read_excel(name, data):
    ext = Path(name).suffix.lower()
    if ext == ".xls":
        return _read_xls(name, data)
    if ext in (".xlsx", ".xlsm") or zipfile.is_zipfile(io.BytesIO(data)):
        _guard_zip(name, data)
        return _read_xlsx(name, data)
    return _read_xls(name, data)


def _read_xlsx(name, data):
    openpyxl = _import("openpyxl", "openpyxl")
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception as exc:
        raise FsError(f"Could not read '{name}': {exc}")
    parts = []
    try:
        sheets = wb.worksheets
        for i, ws in enumerate(sheets):
            if i >= MAX_SHEETS:
                parts.append(f"(Further sheets omitted; {len(sheets) - MAX_SHEETS} more.)")
                break
            rows, truncated = [], False
            for r_i, row in enumerate(ws.iter_rows(values_only=True)):
                if r_i >= MAX_ROWS:
                    truncated = True
                    break
                rows.append([_cell(c) for c in list(row)[:MAX_COLS]])
            table = _markdown(rows)
            block = f"## Sheet: {ws.title}\n{table or '(empty)'}"
            if truncated:
                block += "\n…(further rows omitted)"
            parts.append(block)
    finally:
        wb.close()
    return "\n\n".join(parts) or "(The workbook has no sheets.)"


def _read_xls(name, data):
    xlrd = _import("xlrd", "xlrd")
    try:
        book = xlrd.open_workbook(file_contents=data)
    except Exception as exc:
        raise FsError(f"Could not read '{name}': {exc}")
    parts = []
    for i in range(min(book.nsheets, MAX_SHEETS)):
        sheet = book.sheet_by_index(i)
        rows = []
        limit = min(sheet.nrows, MAX_ROWS)
        for r in range(limit):
            vals = []
            for c in range(min(sheet.ncols, MAX_COLS)):
                cell = sheet.cell(r, c)
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
                vals.append(_cell(value))
            rows.append(vals)
        table = _markdown(rows)
        block = f"## Sheet: {sheet.name}\n{table or '(empty)'}"
        if sheet.nrows > MAX_ROWS:
            block += "\n…(further rows omitted)"
        parts.append(block)
    if book.nsheets > MAX_SHEETS:
        parts.append(f"(Further sheets omitted; {book.nsheets - MAX_SHEETS} more.)")
    return "\n\n".join(parts) or "(The workbook has no sheets.)"


# ---------------------------------------------------------------------------
# Word
# ---------------------------------------------------------------------------
def _read_word(name, data):
    if zipfile.is_zipfile(io.BytesIO(data)):
        _guard_zip(name, data)
        return _read_docx(name, data)
    return _read_doc(name, data)


def _read_docx(name, data):
    docx = _import("docx", "python-docx")
    try:
        doc = docx.Document(io.BytesIO(data))
    except Exception as exc:
        raise FsError(f"Could not read '{name}': {exc}")
    parts = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        rows = []
        for row in table.rows:
            rows.append([_cell(cell.text) for cell in row.cells[:MAX_COLS]])
            if len(rows) >= MAX_ROWS:
                break
        table_md = _markdown(rows)
        if table_md:
            parts.append(table_md)
    return "\n\n".join(parts) or "(The document has no text.)"


def _read_doc(name, data):
    """Best-effort text pull from a legacy .doc (OLE) file. .docx is much more reliable."""
    if data[:8] != OLE_MAGIC:
        raise FsError(f"'{name}' is not a Word document. Save it as .docx and attach that.")
    runs = []
    seen = set()
    for align in (0, 1):
        blob = data[align:]
        if len(blob) % 2:
            blob = blob[:-1]
        text = blob.decode("utf-16le", errors="ignore")
        for match in re.finditer(r"[^\x00-\x08\x0b\x0c\x0e-\x1f]{12,}", text):
            piece = match.group().strip()
            letters = sum(ch.isalpha() for ch in piece)
            if letters < 6 or piece in seen:
                continue
            seen.add(piece)
            runs.append(piece)
            if sum(len(r) for r in runs) > PER_FILE_CHARS:
                break
    if not runs:
        return "(No text could be read from this .doc file. Save it as .docx and attach that.)"
    return "\n".join(runs)
