# github-reader

[中文](README.md) · **English**

Turn GitHub activity into a daily digest you can actually finish reading.

The problem isn't "I can't find projects" — it's that finding them is slow, draining, and you
forget what you saw. `github-reader` searches a configurable set of domains, filters out the noise,
estimates how long each project takes to get running, and remembers what you saw yesterday.

```bash
python -m github_reader --zh
```

> This is the short English overview. **[README.md](README.md) (Chinese) is the full reference** —
> it covers calibration, design tradeoffs, and the pitfalls the test suite guards against.

---

## What it does that Trending doesn't

1. **Removes noise.** awesome-lists, tutorials, interview prep, and evergreen mega-repos are
   filtered out — huge star counts, never news.
2. **Estimates onboarding cost.** Inferred from README structure: one `docker` command, or a GPU
   and a half-hour build? Every entry gets an estimated minute count.
3. **Tracks across days.** Recurring projects are annotated "3 days in a row, +420 ★ total,"
   so the digest is continuous tracking instead of a fresh start every morning.

Output is two-tiered: a 30-second scan at the top, then featured entries with full assessments,
then a one-line-each activity list.

## Requirements

- **Python 3.10+** — standard library only, no `pip install` anything
- **GitHub credentials** — optional but strongly recommended

Unauthenticated GitHub API access allows 60 requests/hour; authenticated allows 5000. That gap
decides how many candidates get scanned. github-reader reuses your GitHub CLI credentials automatically:

```bash
gh auth status     # if you're logged in, you're set
```

Otherwise set `GITHUB_TOKEN` (or `GH_TOKEN`).

## Usage

```bash
python -m github_reader                      # generate today's digest
python -m github_reader --top 8 --feed 30    # 8 featured + 30 activity entries
python -m github_reader --domains ai         # only AI/LLM/Agent
python -m github_reader --offline            # cache only, no quota spent
python -m github_reader --verbose            # show filtered-out repos and why
python -m github_reader --out ./notes        # custom output directory
```

A full run takes ~105 seconds and ~450 API calls (3 domains, 24 keywords, 360 candidates).
Most of that is fetching and parsing READMEs, so re-running doesn't get much faster —
**caching saves quota, not time**. `--offline` only helps for repeated same-day runs, since
search queries embed a date window that changes the cache key.

### Chinese summaries

`--zh` appends a Chinese one-line summary to each entry, as an **added field** rather than a
replacement — the English original is always preserved.

```bash
python -m github_reader --zh          # fetch + generate + translate
python -m github_reader --zh-only     # no fetching, just backfill Chinese (fast, no GitHub quota)
```

It reuses `DEEPSEEK_API_KEY` from `~/.dsh/.credentials.yaml` (or the env var). Translation is
idempotent: re-running fills only the gaps, so "just run it again" is always safe.
Technical terms (agent, LLM, RAG, MCP, harness) deliberately stay in English.

```markdown
**是什么**：Continual learning infra for self-improving agents
**一句话**：让 agent 持续自我改进的 continual learning 基础设施
```

## Configuration

Edit `config.json` — no code changes needed.

```json
"domains": {
  "My area": ["keyword1", "keyword2"]
}
```

Each keyword generates 2 search queries (new projects + active older ones), so 8–10 keywords
per domain is a comfortable amount. Other dials: `min_stars_fresh`, `min_stars_established`,
`fresh_days`, `evergreen_star_threshold`, `cache_ttl_minutes`.

## Output

| Path | Description |
|---|---|
| `digests/YYYY-MM-DD.md` | Each day's digest, archived automatically |
| `digests/latest.md` | Copy of the most recent one |
| `.cache/state.json` | Tracking history (consecutive days, star deltas) |
| `.cache/http/` | HTTP cache; deleting it re-spends quota |

## Known limitations

- **Heuristics get things wrong.** A good project with a thin README may be judged
  "documentation is sparse." The tool orders your attention; it doesn't make the judgment for you.
- **Star velocity is a lifetime average**, and only shown for projects under 60 days old. Older
  projects show a verifiable "N days since last push" instead of a misleading growth number.
- **Public repos only** — no private repos, no issue/PR tracking.
- **Chinese summaries need a network call** to DeepSeek. Everything else is offline and zero-dependency.
- **The digest itself is rendered in Chinese**, including field labels. This is a deliberate
  design choice, not an oversight — see the Chinese README.

## Development

```bash
python tests/test_github_reader.py     # 47 tests: fetching, filtering, analysis, rendering, state
python tests/test_translate.py    # 29 tests: the structural work of Chinese translation
```

Tests target pitfalls actually hit in production — CRLF line endings breaking regexes, a
dependency section's `pip install` mistaken for the project's own install command, translation
idempotency requiring a two-pass scan. See the Chinese README for the full list.

| File | Responsibility |
|---|---|
| `github_reader/github_client.py` | API client: credentials, rate-limit backoff, disk cache |
| `github_reader/discover.py` | Candidate discovery, momentum scoring, noise filtering |
| `github_reader/analyze.py` | README heuristics: summary, install method, onboarding cost |
| `github_reader/state.py` | Cross-run state: consecutive days, star deltas |
| `github_reader/render.py` | Markdown rendering |
| `github_reader/llm.py` | LLM calls (reuses `~/.dsh` credentials) |
| `github_reader/translate.py` | Chinese summaries: location, batching, validation, write-back |
| `github_reader/cli.py` | Main flow orchestration |

Two decisions worth not "optimizing" away: **standard library only** (installing dependencies is
itself a cost), and **the LLM does exactly one thing — translate** (everything else is
heuristic, reproducible, and free).

## License

Not yet specified.
