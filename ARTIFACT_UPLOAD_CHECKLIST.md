# Artifact Upload Checklist (VPS)

The model bundle, decision-engine exports, feature shortlist, and bundled
market data are **intentionally git-ignored**. They are not committed to the
repository, so after you `git clone` onto the Hostinger VPS these folders will
be **missing** and must be uploaded manually (e.g. via `scp`, `rsync`, or
`sftp`) before the worker can run.

## Required ignored folders / files

Upload the following into the cloned repo on the VPS (under `worker/`):

```
worker/runtime/model files/
worker/runtime/model files/ethusdt_15m_short_expansion_mandatory_ml_live_bundle.joblib
worker/runtime/model files/ethusdt_15m_short_expansion_mandatory_ml_config.json
worker/runtime/model files/v22_live_engine_export/run_*/
worker/runtime/eth_feature_shortlist_outputs/ethusdt_feature_shortlist_best3_global.csv
worker/data/
```

These contain:

- **`worker/runtime/model files/`** — the frozen ML live bundle (`.joblib`),
  its config (`.json`), and the `v22_live_engine_export/run_*/` decision-engine
  / parity exports.
- **`worker/runtime/eth_feature_shortlist_outputs/`** — the feature shortlist
  CSV used at inference time.
- **`worker/data/`** — the bundled market data (e.g. Tardis LSR CSVs).

> ⚠️ Do **not** add these to Git. They are excluded on purpose (large binaries
> / data). Keep them out of commits and upload them out-of-band to the VPS.

## Why they are ignored

`.gitignore` excludes model files, joblib bundles, CSVs, and runtime data so
the repository stays small and free of large/binary artifacts. Because Git does
not track them, cloning the repo will **not** bring them down — they must be
copied to the VPS separately after cloning.

## Verification commands (run on the VPS)

After uploading, confirm the artifacts are present:

```bash
ls -lah "worker/runtime/model files"
ls -lah "worker/runtime/eth_feature_shortlist_outputs"
ls -lah worker/data
```

Each command should list the expected files (the `.joblib` bundle, the
`.json` config, the `v22_live_engine_export/run_*/` export directory, the
feature shortlist CSV, and the bundled data). If any are empty or missing, the
worker will not have the artifacts it needs — re-upload before running
`docker compose up -d` from `worker/`.
