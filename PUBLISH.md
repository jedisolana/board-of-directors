# Publishing notes

Kept here so the repo's own description is version-controlled rather than typed once into a
web form and forgotten.

## Repository

**Name:** `board-of-directors`

**On PyPI:** `jedi-board-of-directors`. Not the same string, and it cannot be: PyPI compares a
proposed name against the existing ones with `-`, `_` and `.` removed, and `boardofdirectors`
is already taken by an unrelated project. `pip` applies the same flattening to what a user
types, so `pip install jedi_board_of_directors` reaches the same package - but
`jedi-boardofdirectors` would not, which is why the hyphenated spelling is the one registered.

The commands are unaffected: `board` and `board-of-directors`. So is the import,
`boardofdirectors`.

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

## Building it by hand

Not how a release is cut - see below - but this is what CI does, and what to run locally
when you want to look inside the artifact:

    rm -rf build dist ./*.egg-info
    python -m build

The `rm` is not housekeeping. `setuptools` copies sources into `build/lib` and never removes
anything from it, so a file you deleted or moved is still there and still goes into the wheel.
Moving `data/` into the package left a ghost copy of the old top-level `data/` in every wheel
built afterwards, which `pip install` then unpacked straight into `site-packages`.

CI builds and installs the wheel on every push for the same reason: the unit tests run against
the source tree, so they cannot see what the artifact actually contains.

## The release

    git tag vX.Y.Z && git push --tags

That is the whole procedure, and it is the only one - `.github/workflows/publish.yml` owns the
rest. It runs the same `tests.yml` the branch runs (called, not copied, so the release gate
cannot drift out of step with the branch gate), then refuses to go on if:

- the tag disagrees with the version in `pyproject.toml` - otherwise tagging `v0.1.1` on a tree
  that still says `0.1.0` re-uploads `0.1.0`, PyPI rejects it as a duplicate, and the mistake
  surfaces at the last possible moment;
- `twine check` is unhappy with the metadata;
- the wheel is missing `web/index.html` or `data/free-models.json`, the two files whose absence
  is what shipped a 404 console the first time.

**There is no API token anywhere** - not on a laptop, not in a repository secret. PyPI is
configured with a Trusted Publisher naming this repository and this workflow file, and GitHub
proves that at upload time with a credential that expires in minutes. Nothing to leak, nothing
to rotate.

`CHANGELOG.md` is the release body.

A failed upload costs nothing: PyPI does not consume a version number on a failure, so the same
tag can be deleted, fixed, and pushed again.

## After the first push

    git config core.hooksPath .githooks

Not automatic on a clone — git does not run hooks from a fetched repo, by design. Worth saying
in any onboarding, because the hook is a real guard and it is off until someone turns it on.
