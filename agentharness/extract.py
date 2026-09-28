"""Document text extraction (plain text, HTML, DOCX, PDF, OCR) plus regex-driven tabulation.

Pipeline:  load_document -> normalize (OCR repair) -> fields / key-values / tables -> rows -> CSV/JSON
Only the standard library is required. OCR uses the `tesseract` CLI and PDFs use
`pdftotext`/`pdftoppm` (poppler) or `pypdf`, when installed; missing tools produce a
clear warning instead of silently empty output.
"""
from __future__ import annotations

import csv
import html
import io
import json
import re
import shutil
import subprocess
import tempfile
import unicodedata
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

TEXT_SUFFIXES = {".txt", ".md", ".csv", ".tsv", ".log", ".json", ".xml", ".yaml", ".yml", ".ini", ".cfg"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp", ".pbm", ".pgm", ".ppm"}

MONTHS = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
BUILTIN = {
    "text": r"[^\n]*\S",
    "word": r"\S+",
    "email": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    "url": r"https?://[^\s<>\"')\]]+",
    "phone": r"(?:\+?\d{1,3}[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}\b",
    "money": r"\(?-?[$€£¥]?\s?\d+(?:[,.]\d{3})*(?:[.,]\d{1,2})?(?!\d)\)?",
    "number": r"-?\d+(?:[.,]\d+)*",
    "int": r"-?\d+",
    "percent": r"-?\d+(?:[.,]\d+)?\s?%",
    "date": (rf"\d{{4}}-\d{{1,2}}-\d{{1,2}}|\d{{1,2}}[/.-]\d{{1,2}}[/.-]\d{{2,4}}"
             rf"|{MONTHS}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}"
             rf"|\d{{1,2}}(?:st|nd|rd|th)?\s+{MONTHS},?\s+\d{{4}}"),
    "time": r"\d{1,2}:\d{2}(?::\d{2})?\s?(?:[AaPp]\.?[Mm]\.?)?",
    "id": r"(?=[A-Za-z0-9/_-]*\d)[A-Za-z0-9][A-Za-z0-9/_-]{2,}",
    "zip": r"\b\d{5}(?:-\d{4})?\b",
    "ipv4": r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
}
# Type implied by a builtin pattern when the field does not name one.
BUILTIN_TYPES = {"money": "money", "number": "float", "int": "int", "percent": "percent", "date": "date"}


# ============================================================ loading

@dataclass
class Document:
    source: str
    text: str
    method: str
    warnings: list[str] = field(default_factory=list)


class _HTMLText(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append("  |  ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(source: str) -> str:
    parser = _HTMLText()
    parser.feed(source)
    return "\n".join(line.strip() for line in "".join(parser.parts).splitlines())


def docx_to_text(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
    xml = re.sub(r"</w:tc>", "\t", xml)  # table cells -> tab separated
    xml = re.sub(r"</w:p>|<w:br[^>]*/>", "\n", xml)
    return html.unescape(re.sub(r"<[^>]+>", "", xml))


def ocr_available() -> bool:
    return shutil.which("tesseract") is not None


def ocr_image(path: Path, lang: str = "eng", psm: int = 6, timeout: float = 180) -> str:
    """OCR one image. psm 6 (uniform block) + preserved spacing keeps table columns apart."""
    if not ocr_available():
        raise RuntimeError("OCR needs the `tesseract` program (apt install tesseract-ocr / brew install tesseract).")
    r = subprocess.run(["tesseract", str(path), "stdout", "-l", lang, "--psm", str(psm),
                        "-c", "preserve_interword_spaces=1"],
                       capture_output=True, text=True, errors="replace", timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"tesseract failed: {r.stderr.strip()[:500]}")
    return r.stdout


def _pdf_text(path: Path) -> str | None:
    if shutil.which("pdftotext"):
        r = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True,
                           text=True, errors="replace", timeout=300)
        if r.returncode == 0:
            return r.stdout
    try:
        from pypdf import PdfReader  # optional dependency
    except ImportError:
        return None
    return "\n\f".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)


def _pdf_ocr(path: Path, lang: str) -> str:
    if not shutil.which("pdftoppm"):
        raise RuntimeError("Scanned PDF OCR needs `pdftoppm` (poppler-utils) and `tesseract`.")
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["pdftoppm", "-r", "300", "-png", str(path), f"{tmp}/page"],
                       check=True, capture_output=True, timeout=600)
        pages = sorted(Path(tmp).glob("page*.png"))
        return "\n\f".join(ocr_image(p, lang) for p in pages)


def load_document(path: str | Path, *, lang: str = "eng", force_ocr: bool = False) -> Document:
    path = Path(path)
    suffix = path.suffix.lower()
    warnings: list[str] = []
    if suffix in IMAGE_SUFFIXES:
        return Document(str(path), normalize_text(ocr_image(path, lang), ocr=True), "ocr")
    if suffix == ".pdf":
        text = None if force_ocr else _pdf_text(path)
        # A text layer with almost no characters means a scanned PDF.
        if text is not None and len(re.sub(r"\s", "", text)) >= 20:
            return Document(str(path), normalize_text(text), "pdf-text")
        if ocr_available():
            return Document(str(path), normalize_text(_pdf_ocr(path, lang), ocr=True), "pdf-ocr")
        warnings.append("PDF has no usable text layer and OCR tools are not installed.")
        return Document(str(path), normalize_text(text or ""), "pdf-text", warnings)
    if suffix == ".docx":
        return Document(str(path), normalize_text(docx_to_text(path)), "docx")
    raw = path.read_text(encoding="utf-8", errors="replace")
    if suffix in (".html", ".htm"):
        return Document(str(path), normalize_text(html_to_text(raw)), "html")
    if suffix not in TEXT_SUFFIXES:
        warnings.append(f"Unknown type {suffix or '(none)'}; read as text.")
    return Document(str(path), normalize_text(raw), "text", warnings)


# ============================================================ OCR repair

_PUNCT = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
                        "\u2013": "-", "\u2014": "-", "\u2212": "-", "\u00a0": " ", "\u2009": " "})
