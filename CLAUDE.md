# CLAUDE.md

Project instructions for Claude Code. The canonical, tool-agnostic orientation
lives in AGENTS.md and is imported below — start there instead of re-reading
the codebase.

@AGENTS.md

## Claude-specific notes

- Read [DESIGN.md](DESIGN.md) before touching scoring, report formats, the
  review grid columns, or anything in `src/stock_analyzer/review/gui*.py`.
- Run the test suite before every commit:
  `for t in tests/test_*.py; do python "$t" || echo "FAIL $t"; done`
- Never add an AI/LLM call, API key, or model SDK to this codebase — the AI
  review happens outside the program by design.
- Never fill a missing value with 0 or a guess; keep it `None`.
- If you change structure, commands, or an output contract, update AGENTS.md /
  DESIGN.md in the same commit.
- The GitHub Pages site is `docs/index.html` (plain static HTML, no build step).
