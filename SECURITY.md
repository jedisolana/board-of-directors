# Security

This program holds an API key and can write files to a folder you point it at. Both of those
are worth being careful about, so here is how to tell us if something is wrong.

## Reporting

Please **do not open a public issue** for a security problem. Use GitHub's private report:

**Security tab → Report a vulnerability**, on this repository. It goes only to the maintainer.

Say what you found, how to reproduce it, and what it lets someone do. You will get a reply.
There is no bounty; there is a thank-you in the changelog if you want one.

## What counts

Anything that would let a page, a model, or a process do one of these without the owner
choosing it:

- read or send the OpenRouter key anywhere but OpenRouter
- spend money with paid models switched off, or past the spend cap
- read a file outside the folder the owner picked, or write a file anywhere at all
- send text that the secret seam should have refused
- reach the console from off the machine

## What is already in place

- The console binds to `127.0.0.1` only and refuses requests whose `Host` or `Origin` is
  not loopback, which blocks CSRF and DNS rebinding from a page you merely visit.
- The key is stored in a `0600` file and is never sent back to the browser.
- Every string a model produces is escaped before it reaches the page.
- The one endpoint that writes files checks containment itself, symlinks included, and
  writes nothing without a click and a copy of the previous contents.
- Paid models require the stored setting *and* per-request consent, and a zero cap
  overrules both.
- Every one of the above has a test that fails without the fix.

Details, and the bugs that led to each of them, are in `CHANGELOG.md`.
