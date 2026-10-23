# Security policy

## Report a vulnerability

Please report a security problem privately, not in a public issue or pull request. On this
repo's GitHub page, open the **Security** tab and choose **Report a vulnerability**, or go
straight to <https://github.com/amdahlco/skills/security/advisories/new>. Only the maintainers
see the report.

Useful details: what an attacker can do, the input or steps that show it, and the commit you
tested.

## What happens next

- We confirm that we received the report within 3 business days.
- We tell you whether we accept it, and the plan, within 10 business days.
- We aim to release a fix within 30 days of accepting a report, sooner for severe problems.
- We publish a GitHub security advisory when the fix is released, and credit you unless you ask
  us not to.

Please give us that time before you disclose the problem publicly.

## Supported versions

This repo has no releases. Fixes go into `main`.

## Scope

In scope: anything in this repo, including its workflows and the mirror that writes approved
skills to `skills/`. Anything that lets untrusted input run code, or that leaks a secret or
credential, counts.

Out of scope: problems that need an attacker to already control your machine or your Claude
session.

## Baseline exceptions

This repo differs from the Amdahl public-repo security baseline in two places. Approved by agu
on 2026-10-22.

- **A bypass actor on the `main` ruleset.** The `amdahl-skills-bot` GitHub App can bypass the
  ruleset, because the mirror (`.github/workflows/mirror.yml`) uses it to copy approved skills
  into `skills/` on `main`.
- **Repo secrets.** The repo holds two secrets, `SKILLS_SCAN_TOKEN` (used by `scan-queue.yml`)
  and `SKILLS_BOT_PRIVATE_KEY` (the bot's key, used by `mirror.yml` and `keepalive.yml`), plus
  the variable `SKILLS_BOT_CLIENT_ID`.
