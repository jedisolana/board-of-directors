"""Board of Directors -- a board made of other people's free models."""
from __future__ import annotations

import argparse
import getpass
import json
import sys

from . import board, budget, catalogue, config, redact, seats, server, usage
from .transport import OfflineTransport, OpenRouterTransport

DASH = "  " + "-" * 74


def _catalogue(args, quiet: bool = False) -> dict:
    c = catalogue.load(live=not getattr(args, "offline", False))
    if not quiet:
        print(f"  catalogue: {len(c['models'])} free model(s), {c['origin']}, "
              f"read {c['captured']}", file=sys.stderr)
    return c


def _seatable(c: dict) -> list[dict]:
    return catalogue.deliberative(c["models"])


def _chosen(c: dict, args) -> list[dict] | None:
    """The board you picked, if you picked one -- resolved against today's live list."""
    want = config.board()
    if not want:
        return None
    by_id = {m["id"]: m for m in c["models"]}
    members, gone = [], []
    for mid in want:
        (members.append(by_id[mid]) if mid in by_id else gone.append(mid))
    if gone:
        print(f"  note: {len(gone)} saved member(s) are no longer free -- "
              f"{', '.join(gone)}. Run `board pick` again.", file=sys.stderr)
    return members or None


# ----------------------------------------------------------------- setup / status

def cmd_setup(args) -> int:
    print("\n  board of directors -- setup\n" + DASH)
    key, where = config.api_key()
    if key:
        print(f"  A key is already set ({config.mask(key)}, from {where}).")
        if input("  Replace it? [y/N] ").strip().lower() not in ("y", "yes"):
            print("  Kept.")
        else:
            key = None
    if not key:
        print("\n  Paste your OpenRouter API key. Get one at https://openrouter.ai/keys")
        print("  It is not echoed, and it is saved to a 0600 file in ~/.board-of-directors.")
        entered = getpass.getpass("  key: ").strip()
        if not entered:
            print("  Nothing entered -- no key saved. it still runs with --offline.")
        else:
            path = config.set_api_key(entered)
            print(f"  saved {config.mask(entered)} -> {path}")

    print("\n" + DASH)
    print("  Your daily free allowance depends on one thing: whether this account has EVER")
    print(f"  had ${budget.CREDIT_THRESHOLD_USD} or more of credits purchased.\n")
    print(f"      no   ->  {budget.RPD_WITHOUT_CREDITS} free requests a day")
    print(f"      yes  ->  {budget.RPD_WITH_CREDITS} free requests a day (a one-time, all-time threshold)\n")
    print("  Nothing can read this from the API, so it has to be told.")
    answer = input(f"  Has this account ever had ${budget.CREDIT_THRESHOLD_USD}+ put in? [y/N] ")
    config.set_tier(budget.CREDIT_THRESHOLD_USD if answer.strip().lower() in ("y", "yes") else 0.0)
    print(f"  allowance set to {budget.Budget(config.tier()).per_day} requests/day")
    print("\n  Next:  board pick     choose who sits on your board")
    print("         board ask \"...\"  put a question to it\n")
    return 0


def cmd_status(args) -> int:
    key, where = config.api_key()
    s = usage.status()
    print("\n  Board of Directors\n" + DASH)
    print(f"  key         {config.mask(key)}  ({where})")
    print(f"  allowance   {s.allowance} requests/day"
          f"   {'($10+ tier)' if s.qualified else '(no credits ever purchased -- $10 makes this 1000)'}")
    print(f"              {budget.RPM} requests/minute")
    print()
    print(f"  today       {s.calls} call(s) made{f', {s.failed} failed' if s.failed else ''}")
    word = "remaining" if s.measured else "remaining (estimated)"
    print(f"              {s.remaining} {word}   ·   resets in {s.resets_in}")
    if not s.measured:
        print("              estimate: OpenRouter sends no count on successful calls, so this")
        print("              counts our own. Calls made with this key elsewhere are invisible,")
        print("              so the true figure is this or lower. A 429 corrects it exactly.")
    if s.per_model:
        print("\n  by model")
        for mid, n in sorted(s.per_model.items(), key=lambda kv: -kv[1]):
            print(f"      {n:>4}  {mid}")

    saved = config.board()
    print("\n  board       " + (f"{len(saved)} member(s) chosen" if saved else "not chosen -- auto-picked at run time"))
    for mid in saved or []:
        print(f"      {mid}")
    if saved:
        b = budget.sessions(budget.Budget(s.tier_usd), members=len(saved), peer_review=True)
        print(f"\n  one session costs {b['requests_per_session']} requests"
              f"  ->  about {s.remaining // b['requests_per_session']} more session(s) today")
    print()
    return 0


# ----------------------------------------------------------------- picking

