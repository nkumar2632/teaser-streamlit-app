# Project guardrails

- Work only in `/Users/Nitin/Developer/teaser-streamlit-app`. Do not search unrelated local repositories, personal directories, credentials, browser data, or network volumes.
- The sole model source is `https://github.com/nkumar2632/Football-Teasers-v1.0`, branch `claude/teaser-model-v1-foundation-b7akky`, commit `94e412e` (full SHA in MODEL_PIN.md).
- `.model_reference/` is a disposable, ignored, read-only model checkout. Never intentionally edit its tracked files, tests, constants, algorithms, or formatting. Never create source-model branches, commits, or pushes. Keep its push remote disabled.
- Before and after each major implementation phase verify its HEAD and empty tracked diff. If integrity fails, investigate, then recreate the disposable reference; do not preserve or push changes.
- Model behavior is immutable. Report source defects or disagreement with the verified Week 2 fixture and stop that path; never fix or compensate for a model defect here.
- Streamlit → presentation/view models → one `TeaserModelAdapter` → actual pinned source. No app implementation of probability, eligibility, geometry, ranking, ticket construction, selection, exposure, EV, or staking algorithms. Display formatting and input validation are allowed.
- Build locally only. Bind QA servers to `127.0.0.1`. No public deployment, LAN exposure, paid resources, sportsbook accounts, or wager-placement integration.
- This app needs no secrets. Ignore secret files and never inspect, print, log, or commit credentials. Network use is limited to the specified model repository, ordinary Python/testing dependencies, and their documentation.
- Make routine reversible engineering choices autonomously. Use independent read-only reviews; avoid simultaneous edits to the same files. Primary agent owns integration and final QA.
- Keep the app small, maintainable, and iPhone portrait first. Keep secondary research separate from live results. Preserve explicit betting signs and full internal model precision.
- Run app/model contracts, the verified Week 2 regression, local startup/mobile checks, and model integrity checks before completion. Document consequential decisions and actual limitations.
