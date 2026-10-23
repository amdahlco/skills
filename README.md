# Amdahl GTM skills

This repo is the open source registry of go-to-market skills behind [amdahl.ai/skills](https://amdahl.ai/skills).

A skill is a folder named after the skill, with a `SKILL.md` file in it. The file follows the [Agent Skills](https://agentskills.io/specification) format. It teaches an AI agent one GTM task, such as discovery call notes or a pipeline review. Most skills are that one file. A folder skill, such as system-diagram, also holds the scripts it runs.

## Install a skill

Install from [amdahl.ai/skills](https://amdahl.ai/skills). Each skill's page gives you three ways to install it:

- Hand the page to your agent and let it walk you through it.
- For a single-file skill, download its `SKILL.md` (the Raw link on the page) and save it as `<name>/SKILL.md` in your agent's skills folder: `~/.claude/skills/` for Claude Code.
- Copy the command on the page, `npx amdahl skills add <publisher>/<skill>`, and run it. At a terminal it asks one question: this project, or every project on your computer. It puts the skill in the skills folder of each agent it finds: `.claude/skills/<name>/` for Claude Code, and `.agents/skills/<name>/` for Codex, Cursor and other agents. It needs Node.js 20 or later.

For a single-file skill, the command saves its `SKILL.md` as `<name>/SKILL.md` in that skills folder. For a folder skill, the command downloads the whole folder from amdahl.ai as one `.tar.gz` file and unpacks it into `<name>/` there. Either way, it checks the download against the checksum amdahl.ai lists, and writes nothing if the check fails. Do not save only the `SKILL.md` of a folder skill: the skill cannot run without its other files.

For each single-file skill, the file in `skills/` here has the same bytes the site serves. Each skill folder here also holds a `README.md`, written by the mirror, with these three ways to install it. That file is not part of the skill.

## Submit a skill

Submit at [amdahl.ai/skills/submit](https://amdahl.ai/skills/submit), or ask your agent to do it for you. Submissions stay private until a person at Amdahl approves them. This repo takes no pull requests for skills: `skills/` is written by the mirror, from the published catalog.

[CONTRIBUTING.md](CONTRIBUTING.md) has the format rules. We accept skills for go-to-market work only.

## Checks and review

Every single-file skill gets three automated checks. Each check uses an open source scanner. A submitted skill is scanned before a person at Amdahl reviews it. A single-file skill that the Amdahl team publishes is scanned after it is listed.

A folder skill, such as system-diagram, is not scanned here. It is Amdahl's own code from its own repository. Its page on amdahl.ai says that the Amdahl team wrote and published it.

| Check | Scanner | What it looks for |
|---|---|---|
| Prompt injection and malicious instructions | [NVIDIA SkillSpector](https://github.com/NVIDIA/SkillSpector) | Instructions that override the agent, hidden text, data theft, harmful actions |
| Threat analysis | [Cisco AI Defense skill-scanner](https://github.com/cisco-ai-defense/skill-scanner) | Prompt injection, data exfiltration and malicious code patterns |
| Secrets | [Betterleaks](https://github.com/betterleaks/betterleaks) | API keys, tokens and other secrets in the text |

How the checks work:

- The scanners read the text. Nothing in a submitted skill is ever run.
- The scanners run with their rules only. No AI model sees the skill here, and this repo holds no model keys.
- Each finding has a severity. None, info and low pass. Medium is a warning. High and critical fail.
- Betterleaks gives a confidence, not a severity. A possible secret is a warning, and a high-confidence one fails.
- If a scanner crashes or times out, the check is a warning that says "Scanner did not finish". A check never passes by default.

The checks are evidence, not a verdict. In June 2026, [Trail of Bits](https://blog.trailofbits.com/2026/06/03/the-sorry-state-of-skill-distribution/) got malicious skills past every scanner they tested, Cisco's skill-scanner among them. So a person at Amdahl reads every skill before it is published.

## How this repo works

amdahl.ai is the source of truth for the catalog. This repo does three jobs on GitHub's free runners.

1. **Security scans.** Every 10 minutes, [`scan-queue.yml`](.github/workflows/scan-queue.yml) pulls skills that wait for a scan from amdahl.ai. It runs the three scanners on each one and posts the results back.
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
    self-test.yml       runs the real scanners on known samples on every push
scanners/               hash-locked install lists for the Python scanners
scripts/
  install_scanners.sh   installs the three pinned scanners
  scan.py               runs the scanners on one skill folder
  self_test.py          checks each scanner's verdict on known samples
  normalize.py          turns scanner output into pass, warn or fail
  scan_queue.py         queue client for amdahl.ai
  mirror.py             catalog mirror
  skillmd.py            name, id, digest and size rules
skills/                 published skills and their install notes (written by the mirror)
tests/                  unit tests, with real scanner output as fixtures
```

## Run the checks yourself

You need Python 3.12 or later, `curl` and `tar`, on Linux x86_64 or macOS arm64.

```bash
bash scripts/install_scanners.sh            # prints three export lines; run them
python3 scripts/scan.py path/to/your-skill  # prints the three checks as JSON
python3 -m unittest discover -s tests -t tests
```

## License

The code in this repo is licensed under [Apache-2.0](LICENSE). A skill can name its own license in the `license` field of its frontmatter.
