import csv
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
PROGRAM = PROJECT / "import_validator.py"
EXAMPLE = PROJECT / "examples" / "inventory.csv"


class ImportValidatorTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name)
        self.database = self.directory / "inventory.sqlite3"
        self.report = self.directory / "rejects.csv"

    def cli(self, *arguments):
        return subprocess.run(
            [sys.executable, PROGRAM, *arguments], capture_output=True, text=True, timeout=10
        )

    def load(self, source, *arguments):
        return self.cli("load", source, "--db", self.database, *arguments)

    def write_input(self, text):
        path = self.directory / "input.csv"
        path.write_text(text, encoding="utf-8")
        return path

    def query(self, statement):
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute(statement).fetchall()

    def report_rows(self):
        with self.report.open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_loads_valid_rows_and_reimport_does_not_duplicate(self):
        first = self.load(EXAMPLE, "--rejects", self.report)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("3 inserted, 1 unchanged, 2 rejected", first.stdout)
        self.assertEqual(
            self.query("SELECT item_id, name, quantity FROM items ORDER BY item_id"),
            [("INV-001", "USB-C Cable", 14), ("INV-002", "Precision Screwdriver", 5),
             ("INV-003", "Parts Bin, Small", 8)],
        )
        rows = self.report_rows()
        self.assertEqual([row["line_number"] for row in rows], ["6", "7"])
        self.assertIn("non-negative", rows[0]["reason"])
        self.assertIn("different values", rows[1]["reason"])
        self.assertEqual(json.loads(rows[1]["row_json"]), ["INV-002", "Precision Screwdriver", "6"])

        second = self.load(EXAMPLE)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("0 inserted, 4 unchanged, 2 rejected", second.stdout)
        self.assertEqual(self.query("SELECT COUNT(*) FROM items"), [(3,)])

    def test_accepts_bom_reordered_headers_and_multiline_values(self):
        source = self.write_input(
            '\ufeffquantity,item_id,name\n3,A,"Two\nlines"\n-5,B,Bad\n1,C,Extra,field\n'
        )
        result = self.load(source, "--rejects", self.report)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.query("SELECT name FROM items"), [("Two\nlines",)])
        self.assertEqual([row["line_number"] for row in self.report_rows()], ["4", "5"])

    def test_invalid_or_empty_header_does_not_create_database(self):
        for header in ("", "item_id,name,name\n"):
            with self.subTest(header=header):
                result = self.load(self.write_input(header))
                self.assertEqual(result.returncode, 1)
                self.assertIn("Expected a CSV header", result.stderr)
                self.assertFalse(self.database.exists())

    def test_invalid_item_values_are_rejected_individually(self):
        source = self.write_input(
            "item_id,name,quantity\n"
            ",Cable,3\nA,,2\nB,Screws,2.5\nC,Labels,9223372036854775808\n"
            "D,Washer,0\nE,Bracket,9223372036854775807\n"
        )
        result = self.load(source, "--rejects", self.report)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("2 inserted, 0 unchanged, 4 rejected", result.stdout)
        self.assertEqual(self.query("SELECT quantity FROM items ORDER BY item_id"), [(0,), (2**63 - 1,)])

    def test_broken_csv_rolls_back_new_items_and_rejections(self):
        result = self.load(EXAMPLE)
        self.assertEqual(result.returncode, 0, result.stderr)
        broken = self.write_input('item_id,name,quantity\nNEW,New,3\nBAD,Invalid,-1\nEND,"Unclosed\n')
        result = self.load(broken, "--rejects", self.report)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Malformed CSV at line 4", result.stderr)
        self.assertFalse(self.report.exists())
        self.assertEqual(self.query("SELECT COUNT(*) FROM items"), [(3,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM import_runs"), [(1,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM rejections"), [(2,)])

    def test_report_can_be_recreated_if_export_failed(self):
        result = self.load(EXAMPLE, "--rejects", self.directory / "missing" / "rejects.csv")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Import 1 committed", result.stdout)

        recovered = self.cli("report", "1", "--db", self.database, "--out", self.report)
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(len(self.report_rows()), 2)
        self.assertEqual(self.query("SELECT COUNT(*) FROM items"), [(3,)])

    def test_report_cannot_replace_input_or_database(self):
        source = self.write_input("item_id,name,quantity\nA,Cable,1\n")
        before = source.read_bytes()
        blocked = self.load(source, "--rejects", source)
        self.assertEqual(blocked.returncode, 1)
        self.assertEqual(source.read_bytes(), before)
        self.assertFalse(self.database.exists())

        result = self.load(source)
        self.assertEqual(result.returncode, 0, result.stderr)
        blocked = self.cli("report", "1", "--db", self.database, "--out", self.database)
        self.assertEqual(blocked.returncode, 1)
        self.assertEqual(self.query("SELECT COUNT(*) FROM items"), [(1,)])

    def test_parallel_imports_share_one_record_per_item(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self.load, (EXAMPLE, EXAMPLE)))
        self.assertTrue(all(result.returncode == 0 for result in results), results)
        self.assertEqual(self.query("SELECT COUNT(*) FROM items"), [(3,)])
        self.assertEqual(
            self.query("SELECT inserted, unchanged, rejected FROM import_runs ORDER BY id"),
            [(3, 1, 2), (0, 4, 2)],
        )


if __name__ == "__main__":
    unittest.main()
