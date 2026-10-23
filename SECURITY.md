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

In scope: anything in this repo, including its workflows and the mirror that writes published
skills to `skills/`. Anything that lets untrusted input run code, or that leaks a secret or
credential, counts.

Out of scope: problems that need an attacker to already control your machine or your Claude
session.

## Baseline exceptions

This repo differs from the Amdahl public-repo security baseline in the places below. The first
two were approved by agu on 2026-10-22.

- **A bypass actor on the `main` ruleset.** The `amdahl-skills-bot` GitHub App can bypass the
  ruleset, because the mirror (`.github/workflows/mirror.yml`) uses it to copy published skills
  into `skills/` on `main`.
- **Repo secrets.** The repo holds two secrets, `SKILLS_SCAN_TOKEN` (used by `scan-queue.yml`)
  and `SKILLS_BOT_PRIVATE_KEY` (the bot's key, used by `mirror.yml` and `keepalive.yml`), plus
  the variable `SKILLS_BOT_CLIENT_ID`.
- **A model API key. Needs agu's sign-off; not approved yet.** The repo also holds
  `SKILLS_REVIEW_ANTHROPIC_KEY`, the Anthropic API key for the AI security review. Only one step
  gets it: the "AI security review" step of `scan-queue.yml`, and the step of the same name in
  `self-test.yml` (never on a pull request from a fork). That step's environment holds this key
  and nothing else.

## How the scan job keeps its secrets apart

`scan-queue.yml` runs as one job. Nothing passes between its steps as a workflow artifact: this
repo is public, so anyone can download its artifacts, and they would carry skills that are not
published and the AI review's private findings. The steps share files in the runner's temp
folder instead, which the first step closes to other users.

- `SKILLS_SCAN_TOKEN` is in the environment of two steps only: the step that claims the queue
  and the step that posts the results.
- The two rules-only scanners (Cisco skill-scanner and Betterleaks) run as `skillscan`, a user
  the job creates (`scripts/sandbox.sh`). It has no sudo rights and no group but its own. Its
  home folder is closed to other users. It runs with an empty environment apart from `PATH` and
  its work folder, so no secret reaches it. It reads the claimed skills from one file and writes
  its results to another. The post step reads that file as untrusted input. After the scan,
  every process of `skillscan` is killed. If one is still running, the AI review and the post
  step do not run.
- The steps after the scan run nothing `skillscan` can change. Every step runs root's own
  `/usr/bin/bash` by its full path. Every Python step after the scanner user exists runs root's
  `/usr/bin/python3 -I -S`, with no `site` module, so no `.pth` file runs. None of them uses the
  Python that `actions/setup-python` puts in `/opt`: as far as we know, GitHub's runner image
  leaves `/opt` writable by every user. Before `skillscan` exists, `scripts/walls.py` removes
  other users' write access from every folder on `PATH` and `LD_LIBRARY_PATH`, from the folders
  above them, and from the commands and libraries in them. Then, as `skillscan` and before it
  reads a skill, it proves that `skillscan` can write none of these, nor the shell, nor that
  Python and its import path. If one check fails, no skill is handed over.
- The AI review runs in its own step, whose environment holds only `ANTHROPIC_API_KEY`.
- The logs never print a skill's text, its id or a finding.

Residual risk: code that runs inside the AI review step could read the Anthropic key, and the
runner user that claims and posts holds the token. Code that broke out of a scanner would run as
`skillscan`. It could read the other skills in the same run and change the two scanner results,
and it can reach the network. It could not read the token, the key or the AI review's findings.
The walls between `skillscan` and the later steps are file permissions, which `scripts/walls.py`
proves on every run before the scan. A kernel bug that let `skillscan` become root would break
them.
