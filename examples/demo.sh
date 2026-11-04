#!/usr/bin/env bash
# End-to-end demo with the FAKE example profile and the dry-run provider (no API keys,
# no network). Produces output/Taylor_Sample_Resume_Acme.docx,
# output/Taylor_Sample_CoverLetter_Acme.docx and output/Acme_ats_report.md.
set -euo pipefail
cd "$(dirname "$0")/.."
export JOBPIPE_PROVIDER=dry-run
DB=output/demo.db

jobpipe --db "$DB" apply \
  --job examples/posting_acme.txt \
  --company "Acme" \
  --profile profile/profile.example.yaml
jobpipe --db "$DB" list
jobpipe --db "$DB" status 1 applied
jobpipe --db "$DB" export --csv --out output/applications.csv
jobpipe score \
  --resume output/Taylor_Sample_Resume_Acme.docx \
  --cover output/Taylor_Sample_CoverLetter_Acme.docx \
  --job examples/posting_acme.txt \
  --out output/rescore.md
