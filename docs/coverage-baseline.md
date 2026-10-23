# Coverage baseline

Recorded 2026-10-22 on `main` after the postmortem fixes (1202 tests passing).

```
.venv/bin/pytest --cov=stockmanager --cov-report=term-missing
```

**Total: 93% statement coverage** (6686 statements, 455 missed; branch coverage is not measured).

Lowest-covered modules:

| Module | Coverage |
|---|---|
| `__main__.py` | 0% |
| `adapters/discord_rest.py` | 32% |
| `adapters/symbol_verifier.py` | 54% |

Treat 93% as the floor: new code should not lower it. `pytest-cov` is in the `dev` extra.
