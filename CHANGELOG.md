# Changelog

## 0.1.0 — 2026-09-04

Released as **Board of Directors**.

First release.

### The board
- Independent answers from models in **disjoint families** — at most one seat per company,
  because a jury of one family is one model with a stutter.
- **Blind peer ranking**: members see each other as "Member A", "Member B", so a name on an
  answer cannot move a ranking. The mapping is kept for the audit trail.
- A **chair that did not vote** synthesises the decision, and falls back to another model if
  the first refuses rather than discarding answers that were already given.
- **A member who failed is not a member who agreed.** Failures are counted and named; too few
  answers returns NO QUORUM instead of a confident-looking result from the survivors.
- Two kinds of question: **decide** (a jury) and **make** (a competition — every member
  attempts the task, the ranking judges whether they actually did it, the chair delivers the
  winning attempt).

### The console
- Runs on `127.0.0.1` from your own machine, so it can hold your key without publishing it.
- One conversation, two modes: a single model at one request a turn, switching to the board
  on any turn and back again. Only the chair's verdict enters the history.
- The cost of the next turn is shown before you send it.
- Live free-model list, sortable by coding or thinking benchmark, with the models that are
  free-priced but not board material filtered out.

### Reading code
- Point it at a folder; it packs the files into one message and asks one model or the board.
  A **loader, not a harness** — you choose what it reads.
- Every file is scanned for secrets first. A folder with findings is refused, with an explicit
  per-send override.

### Honesty about limits
- Free-tier allowance is read from the account (`is_free_tier`) rather than asked of the user.
- The request counter says **estimated** until a 429 hands over the real number, then keeps
  subtracting from it.
- Models the catalogue calls free but the API refuses are remembered and not picked again.
- The daily limit is account-wide: more models buys independence, not headroom. Said plainly.

### Under it
- Standard library only. 59 tests, no network. Ruff clean. CI on Ubuntu and macOS across
  Python 3.10, 3.12 and 3.13.