# Letters OCR commonly confuses with digits, only fixed inside numeric-looking tokens.
_DIGIT_FIX = str.maketrans({"O": "0", "o": "0", "D": "0", "I": "1", "l": "1", "|": "1",
                            "S": "5", "B": "8", "Z": "2"})
_NUMERIC_TOKEN = re.compile(r"(?<![\w|])[0-9ODoIlSBZ|]+(?:[.,][0-9ODoIlSBZ|]+)*(?![\w|])")


def _fix_numeric_token(m: re.Match) -> str:
    tok = m.group(0)
    digits = sum(c.isdigit() for c in tok)
    letters = sum(c.isalpha() or c == "|" for c in tok)
    # Only repair when the token is clearly a number with a few misread characters.
    if digits >= 2 and letters and letters <= digits:
        return tok.translate(_DIGIT_FIX)
    return tok


def normalize_text(text: str, ocr: bool = False) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_PUNCT)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n")
    if ocr:
        text = re.sub(r"([a-z])-\n([a-z])", r"\1\2", text)          # re-join hyphenated words
        text = _NUMERIC_TOKEN.sub(_fix_numeric_token, text)          # 1O0 -> 100, l2.5O -> 12.50
    lines = [line.rstrip() for line in text.split("\n")]            # keep inner spacing: columns
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n")


# ============================================================ typed values

def parse_number(value: str) -> float | None:
    s = value.strip()
    negative = s.startswith("(") and s.endswith(")") or "-" in s[:2] or s.endswith("-")
    s = re.sub(r"[^\d.,]", "", s)
    if not re.search(r"\d", s):
        return None
    if "," in s and "." in s:
        decimal = "," if s.rfind(",") > s.rfind(".") else "."
    elif "," in s:
        head, _, tail = s.rpartition(",")
        decimal = "," if len(tail) in (1, 2) and s.count(",") == 1 else None
    elif s.count(".") > 1:
        decimal = None  # 1.234.567 -> thousands separators
    else:
        decimal = "."
    thousands = {",": ".", ".": ",", None: ",."}[decimal]
    for ch in thousands:
        s = s.replace(ch, "")
    if decimal == ",":
        s = s.replace(",", ".")
    try:
        n = float(s)
    except ValueError:
        return None
    return -n if negative else n


_DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m.%d.%Y",
                 "%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y"]
_DAYFIRST_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d.%m.%Y",
                     "%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y"]


def parse_date(value: str, dayfirst: bool = False) -> str | None:
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", value.strip())
    s = re.sub(r"[,]", " ", s)
    s = re.sub(r"\b(Sept)\b", "Sep", s)
    s = re.sub(r"(?<=[A-Za-z])\.", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    for fmt in (_DAYFIRST_FORMATS if dayfirst else _DATE_FORMATS):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def coerce(value: str, kind: str, dayfirst: bool = False):
    """Convert a matched string to a typed value; returns (value, warning|None)."""
    if kind in ("str", "text", None):
        return value.strip(), None
    if kind in ("float", "money", "number"):
        n = parse_number(value)
        return (round(n, 2) if kind == "money" and n is not None else n), \
            (None if n is not None else f"not a number: {value!r}")
    if kind == "int":
        n = parse_number(value)
        return (int(n) if n is not None else None), (None if n is not None else f"not an integer: {value!r}")
    if kind == "percent":
        n = parse_number(value.replace("%", ""))
        return n, (None if n is not None else f"not a percent: {value!r}")
    if kind == "date":
        d = parse_date(value, dayfirst)
        return (d or value.strip()), (None if d else f"unrecognised date: {value!r}")
    raise ValueError(f"Unknown field type {kind!r}")


# ============================================================ fields

@dataclass
class Field:
    """One column to extract.

    pattern: a regex, or "@builtin" (see BUILTIN). A named group `value`, or the first
             group, is the captured value; otherwise the whole match.
    label:   optional text that precedes the value ("Invoice No", "Total Due"). The value is
             looked for after the label on the same line, then on the next non-empty line.
    """
    name: str
    pattern: str = "@text"
    label: str | None = None
    type: str | None = None
    multiple: bool = False
    required: bool = False

    def regex(self) -> re.Pattern:
        if self.pattern.startswith("@"):
            key = self.pattern[1:]
            if key not in BUILTIN:
                raise ValueError(f"Unknown builtin @{key}; choose from {sorted(BUILTIN)}")
            return re.compile(BUILTIN[key], re.I if key in ("date", "time") else 0)
        return re.compile(self.pattern)

    def kind(self) -> str:
        return self.type or BUILTIN_TYPES.get(self.pattern.lstrip("@"), "str")

    @classmethod
    def parse(cls, spec: str) -> "Field":
        """CLI shorthand  name[:Label]=pattern[:type]   e.g.  total:Total Due=@money"""
        if "=" not in spec:
            raise ValueError(f"Field spec needs '=': {spec!r}")
        left, pattern = spec.split("=", 1)
        name, _, label = left.partition(":")
        kind = None
        if m := re.fullmatch(r"(@\w+):(\w+)", pattern):
            pattern, kind = m.groups()
        return cls(name.strip(), pattern or "@text", label.strip() or None, kind)


def _value(m: re.Match) -> str:
    if "value" in m.re.groupindex and m.group("value") is not None:
        return m.group("value")
    if m.re.groups:
        return next((g for g in m.groups() if g is not None), m.group(0))
    return m.group(0)


def _label_regex(label: str) -> re.Pattern:
    words = [re.escape(w) for w in label.split()]
    return re.compile(r"(?<!\w)" + r"\s+".join(words) + r"(?!\w)", re.I)


def find_values(text: str, f: Field) -> list[str]:
    rx = f.regex()
    if not f.label:
        return [_value(m).strip() for m in rx.finditer(text)]
    found = []
    for lm in _label_regex(f.label).finditer(text):
        line_end = text.find("\n", lm.end())
        line_end = len(text) if line_end == -1 else line_end
        rest = text[lm.end():line_end]
        rest_stripped = re.sub(r"^[\s:#.\-=|]*", "", rest)
        m = None
        if rest_stripped:
            offset = lm.end() + (len(rest) - len(rest_stripped))
            m = rx.match(text, offset, line_end) or rx.search(text, offset, line_end)
        else:  # value on the next non-empty line
            nxt = re.compile(r"\n[ \t]*(?=\S)").search(text, line_end)
            if nxt:
                end2 = text.find("\n", nxt.end())
                m = rx.match(text, nxt.end(), len(text) if end2 == -1 else end2)
        if m and _value(m).strip():
            found.append(_value(m).strip())
    return found


def extract_fields(text: str, fields: list[Field], dayfirst: bool = False) -> tuple[dict, list[str]]:
    record, warnings = {}, []
    for f in fields:
        values = find_values(text, f)
        typed = []
        for v in values if f.multiple else values[:1]:
            val, warn = coerce(v, f.kind(), dayfirst)
            typed.append(val)
            if warn:
                warnings.append(f"{f.name}: {warn}")
        if not typed:
            if f.required:
                warnings.append(f"{f.name}: required field not found")
            record[f.name] = [] if f.multiple else None
        else:
            record[f.name] = typed if f.multiple else typed[0]
    return record, warnings


def key_values(text: str) -> dict:
    """Generic `Key: Value` lines (keys up to 40 chars, first occurrence wins)."""
    out = {}
    for m in re.finditer(r"^[ \t]*([A-Za-z][\w .#/()&-]{0,39}?)[ \t]*:(?!//)[ \t]*(\S[^\n]*)$", text, re.M):
        key = re.sub(r"\s+", "_", m.group(1).strip().lower())
        out.setdefault(key, m.group(2).strip())
    return out


# ============================================================ tables

@dataclass
class Table:
    header: list[str]
    rows: list[list[str]]
    start_line: int

    def records(self) -> list[dict]:
        return [dict(zip(self.header, row)) for row in self.rows]


def split_row(line: str) -> list[str]:
    s = line.strip()
    if "|" in s:
        cells = [c.strip() for c in s.strip("|").split("|")]
    elif "\t" in s:
        cells = [c.strip() for c in s.split("\t")]
    else:
        cells = re.split(r"\s{2,}", s)  # OCR / fixed-width: two or more spaces separate columns
    return [c for c in cells] if any(cells) else []


def _is_rule(cells: list[str]) -> bool:
    return all(re.fullmatch(r":?-{2,}:?|=+|", c) for c in cells)


def _numeric(cell: str) -> bool:
    return bool(re.fullmatch(r"[\s$€£¥%()+-]*\d[\d.,\s]*[%)]?\s*", cell))


def _dedupe(names: list[str]) -> list[str]:
    seen: Counter = Counter()
    out = []
    for i, n in enumerate(names):
        n = re.sub(r"\s+", "_", n.strip().lower()) or f"col{i + 1}"
        seen[n] += 1
        out.append(n if seen[n] == 1 else f"{n}_{seen[n]}")
    return out


def parse_tables(text: str, min_rows: int = 2, min_cols: int = 2) -> list[Table]:
    blocks, current = [], []
    for i, line in enumerate(text.split("\n")):
        cells = split_row(line)
        if len(cells) >= min_cols:
            if not _is_rule(cells):
                current.append((i + 1, cells))
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)

    tables = []
    for block in blocks:
        if len(block) < min_rows:
            continue
        width = Counter(len(c) for _, c in block).most_common(1)[0][0]
        rows = []
        for _, cells in block:
            if len(cells) < width:
                cells = cells + [""] * (width - len(cells))
            elif len(cells) > width:  # overflow merges into the last column
                cells = cells[:width - 1] + [" ".join(cells[width - 1:])]
            rows.append(cells)
        first = rows[0]
        # Header: a first row with no numbers, above rows that contain numbers (or a longer table).
        has_header = not any(_numeric(c) for c in first) and (
            any(_numeric(c) for r in rows[1:] for c in r) or len(rows) > 2)
        if has_header:
            header, body = _dedupe(first), rows[1:]
        else:
            header, body = [f"col{i + 1}" for i in range(width)], rows
        if len(body) >= 1:
            tables.append(Table(header, body, block[0][0]))
    return tables


