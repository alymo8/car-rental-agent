# CLAUDE.md

## Build, run, test

- `python -m pytest -q` and `ruff check src tests evals && ruff format --check src tests evals`
  from the repo root. Unit tests are network-free.
- `python -m evals.run_evals --runs 2` calls the live model and mock API, costs tokens and
  commits real extensions on the shared mock; run it only to check success criterion 3.

## Gotchas

- Keep the repo `ruff.toml` (`target-version = "py310"`, `src = ["src", "."]`). Without it,
  ruff picks up a parent directory's config when the checkout is nested (for example a
  worktree under another project), so import sorting and the enabled rules change by path.
- `grace_question` is prompt-sensitive: the official articles that rank first only say
  "the applicable grace period"; the agent must search again for the extension policy
  (`kb_ext_01`). Re-run `python -m evals.run_evals --scenario grace_question` a few times
  after any change to `INSTRUCTIONS` in `src/agent.py`.
- `KnowledgeBase.search` drops a legacy article ranked below a same-category official one.
  In KB test fixtures, give a legacy article its own `category` when the test needs to see
  it (see `art(..., category)` in `tests/test_kb.py`).
- In the web console, run every turn through `SHARED_RUNNER` (`src/webui.py`), never a bare
  `Runner.run_sync` in a new thread per turn: the SDK's shared model HTTP client keeps a
  connection bound to the first turn's event loop, so the second turn hangs.
- The agent sometimes answers "I'd like to extend my rental." by calling `verify_customer` with
  an empty ID, which uses one of the two verification attempts. It can make the "Wrong email"
  demo hand off one message early; reset and rerun the scenario if that happens live.
