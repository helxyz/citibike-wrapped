"""Shrink a year of trip files to save disk space.

Converts each trip CSV (inside the downloaded .zip files, including zips nested in zips)
to a compressed Parquet file, ONE AT A TIME, deleting the temporary CSV straight away.
A year of NYC rides goes from ~10 GB of CSV to ~1-1.5 GB of Parquet, and you never need
more than one month unzipped at once.

    python prepare.py --zips ~/Downloads/citibike-2025 --out ~/Downloads/citibike-2025-parquet
    python prepare.py ... --delete-zips      # also delete each zip once it is converted
"""
import argparse, shutil, tempfile, zipfile
from pathlib import Path

import duckdb

COLS = """rideable_type, started_at, ended_at, start_station_name, start_station_id,
          end_station_name, end_station_id, start_lat, start_lng, end_lat, end_lng, member_casual"""


def convert_csv(csv: Path, out_dir: Path) -> None:
    out = out_dir / (csv.stem + ".parquet")
    if out.exists():
        print(f"  skip {out.name} (already done)")
        return
    duckdb.execute(f"""COPY (SELECT {COLS} FROM read_csv('{csv}',
        types = {{'start_station_id': 'VARCHAR', 'end_station_id': 'VARCHAR'}}))
        TO '{out}' (FORMAT parquet, COMPRESSION zstd)""")
    print(f"  {csv.name}: {csv.stat().st_size / 1e6:,.0f} MB csv -> {out.stat().st_size / 1e6:,.0f} MB parquet")


def handle_zip(zpath: Path, out_dir: Path) -> None:
    with zipfile.ZipFile(zpath) as z:
        for name in z.namelist():
            if name.startswith("__MACOSX") or name.endswith("/"):
                continue
            if not name.lower().endswith((".csv", ".zip")):
                continue
            with tempfile.TemporaryDirectory() as tmp:
                member = Path(z.extract(name, tmp))      # one file unzipped at a time
                if member.suffix.lower() == ".zip":
                    handle_zip(member, out_dir)           # yearly zips contain monthly zips
                else:
                    convert_csv(member, out_dir)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zips", required=True, help="folder with downloaded .zip files (and/or .csv files)")
    ap.add_argument("--out", required=True, help="folder for the .parquet files")
    ap.add_argument("--delete-zips", action="store_true", help="delete each zip/csv after converting it")
    a = ap.parse_args()
    src, out = Path(a.zips).expanduser(), Path(a.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    for f in sorted(src.rglob("*")):
        if f.suffix.lower() == ".zip":
            print(f.name); handle_zip(f, out)
        elif f.suffix.lower() == ".csv":
            print(f.name); convert_csv(f, out)
        else:
            continue
        if a.delete_zips:
            f.unlink()
    total = sum(p.stat().st_size for p in out.glob("*.parquet")) / 1e9
    print(f"done: {len(list(out.glob('*.parquet')))} parquet files, {total:.2f} GB in {out}")


if __name__ == "__main__":
    main()
