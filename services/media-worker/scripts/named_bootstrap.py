"""Exercise the one-time CLI with a disposable database. Real provisioning stays locked."""
import argparse
import getpass
import tempfile
from pathlib import Path

from app.named_auth import bootstrap


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable-test", action="store_true")
    args = parser.parse_args()
    if not args.disposable_test:
        parser.error("Real provisioning requires a designated, verified local bootstrap authority")
    with tempfile.TemporaryDirectory(prefix="pgm-bootstrap-qa-") as directory:
        bootstrap(Path(directory) / "qa.sqlite", input("Synthetic account name: "),
                  getpass.getpass("Disposable test password: "), verified_operator="disposable-cli-test")
        print("Disposable bootstrap passed.")


if __name__ == "__main__":
    main()
