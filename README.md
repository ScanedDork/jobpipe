# jobpipe

`jobpipe` turns a job posting into a tailored resume, a cover letter and an honest ATS
audit, then logs the application in a local tracker. It targets the European market
(Czech Republic, Germany, the wider EU): A4 pages, single-column layouts, British English
and CEFR language levels.

```bash
jobpipe apply --job posting.txt --company "Acme"
```

produces

```text
output/Ranjeet_Skanda_Resume_Acme.docx
output/Ranjeet_Skanda_CoverLetter_Acme.docx
output/Acme_ats_report.md
```

and adds a row to the tracker (SQLite, exportable to CSV).

Every fact in the generated documents comes from `profile/profile.yaml`. The LLM may
reorder, emphasise and rephrase. A claims check rejects anything it adds.

## Setup

Python 3.11+ is required.

With [uv](https://docs.astral.sh/uv/):

```bash
uv venv && uv pip install -e ".[dev]"
```

or with pip:

```bash
python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
```

Then create your profile from the template:

```bash
cp profile/profile.template.yaml profile/profile.yaml
```

`profile/profile.yaml` is git-ignored so your real details never get committed. The
template is pre-filled with the facts you already have. Everything still marked `TODO`
(contact details, achievement bullets with real metrics, skills, languages, years) is left
out of the documents and listed under "Profile gaps" in every ATS report until you fill
it in.

## Quick start: the demo (no API key needed)

The repository ships a fake profile (`profile/profile.example.yaml`) and a sample posting
(`examples/posting_acme.txt`). The demo uses the deterministic `dry-run` provider, so it
needs no network access:

```bash
./examples/demo.sh
```

or step by step:

```bash
export JOBPIPE_PROVIDER=dry-run
jobpipe apply --job examples/posting_acme.txt --company "Acme" --profile profile/profile.example.yaml
```

```text
Resume:       output/Taylor_Sample_Resume_Acme.docx
Cover letter: output/Taylor_Sample_CoverLetter_Acme.docx
ATS report:   output/Acme_ats_report.md
ATS score:    87/100 (B)
Tracked as application #1 (status: prepared)
  ! Missing must-have keywords: Customer Success Manager, CRM, KPI
```

The report this run produces is in
[`examples/sample_output/Acme_ats_report.md`](examples/sample_output/Acme_ats_report.md).

## Commands

| Command | What it does |
|---|---|
| `jobpipe apply --job FILE --company NAME [--role R] [--url U] [--profile P] [--out DIR] [--template T]` | Tailor, render, score and log one application. The role defaults to the posting's first line; the URL defaults to the first link in the posting. |
| `jobpipe score --resume FILE.docx [--cover FILE.docx] [--job FILE.txt] [--out REPORT.md]` | Score any resume, including one jobpipe did not make. Keyword match needs `--job`. |
| `jobpipe list` | Show tracked applications. |
| `jobpipe status ID STATUS` | Set the status: `prepared`, `applied`, `interview`, `offer`, `rejected` or `withdrawn`. |
| `jobpipe export --csv [--out FILE]` | Export the tracker to CSV (default `output/applications.csv`). |

The tracker lives in `data/jobpipe.db`. Use `--db PATH` before the command, or set
`JOBPIPE_DB`, to use another file. New rows start as `prepared` because jobpipe writes the
documents but does not send them. Run `jobpipe status ID applied` once you have applied.
Tracker columns: `id, date, company, role, url, status, resume_path, cover_path,
ats_score, notes`.

## LLM providers

The provider is chosen from environment variables, in this order:

| Priority | Set | Provider |
|---|---|---|
| 1 | `JOBPIPE_PROVIDER=anthropic`, `openai` (alias `ollama`) or `dry-run` | the one you name |
| 2 | `ANTHROPIC_API_KEY` | Anthropic Messages API |
| 3 | `OPENAI_BASE_URL` or `OPENAI_API_KEY` (+ `MODEL`) | any OpenAI-compatible endpoint |
| 4 | nothing | `dry-run`, with a warning |

**Anthropic**

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export ANTHROPIC_MODEL=claude-opus-5-5   # optional; this is the default
```

**OpenAI or any OpenAI-compatible server**

```bash
export OPENAI_API_KEY=sk-...
export MODEL=gpt-4o-mini
# OPENAI_BASE_URL defaults to https://api.openai.com/v1
```

**Ollama (local, free)**

```bash
ollama pull llama3.1
export OPENAI_BASE_URL=http://localhost:11434/v1
export MODEL=llama3.1
```

No API key is needed for Ollama. LM Studio, vLLM and similar servers work the same way
with their own base URL.

**Dry run**

```bash
export JOBPIPE_PROVIDER=dry-run
```

The dry-run provider never touches the network. It returns the deterministic, fact-only
text the pipeline would otherwise use as its fallback. Tests and CI always use it.

Prompts are plain text files in [`prompts/`](prompts/) (`system.txt`,
`tailor_bullets.txt`, `summary.txt`, `cover_letter.txt`). Each one restates the hard rules.
You can edit them freely: values such as `$company` are filled in with Python's
`string.Template`, and a prompt with a missing value fails loudly instead of being sent.

## Hard rules and how they are enforced

| Rule | Enforcement |
|---|---|
| A cover letter never opens with "I am writing to apply for" or a close variant ("I am writing to express my interest", "I would like to apply", "Please accept this letter", ...) | `validators.check_cover_letter_opening` rejects the draft. The generator retries with the reason as feedback (3 attempts), then falls back to a fact-only letter that always passes. |
| Work authorisation is only ever "EU work authorised, residency in process — no sponsorship required", with no relocation reasons or personal relationships | `validators.check_work_authorisation` flags any other wording (visa, permit, sponsorship, residence, citizenship, relocation, spouse/partner/family). Checked when the profile loads, on every generated sentence, and again by the scorer. |
| Files are named `{Name}_Resume_{Company}.docx`, `{Name}_CoverLetter_{Company}.docx` and `{Company}_ats_report.md` | `validators.output_filenames` builds the names from `name.preferred` and the company, sanitised to ASCII letters, digits, `-` and `_` (`Škoda Auto a.s.` becomes `Skoda_Auto_a_s`; path separators and Windows reserved names are neutralised). The scorer checks the names too. |
| Only facts from `profile.yaml` | `validators.claims_check` compares each generated bullet, summary and first-person letter sentence with the profile. It flags numbers, tools, skills, titles, languages and proper nouns the profile does not contain, and bullets that drift too far from their source. Flagged bullets revert to your own wording, and the report lists every rejection. |

If a finished document still breaks a hard rule, `apply` exits with code 1 and logs
nothing to the tracker.

## ATS scoring

`jobpipe score` (and every `apply`) grades documents from 0 to 100 with a letter grade per
category. The scoring is strict on purpose: B means safe to send, and A is rare.

| Category | Checks |
|---|---|
| Keyword match (needs a posting) | Tools, hard skills, titles and languages from the posting, weighted by section ("requirements" x2, "nice to have" x0.5). An exact phrase earns full credit; a synonym earns 75%, since many ATS miss synonyms. Seniority terms and years of experience are reported but not scored. |
| Structure & parseability | Tables, text boxes, images or icons, multiple columns, header/footer text, non-standard fonts, missing or non-standard headings, contact details, decorative symbols. |
| Bullet quality | Action verb first, a number in the bullet, weak phrases ("responsible for"), pronouns, length. |
| Length | Resume words and estimated pages (1-2); cover letter body of 250-350 words. |
| Rule violations | The hard rules above, plus leftover placeholders. Any hard violation caps the overall score at 59 (F). |
| Cover letter content | A company-specific hook, quantified evidence, 3-5 paragraphs, no clichés. |

The markdown report lists red flags, line-level fixes, a full keyword table (resume and
cover letter), notes for each category and, from `apply`, the generation notes, the claims
check and your profile gaps. See [`docs/ats-rules.md`](docs/ats-rules.md) for the full
rubric.

## Templates

Templates are YAML files in [`templates/`](templates/). Two are included: `classic`
(Calibri 11 pt, the default) and `compact` (Arial 10 pt, experience before skills). Select
one with `--template compact`, or pass the path to your own YAML file.

To add a template:

1. Copy `templates/classic.yaml` to `templates/<name>.yaml`.
2. Change the values:
   - `font`: a standard font (Calibri, Arial, Garamond, Georgia, Cambria, Helvetica, Times
     New Roman, Verdana, Tahoma, Aptos, ...)
   - `body_size` (9.5-12), `heading_size` (10-14), `name_size` (12-20)
   - `margins_cm` (1.27-2.54), `space_after_pt` (0-8)
   - `headings`: the heading for each section, which must be a standard name such as
     `Professional Experience`, `Work Experience`, `Skills`, `Key Skills`, `Summary`,
     `Profile`, `Education` or `Certifications`
   - `section_order`: any order of `summary`, `skills`, `experience`, `education`,
     `certifications` and `languages`, as long as it includes experience, education and
     skills
3. Run `jobpipe apply ... --template <name>`.

`load_template` validates every value, so a template cannot introduce an unsafe font,
an unrecognised heading or a missing core section. Layout features that hurt parsing
(tables, columns, text boxes, icons, headers) have no settings at all.

## Development

```bash
pytest           # 160+ tests, no network access
ruff check .
```

GitHub Actions (`.github/workflows/ci.yml`) runs ruff, pytest and the demo on Python
3.11-3.13 with `JOBPIPE_PROVIDER=dry-run`. No API key is configured there, and the test
suite blocks any network call.

```text
src/jobpipe/
  cli.py         Typer commands
  profile.py     profile.yaml loading and validation
  keywords.py    vocabulary, synonyms, posting keyword extraction and matching
  tailor.py      relevance ordering, LLM rephrasing, cover letter retry/fallback
  render.py      .docx rendering and template loading
  scorer.py      ATS scoring and the markdown report
  validators.py  hard rules, claims check, file names
  tracker.py     SQLite tracker and CSV export
  llm.py         LLMClient, providers, prompt loading
prompts/         prompt text files
templates/       resume/cover letter templates
profile/         profile.template.yaml (yours) and profile.example.yaml (fake)
examples/        sample posting, demo script, sample report
docs/            ATS rules and scoring rubric
tests/
```
