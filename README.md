# freeboard

**A board of directors made of free models.**

Ask a question once, and several models from *different families* answer it independently,
rank each other blind, and a chair that didn't vote writes the decision. It runs on
OpenRouter's free tier, so the whole thing costs nothing to try.

```bash
python3 -m freeboard.cli --offline ask "Should we rewrite the parser this quarter?"
```

That works with no API key and no network — offline mode seats a real board from the bundled
catalogue and answers with stubs, so you can see the machinery before you spend a request.
Set `OPENROUTER_API_KEY` and drop `--offline` to run it for real.

---

## Why a board instead of one good model

Because a single model agrees with itself. Ask it twice and you get its opinion twice, which
looks like confirmation and isn't.

The research this is built on ([Verga et al., *Replacing Judges with
Juries*](https://arxiv.org/abs/2404.18796)) found that a panel of several *smaller* models
beats one big judge — and the reason is that the panel is drawn from **disjoint model
families**. That word is doing all the work. Seat three checkpoints of the same family and you
haven't built a jury, you've built one model with a stutter.

So freeboard enforces it: **at most one seat per family.** Ask for more seats than there are
families and you get fewer seats and a reason — never a padded board.

```
$ python3 -m freeboard.cli --offline board

  4 seat(s), one per family:

    minimax/minimax-m3:free                    minimax        ctx  1048576  json
    dots-studio/dots-3-note-preview:free       dots-studio    ctx   512000  json
    google/gemma-4-26b-a4b-it:free             google         ctx   262144  json
    nvidia/nemotron-3-super-120b-a12b:free     nvidia         ctx   262144  json

    chair (does not vote): thinkingmachines/inkling-small:free
```

---

## The rules it enforces

**One seat per family.** Independence is structural, not a prompt you write.

**Members answer alone.** In round one no member sees another's answer. The moment one reads
another you have one opinion and some agreement, and agreement is not evidence. There's a test
for this.

**The ranking round is blind.** Members are shown the other answers as "Member A", "Member B" —
identities stripped, because a name on an answer moves a ranking. The mapping is kept so you
can audit who was ranked where, and revealed only afterwards. There's a test that no model id
ever leaks into a ranking prompt.

**The chair does not vote.** A member who also counts the votes is not a chair.

**A member who failed is not a member who agreed.** This is the one that matters most.

> When a model is rate limited it doesn't answer. If the board reads "no answer" as "no
> objection", the vote still completes, still prints a tidy consensus, and is now a decision
> made by whoever happened not to be throttled. **The board looks more confident exactly when
> it knows less.**

So every call returns an `Answer` or a `Failure`, never an empty `Answer`. Failures are counted
and named in the report, and if too few members got through, the session returns **NO QUORUM**
instead of a confident-looking answer from the survivors.

**Nothing leaves the machine without passing the seam.** A question pasted out of a terminal
carries whatever was on the terminal. `freeboard.redact` refuses — it does not scrub — on API
keys, private keys, JWTs, bearer headers, private and Tailscale addresses, `.ssh` paths, `.env`
files, and home-directory paths. A scrubber that quietly rewrites your prompt is worse than a
wall: you never learn you nearly sent a key. Refusals name the finding with the secret masked.

```
$ python3 -m freeboard.cli check "deploy with sk-or-v1-0123456789abcdef0123"
  1 finding(s) -- this would be REFUSED:
    an OpenRouter API key (openrouter key): sk-o...0123
```

---

## What the free tier actually gives you

These numbers are from [OpenRouter's rate-limit
docs](https://openrouter.ai/docs/api-reference/limits), read 2026-09-04:

| | requests/day |
|---|---|
| under $10 of credits ever purchased | **50** |
| $10+ of credits ever purchased | **1000** |

Plus **20 requests per minute**, uniformly.

The $10 is a **one-time, all-time threshold** — not a balance you burn down. It moves which row
of the table you're on, permanently. **That's 20× your daily free capacity for ten dollars,**
and it's the largest single lever on the platform.

In boards rather than requests:

```
$ python3 -m freeboard.cli budget

  A 5-member board with blind peer review plus a chair = 11 requests per session.

      under $10 of credits ever purchased      50 req/day   ->    4 board session(s)/day
      $10+ of credits ever purchased         1000 req/day   ->   90 board session(s)/day

    Pace to stay under 20/min: one session every 33.0s.
```

**One thing to be honest about:** the daily limit is account-wide across free models. Seating
more models buys you independence and routes around a slow provider — **it does not raise the
ceiling.** If you've read that spreading a board across many free models multiplies your free
capacity, that's wrong, and this library won't tell you it.

(OpenRouter's docs give the daily figure as "your free model rate limit", keyed on credits
purchased by the account, so it reads as account-wide — they don't say so in as many words.
`budget.py` assumes account-wide, which is the conservative reading: if you assume per-model
and you're wrong, you plan a board you can't run.)

---

## Free models come and go

Every hard-coded free-model list rots. `catalogue.py` reads the live catalogue from
OpenRouter's public models endpoint (no key needed) and falls back to the bundled snapshot in
`data/` only when the network fails — and it always tells you which one it used and how old it
is, because a model that stopped being free yesterday will bill you.

```bash
python3 -m freeboard.cli refresh    # re-read the live catalogue into the snapshot
```

It also filters the free list down to models that can actually hold a seat. Some rows are
priced at zero but aren't board material — a router that hides which member answered, a
guardrail classifier, a music model. Seating those looks like a working board that never
deliberates.

---

## Two more traps the catalogue hides

**Context and completion limits are asymmetric.** A model with room for your prompt may still
refuse your output length. Checking only the context window is the common bug; `catalogue.fits`
checks both.

**Free variants are not the paid model with a zero price.** They're the paid model with
*parameters removed* — most importantly `response_format` and `structured_outputs`, which is
how you get a vote back as data instead of prose. And OpenRouter **silently drops** a parameter
the model doesn't support rather than erroring. So you ask for JSON, you get an essay, and you
get a `200`. `transport.py` only sends what the catalogue says the model supports.

---

## Install and use

No dependencies. Python 3.10+, standard library only.

```bash
git clone <this repo> && cd freeboard
python3 -m unittest discover -s tests      # 27 tests, no network
```

```python
from freeboard import board

session = board.ask("Should we rewrite the parser this quarter?")
print(session.report())

print(session.voted)         # members who actually answered
print(session.failures)      # members who did not -- never counted as agreement
print(session.no_quorum)     # set if the board could not legitimately decide
print(session.labels)        # "Member A" -> model id, the audit trail
```

| command | what it does |
|---|---|
| `ask "<question>"` | run a full session |
| `board` | show who'd be seated, and the chair |
| `budget` | how many sessions your account actually gets |
| `check "<text>"` | test the outbound seam without sending anything |
| `refresh` | re-read the live free-model catalogue |

---

## Where this came from

A research swarm was pointed at OpenRouter for four days and produced 280 attack-survived
findings about it. This library is the buildable half of them.

It also came out of a mistake worth repeating. Four of those findings stated the free-tier
rate limit like this:

> *"If you have purchased at least ⟨ ⟩ credits, the free models will be limited to ⟨ ⟩
> requests per day."*

The numbers were gone. OpenRouter's docs page writes them in JavaScript, the scraper captured
the version where the slots were still empty, and the sentence stayed grammatical — so the
grounding check ("does this quote appear verbatim in the source?") passed, because the source
was damaged in the same place as the quote. A corrupt source and a corrupt quote agree with
each other perfectly.

Four days spent hunting free capacity, blind to the number that defines it.

**A missing number does not leave a hole a text check can see.** That's why the numbers in this
README carry the date they were read, and why `refresh` exists.

## Licence

MIT.
