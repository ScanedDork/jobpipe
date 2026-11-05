# ATS rules and scoring rubric

This page lists the rules jobpipe applies and the points behind each grade. The constants
live in `src/jobpipe/validators.py` and `src/jobpipe/scorer.py`. Change them there and the
tests will show you what moved.

## Layout rules (resume and cover letter)

These rules are built into `render.py` and checked again by re-opening the files in tests:

- A4 page, one column, 1.27-2.54 cm margins.
- A standard font (Calibri, Arial, Aptos, Cambria, Garamond, Georgia, Helvetica, Times New
  Roman, Verdana, Tahoma, ...), black text, theme fonts removed so Word cannot substitute
  another font.
- Real Word `Heading 1` paragraphs with standard names: Professional Summary / Profile,
  Skills, Professional Experience / Work Experience, Education, Certifications, Languages.
- Real Word bullets (`List Bullet`), with no decorative glyphs.
- Contact details in the document body. Many parsers skip page headers and footers.
- No tables, text boxes, images, icons or shapes.
- One page is ideal and two is the maximum. If the estimated length exceeds 2 pages,
  `render_resume` leaves out the least relevant bullet of the oldest role and re-renders,
  repeating as needed. Older roles keep at least 2 bullets and the most recent role keeps
  at least 3. Every bullet left out is listed in the report's generation notes.

## Keyword extraction

- The vocabulary in `keywords.py` covers tools, hard skills, job titles, seniority terms
  and languages, each with common synonyms (for example `customer success` / `client
  success`, `QBR` / `quarterly business reviews`, `GTM` / `go-to-market`). Unknown
  acronyms in the posting (such as `DACH`) are kept as low-weight "other" keywords.
- Posting sections are detected from their headings. "Requirements", "What you bring",
  "Your profile" and similar get weight 2. "Nice to have", "Bonus" and "Preferred" get
  0.5. Everything else gets 1. Terms in the job title always get weight 2.
- Longer terms claim their text first, so "net revenue retention" is not also counted as
  "retention". The company's own name is never a keyword.
- Matching is whole-word and ignores case, except for tool names that are also ordinary
  words (`Excel`, `Notion`, `Slack`, ...). Singular and plural forms both count as exact.

## Score categories

The overall score is a weighted average:

| Category | Weight with a posting | Weight without |
|---|---|---|
| Keyword match | 40 | (not scored) |
| Structure & parseability | 20 | 35 |
| Bullet quality | 20 | 35 |
| Length | 10 | 15 |
| Rule violations | 10 | 15 |
| Cover letter content (when a letter is given) | +10 | +10 |

Any hard-rule violation caps the overall score at 59.

Grades: A ≥ 90, B ≥ 80, C ≥ 70, D ≥ 60, F below 60.

### Keyword match

Weighted coverage of posting keywords in the resume: exact phrase = 100%, synonym only =
75%, missing = 0. Seniority terms and "N+ years" are reported but not scored, because a
resume cannot honestly "contain" them.

### Structure & parseability (start at 100)

| Deduction | Issue |
|---|---|
| -30 | tables |
| -30 | text boxes |
| -30 | more than one column |
| -20 | images, icons or shapes |
| -15 each | missing Professional Experience, Education or Skills heading |
| -10 | text in a header or footer |
| -10 | non-standard font |
| -10 | no email address in the first lines |
| -5 | no summary/profile section |
| -5 each | non-standard heading |
| -5 | no phone number |
| -5 | decorative symbols or emoji |
| -3 | no LinkedIn URL |

### Bullet quality

Only bullets under the experience heading are scored. Each bullet earns 40 points for
leading with an action verb, 40 for containing a number, and 20 for being clean: no weak
phrase ("responsible for", "helped with", "various"...), no I/my, and 8-30 words long. The
category score is the average. Fewer than 3 bullets caps it at 50, and it is a red flag
when fewer than half the bullets are quantified.

### Length

- Resume: under 250 words = 40 (red flag), over about 2 pages = 40 (red flag), under 350
  words = 75, otherwise 100. Pages are estimated from line counts.
- Cover letter body (salutation and sign-off excluded): 100 minus 1 point per word
  outside 250-350.
- With a cover letter, the category is 70% resume and 30% letter.

### Rule violations

100 minus 40 per hard-rule violation, minus 25 if any placeholder (TODO, [Company], ...)
is left. Hard rules: banned cover letter opening, non-canonical work-authorisation
wording, relocation reasons or personal relationships, and file names that break the
`{Name}_Resume_{Company}.docx` / `{Name}_CoverLetter_{Company}.docx` patterns.

### Cover letter content (start at 100)

| Deduction | Issue |
|---|---|
| -30 | the company is never named (no specific hook) |
| -15 | no quantified evidence at all |
| -10 | only one quantified evidence point (the target is 2-3) |
| -10 | fewer than 3 or more than 5 body paragraphs |
| -10 each | a cliché ("team player", "passionate about", "to whom it may concern", ...) |

## Claims check

`validators.claims_check` compares generated text with profile facts:

- **Numbers**: every number (digits or words such as "six" or "doubled") must appear in
  the profile.
- **Vocabulary terms**: any tool, skill, title or language from the keyword vocabulary
  must appear in the profile, either directly or as a synonym.
- **Proper nouns**: names of companies, clients, places and products must appear in the
  profile. For cover letters, names from the posting are also allowed.
- **Drift** (bullets only): if more than 30% of a rephrased bullet's content words (and
  more than two) appear nowhere in its source bullet and role, the bullet is rejected.

Cover letters are checked sentence by sentence, and only first-person sentences ("I",
"my", "me") count as claims. A sentence describing the company is not a claim about you.
Rejected resume text reverts to the profile wording. Rejected letters are retried with
feedback, then replaced by the deterministic fact-only letter.