# ============================================================ run + output

@dataclass
class Extraction:
    records: list[dict]
    tables: list[dict]
    documents: list[Document]


def extract(paths, fields: list[Field] = (), *, tables: bool = False, kv: bool = False,
            dayfirst: bool = False, lang: str = "eng", force_ocr: bool = False) -> Extraction:
    records, table_rows, docs = [], [], []
    for path in paths:
        try:
            doc = load_document(path, lang=lang, force_ocr=force_ocr)
        except Exception as exc:  # one bad file must not sink the batch
            doc = Document(str(path), "", "error", [f"{type(exc).__name__}: {exc}"])
        docs.append(doc)
        row = {"_source": doc.source, "_method": doc.method}
        if kv:
            row.update({f"kv.{k}": v for k, v in key_values(doc.text).items()})
        values, warnings = extract_fields(doc.text, list(fields), dayfirst)
        row.update(values)
        row["_warnings"] = "; ".join(doc.warnings + warnings)
        records.append(row)
        if tables:
            for t_index, t in enumerate(parse_tables(doc.text)):
                for r in t.records():
                    table_rows.append({"_source": doc.source, "_table": t_index,
                                       "_line": t.start_line, **r})
    return Extraction(records, table_rows, docs)


def aggregate(rows: list[dict], group_by: str | None, sum_fields: list[str]) -> list[dict]:
    """Group rows and total numeric fields (strings like "$1,200.50" are parsed)."""
    groups: dict = {}
    for r in rows:
        key = r.get(group_by) if group_by else "(all)"
        g = groups.setdefault(key, {group_by or "group": key, "count": 0,
                                    **{f"sum_{f}": 0.0 for f in sum_fields}})
        g["count"] += 1
        for f in sum_fields:
            v = r.get(f)
            n = v if isinstance(v, (int, float)) else parse_number(str(v)) if v not in (None, "") else None
            if n is not None:
                g[f"sum_{f}"] = round(g[f"sum_{f}"] + n, 6)
    return list(groups.values())


