# Python Import Validator

A command-line tool that validates an inventory CSV, imports valid items into SQLite, and records why each rejected row failed. It uses the Python 3.10+ standard library and includes a synthetic dataset for a local demo.

## Run the demo

From this directory, run the command below with a new database path.

```sh
python3 import_validator.py load examples/inventory.csv --db inventory.sqlite3 --rejects rejects.csv
```

```text
Import 1 committed | 3 inserted, 1 unchanged, 2 rejected
```

Open `rejects.csv` to see the source line, rejection reason, and original row as a JSON array. Run the same command again to see `0 inserted, 4 unchanged, 2 rejected`. The database still holds three items. Each run receives its own audit record.

## Input format

The UTF-8 CSV header must contain `item_id`, `name`, and `quantity` once each. Columns can appear in any order. IDs and names must be nonempty after trimming surrounding spaces. Quantity must be a whole number from zero through 9,223,372,036,854,775,807. Item IDs are case-sensitive. The importer skips blank lines.

For a repeated ID, an identical item counts as unchanged. A row with different values is rejected so the original item stays intact. Field-level failures are recorded and the import continues. An invalid header or broken CSV syntax stops the import. A syntax error discovered partway through rolls back the run.

## Recover a rejection report

Items and rejection details commit together in SQLite. The export runs after that commit. If the export fails, use the run ID printed by `load` to regenerate the CSV from the database.

```sh
python3 import_validator.py report 1 --db inventory.sqlite3 --out rejects.csv
```

Reports replace the destination CSV after a successful write. Use a different output path for each run if you want to keep several reports on disk. An import that records rejected rows still succeeds. A file, database, or export error exits with status 1.

## Run the tests

```sh
python3 -m unittest discover -s tests -v
```

The tests exercise the command line with temporary files and databases. They cover duplicate imports, conflicting IDs, malformed records, a broken file, concurrent imports, and report recovery. The `items` table stores the current inventory. `import_runs` and `rejections` preserve the audit trail.
