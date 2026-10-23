# Publishing a skill

Thank you for sharing a skill. You publish it on amdahl.ai, not here. This repo takes no pull requests for skills. This page has the rules a skill must follow and what happens after you send it.

## What we accept

We accept skills for go-to-market work. Examples: prospect research, outbound messages, discovery calls, call prep and recaps, CRM updates, pipeline reviews, proposals and pricing, renewals, and marketing.

We do not accept general coding tools or skills unrelated to selling and marketing.

## How to submit

Go to [amdahl.ai/skills/submit](https://amdahl.ai/skills/submit), or ask your agent to submit for you (the page has the prompt).

- **Upload the file. This is the default.** Paste your `SKILL.md`, or drop the `.md`, `.skill` or `.zip` file you have, for example a skill you made in Claude. No GitHub repo needed.
- **Or link it on GitHub.** A GitHub link is optional. Link the skill's `SKILL.md` in any public repository. The skill's page then shows that repository as its source.

A skill on amdahl.ai is one `SKILL.md` file. If a `.skill` or `.zip` file holds other files, such as scripts or references, it is refused: put what the skill needs into `SKILL.md`.

Publish under your own name, or under an organization on amdahl.ai/skills if you are one of its members. We email a 6-digit code to the address you give. The submission starts once you enter it.

Only a skill's author can change it. For an organization's skill, the author is any of its members. A new version, Check again or taking the skill down each needs a new code sent to the author. A submission is private until it is published.

## SKILL.md rules

These rules follow the [Agent Skills specification](https://agentskills.io/specification).

- The file starts with a YAML frontmatter block between two `---` lines.
  - If your file has none, the upload form asks for a name and a one-line description and writes the frontmatter for you. A file sent through the API must have it.
- `name` is required.
  - 1 to 64 characters.
  - Lowercase letters, digits and single hyphens. It cannot start or end with a hyphen. The pattern is `^[a-z0-9]+(-[a-z0-9]+)*$`.
  - It must match the folder name.
  - It must be free on amdahl.ai/skills: two skills cannot share a name, because they would install into the same folder. If yours is taken, the form helps you choose another.
- `description` is required.
  - 1 to 1024 characters.
  - Say what the skill does AND when to use it.
- Keep the body after the frontmatter under 500 lines. More than 1000 is refused.
- The body must not be empty.
- `license` is optional. Name a license, for example `MIT` or `Apache-2.0`. See "License" below.

- The frontmatter must be valid YAML. Keep it simple: one `key: value` per line. If a value contains a colon followed by a space, put it in double quotes.
- No `hooks`: a skill on amdahl.ai cannot run commands on its own. `allowed-tools` is allowed, and it is shown in the skill's entry in amdahl.ai/skills.md.

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

## Things the checks flag

- Instructions that tell the agent to ignore its rules or the user.
- Hidden text, such as zero-width characters or white space that hides instructions.
- Instructions to send data, files or credentials to an outside address.
- Steps the agent must hide from the user, or steps that do not fit what the skill says it is for.
- API keys, tokens or other secrets, even fake ones.

Also leave out customer names and personal data. No check looks for them, so this one is up to you.

## What happens next

1. amdahl.ai checks the format the moment you submit. Fix anything it lists and submit again.
2. You enter the code we email you, and you get a private link to your submission's status. A submission that is not confirmed within 24 hours expires and is never checked.
3. Within about ten minutes, [`scan-queue.yml`](.github/workflows/scan-queue.yml) runs the three checks on it: Cisco skill-scanner, Betterleaks and the AI security review. The README section "Checks and review" says how each one works.
4. If every check passes, the skill publishes automatically and is listed on amdahl.ai/skills.
   - A warning does not stop it. The skill publishes, and the status page and an email list each warning.
   - A possible secret waits for your answer. Remove it and press Check again, or confirm that it is not a real key, and the skill goes live.
   - If a check fails, the skill is declined. The status page and an email say which check, the line and why.
5. Within an hour of publishing, the mirror writes the skill to `skills/<publisher>/<name>/SKILL.md` here.

Checks can be wrong. To try again, fix the file and press Check again on your status page: a GitHub source is read again from the same repository, path and branch or tag, and for an upload you send the fixed file. Every check runs again. A clean result replaces the live version, and a new version that fails never takes the live one down. If you think a decline is wrong, tell us it is a false alarm on the same page.

The mirror keeps `skills/` equal to the published catalog. It removes anything that is not published.

## License

<!-- LEGAL REVIEW NEEDED (2026-10-23): the licence of this repo's code and of the skills in skills/, the paragraph below. -->
A published skill is public: anyone can read, install and copy it, and each published version stays in this repo's public git history. A skill has the licence its `license` field names. If it names none, no licence is granted beyond reading it and installing it from amdahl.ai. Apache-2.0 ([LICENSE](LICENSE)) covers only this repo's code, not the skills in `skills/`.

## Check your skill before you submit

You need Python 3.12 or later, on Linux x86_64 or macOS arm64.

```bash
bash scripts/install_scanners.sh            # prints two export lines; run them
python3 scripts/scan.py path/to/your-skill
```

The AI review runs only when `ANTHROPIC_API_KEY` is set. It then sends your `SKILL.md` to the Anthropic API, billed to that key. Without the key, that check says "Scanner did not finish".
