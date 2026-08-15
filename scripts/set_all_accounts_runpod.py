"""Batch-update accounts to RunPod provider settings.

Usage:
    python scripts/set_all_accounts_runpod.py --help

Environment variables:
    RUNPOD_AI_BASE_URL
        Required if --base-url is omitted.
    RUNPOD_AI_MODEL
        Required if --model is omitted.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from urllib.parse import urlparse

from app.account import validate_provider_url
from app.config import load_settings
from app.memory import SqliteMemoryStore


def _clean_text(value: str | None) -> str:
    return (value or "").strip()


def _is_openrouter_base(value: str) -> bool:
    host = (urlparse(value).hostname or "").lower()
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


def _is_runpod_base(value: str) -> bool:
    host = (urlparse(value).hostname or "").lower()
    return (
        host == "runpod.net"
        or host.endswith(".runpod.net")
        or host == "runpod.ai"
        or host.endswith(".runpod.ai")
        or host == "proxy.runpod.net"
        or host.endswith(".proxy.runpod.net")
    )


async def _run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base-url",
        default="",
        help=(
            "RunPod OpenAI-compatible base URL, e.g. "
            "https://api.runpod.ai/v2/<endpoint>/openai/v1"
            " or https://<pod-id>-<port>.proxy.runpod.net/openai/v1"
        ),
    )
    parser.add_argument(
        "--model",
        default="",
        help="RunPod model ID, e.g. deepseek-ai/DeepSeek-V2.5",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the target update plan without writing to DB",
    )
    parser.add_argument(
        "--db",
        default="",
        help="Optional memory DB path (defaults to MEMORY_DB_PATH)",
    )
    parser.add_argument(
        "--all-accounts",
        action="store_true",
        help="Also migrate non-OpenRouter accounts",
    )
    args = parser.parse_args(argv)

    settings = load_settings()

    base_url = _clean_text(args.base_url) or settings.runpod_ai_base_url
    if not base_url:
        raise SystemExit("set_all_accounts_runpod.py: base_url is required")
    model = _clean_text(args.model) or settings.runpod_ai_model
    if not model:
        raise SystemExit("set_all_accounts_runpod.py: model is required")
    if not _is_runpod_base(base_url):
        raise SystemExit(
            "set_all_accounts_runpod.py: base_url must point to "
            "*.runpod.ai, *.runpod.net, or *.proxy.runpod.net"
        )

    validate_provider_url(base_url)
    store = SqliteMemoryStore(args.db or settings.memory_db_path)
    await store.open()

    try:
        accounts = await store.list_accounts()
        selected = [
            account
            for account in accounts
            if args.all_accounts or _is_openrouter_base(account.ai_base_url)
        ]
        changed = [
            account
            for account in selected
            if account.ai_base_url != base_url or account.ai_model != model
        ]

        if not selected:
            print("No matching accounts found")
            return 0

        if args.dry_run:
            print(
                "Dry-run summary:\n"
                + "\n".join(
                    f"{account.id}: {account.ai_base_url} -> {base_url}, "
                    f"{account.ai_model} -> {model}"
                    for account in changed
                )
            )
            if not changed:
                print("No account needs updates")
            return 0

        migrated = 0
        for account in changed:
            updated = account.with_updates(
                ai_base_url=base_url,
                ai_model=model,
            )
            await store.update_account(
                updated,
                expected_revision=account.revision,
                changed_fields=["ai_base_url", "ai_model"],
            )
            migrated += 1

        print(f"Migrated {migrated} account(s) to RunPod settings")
        return 0
    finally:
        await store.close()


if __name__ == "__main__":
    import asyncio

    raise SystemExit(asyncio.run(_run()))
