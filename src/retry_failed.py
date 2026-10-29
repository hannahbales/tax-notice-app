
import argparse
import hashlib
from pathlib import Path

from processing_history import (
    initialize_database,
    get_record,
    reset_failed_file,
)


BASE_DIR = Path(r"C:\TTI-Notice-Dev")
INTAKE_DIR = BASE_DIR / "intake"


def file_hash(path):
    """Generate the same SHA-256 ID used by the watcher."""
    sha256 = hashlib.sha256()

    with path.open("rb") as file:
        for chunk in iter(
            lambda: file.read(1024 * 1024),
            b"",
        ):
            sha256.update(chunk)

    return sha256.hexdigest()


def retry_failed(filename):
    """
    Reset one failed PDF for automatic reprocessing.
    """

    # only allow filenames, not arbitrary file paths
    if Path(filename).name != filename:
        print("ERROR: Provide a filename, not a path.")
        return

    path = INTAKE_DIR / filename

    if not path.is_file():
        print(f"ERROR: PDF not found: {path}")
        return

    if path.suffix.lower() != ".pdf":
        print("ERROR: Only PDF files can be retried.")
        return

    file_id = file_hash(path)
    record = get_record(file_id)

    if record is None:
        print("This PDF has no processing history.")
        print("The watcher should process it automatically.")
        return

    status, attempts = record

    if status == "completed":
        print("This PDF was already processed successfully.")
        print("Reset canceled to prevent duplicate processing.")
        return

    if status == "processing":
        print("This PDF is currently marked as processing.")
        print("Reset canceled.")
        return

    if status != "failed":
        print(f"This PDF has status '{status}'.")
        print("Only failed PDFs can be reset.")
        return

    print(f"\nFile: {filename}")
    print(f"Current status: {status}")
    print(f"Previous attempts: {attempts}")

    confirmation = input(
        "\nReset this PDF for automatic retry? (y/n): "
    ).strip().lower()

    if confirmation != "y":
        print("Canceled.")
        return

    if reset_failed_file(file_id):
        print("\nRetry status reset successfully!")
        print("The intake watcher will process this PDF again.")
    else:
        print("Reset failed. The record may have changed.")


def main():
    parser = argparse.ArgumentParser(
        description="Manually retry a failed PDF batch."
    )

    parser.add_argument(
        "--filename",
        required=True,
        help="Name of the failed PDF in the intake folder.",
    )

    args = parser.parse_args()

    initialize_database()
    retry_failed(args.filename)


if __name__ == "__main__":
    main()