def cmd_pick(args) -> int:
    c = _catalogue(args)
    pool = sorted(_seatable(c), key=lambda m: (m["family"], m["id"]))
    print("\n  Free models available right now. Pick your board.\n")
    print(f"  {'#':<4}{'model':<50}{'read':>8}{'write':>8}  {'json':<5}{'family'}")
    print(DASH)
    for i, m in enumerate(pool, 1):
        print(f"  {i:<4}{m['id'][:49]:<50}{_k(m['context_length']):>8}{_k(m['max_completion_tokens']):>8}"
              f"  {'yes' if catalogue.speaks_json(m) else '-':<5}{m['family']}")
    print(DASH)
    print("  One seat per family -- two models from the same company is not a second opinion.")
    print("  Type numbers separated by spaces (e.g. 2 5 9 13), or `auto` to let it choose.\n")
    raw = input("  members: ").strip()

    if raw.lower() in ("auto", ""):
        members = seats.seat(c["models"], size=args.size)
    else:
        try:
            picked = [pool[int(n) - 1] for n in raw.split()]
        except (ValueError, IndexError):
            print("  Didn't understand that. Nothing saved.")
            return 1
        seen, members, dupes = set(), [], []
        for m in picked:
            (dupes if m["family"] in seen else members).append(m)
            seen.add(m["family"])
        if dupes:
            print(f"\n  Dropped {len(dupes)} same-family pick(s): "
                  f"{', '.join(m['id'] for m in dupes)}")
            print("  A board needs disjoint families or it agrees with itself.")

    try:
        seats.quorum(members, args.minimum)
    except seats.NoQuorum as e:
        print(f"\n  {e}\n  Nothing saved -- pick at least {args.minimum} different families.")
        return 1

    config.set_board([m["id"] for m in members])
    print(f"\n  Board saved ({len(members)} members, {len({m['family'] for m in members})} families):")
    for m in members:
        print(f"      {m['id']}")
    ch = seats.chair(c["models"], members)
    print(f"\n  chair, chosen at run time (does not vote): {ch['id']}")
    b = budget.sessions(budget.Budget(config.tier()), members=len(members), peer_review=True)
    print(f"  one session = {b['requests_per_session']} requests -> {b['sessions_per_day']} session(s)/day\n")
    return 0


def _k(n) -> str:
    if not n:
        return "-"
    return f"{n / 1000:.0f}k" if n < 1_000_000 else f"{n / 1_000_000:g}M"


def cmd_board(args) -> int:
    c = _catalogue(args)
    members = _chosen(c, args) or seats.seat(c["models"], size=args.size)
    src = "your saved board" if config.board() else "auto-picked"
    print(f"\n  {len(members)} seat(s), {src}, one per family:\n")
    for m in members:
        print(f"    {m['id']:<50}{m['family']:<16}{_k(m['context_length']):>7}"
              f"  {'json' if catalogue.speaks_json(m) else 'prose-only'}")
    try:
        seats.quorum(members, args.minimum)
    except seats.NoQuorum as e:
        print(f"\n  NO QUORUM: {e}")
        return 1
    ch = seats.chair(c["models"], members)
    print(f"\n    chair (does not vote): {ch['id']}")
    return 0


# ----------------------------------------------------------------- asking

def cmd_ask(args) -> int:
    c = _catalogue(args)
    key, _ = config.api_key()
    if args.offline or not key:
        if not args.offline:
            print("  no API key -- running offline with stub members. `board setup` to fix.\n",
                  file=sys.stderr)
        transport = OfflineTransport()
    else:
        transport = OpenRouterTransport(key, app_title="Board of Directors")

    members = _chosen(c, args)
    models = c["models"]
    if members:
        # a chosen board is honoured exactly: these members, in this order
        models = members + [m for m in c["models"] if m["id"] not in {x["id"] for x in members}]
    try:
        s = board.ask(args.question, transport=transport, models=models,
                      size=len(members) if members else args.size,
                      minimum=args.minimum, peer_review=not args.no_peer_review)
    except redact.Refused as e:
        print("\n  REFUSED TO SEND -- the question contains:", file=sys.stderr)
        for f in e.findings:
            print(f"    {f}", file=sys.stderr)
        print("\n  Nothing left this machine. Remove it and ask again.\n", file=sys.stderr)
        return 2
    except seats.NoQuorum as e:
        print(f"\n  NO QUORUM: {e}", file=sys.stderr)
        return 1
    print()
    print(s.report())
    if not args.offline and key:
        st = usage.status()
        print(f"  {st.calls} call(s) used today, about {st.remaining} left"
              f"{'' if st.measured else ' (estimated)'}\n")
    return 1 if s.no_quorum else 0


# ----------------------------------------------------------------- odds and ends

