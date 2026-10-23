# Contributing a skill

Thank you for sharing a skill. This page has the rules a skill must follow.

## What we accept

We accept skills for go-to-market work. Examples: prospect research, outbound messages, discovery calls, call prep and recaps, CRM updates, pipeline reviews, proposals and pricing, renewals, and marketing.

We do not accept general coding tools or skills unrelated to selling and marketing.

## How to submit

Go to [amdahl.ai/skills/submit](https://amdahl.ai/skills/submit), or ask your agent to submit for you (the page has the prompt).

- **As yourself:** link the skill's folder or its `SKILL.md` in a public GitHub repository under your own GitHub user. Your skills appear at `amdahl.ai/skills/<your-github-user>`.
- **As an organization** that is already on amdahl.ai/skills: link the file or paste it.

Submissions stay private until a person at Amdahl approves them. This repo takes no pull requests for skills.

## SKILL.md rules

These rules follow the [Agent Skills specification](https://agentskills.io/specification).

- The file starts with a YAML frontmatter block between two `---` lines.
- `name` is required.
  - 1 to 64 characters.
  - Lowercase letters, digits and single hyphens. It cannot start or end with a hyphen. The pattern is `^[a-z0-9]+(-[a-z0-9]+)*$`.
  - It must match the folder name.
- `description` is required.
  - 1 to 1024 characters.
  - Say what the skill does AND when to use it.
- Keep the body after the frontmatter under 500 lines. More than 1000 is refused.
- The body must not be empty.
- `license` is optional. Name a license, for example `MIT` or `Apache-2.0`.

- The frontmatter must be valid YAML. Keep it simple: one `key: value` per line. If a value contains a colon followed by a space, put it in double quotes.
- No `hooks`: a skill on amdahl.ai cannot run commands on its own. `allowed-tools` is allowed, but it is shown to the reviewer and in the skill's entry in amdahl.ai/skills.md.

## Example

```markdown
---
name: discovery-call-notes
description: Turns a discovery call transcript into notes on pain, impact, decision process and next step. Use after a sales discovery call when you need notes for the CRM or a hand-off.
license: MIT
---

# Discovery call notes

1. Read the transcript the user gave you. Do not invent facts.
2. Write four sections: Pain, Impact, Decision process, Next step.
3. If the call did not cover a section, write "Not covered".
```

## Things that fail review

- Instructions that tell the agent to ignore its rules or the user.
- Hidden text, such as zero-width characters or white space that hides instructions.
- Instructions to send data, files or credentials to an outside address.
- API keys, tokens or other secrets, even fake ones.
- Customer names or personal data.

## What happens next

1. amdahl.ai checks the format the moment you submit. Fix anything it lists and submit again.
2. Within about ten minutes, [`scan-queue.yml`](.github/workflows/scan-queue.yml) runs the three scanners on it.
3. A person at Amdahl reviews the skill and the scan results.
4. Once approved, it is listed on amdahl.ai/skills, and within an hour the mirror writes it to `skills/<publisher>/<name>/SKILL.md` here.

The mirror keeps `skills/` equal to the published catalog. It removes anything that is not published.

## Check your skill before you submit

You need Python 3.12 or later, on Linux x86_64 or macOS arm64.

```bash
bash scripts/install_scanners.sh            # prints three export lines; run them
python3 scripts/scan.py path/to/your-skill
```
