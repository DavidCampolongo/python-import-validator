# Python Import Validator

[![Tests](https://github.com/DavidCampolongo/python-import-validator/actions/workflows/tests.yml/badge.svg)](https://github.com/DavidCampolongo/python-import-validator/actions/workflows/tests.yml)

A command-line tool that validates an inventory CSV, imports valid items into SQLite, and records why each rejected row failed. It uses the Python 3.10+ standard library and includes a synthetic dataset for a local demo.

## Run the demo

From the repository root, run the sample twice against a fresh temporary database.

```sh
demo_dir=$(mktemp -d)
db="$demo_dir/inventory.sqlite3"
python3 import_validator.py load examples/inventory.csv --db "$db" --rejects "$demo_dir/rejects.csv"
python3 import_validator.py load examples/inventory.csv --db "$db"
cat "$demo_dir/rejects.csv"
```

The two imports print these summaries.

```text
Import 1 committed | 3 inserted, 1 unchanged, 2 rejected
Import 2 committed | 0 inserted, 4 unchanged, 2 rejected
```

The database holds three items and two import runs. The first run's rejection report shows the source line, reason, and original row for a negative quantity and a conflicting item ID.

## Check rollback on malformed CSV

The [malformed sample](examples/malformed.csv) contains a new valid item, a row with a negative quantity, and an unclosed quote on line 4. Run it against the same demo database.

```sh
python3 import_validator.py load examples/malformed.csv --db "$db" --rejects "$demo_dir/malformed-rejects.csv" ||
  printf 'Exit status: %s\n' "$?"
```

```text
Error: Malformed CSV at line 4 (unexpected end of data).
Exit status: 1
```

The syntax error rolls back the new item, rejection, and import run. Compare the row counts with the two successful runs and check whether the failed run wrote a report.

```sh
python3 - "$db" "$demo_dir/malformed-rejects.csv" <<'PYTHON'
import sqlite3
import sys
from pathlib import Path

with sqlite3.connect(sys.argv[1]) as db:
    for table in ("items", "import_runs", "rejections"):
        count = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"{table}: {count}")
print(f"Failed run report exists: {Path(sys.argv[2]).exists()}")
PYTHON
```

```text
items: 3
import_runs: 2
rejections: 4
Failed run report exists: False
```

## Input format

The UTF-8 CSV header must contain `item_id`, `name`, and `quantity` once each. Columns can appear in any order. IDs and names must be nonempty after trimming surrounding spaces. Quantity must be a whole number from zero through 9,223,372,036,854,775,807. Item IDs are case-sensitive. The importer skips blank lines.

For a repeated ID, an identical item counts as unchanged. A row with different values is rejected so the original item stays intact. Field-level failures are recorded and the import continues. An invalid header or broken CSV syntax stops the import. A syntax error discovered partway through rolls back the run.

## Recover a rejection report

Items and rejection details commit together in SQLite. The export runs after that commit. If the export fails, use the run ID printed by `load` to regenerate the CSV from the database. This command regenerates the first demo run's report.

```sh
python3 import_validator.py report 1 --db "$db" --out "$demo_dir/recovered.csv"
```

Reports replace the destination CSV after a successful write. Use a different output path for each run if you want to keep several reports on disk. An import that records rejected rows still succeeds. A file, database, or export error exits with status 1.

## Run the tests

```sh
python3 -m unittest discover -s tests -v
```

The tests exercise the command line with temporary files and databases. They cover duplicate imports, conflicting IDs, malformed records, a broken file, concurrent imports, and report recovery. The `items` table stores the current inventory. `import_runs` and `rejections` preserve the audit trail.
