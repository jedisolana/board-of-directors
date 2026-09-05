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

### Watching it happen
- The board **streams**: seats fill one at a time, each taking the colour of the vote it
  declared, with a live line saying who is being asked and what stage the session is at.
- A broken display can never fail a session.

### The vote
- Members declare **for / against / conditional**; the board counts it and shows one dot per
  seat, the totals, and whether the motion **carried**.
- Read from what was declared, never inferred from prose. Undeclared is shown as undeclared.
- A tie reads as **split**, not as a decision.
- The chair is handed the count instead of recounting five essays.
- The tally persists in saved sessions and in exported minutes.

### Paid models
- Every model on OpenRouter can hold a seat, each with its price on the row. **Off by
  default**; the permission and the individual send must *both* allow it.
- The **estimate is shown before you send**, rounds up, and a spend cap refuses anything over
  it. Unpriced models are refused rather than costed at zero.
- The chair follows the members' permission, so it can never turn a free session paid.
- Your balance is read from OpenRouter with an ordinary key.
- Routers (`openrouter/*`) can never hold a seat: two of them can pick the same underlying
  model and one-seat-per-family would guarantee nothing.

### The board
- **Six seats by default**, with a picker that FILLS the board rather than asking you to tick
  checkboxes, and an **edit** panel for choosing who sits in each one. The ceiling is the number of companies available,
  since one seat per family means there is no honest board bigger than that.

### Three model tiers
- A **search box**, because 400 models in a scroller is not a way to find one.
- Every paid model shows its price, plus what one call costs in cents, and a **cheapest** sort.
- **free only · paid only · both.** Paid-only is a real want: somebody paying for quality may
  not want free models on the board at all.
- An unrecognised tier falls back to free — a typo must not silently widen what may be spent.

### Locking spending off
- A `$0.00` cap makes spending **impossible**, not merely unselected — it overrules the paid
  toggle and any saved board, because someone who locked spending has said so about their
  money, not about a checkbox.
- For anyone who bought the $10 purely for the 20× rate limit: the balance is a key, not a
  wallet, and the header says so.

### Honest metering
- Optional **management key** makes the request meter exact, reading OpenRouter's own
  analytics instead of counting locally. Opt-in, stored separately, and only ever sent to the
  analytics endpoint to read.
- Retries no longer count as separate requests, and a 429 from an upstream **provider** is
  told apart from OpenRouter's own limit — it costs nothing and does not move the meter.
- `board reset-count` (and a button) starts the meter clean, because a count that was already
  wrong is not repaired by counting correctly afterwards.
- The ledger is never clamped; going past the allowance is said in words, not hidden.

### An OpenAI-compatible endpoint
- `POST /v1/chat/completions` with `model: "board"` — any OpenAI client gets a whole board's
  decision in the shape it already parses. `board:make`, `board:3` select the shape.
- `usage` reports **requests**, not guessed tokens. A `board` object carries every member's
  answer, the tally, and who failed. No quorum is a `409`, not a confident `200`.

### Local server hardening
- Cross-site `Origin` and non-loopback `Host` headers are refused: loopback keeps the network
  out, but not a web page you visit, and not DNS rebinding.

### Writing changes
- Describe a task and the board **writes the fix**, returned as a diff per file with an apply
  button. Nothing is written until you press it, one file at a time.
- Path traversal, files the board never saw, and files that moved since the scan are all
  refused. The previous contents are kept before every write.

### Sessions
- Conversations are saved locally and reopen with the **whole proceeding** — every member's
  answer, every failure and its reason, the chair — not just the verdict.
- **Export** a session as markdown for a pull request or a decision log.
- Renaming the project carries an existing install's key, tier and call count across.

### Keeping the key out
- The key lives outside the repo by construction — there is no path by which committing the
  project commits your credentials.
- A **pre-commit hook** reads staged content and refuses a commit carrying anything
  credential-shaped.

### Documentation
- `README.md` is the product; `docs/library.md` is the Python API.
- Score tabs say how many models they can rank, and separate scored from unscored — the
  published scores are sparse and they change.

### Under it
- Standard library only. 102 tests, no network. Ruff clean. CI on Ubuntu and macOS across
  Python 3.10, 3.12 and 3.13.
