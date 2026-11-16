# The Claude Code skill

`skill/SKILL.md` teaches [Claude Code](https://claude.com/claude-code) to run darkroom the way it was developed: one stage at a time on a new target, looking at the result of each stage before moving on.

It covers:

- surveying the subs and running `doctor` before promising anything
- what to check after each stage, and how to measure it (frames kept, sky evenness, the SPCC warning, noise before and after denoise, clipping)
- comparing two or three variants side by side before choosing a setting, and what each `finish` setting looks like pushed too far
- choosing between the galaxy and cluster looks
- the known failures and their fixes, including leaving the StarNet Gatekeeper decision to you
- keeping site coordinates and device IDs out of anything shared

`setup.sh` links the `skill` folder to `~/.claude/skills/darkroom`, so the skill is available in any Claude Code session, wherever it was started. Pass `--no-skill` to skip that. With it installed, asking Claude to process a folder of subs is enough to start.

Because it is a link, updating the repo updates the skill.
