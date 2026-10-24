"""Operator commands, run inside the gateway pod with `kubectl exec` (cluster access required).

  python -m gateway.cli create-admin --name <name>
      Bootstrap: creates the first admin and prints its API key once.
      Refuses if an active admin already exists.
  python -m gateway.cli issue-key --name <existing user> [--key-name <label>]
      Break-glass: issue a new key for an existing user (e.g. a locked-out admin).

Keys are printed to this terminal only (never logged or stored in plain text).
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from .config import Settings
from .db import init_schema, make_engine
from .store import Store


async def _run(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    engine = make_engine(settings.database_url)
    try:
        await init_schema(engine)
        store = Store(engine, settings.key_pepper)
        users = await store.list_users()
        if args.cmd == "create-admin":
            if await store.active_admin_count() > 0:
                print("refusing: an active admin already exists; use the API (or issue-key) instead", file=sys.stderr)
                return 2
            if any(u["name"] == args.name for u in users):
                print(f"refusing: a user named {args.name!r} already exists", file=sys.stderr)
                return 2
            user = await store.create_user(args.name, "admin")
            key, _ = await store.create_key(user["id"], "bootstrap")
            await store.audit(event="admin.bootstrap", principal=None, success=True, detail={"target_user": user["id"], "key_id": key.key_id})
        else:
            user = next((u for u in users if u["name"] == args.name), None)
            if user is None:
                print(f"no user named {args.name!r}", file=sys.stderr)
                return 2
            key, _ = await store.create_key(user["id"], args.key_name)
            await store.audit(event="admin.key.break_glass", principal=None, success=True, detail={"target_user": user["id"], "key_id": key.key_id})
        print(f"user_id: {user['id']}\nrole:    {user['role']}\napi_key: {key.raw}\n"
              "Store this key now; it cannot be shown again.")
        return 0
    finally:
        await engine.dispose()


def main() -> None:
    ap = argparse.ArgumentParser(prog="gateway.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("create-admin")
    a.add_argument("--name", required=True)
    k = sub.add_parser("issue-key")
    k.add_argument("--name", required=True)
    k.add_argument("--key-name", default="break-glass")
    sys.exit(asyncio.run(_run(ap.parse_args())))


if __name__ == "__main__":
    main()
