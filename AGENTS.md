# Guidelines for AI agents working on this project

- Code style: every function has a docstring (JSDoc in JavaScript); global constants get an introductory comment explaining their purpose. Match the surrounding comment density.
- This is strictly one page. Keep `index.html` at the root with no templates or Jinja; keep presentation in `static/styles.css` and behavior in `static/app.js`. Do not add a separate leaderboard route; the page reads `GET /api/leaderboard`.
- `hal/models.json` and `hal/config.json` are the only places that list model IDs, efforts, and judge settings. Verify IDs and `reasoning.supported_efforts` against `GET https://openrouter.ai/api/v1/models` before changing them.
- Any change to models, efforts, prompts, aliases, judge, or rubric bumps `PROTOCOL_VERSION` and adds (never edits) an entry in `tests/test_protocol.py::PUBLISHED`. Never mix protocol versions in one aggregate.
- Prompts sent to any model (tested or judge) must use the aliases in `hal/protocol.py::ALIASES` and must not contain anything in `FORBIDDEN_TERMS`. The page shows canonical names; map aliases back only for display with `display_text`, never in stored records.
- Tested calls are plain chat: no tools, no response format, no decision menus, no self-labeling.
- The judge must never receive the tested model's name, lab, ID, or reasoning text. Keep `judge_request` free of any parameter that could carry them.
- Outcomes are computed in `hal/outcomes.py` from judge labels; the judge never names an outcome.
- Pass the API key explicitly, keep it in closures and the Authorization header only, and never persist, log, or echo it. Every stored object goes through `hal.storage.dumps`, which refuses credential-shaped keys.
- Checkpoint tested and judge stages separately, and never repay a final stage on resume. Only `provider_error` stages are retried.
- Wrap every Flask streaming generator with `stream_with_context()` and emit one JSON object per line.
- Run IDs are resume handles: never return them from public leaderboard, sample, or flag endpoints; use `unit_ref`.
- Insert all model and judge text with `textContent`. Keep colorblind-safe outcome colors with text labels, keyboard operation, light/dark themes, and reduced-motion support. Re-validate the palette if you change a color.
- Mock all network calls in tests, which live under `tests/`. Do not add tests that build or run the Docker container.
- Run `pytest`, `ruff check .`, and `ruff format --check .` before committing.
- Keep `README.md` and this file current with important design changes.
