# Publishing notes

Kept here so the repo's own description is version-controlled rather than typed once into a
web form and forgotten.

## Repository

**Name:** `board-of-directors`

**Description** (the one line under the repo title):

> Ask one question, get answers from several free models at different companies — they rank
> each other blind and a chair that didn't vote writes the decision. Runs on OpenRouter's
> free tier. No dependencies.

**Topics:** `llm` `openrouter` `multi-agent` `ensemble` `llm-judge` `mixture-of-agents`
`ai-agents` `python` `zero-dependencies` `local-first`

## What to say when linking it

The short version, for a post or a message:

> A board of directors made of other people's free models. Several models from *different
> companies* answer your question independently, rank each other blind, and a chair that
> didn't vote writes the decision — with the vote counted and the dissent kept. The rule
> underneath: a member that was rate-limited is never counted as agreement. Runs free, runs
> local, no dependencies.

## Building the release

    rm -rf build dist ./*.egg-info
    python -m build

The `rm` is not housekeeping. `setuptools` copies sources into `build/lib` and never removes
anything from it, so a file you deleted or moved is still there and still goes into the wheel.
Moving `data/` into the package left a ghost copy of the old top-level `data/` in every wheel
built afterwards, which `pip install` then unpacked straight into `site-packages`.

CI builds and installs the wheel on every push for the same reason: the unit tests run against
the source tree, so they cannot see what the artifact actually contains.

## The release

Tag `v0.1.0`. `CHANGELOG.md` is the release body.

## After the first push

    git config core.hooksPath .githooks

Not automatic on a clone — git does not run hooks from a fetched repo, by design. Worth saying
in any onboarding, because the hook is a real guard and it is off until someone turns it on.
