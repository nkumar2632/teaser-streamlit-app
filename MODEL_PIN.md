# Immutable model pin

- Repository: https://github.com/nkumar2632/Football-Teasers-v1.0
- Required branch: `claude/teaser-model-v1-foundation-b7akky`
- Full commit: `94e412ed5bedc03a60b0b86f1146ff80c9f9e195`
- Date pinned: 2026-09-20
- Checkout: `.model_reference/`, detached HEAD; fetched with single-branch clone.
- Initial integrity: `git status --porcelain` empty; `git diff --exit-code HEAD` passed; HEAD resolved to the full SHA above.
- Push URL: `DISABLED`. Read/fetch origin retains the specified GitHub URL.
- Initial app state: empty directory, no Git repository; initialized a new repository. Only `.gitignore` and `AGENTS.md` existed before cloning. No other local model repository was searched or accessed.

The app must verify this exact commit and an empty tracked diff before loading the model. The reference is disposable and excluded from app Git history. Never modify or commit source-model files.

## Integrity recovery on 2026-09-20

The pinned source suite passed, but its `test_the_synthetic_rehearsal_runs_end_to_end` invoked a script that rewrote the generated timestamp in tracked `reports/live/nfl_2026_week_03_rehearsal.md`. This was an integrity failure in the disposable reference. The reference was deleted and cloned anew from the specified branch, checked out at the same full SHA, and push disabled. The recreated checkout has an empty `git status --porcelain --untracked-files=all` and no tracked diff. Do not run that write-producing test inside this pinned checkout again.
