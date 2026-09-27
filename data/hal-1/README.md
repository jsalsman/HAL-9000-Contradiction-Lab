# Protocol hal-1 run records

A verified copy of every stored object for protocol `hal-1`, taken on 2026-09-27 from the lab's public results bucket, https://storage.googleapis.com/hal-9000-tests/ (readable without authentication). Each file matched the bucket's MD5 checksum when copied; `SHA256SUMS` lists the SHA-256 of every file here (check with `sha256sum -c SHA256SUMS` in this directory).

These are the 95 units (19 models, five scenarios each) from the two runs behind the leaderboard and the paper in `paper/`. The layout matches the bucket:

```text
runs/<run_id>/run.json                        run snapshot: protocol, model set, aliases, status
runs/<run_id>/units/<model>/<S#>.tested.json  tested call: visible text, reasoning (if returned), status, provider, usage, cost, latency
runs/<run_id>/units/<model>/<S#>.judge.json   judge labels, rationale, outcome, attempts, cost
summaries/hal-1/<run_id>.json                 compact judged units the leaderboard aggregates
```

Model directories replace `/` in the OpenRouter model ID with `__`. Refused units (five) have a tested record but no judge record, which is why there are 95 tested and 90 judge files. Model text is stored raw, with the aliases the models saw (MERIDIAN, Kestrel, Okafor, Reyes, Ossian); `display_aliases` in each `run.json` maps them back to the story's names. Run IDs are one-way digests; no API key or fragment of one is stored.

The records are released under the repository's Apache-2.0 license (see `LICENSE`). Copyright in model-generated text may be limited; the license covers the collection, labels, and metadata.
