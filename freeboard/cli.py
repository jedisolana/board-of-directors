"""freeboard -- a board of directors made of free models."""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import board, budget, catalogue, redact, seats
from .transport import OfflineTransport, OpenRouterTransport


def _catalogue(args) -> dict:
    c = catalogue.load(live=not args.offline)
    print(f"  catalogue: {len(c['models'])} free model(s), {c['origin']}, captured {c['captured']}",
          file=sys.stderr)
    return c


def cmd_board(args) -> int:
    c = _catalogue(args)
    members = seats.seat(c["models"], size=args.size)
    print(f"\n  {len(members)} seat(s), one per family:\n")
    for m in members:
        print(f"    {m['id']:<48} {m['family']:<16} ctx {m['context_length']:>9}"
              f"  {'json' if catalogue.speaks_json(m) else 'prose-only'}")
    try:
        seats.quorum(members, args.minimum)
    except seats.NoQuorum as e:
        print(f"\n  NO QUORUM: {e}")
        return 1
    ch = seats.chair(c["models"], members)
    print(f"\n    chair (does not vote): {ch['id']}  ctx {ch['context_length']}")
    b = budget.sessions(budget.Budget(args.credits), members=len(members), peer_review=True)
    print(f"\n    one session = {b['requests_per_session']} requests"
          f"  ->  {b['sessions_per_day']} session(s)/day on this account")
    return 0


def cmd_budget(args) -> int:
    u = budget.upgrade(members=args.size, peer_review=not args.no_peer_review)
    per = u["before"]["requests_per_session"]
    print(f"\n  A {args.size}-member board"
          f"{' with blind peer review' if not args.no_peer_review else ''} plus a chair"
          f" = {per} requests per session.\n")
    print(f"    OpenRouter free tier: {budget.RPM} requests/minute, and per day:\n")
    print(f"      under ${budget.CREDIT_THRESHOLD_USD} of credits ever purchased"
          f"   {budget.RPD_WITHOUT_CREDITS:>5} req/day   ->"
          f" {u['before']['sessions_per_day']:>4} board session(s)/day")
    print(f"      ${budget.CREDIT_THRESHOLD_USD}+ of credits ever purchased"
          f"       {budget.RPD_WITH_CREDITS:>5} req/day   ->"
          f" {u['after']['sessions_per_day']:>4} board session(s)/day")
    print(f"\n    ${u['cost_usd']} one time, all-time threshold -> {u['multiplier']}x the daily capacity.")
    print(f"    Pace to stay under {budget.RPM}/min: one session every "
          f"{budget.pace(args.size, not args.no_peer_review)}s.")
    if u["before"]["burst_exceeds_rpm"]:
        print("    WARNING: a single session of this size exceeds the per-minute limit on its own.")
    print("\n    Note: the daily limit is account-wide across free models. Seating more models\n"
          "    buys independence and routes around a slow provider -- it does not raise the ceiling.\n")
    return 0


def cmd_ask(args) -> int:
    c = _catalogue(args)
    key = os.environ.get("OPENROUTER_API_KEY")
    if args.offline or not key:
        if not args.offline:
            print("  no OPENROUTER_API_KEY -- running offline with stub members\n", file=sys.stderr)
        transport = OfflineTransport()
    else:
        transport = OpenRouterTransport(key, app_title="freeboard")
    try:
        s = board.ask(args.question, transport=transport, models=c["models"], size=args.size,
                      minimum=args.minimum, peer_review=not args.no_peer_review)
    except redact.Refused as e:
        print("\n  REFUSED TO SEND -- the question contains:", file=sys.stderr)
        for f in e.findings:
            print(f"    {f}", file=sys.stderr)
        print("\n  Nothing left this machine. Remove it and ask again.", file=sys.stderr)
        return 2
    except seats.NoQuorum as e:
        print(f"\n  NO QUORUM: {e}", file=sys.stderr)
        return 1
    print()
    print(s.report())
    return 1 if s.no_quorum else 0


def cmd_check(args) -> int:
    found = redact.scan(args.text)
    if not found:
        print("  clean -- nothing in the outbound seam's rules matched.")
        return 0
    print(f"  {len(found)} finding(s) -- this would be REFUSED:")
    for f in found:
        print(f"    {f}")
    return 2


def cmd_refresh(args) -> int:
    c = catalogue.fetch()
    with open(catalogue.SNAPSHOT, "w") as f:
        json.dump(c, f, indent=2)
    print(f"  {len(c['models'])} free model(s) of {c['total_models_seen']} -> {catalogue.SNAPSHOT}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="freeboard", description=__doc__)
    p.add_argument("--offline", action="store_true", help="use the bundled snapshot and stub members")
    p.add_argument("--size", type=int, default=5, help="how many voting members (default 5)")
    p.add_argument("--minimum", type=int, default=3, help="members needed for a quorum (default 3)")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("ask", help="put a question to the board")
    a.add_argument("question")
    a.add_argument("--no-peer-review", action="store_true", help="skip the blind ranking round")
    a.set_defaults(fn=cmd_ask)

    b = sub.add_parser("board", help="show who would be seated, and why")
    b.add_argument("--credits", type=float, default=0.0, help="credits purchased on the account, all time")
    b.set_defaults(fn=cmd_board)

    g = sub.add_parser("budget", help="how many sessions the free tier gives you")
    g.add_argument("--no-peer-review", action="store_true")
    g.set_defaults(fn=cmd_budget)

    k = sub.add_parser("check", help="test the outbound seam against some text")
    k.add_argument("text")
    k.set_defaults(fn=cmd_check)

    r = sub.add_parser("refresh", help="re-read the live free-model catalogue into the snapshot")
    r.set_defaults(fn=cmd_refresh)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
