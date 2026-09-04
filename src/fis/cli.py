"""Service entry points — the `fis` console script.

`fis api` runs the HTTP surface the broker's `auth_backend_http` calls.
`fis principal` mints and administers principals: `create` prints the new
id, which is the CN to cut the certificate with (`gwcert key add
--common-name <id>`); `suspend` / `activate` are the emergency-eviction
lever.
"""

import argparse
import sys

import uvicorn

from fis.db.models import PrincipalKind, PrincipalStatus
from fis.db.session import SessionLocal
from fis.principals import (
    create_principal,
    list_principals,
    set_principal_status,
)
from fis.settings import ApiRunSettings


def _run_api() -> None:
    run = ApiRunSettings()
    uvicorn.run("fis.api:app", host=run.api_host, port=run.api_port)


def _run_principal(args: argparse.Namespace) -> int:
    with SessionLocal() as session:
        if args.principal_command == "create":
            row = create_principal(
                session,
                kind=PrincipalKind(args.kind),
                g_node_id=args.g_node_id,
                display_name=args.display_name,
            )
            # The id alone on stdout: it is the CN, and pipes straight into
            # gwcert. Everything else goes to stderr.
            print(row.id)
            print(
                f"minted {row.kind.value} principal {row.id}"
                f" ({row.display_name or 'no display name'}); "
                f"cut its cert with: gwcert key add --common-name {row.id}",
                file=sys.stderr,
            )
        elif args.principal_command == "list":
            for row in list_principals(session):
                print(
                    f"{row.id}  {row.kind.value:8}{row.status.value:10}"
                    f"{row.display_name or ''}"
                )
        else:
            status = (
                PrincipalStatus.Suspended
                if args.principal_command == "suspend"
                else PrincipalStatus.Active
            )
            row = set_principal_status(session, args.principal_id, status)
            print(f"{row.id} is now {row.status.value}", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="fis", description="Fleet Index Service")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("api", help="run the HTTP auth surface")

    principal = sub.add_parser("principal", help="mint and administer principals")
    psub = principal.add_subparsers(dest="principal_command", required=True)
    create = psub.add_parser(
        "create", help="mint a principal row; prints its id (the cert CN)"
    )
    create.add_argument(
        "--kind", required=True, choices=[k.value for k in PrincipalKind]
    )
    create.add_argument(
        "--g-node-id", help="the GNodeId, required for (and only for) a GNode"
    )
    create.add_argument("--display-name")
    psub.add_parser("list", help="every principal, oldest first")
    for verb, help_text in (
        ("suspend", "deny this principal on every connect (emergency eviction)"),
        ("activate", "lift a suspension"),
    ):
        p = psub.add_parser(verb, help=help_text)
        p.add_argument("principal_id")

    args = parser.parse_args()
    if args.command == "api":
        _run_api()
        return 0
    try:
        return _run_principal(args)
    except ValueError as e:
        print(f"fis principal: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
