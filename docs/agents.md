# Using darkroom with an AI agent

`skill/SKILL.md` teaches an AI agent to run darkroom the way it was developed: one stage at a time on a new target, looking at the result of each stage before moving on. It follows the [Agent Skills](https://agentskills.io) format, a folder with a `SKILL.md` that has a `name` and a `description`, so any agent that loads skills from a folder can use it. Claude Code is the one it has been used with.

The skill covers:

- surveying the subs and running `doctor` before promising anything
- what to check after each stage, and how to measure it (frames kept, sky evenness, the SPCC warning, noise before and after denoise, clipping)
- comparing two or three variants side by side before choosing a setting, and what each `finish` setting looks like pushed too far
- choosing between the galaxy and cluster looks
- the known failures and their fixes, including leaving the StarNet Gatekeeper decision to you
- keeping site coordinates and device IDs out of anything shared

The agent needs to run shell commands. It should also be able to look at images; if it cannot, the skill tells it to report the measurements and ask you to look at the previews instead.

## Installing the skill

`setup.sh` links the `skill` folder into an agent's skills folder as `darkroom`. By default that is `~/.claude/skills`, which Claude Code reads. For another agent, pass its skills folder, as many times as you need:

```sh
bash setup.sh --skill-dir path/to/agent/skills
bash setup.sh --skill-dir ~/.claude/skills --skill-dir path/to/other/agent/skills
```

`--no-skill` skips linking. Because it is a link, updating the repo updates the skill. With it installed, asking the agent to process a folder of subs is enough to start.

## Agents that read AGENTS.md

Some coding agents do not load skills but read an `AGENTS.md` file from the repository root. darkroom has one. It points them to `skill/SKILL.md` for processing images, and gives the rules for working on the code itself: how to run the tests, where things live, how settings are added, and what must never be committed.
