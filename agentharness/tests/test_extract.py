"""OCR clean-up, regex fields, tables and output formats (no OCR engine needed)."""
import csv
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from agentharness import extract as ex

INVOICE_OCR = """ACME Supplies Ltd.
Invoice No: INV-2O26-0042
Date: March 3rd, 2026
Bill To:
  Jane Customer
Contact: billing@example.com  (555) 123-4567

Item            Qty    Unit Price    Amount
Widget A        2      $1O.5O        $21.00
Gadget-B        1      $1,2O0.00     $1,200.00
Service fee     1      $15.00        $15.00

Subtotal ........ $1,236.00
Tax (8%)          $98.88
Total Due
$1,334.88
"""


class Normalize(unittest.TestCase):
    def test_ocr_digit_repair_is_targeted(self):
        text = ex.normalize_text(INVOICE_OCR, ocr=True)
        self.assertIn("$10.50", text)
        self.assertIn("$1,200.00", text)
        self.assertIn("INV-2026-0042", text)          # O between digits repaired
        self.assertIn("ACME Supplies", text)          # words untouched
        self.assertIn("Widget A        2", text)      # column spacing preserved

    def test_hyphenation_and_quotes(self):
        self.assertEqual(ex.normalize_text("infor-\nmation “ok”", ocr=True), 'information "ok"')


class Values(unittest.TestCase):
    def test_numbers(self):
        cases = {"$1,234.50": 1234.5, "1.234,50": 1234.5, "(12.00)": -12.0, "-7": -7.0,
                 "12,5": 12.5, "1,234": 1234.0, "€ 3.000.000": 3000000.0, "abc": None}
        for text, want in cases.items():
            self.assertEqual(ex.parse_number(text), want, text)

    def test_dates(self):
        self.assertEqual(ex.parse_date("March 3rd, 2026"), "2026-03-03")
        self.assertEqual(ex.parse_date("03/04/2026"), "2026-03-04")
        self.assertEqual(ex.parse_date("03/04/2026", dayfirst=True), "2026-04-03")
        self.assertEqual(ex.parse_date("4 Sept. 2026"), "2026-09-04")
        self.assertIsNone(ex.parse_date("someday"))


class Fields(unittest.TestCase):
    def setUp(self):
        self.text = ex.normalize_text(INVOICE_OCR, ocr=True)

    def test_label_anchored_fields(self):
        fields = [ex.Field("invoice", "@id", "Invoice No"),
                  ex.Field("date", "@date", "Date"),
                  ex.Field("total", "@money", "Total Due"),   # value on the next line
                  ex.Field("subtotal", "@money", "Subtotal"),  # after dot leaders
                  ex.Field("email", "@email"),
                  ex.Field("phone", "@phone"),
                  ex.Field("amounts", r"\$[\d,]+\.\d\d", multiple=True, type="money"),
                  ex.Field("po", "@id", "PO Number", required=True)]
        rec, warnings = ex.extract_fields(self.text, fields)
        self.assertEqual(rec["invoice"], "INV-2026-0042")
        self.assertEqual(rec["date"], "2026-03-03")
        self.assertEqual(rec["total"], 1334.88)
        self.assertEqual(rec["subtotal"], 1236.0)
        self.assertEqual(rec["email"], "billing@example.com")
        self.assertEqual(rec["phone"], "(555) 123-4567")
        self.assertIn(1200.0, rec["amounts"])
        self.assertIsNone(rec["po"])
        self.assertEqual(warnings, ["po: required field not found"])

    def test_named_group_and_cli_shorthand(self):
        f = ex.Field.parse("tax:Tax=@money")
        self.assertEqual((f.name, f.label, f.pattern), ("tax", "Tax", "@money"))
        rec, _ = ex.extract_fields(self.text, [ex.Field("rate", r"\((?P<value>\d+)%\)", type="int")])
        self.assertEqual(rec["rate"], 8)
        self.assertEqual(ex.Field.parse("d=@date:str").type, "str")
        with self.assertRaises(ValueError):
            ex.Field("x", "@nope").regex()

    def test_key_values(self):
        kv = ex.key_values(self.text + "\nSee: https://example.com\n")
        self.assertEqual(kv["invoice_no"], "INV-2026-0042")
        self.assertEqual(kv["see"], "https://example.com")


class Tables(unittest.TestCase):
    def test_spaced_ocr_table(self):
        tables = ex.parse_tables(ex.normalize_text(INVOICE_OCR, ocr=True))
        items = next(t for t in tables if "item" in t.header)
        self.assertEqual(items.header, ["item", "qty", "unit_price", "amount"])
        self.assertEqual(items.rows[1], ["Gadget-B", "1", "$1,200.00", "$1,200.00"])
        self.assertEqual(len(items.rows), 3)

    def test_markdown_pipe_table_and_ragged_rows(self):
        text = "| Name | Score |\n|---|---|\n| Ann | 9 |\n| Bob | 7 | extra |\n"
        t = ex.parse_tables(text)[0]
        self.assertEqual(t.header, ["name", "score"])
        self.assertEqual(t.rows, [["Ann", "9"], ["Bob", "7 extra"]])


class Files(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_formats_end_to_end(self):
        (self.tmp / "a.txt").write_text("Vendor: ACME\nTotal: $10.00\n")
        (self.tmp / "b.html").write_text("<html><style>x{}</style><p>Vendor: Beta</p><p>Total: $5.50</p></html>")
        with zipfile.ZipFile(self.tmp / "c.docx", "w") as z:
            z.writestr("word/document.xml",
                       '<w:document><w:body><w:p><w:r><w:t>Vendor: ACME</w:t></w:r></w:p>'
                       '<w:p><w:r><w:t>Total: $2.50 &amp; tax</w:t></w:r></w:p></w:body></w:document>')
        fields = [ex.Field("vendor", "@text", "Vendor"), ex.Field("total", "@money", "Total")]
        result = ex.extract(sorted(self.tmp.iterdir()), fields)
        got = [(r["vendor"], r["total"], r["_method"]) for r in result.records]
        self.assertEqual(got, [("ACME", 10.0, "text"), ("Beta", 5.5, "html"), ("ACME", 2.5, "docx")])

        summary = ex.aggregate(result.records, "vendor", ["total"])
        self.assertEqual({r["vendor"]: (r["count"], r["sum_total"]) for r in summary},
                         {"ACME": (2, 12.5), "Beta": (1, 5.5)})

        out = ex.write_rows(result.records, self.tmp / "out" / "rows.csv")
        with out.open() as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(rows[1]["vendor"], "Beta")
        ex.write_rows(result.records, self.tmp / "rows.json")
        self.assertEqual(len(json.loads((self.tmp / "rows.json").read_text())), 3)
        self.assertIn("| vendor |", ex.to_markdown(result.records).replace("_source | _method | ", ""))

    def test_missing_ocr_is_a_warning_not_a_crash(self):
        (self.tmp / "scan.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        if ex.ocr_available():
            self.skipTest("tesseract installed; this checks the missing-tool path")
        result = ex.extract([self.tmp / "scan.png"], [ex.Field("x", "@text")])
        self.assertEqual(result.records[0]["_method"], "error")
        self.assertIn("tesseract", result.records[0]["_warnings"])


if __name__ == "__main__":
    unittest.main()
