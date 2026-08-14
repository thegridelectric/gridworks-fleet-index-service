"""Service entry points — the `fis` console script.

`fis api` runs the HTTP surface the broker's `auth_backend_http` calls.
"""

import argparse
import sys

import uvicorn

from fis.settings import ApiRunSettings


def _run_api() -> None:
    run = ApiRunSettings()
    uvicorn.run("fis.api:app", host=run.api_host, port=run.api_port)


def main() -> int:
    parser = argparse.ArgumentParser(prog="fis", description="Fleet Index Service")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("api", help="run the HTTP auth surface")

    args = parser.parse_args()
    if args.command == "api":
        _run_api()
    return 0


if __name__ == "__main__":
    sys.exit(main())
