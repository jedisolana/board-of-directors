# Using the board from Python

The console is one face on it. Everything it does is available as a library, with no
dependencies beyond the standard library.

```bash
pip install -e .
```

---

## The five-line version

```python
from boardofdirectors import board

session = board.ask("Should we rewrite the parser this quarter?")
print(session.report())
```

That seats a board from the live free catalogue, asks each member independently, has them rank
each other blind, and returns a chair's decision. No key needed to *try* it — without one you
get the offline stub, which answers deterministically so tests and demos cost nothing.

With a key set (`OPENROUTER_API_KEY`, or `board setup`), it runs for real.

---

## What comes back

```python
session.members        # the models that were seated
session.chair_model    # the model that wrote the decision, and did not vote
session.answers        # Answer objects — .model, .text
session.failures       # Failure objects — .model, .reason.  NEVER counted as agreement
session.rankings       # the blind peer rankings
session.labels         # {"Member A": "minimax/minimax-m3:free", ...} — the audit trail
session.tally          # {"FOR": 3, "AGAINST": 1, "DEPENDS": 1, "UNCLEAR": 0,
                       #  "decided": 4, "carried": True, "split": False}
session.decision       # the chair's text, or None
session.no_quorum      # set when the board could not legitimately decide
session.chair_failures # models that refused to chair before one accepted
```

`answers` and `failures` are disjoint and both are real. A member that was rate-limited is in
`failures` with a reason — it is not an empty `Answer`, and it is not a vote either way.

### Reading a vote

```python
board.read_vote(text)    # "FOR" | "AGAINST" | "DEPENDS" | "UNCLEAR"
board.strip_vote(text)   # the reasoning without the marker line
board.tally(answers)     # the count, plus `carried` and `split`
```

`UNCLEAR` means the member did not declare a position. It is never inferred from wording —
guessing a vote from someone's prose is putting words in their mouth and then counting them.

---

## The two kinds

```python
board.ask("Should we adopt Postgres?", kind="decide")   # a jury: positions and reasons
board.ask("Write me a TOML parser", kind="make")        # a competition: attempts, ranked
```

`decide` asks each member for a position, the strongest reason for it, and what would change
their mind. `make` asks each of them to *do the task*; the blind ranking judges whether they
actually did it, and the chair delivers the winning attempt improved with what the others got
right.

`board.looks_like_a_task(q)` guesses which one a question wants. It is a *suggestion* — the
console uses it to offer a switch, never to switch silently.

---

## Watching it happen

```python
def on(ev):
    print(ev["type"], ev.get("model", ""))

board.ask("Ship it?", on_event=on)
```

Events, in order: `seated`, then `asking`/`answer`/`failure` per member, `tally`, `labels`,
`ranking`/`ranked`, `chairing`, and finally `decision` or `no_quorum`.

Every callback is wrapped. **A broken listener cannot fail a session** — there is a test that
raises from the listener and asserts the board still returns its decision.

---

## Continuing a conversation

```python
prior = [{"role": "user", "content": "we are choosing a database"},
         {"role": "assistant", "content": "postgres is the safe default"}]

session = board.ask_in_context("should we switch to sqlite?", prior=prior)
```

Every member reads the thread before answering. Only the chair's verdict is meant to go back
into the history — putting all five raw answers in would multiply what each later turn has to
re-read, and price a long conversation off the free tier.

---

## Choosing the board yourself

```python
from boardofdirectors import catalogue, seats

models  = catalogue.load()["models"]          # live, falls back to the bundled snapshot
usable  = catalogue.deliberative(models)      # free, text-capable, not routers or guardrails
members = seats.seat(models, size=5)          # at most ONE per family
chair   = seats.chair(models, members)        # never one of the members

seats.quorum(members, minimum=3)              # raises NoQuorum if too few families

board.ask("...", members=members)             # used exactly as given, never re-seated
```

`seat()` enforces one seat per family. Ask for more seats than there are families and you get
fewer seats, not duplicates — three checkpoints of one family is one model with a stutter, not
a jury.

Pass `members=` and they are used exactly as chosen. (This is not decoration: an earlier
version re-seated unconditionally and handed the caller back the list it had *asked* for, so
the substitution was invisible from both the API and the screen.)

### What the catalogue knows

```python
m["free"]                   # bool
m["price_in"], m["price_out"]   # USD per MILLION tokens, or None when unpriced
m["context_length"]         # what it can read
m["max_completion_tokens"]  # what it can WRITE — a different, smaller number
m["score"]                  # {"coding": .., "thinking": .., "agentic": ..} or Nones
m["supported_parameters"]   # free variants have things REMOVED, not just a zero price

catalogue.speaks_json(m)    # can it return a vote as data rather than prose
catalogue.fits(m, prompt_tokens, completion_tokens)   # the asymmetric check
catalogue.rank(models, "coding")                      # unmeasured sorts last, never zeroed
```

---

## Paid models

Off unless asked for, at every level:

```python
seats.seat(models, size=5, allow_paid=True)
board.ask("...", allow_paid=True)
```

```python
from boardofdirectors import cost

est = cost.session(members, chair, peer_review=True)
est.usd          # rounds UP to the cent
est.human()      # "free" | "under $0.01" | "about $1.54"
est.per_model    # (("openai/o1-pro", 2.64), ...)

cost.over_cap(est, 0.25)          # a cap is a wall, not a warning
```

