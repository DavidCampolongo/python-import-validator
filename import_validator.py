"""Load inventory records from CSV and export an audit of rejected rows."""

import argparse
import csv
import json
import os
import sqlite3
import sys
import tempfile
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO


FIELDS = ("item_id", "name", "quantity")
MAX_QUANTITY = 2**63 - 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    item_id TEXT PRIMARY KEY NOT NULL,
    name TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity >= 0)
);
CREATE TABLE IF NOT EXISTS import_runs (
    id INTEGER PRIMARY KEY,
    inserted INTEGER NOT NULL DEFAULT 0,
    unchanged INTEGER NOT NULL DEFAULT 0,
    rejected INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS rejections (
    run_id INTEGER NOT NULL REFERENCES import_runs (id),
    line_number INTEGER NOT NULL,
    reason TEXT NOT NULL,
    row_json TEXT NOT NULL,
    PRIMARY KEY (run_id, line_number)
);
"""


@dataclass(frozen=True)
class Item:
    item_id: str
    name: str
    quantity: int


@dataclass(frozen=True)
class ImportResult:
    run_id: int
    inserted: int
    unchanged: int
    rejected: int


def read_rows(source: TextIO) -> Iterator[tuple[int, list[str]]]:
    reader = csv.reader(source, strict=True)
    while True:
        line_number = reader.line_num + 1
        try:
            row = next(reader)
        except StopIteration:
            return
        except csv.Error as error:
            raise ValueError(f"Malformed CSV at line {line_number} ({error}).") from error
        yield line_number, row


def parse_item(row: list[str], columns: tuple[int, ...]) -> Item:
    if len(row) != len(FIELDS):
        raise ValueError(f"Expected {len(FIELDS)} columns, found {len(row)}.")

    item_id, name, quantity_text = (row[index].strip() for index in columns)
    if not item_id:
        raise ValueError("item_id is required.")
    if not name:
        raise ValueError("name is required.")
    if not (quantity_text.isascii() and quantity_text.isdecimal()):
        raise ValueError("quantity must be a non-negative whole number.")

    digits = quantity_text.lstrip("0") or "0"
    if len(digits) > 19 or int(digits) > MAX_QUANTITY:
        raise ValueError(f"quantity must be at most {MAX_QUANTITY}.")
    return Item(item_id, name, int(digits))


def store_item(connection: sqlite3.Connection, item: Item) -> bool:
    existing = connection.execute(
        "SELECT name, quantity FROM items WHERE item_id = ?", (item.item_id,)
    ).fetchone()
    if existing is None:
        connection.execute(
            "INSERT INTO items (item_id, name, quantity) VALUES (?, ?, ?)",
            (item.item_id, item.name, item.quantity),
        )
        return True
    if existing != (item.name, item.quantity):
        raise ValueError("item_id already exists with different values.")
    return False


def load_csv(input_path: Path, database: Path) -> ImportResult:
    with input_path.open(encoding="utf-8-sig", newline="") as source:
        rows = read_rows(source)
        _, header = next(rows, (0, []))
        if len(header) != len(FIELDS) or set(header) != set(FIELDS):
            raise ValueError("Expected a CSV header with item_id, name, and quantity.")
        columns = tuple(header.index(field) for field in FIELDS)

        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(SCHEMA)
            # Hold the write lock through the duplicate check and insert.
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute("INSERT INTO import_runs DEFAULT VALUES")
            run_id = cursor.lastrowid
            inserted = unchanged = rejected = 0

            for line_number, row in rows:
                if not row:
                    continue
                try:
                    added = store_item(connection, parse_item(row, columns))
                except ValueError as error:
                    connection.execute(
                        "INSERT INTO rejections VALUES (?, ?, ?, ?)",
                        (run_id, line_number, str(error), json.dumps(row, ensure_ascii=False)),
                    )
                    rejected += 1
                else:
                    inserted += added
                    unchanged += not added

            connection.execute(
                "UPDATE import_runs SET inserted = ?, unchanged = ?, rejected = ? WHERE id = ?",
                (inserted, unchanged, rejected, run_id),
            )
            return ImportResult(run_id, inserted, unchanged, rejected)


def write_report(database: Path, run_id: int, output: Path) -> None:
    """Write the rejection CSV atomically, using the committed import audit."""
    uri = f"{database.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        if connection.execute("SELECT 1 FROM import_runs WHERE id = ?", (run_id,)).fetchone() is None:
            raise ValueError(f"Import run {run_id} was not found.")
        rejections = connection.execute(
            "SELECT line_number, reason, row_json FROM rejections WHERE run_id = ? ORDER BY line_number",
            (run_id,),
        )
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="", dir=output.parent, delete=False
            ) as handle:
                temporary = Path(handle.name)
                writer = csv.writer(handle)
                writer.writerow(("line_number", "reason", "row_json"))
                writer.writerows(rejections)
            os.replace(temporary, output)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def require_distinct_paths(*paths: Path) -> None:
    if len({path.resolve() for path in paths}) != len(paths):
        raise ValueError("The input, database, and report must use separate paths.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    load = commands.add_parser("load", help="validate and import an inventory CSV")
    load.add_argument("input", type=Path)
    load.add_argument("--db", required=True, type=Path)
    load.add_argument("--rejects", type=Path, help="write a rejection CSV after the import")
    report = commands.add_parser("report", help="export rejected rows from a committed import")
    report.add_argument("run_id", type=int)
    report.add_argument("--db", required=True, type=Path)
    report.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    try:
        if args.command == "report":
            require_distinct_paths(args.db, args.out)
            write_report(args.db, args.run_id, args.out)
            print(f"Wrote rejection report for import {args.run_id} to {args.out}")
            return 0

        paths = (args.input, args.db) if args.rejects is None else (args.input, args.db, args.rejects)
        require_distinct_paths(*paths)
        result = load_csv(args.input, args.db)
        print(
            f"Import {result.run_id} committed | {result.inserted} inserted, "
            f"{result.unchanged} unchanged, {result.rejected} rejected",
            flush=True,
        )
        if args.rejects is not None:
            write_report(args.db, result.run_id, args.rejects)
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
