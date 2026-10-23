# Amdahl GTM skills

This repo runs the security checks and keeps the public mirror for [amdahl.ai/skills](https://amdahl.ai/skills), a library of go-to-market skills. The code in this repo is open source.

A skill is a folder named after the skill, with a `SKILL.md` file in it. The file follows the [Agent Skills](https://agentskills.io/specification) format. It teaches an AI agent one GTM task, such as discovery call notes or a pipeline review. Most skills are that one file. A folder skill, such as system-diagram, also holds the scripts it runs.

## Install a skill

Install from [amdahl.ai/skills](https://amdahl.ai/skills). Each skill's page gives you three ways to install it:

- Hand the page to your agent and let it walk you through it.
- For a single-file skill, download its `SKILL.md` (the Raw link on the page) and save it as `<name>/SKILL.md` in your agent's skills folder: `~/.claude/skills/` for Claude Code.
- Copy the command on the page, `npx amdahl skills add <publisher>/<skill>`, and run it. At a terminal it asks one question: this project, or every project on your computer. It puts the skill in the skills folder of each agent it finds: `.claude/skills/<name>/` for Claude Code, and `.agents/skills/<name>/` for Codex, Cursor and other agents. It needs Node.js 20 or later.

For a single-file skill, the command saves its `SKILL.md` as `<name>/SKILL.md` in that skills folder. For a folder skill, the command downloads the whole folder from amdahl.ai as one `.tar.gz` file and unpacks it into `<name>/` there. Either way, it checks the download against the checksum amdahl.ai lists, and writes nothing if the check fails. Do not save only the `SKILL.md` of a folder skill: the skill cannot run without its other files.

For each single-file skill, the file in `skills/` here has the same bytes the site serves. Each skill folder here also holds a `README.md`, written by the mirror, with these three ways to install it. That file is not part of the skill.

## Publish a skill

Publish at [amdahl.ai/skills/submit](https://amdahl.ai/skills/submit), or ask your agent to do it for you. Upload or paste your `SKILL.md` there. No GitHub repo needed. A GitHub link is optional.

- A submission is private until it is published.
- Skills that pass every check publish automatically.
- Only a skill's author can change it. For an organization's skill, the author is any of its members. A new version, Check again or taking the skill down each needs a code we email to the author.
- This repo takes no pull requests for skills: `skills/` is written by the mirror, from the published catalog.

[CONTRIBUTING.md](CONTRIBUTING.md) has the format rules and what happens after you submit. We accept skills for go-to-market work only.

## Checks and review

Every single-file skill gets three automated checks. Two are scanners from other projects. The third is an AI security review. Skills that pass every check publish automatically. A failed check declines the skill, with the reason. A single-file skill that the Amdahl team publishes is checked after it is listed.

What the author hears:

- **A warning does not stop a skill**, apart from a possible secret (next). The skill publishes, and we tell the author about each warning: which check, the line and why.
- **A possible secret waits for the author's answer.** If Betterleaks finds what may be a key, the skill is not published yet. The author removes it and presses Check again, or confirms it is not a real key, and then the skill goes live.
- **A failed check declines the skill.** We tell the author which check failed, the line and why. Checks can be wrong. The author can fix the file and press Check again, or tell us it is a false alarm.

A folder skill, such as system-diagram, is not scanned here. It is Amdahl's own code from its own repository. Its page on amdahl.ai says that the Amdahl team wrote and published it.

| Check | Run by | What it looks for |
|---|---|---|
| Threat analysis | [Cisco AI Defense skill-scanner](https://github.com/cisco-ai-defense/skill-scanner) | Prompt injection, data exfiltration and malicious code patterns |
| Secrets | [Betterleaks](https://github.com/betterleaks/betterleaks) | API keys, tokens and other secrets in the text |
| AI security review | Amdahl AI review: Claude Haiku 5.5, then Claude Sonnet 5.5 | Instructions that send data away, hide steps, override the agent or the user, change settings, run remote code, delete files, misuse credentials, or do not fit the skill's purpose |

How the checks work:

- The checks read the text. Nothing in a submitted skill is ever run.
- The two scanners run with their rules only, as a separate user that has no keys and no tokens.
- For the scanners, each finding has a severity. None, info and low pass. Medium is a warning. High and critical fail.
- Betterleaks gives a confidence, not a severity. A possible secret is a warning, and a high-confidence one fails.
- If a check crashes, times out or gives an answer we cannot read, the check is a warning that says "Scanner did not finish". A check never passes by default. amdahl.ai runs the checks again.

### The AI security review

The AI review runs after the two scanners. It has two steps.

1. Claude Haiku 5.5 reviews the `SKILL.md`. If its verdict is pass, the check passes.
2. If Haiku flags anything, or does not finish, Claude Sonnet 5.5 reviews the same text. Its verdict is final.

A pass verdict passes. A review verdict is a warning. A block verdict fails.

The result of the AI review is private. The author sees it on their status page and in the email. The public page of a skill shows the two scanners only.

The model checks every instruction against eight checks: data leaving, hidden actions, overrides, persistence, remote code, destructive actions, credentials and purpose mismatch. Normal steps that fit the skill's purpose are not findings. The full rubric is in [`scripts/ai_review.py`](scripts/ai_review.py).

What the model gets:

- The `SKILL.md` text with line numbers, and the rubric. Nothing else.
- No tools, no files and no credentials. It cannot run a command or open a link.
- The skill text is marked as untrusted data to judge, not instructions to follow. Text in a skill that speaks to the reviewer is a finding on its own.

The review sends the text to the Anthropic API over HTTPS. The API key is a repository secret. Only the "AI security review" step gets it, in the queue scan and in the self-test. If Anthropic refuses for the spend limit or the credit balance, the check did not finish, and amdahl.ai runs it again later.

The summary of the check names the most severe finding: its check, its line and the model's reason. It leaves out the quote. The details list every finding and say which model gave the final verdict. Their quotes from the skill add up to 120 characters at most.

No check is perfect. In June 2026, [Trail of Bits](https://blog.trailofbits.com/2026/06/03/the-sorry-state-of-skill-distribution/) got malicious skills past every scanner they tested, Cisco's skill-scanner among them. Pattern rules miss an instruction that looks normal. The AI review compares each instruction with the purpose the skill states. It can still be wrong.

## How this repo works

amdahl.ai is the source of truth for the catalog. This repo does three jobs on GitHub's free runners.

1. **Security scans.** Every 10 minutes, [`scan-queue.yml`](.github/workflows/scan-queue.yml) claims skills that wait for a scan from amdahl.ai. It runs the three checks on each one and posts the results back. It is one job with no workflow artifacts, and each secret reaches only the step that needs it. [SECURITY.md](SECURITY.md) says how.
2. **Public mirror.** Every hour, [`mirror.yml`](.github/workflows/mirror.yml) writes each published skill to `skills/<publisher>/<name>/SKILL.md`, with a `README.md` beside it that gives the ways to install it from amdahl.ai. It also removes skills that are no longer published. The git history is a public record of every published version. The mirror does not copy a folder skill (a skill that installs from a tar.gz, such as system-diagram), because its source is its own repository.
3. **Keepalive.** On the 1st and the 15th of each month, [`keepalive.yml`](.github/workflows/keepalive.yml) commits the time to `.github/keepalive`. GitHub turns off scheduled workflows in a public repository after 60 days with no activity, and the mirror commits only when the catalog changes.

## Layout

```
.github/
  CODEOWNERS
  workflows/
    scan-queue.yml      scheduled scans for amdahl.ai
    mirror.yml          hourly copy of the published catalog
    keepalive.yml       twice-monthly commit that keeps the schedules on
    self-test.yml       runs the real checks on known samples on every push
scanners/               hash-locked install list for Cisco skill-scanner
scripts/
  install_scanners.sh   installs the two pinned scanners
  sandbox.sh            runs the two scanners as an unprivileged user (in Actions)
  walls.py              proves that user cannot change what the later steps run
  scan.py               runs the three checks on one skill folder
  ai_review.py          the two-step AI security review (Anthropic API)
  self_test.py          checks each verdict on known samples
  normalize.py          turns scanner and review output into pass, warn or fail
  scan_queue.py         queue client for amdahl.ai: claim, scan, ai-review, post
  handoff.py            the files the queue steps pass to each other
  mirror.py             catalog mirror
  skillmd.py            name, id, digest and size rules
skills/                 published skills and their install notes (written by the mirror)
tests/                  unit tests, with real scanner output as fixtures and a fake model API
```

## Run the checks yourself

You need Python 3.12 or later, `curl` and `tar`, on Linux x86_64 or macOS arm64.

```bash
bash scripts/install_scanners.sh            # prints two export lines; run them
python3 scripts/scan.py path/to/your-skill  # prints the three checks as JSON
python3 -m unittest discover -s tests -t tests
```

The AI review runs only when `ANTHROPIC_API_KEY` is set. It then sends your `SKILL.md` to the Anthropic API, billed to that key. Without the key, the AI check says "Scanner did not finish". The unit tests never call the API.

## License

<!-- LEGAL REVIEW NEEDED (2026-10-23): the licence of this repo's code and of the skills in skills/, the paragraph below. -->
Apache-2.0 ([LICENSE](LICENSE)) covers only this repo's code: its scripts, workflows and tests. It does not cover the skills in `skills/`. A skill has the licence its `license` field names. If it names none, no licence is granted beyond reading it and installing it from amdahl.ai.