`cost.Unpriced` is raised for a model with no published price. Unknown is not free, and
unknown cannot be consented to.

---

## Calling it from anything, not just Python

The server speaks the one dialect every LLM tool already speaks. Point any OpenAI-compatible
client at it and ask for the model `board`:

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8420/v1", api_key="unused")
r = client.chat.completions.create(
    model="board",
    messages=[{"role": "user", "content": "Should we use Postgres or SQLite?"}],
)
print(r.choices[0].message.content)      # the chair's decision
```

```bash
curl http://127.0.0.1:8420/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "board:3", "messages": [{"role": "user", "content": "Ship it?"}]}'
```

The **model name selects the shape**, not a model:

| | |
|---|---|
| `board` | a jury — positions, blind ranking, a chair's decision |
| `board:make` | a competition — attempts, ranked, the best one delivered |
| `board:3` | three seats instead of five |
| `board:make:4` | both |
| any model id | passed straight through to that one model |

`GET /v1/models` lists the boards first, then every seatable model, so a client's model picker
shows them.

### What it does not pretend

A board is not a chat completion, and the differences are reported rather than flattened.

**`usage` carries `requests`, not guessed tokens.** A guess in that field would be read as a
measurement. A board's real cost is the number of calls it made.

**A `board` object rides alongside the completion** with every member's answer, their votes,
the tally, the chair, and — separately — anyone who failed. A client that ignores it gets a
normal-looking completion; one that reads it can see the vote, and can see that the decision
came from four models rather than five.

**No quorum is a `409`**, with the answers still attached, rather than a confident-looking
`200` from whoever got through.

### What "no auth" actually means

It binds to `127.0.0.1`, so nothing on your network — or the internet — can reach it.

But **loopback keeps the network out, not the browser.** Two things it does not stop on its
own, both of which are now blocked:

**Cross-site requests.** A page you merely visit can POST to `127.0.0.1` from your browser. It
cannot *read* the reply without CORS, and none is sent — but a fire-and-forget POST is enough
to spend your balance or write a file, and not reading the answer costs the attacker nothing.
Any request carrying a cross-site `Origin` is refused with a `403`.

**DNS rebinding.** A hostname the attacker controls, re-pointed at `127.0.0.1`, makes their
page same-origin with this server and defeats an Origin check by itself. So the `Host` header
must also be a loopback name.

**What neither closes:** another *program* on your machine, running as you, can still reach
it. Nothing short of a token fixes that, and a token stored next to the key it protects buys
less than it looks like. If you need it, the honest answer today is: do not run the server
while running code you do not trust.

**And do not put it on a network as it stands.** Anyone who reaches the socket can spend your
OpenRouter balance and write to the folders you have scanned.

## Writing your own transport

```python
class MyTransport:
    def ask(self, model, messages, **kw):
        ...
        return Answer(model["id"], text)      # or Failure(model["id"], reason)
```

Anything with that method works. `OfflineTransport(fail={"a/b:free"})` is the built-in stub and
takes a set of ids to fail, which is how the failure paths get tested without a network.

`OpenRouterTransport` handles the parts that matter: it honours `Retry-After`, tells a
platform 429 apart from a provider one, catches the mid-stream 429 that arrives *after* a
`200`, and sends only the parameters a given model actually supports — OpenRouter silently
drops the rest, so asking for JSON from a model without `response_format` returns prose and a
`200`.

---

## The outbound seam

```python
from boardofdirectors import redact

redact.check(text)     # returns the text, or raises Refused
redact.scan(text)      # the findings, each with the secret masked
```

It **refuses**; it does not scrub. A scrubber that quietly rewrites your prompt is worse than a
wall — you never learn you nearly sent a key. `board.ask` runs it before anything leaves the
machine.

---

## Reading a folder of code

```python
from boardofdirectors import codebase

scan = codebase.scan("~/Desktop/myproject")
len(scan.files), scan.tokens, scan.clean, scan.findings

msg = codebase.audit_message(scan, budget_tokens=150_000)
board.ask_in_context(msg, members=members)
```

A loader, not a harness: you choose what it reads, and the model never decides what to open
next. Files that do not fit are **named in the message** — a model told it is seeing part of a
codebase reasons differently from one that believes it saw all of it.

---

## Sessions

```python
from boardofdirectors import sessions

sid = sessions.new_id()
sessions.save(sid, turns, {"requests": 11})
sessions.listing()                       # newest first
print(sessions.as_markdown(sessions.load(sid)))   # minutes, with the dissent in them
```

---

## Where things are stored

`~/.board-of-directors/` — key, chosen board, call ledger, saved sessions. All `0600`, none of
it inside the repo. Override with `BOARD_HOME`, which also disables the migration from an
earlier install: setting it is a request for *that* directory, not an invitation to import
someone else's state.

## Where it looks for projects

The **audit code** panel offers folders it finds under `~/Desktop`, `~/Documents` and `~`.
Override with `BOARD_PROJECT_ROOTS`, colon-separated:

```bash
BOARD_PROJECT_ROOTS=~/src:~/work board
```

Useful if your code lives elsewhere — and the only way to screenshot that panel without
publishing a list of somebody's private work, which is how it came to exist.