def _cell(v):
    return json.dumps(v) if isinstance(v, (list, dict)) else ("" if v is None else v)


def write_rows(rows: list[dict], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = list(dict.fromkeys(k for r in rows for k in r))
    suffix = path.suffix.lower()
    if suffix == ".json":
        path.write_text(json.dumps(rows, indent=2, ensure_ascii=False))
    elif suffix == ".jsonl":
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    elif suffix == ".md":
        path.write_text(to_markdown(rows, columns))
    else:
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=columns, delimiter="\t" if suffix == ".tsv" else ",")
            w.writeheader()
            for r in rows:
                w.writerow({k: _cell(r.get(k)) for k in columns})
    return path


def to_markdown(rows: list[dict], columns: list[str] | None = None) -> str:
    columns = columns or list(dict.fromkeys(k for r in rows for k in r))
    if not columns:
        return "(no rows)\n"
    esc = lambda v: str(_cell(v)).replace("|", "\\|").replace("\n", " ")
    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    lines += ["| " + " | ".join(esc(r.get(c)) for c in columns) + " |" for r in rows]
    return "\n".join(lines) + "\n"


def load_spec(path: str | Path) -> dict:
    """Spec file: {"fields": [{"name", "pattern", "label", "type", "multiple", "required"}],
    "tables": bool, "kv": bool, "dayfirst": bool}"""
    spec = json.loads(Path(path).read_text())
    spec["fields"] = [Field(**f) for f in spec.get("fields", [])]
    return spec


def rows_to_csv_text(rows: list[dict]) -> str:
    buf = io.StringIO()
    columns = list(dict.fromkeys(k for r in rows for k in r))
    w = csv.DictWriter(buf, fieldnames=columns)
    w.writeheader()
    for r in rows:
        w.writerow({k: _cell(r.get(k)) for k in columns})
    return buf.getvalue()
