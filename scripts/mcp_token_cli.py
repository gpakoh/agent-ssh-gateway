#!/usr/bin/env python3
"""MCP token management CLI.

Manage bearer tokens for the MCP Gateway fleet. Tokens are stored as
SHA-256 hashes (never raw) in a JSON store file.

Usage:
    mcp-token create <name> [--profile PROFILE] [--output text|json]
    mcp-token list [--profile PROFILE] [--output text|json]
    mcp-token revoke <id>
    mcp-token rotate <id> [--output text|json]
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time

from examples.mcp_server.oauth_provider import hash_token
from examples.mcp_server.token_store import StoredTokenEntry, TokenStore
from examples.mcp_server.tool_scopes import ACCESS_PROFILES, get_profile_scopes


def _default_profile() -> str:
    return os.environ.get("MCP_PUBLIC_TOKEN_PROFILE", "full")


def _generate_token_id() -> str:
    ts = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    rand = secrets.token_hex(4)
    return f"mcp_tok_{ts}_{rand}"


def _generate_raw_token() -> str:
    return "mcp_" + secrets.token_urlsafe(32)


def _output_text(entries: list[dict], verb: str) -> None:
    for e in entries:
        print(f"ID:         {e['id']}")
        print(f"Name:       {e['name']}")
        print(f"Profile:    {e['profile']}")
        print(f"Scopes:     {', '.join(e['scopes'])}")
        print(f"Created:    {e['created_at']}")
        if e.get("token"):
            print(f"Token:      {e['token']}")
        if e.get("token_hash"):
            print(f"Hash:       {e['token_hash']}")
        if e.get("revoked_at"):
            print(f"Revoked:    {e['revoked_at']}")
        if e.get("expires_at"):
            print(f"Expires:    {e['expires_at']}")
        if e.get("last_used_at"):
            print(f"Last used:  {e['last_used_at']}")
        print()


def _handle_create(args: argparse.Namespace) -> int:
    store = TokenStore()
    raw_token = _generate_raw_token()
    token_hash = hash_token(raw_token)
    token_id = _generate_token_id()
    profile = args.profile or _default_profile()
    scopes = get_profile_scopes(profile)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    entry = StoredTokenEntry(
        id=token_id,
        token_hash=token_hash,
        name=args.name,
        profile=profile,
        scopes=scopes,
        created_at=now,
    )
    store.add(entry)

    result = {
        "id": token_id,
        "name": entry.name,
        "profile": entry.profile,
        "scopes": entry.scopes,
        "token": raw_token,
        "token_hash": entry.token_hash,
        "created_at": entry.created_at,
    }

    if args.output == "json":
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        _output_text([result], "create")
    return 0


def _handle_list(args: argparse.Namespace) -> int:
    store = TokenStore()
    entries = store.load()
    profile_filter = args.profile

    if profile_filter:
        entries = [e for e in entries if e.profile == profile_filter]

    if not entries:
        if args.output == "json":
            print("[]")
        else:
            print("No tokens found.")
        return 0

    dicts = []
    for e in entries:
        d = {
            "id": e.id,
            "name": e.name,
            "profile": e.profile,
            "scopes": e.scopes,
            "created_at": e.created_at,
            "revoked_at": e.revoked_at,
            "expires_at": e.expires_at,
            "last_used_at": e.last_used_at,
        }
        dicts.append(d)

    if args.output == "json":
        print(json.dumps(dicts, indent=2, ensure_ascii=False))
    else:
        _output_text(dicts, "list")
    return 0


def _handle_revoke(args: argparse.Namespace) -> int:
    store = TokenStore()
    result = store.revoke(args.id)
    if result is None:
        print(f"Error: token '{args.id}' not found or already revoked.", file=sys.stderr)
        return 1
    print(f"Token '{args.id}' revoked.")
    return 0


def _handle_rotate(args: argparse.Namespace) -> int:
    store = TokenStore()
    entries = store.load()
    old_entry = next((e for e in entries if e.id == args.id), None)
    if old_entry is None:
        print(f"Error: token '{args.id}' not found.", file=sys.stderr)
        return 1
    if old_entry.revoked_at is not None:
        print(f"Error: token '{args.id}' is already revoked.", file=sys.stderr)
        return 1

    raw_token = _generate_raw_token()
    token_hash = hash_token(raw_token)
    token_id = _generate_token_id()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    new_entry = StoredTokenEntry(
        id=token_id,
        token_hash=token_hash,
        name=old_entry.name,
        profile=old_entry.profile,
        scopes=old_entry.scopes,
        created_at=now,
    )
    store.revoke(args.id)
    store.add(new_entry)

    result = {
        "id": token_id,
        "name": new_entry.name,
        "profile": new_entry.profile,
        "scopes": new_entry.scopes,
        "token": raw_token,
        "token_hash": new_entry.token_hash,
        "created_at": new_entry.created_at,
        "previous_id": args.id,
    }

    if args.output == "json":
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        _output_text([result], "rotate")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-token",
        description="Manage MCP Gateway bearer tokens.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="Create a new bearer token")
    p_create.add_argument("name", help="Human-readable token name")
    p_create.add_argument("--profile", default=None, choices=list(ACCESS_PROFILES), help="Access profile (default: $MCP_PUBLIC_TOKEN_PROFILE or 'full')")
    p_create.add_argument("--output", default="text", choices=["text", "json"], help="Output format")

    p_list = sub.add_parser("list", help="List stored tokens")
    p_list.add_argument("--profile", default=None, choices=list(ACCESS_PROFILES), help="Filter by profile")
    p_list.add_argument("--output", default="text", choices=["text", "json"], help="Output format")

    p_revoke = sub.add_parser("revoke", help="Revoke a token by ID")
    p_revoke.add_argument("id", help="Token ID to revoke")

    p_rotate = sub.add_parser("rotate", help="Rotate a token (revoke old, create new)")
    p_rotate.add_argument("id", help="Token ID to rotate")
    p_rotate.add_argument("--output", default="text", choices=["text", "json"], help="Output format")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    handlers = {
        "create": _handle_create,
        "list": _handle_list,
        "revoke": _handle_revoke,
        "rotate": _handle_rotate,
    }
    handler = handlers.get(args.command)
    if handler is None:
        parser.print_help()
        return 1
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
