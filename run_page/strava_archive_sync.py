"""Import new activities from a Strava account archive without using the API."""

import argparse
import csv
import gzip
import io
import json
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from config import JSON_FILE, SQL_FILE
from generator import Generator
from generator.db import Activity, update_or_create_activity
from gpxtrackposter.track import Track


STRAVA_DATE_FORMAT = "%b %d, %Y, %I:%M:%S %p"
SUPPORTED_FORMATS = {"fit", "gpx", "tcx"}


def _read_activity_rows(archive: zipfile.ZipFile):
    with archive.open("activities.csv") as csv_file:
        text_file = io.TextIOWrapper(csv_file, encoding="utf-8-sig", newline="")
        return list(csv.DictReader(text_file))


def _normalized_activity_date(row):
    return datetime.strptime(row["Activity Date"], STRAVA_DATE_FORMAT).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _activity_format(filename):
    name = filename.removesuffix(".gz")
    return Path(name).suffix.removeprefix(".").lower()


def _load_track(archive, row, temp_dir):
    member = row["Filename"]
    file_format = _activity_format(member)
    if file_format not in SUPPORTED_FORMATS:
        raise ValueError(f"unsupported activity format: {member}")

    file_data = archive.read(member)
    if member.lower().endswith(".gz"):
        file_data = gzip.decompress(file_data)

    activity_file = Path(temp_dir) / f"{row['Activity ID']}.{file_format}"
    activity_file.write_bytes(file_data)

    track = Track()
    getattr(track, f"load_{file_format}")(str(activity_file))
    if track.start_time is None or track.length <= 0:
        raise ValueError(f"could not parse activity file: {member}")
    return track, file_format


def _write_json(generator):
    activities = generator.load()
    with open(JSON_FILE, "w", encoding="utf-8") as json_file:
        json.dump(activities, json_file)


def import_archive(archive_path, dry_run=False):
    generator = Generator(SQL_FILE)
    existing = generator.session.query(Activity).all()
    existing_ids = {int(activity.run_id) for activity in existing}
    existing_dates = {str(activity.start_date)[:19] for activity in existing}

    with zipfile.ZipFile(archive_path) as archive:
        rows = _read_activity_rows(archive)
        new_rows = [
            row
            for row in rows
            if int(row["Activity ID"]) not in existing_ids
            and _normalized_activity_date(row) not in existing_dates
        ]

        print(
            f"Archive activities: {len(rows)}; existing: {len(existing)}; "
            f"new: {len(new_rows)}"
        )
        for row in new_rows:
            print(
                f"  {row['Activity ID']}  {row['Activity Date']}  "
                f"{row['Activity Name']}"
            )

        if dry_run or not new_rows:
            return len(new_rows)

        imported = 0
        with tempfile.TemporaryDirectory(prefix="strava-archive-") as temp_dir:
            for row in new_rows:
                try:
                    track, file_format = _load_track(archive, row, temp_dir)
                    activity_data = track.to_namedtuple(
                        run_from=file_format
                    )._asdict()
                    activity_data.update(
                        id=int(row["Activity ID"]),
                        name=row["Activity Name"],
                        type=row["Activity Type"] or activity_data["type"],
                        # The archive does not contain Strava's geocoded location.
                        # Keep the GPS route, but avoid an external lookup here.
                        start_latlng=[],
                        location_country="",
                    )
                    created = update_or_create_activity(
                        generator.session, SimpleNamespace(**activity_data)
                    )
                    if created:
                        imported += 1
                except Exception as error:
                    print(f"Skipping activity {row['Activity ID']}: {error}")

        generator.session.commit()
        _write_json(generator)
        print(f"Imported {imported} new activities")
        return imported


def main():
    parser = argparse.ArgumentParser(
        description="Import new activities from a Strava account archive ZIP"
    )
    parser.add_argument("archive", help="path to the Strava export ZIP")
    parser.add_argument(
        "--dry-run", action="store_true", help="show new activities without importing"
    )
    options = parser.parse_args()
    import_archive(options.archive, dry_run=options.dry_run)


if __name__ == "__main__":
    main()