def cmd_budget(args) -> int:
    saved = config.board()
    size = len(saved) if saved else args.size
    u = budget.upgrade(members=size, peer_review=not args.no_peer_review)
    print(f"\n  A {size}-member board"
          f"{' with blind peer review' if not args.no_peer_review else ''} plus a chair"
          f" = {u['before']['requests_per_session']} requests per session.\n")
    print(f"    OpenRouter free tier: {budget.RPM} requests/minute, and per day:\n")
    print(f"      under ${budget.CREDIT_THRESHOLD_USD} ever purchased "
          f"  {budget.RPD_WITHOUT_CREDITS:>5} req/day  -> {u['before']['sessions_per_day']:>4} session(s)/day")
    print(f"      ${budget.CREDIT_THRESHOLD_USD}+ ever purchased     "
          f"  {budget.RPD_WITH_CREDITS:>5} req/day  -> {u['after']['sessions_per_day']:>4} session(s)/day")
    print(f"\n    ${u['cost_usd']} once, all-time threshold -> {u['multiplier']}x daily capacity.")
    print(f"    Pace to stay under {budget.RPM}/min: one session every "
          f"{budget.pace(size, not args.no_peer_review)}s.")
    if u["before"]["burst_exceeds_rpm"]:
        print("    WARNING: one session of this size busts the per-minute limit on its own.")
    print("\n    The daily limit is account-wide. More models buys independence and routes")
    print("    around a slow provider -- it does not raise the ceiling.\n")
    return 0


def cmd_check(args) -> int:
    found = redact.scan(args.text)
    if not found:
        print("  clean -- nothing in the outbound seam's rules matched.")
        return 0
    print(f"  {len(found)} finding(s) -- this would be REFUSED:")
    for f in found:
        print(f"    {f}")
    return 2


def cmd_reset(args) -> int:
    was = usage.reset_today()
    print(f"\n  discarded today's count: {was.get('calls', 0)} call(s), "
          f"{was.get('failed', 0)} failed, {was.get('provider_busy', 0)} provider-busy")
    print("  the meter now reads `since reset`, not `0 of 50` - clearing our count gives back")
    print("  none of OpenRouter's allowance, and what was already spent cannot be recovered.")
    print("  The true remaining is unknown until the daily limit rolls over.\n")
    return 0


def cmd_refresh(args) -> int:
    c = catalogue.fetch()
    with open(catalogue.SNAPSHOT, "w") as f:
        json.dump(c, f, indent=2)
    free = sum(1 for m in c["models"] if m.get("free"))
    print(f"  {len(c['models'])} model(s), {free} free -> {catalogue.SNAPSHOT}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="board", description=__doc__)
    p.add_argument("--offline", action="store_true",
                   help="bundled catalogue and stub members -- no key, no network")
    p.add_argument("--size", type=int, default=5, help="members when auto-picking (default 5)")
    p.add_argument("--minimum", type=int, default=3, help="members needed for a quorum (default 3)")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("setup", help="enter your API key and set your allowance")
    s.set_defaults(fn=cmd_setup)

    t = sub.add_parser("status", help="key, allowance, calls used today, your board")
    t.set_defaults(fn=cmd_status)

    k = sub.add_parser("pick", help="choose who sits on your board, from today's free list")
    k.set_defaults(fn=cmd_pick)

    a = sub.add_parser("ask", help="put a question to the board")
    a.add_argument("question")
    a.add_argument("--no-peer-review", action="store_true", help="skip the blind ranking round")
    a.set_defaults(fn=cmd_ask)

    b = sub.add_parser("board", help="show who is seated, and the chair")
    b.set_defaults(fn=cmd_board)

    g = sub.add_parser("budget", help="how many sessions your account actually gets")
    g.add_argument("--no-peer-review", action="store_true")
    g.set_defaults(fn=cmd_budget)

    c = sub.add_parser("check", help="test the outbound seam against some text")
    c.add_argument("text")
    c.set_defaults(fn=cmd_check)

    u = sub.add_parser("ui", help="open the console in your browser (local only)")
    u.add_argument("--port", type=int, default=8420)
    u.add_argument("--no-open", action="store_true", help="do not open a browser")
    u.set_defaults(fn=lambda a: server.serve(a.port, not a.no_open))

    z = sub.add_parser("reset-count", help="forget today's request count and start clean")
    z.set_defaults(fn=cmd_reset)

    r = sub.add_parser("refresh", help="re-read the live free-model catalogue")
    r.set_defaults(fn=cmd_refresh)

    args = p.parse_args(argv)
    if not args.cmd:                      # bare `board` -> open the console
        return server.serve()
    rc = args.fn(args)
    if args.cmd == "status":              # a nudge toward whatever is still missing
        if not config.api_key()[0]:
            print("  start here:  board setup   (or just `board` for the console)\n")
        elif not config.board():
            print("  next:        board pick    (or just `board` for the console)\n")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
