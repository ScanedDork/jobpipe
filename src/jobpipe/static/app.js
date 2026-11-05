/* jobpipe web interface. Vanilla JS, no build step, no external requests.
   All server data is inserted with textContent / DOM nodes, never as HTML. */
(() => {
  "use strict";

  const TABS = ["apply", "tracker", "score", "profile"];
  const FALLBACK_STATUSES = ["prepared", "applied", "interview", "offer", "rejected", "withdrawn"];
  const state = { status: null, tab: null };
  const $ = (sel, root = document) => root.querySelector(sel);

  // --- helpers --------------------------------------------------------------------------
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") el.className = value;
      else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
      else el.setAttribute(key, value === true ? "" : String(value));
    }
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return el;
  }

  async function api(path, options = {}) {
    const res = await fetch(path, options);
    const type = res.headers.get("content-type") || "";
    const body = type.includes("application/json") ? await res.json() : null;
    if (!res.ok) {
      let message = `Request failed (${res.status})`;
      if (body && typeof body.detail === "string") message = body.detail;
      else if (body && Array.isArray(body.detail)) {
        message = body.detail.map((d) => `${(d.loc || []).slice(-1)[0]}: ${d.msg}`).join("; ");
      }
      throw new Error(message);
    }
    return body;
  }

  function toast(message) {
    const el = $("#toast");
    el.textContent = message;
    el.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { el.hidden = true; }, 2800);
  }

  const gradeOf = (score) => (score >= 90 ? "A" : score >= 80 ? "B" : score >= 70 ? "C" : score >= 60 ? "D" : "F");
  const gradeClass = (grade) => (grade === "A" || grade === "B" ? "g-good" : grade === "C" ? "g-warn" : "g-bad");
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  const alertBox = (kind, ...children) => h("div", { class: `alert ${kind}` }, ...children);
  const prefersReducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function setBusy(prefix, busy) {
    $(`#${prefix}-submit`).disabled = busy;
    $(`#${prefix}-progress`).hidden = !busy;
  }

  function showFormError(prefix, message) {
    const box = $(`#${prefix}-error`);
    box.textContent = message || "";
    box.hidden = !message;
  }

  function revealResult(box) {
    const heading = box.querySelector("h2");
    if (heading) {
      heading.setAttribute("tabindex", "-1");
      heading.focus({ preventScroll: true });
    }
    if (window.innerWidth <= 960) {
      box.scrollIntoView({ behavior: prefersReducedMotion() ? "auto" : "smooth", block: "start" });
    }
  }

  // Minimal markdown for report sections: "- " lists (one nested level), **bold**, `code`.
  function inline(text) {
    return text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).filter(Boolean).map((part) => {
      if (part.length > 4 && part.startsWith("**") && part.endsWith("**")) return h("strong", null, part.slice(2, -2));
      if (part.length > 2 && part.startsWith("`") && part.endsWith("`")) return h("code", null, part.slice(1, -1));
      return part;
    });
  }

  function markdown(text) {
    const root = h("div", { class: "md" });
    let list = null;
    let nested = null;
    let lastItem = null;
    for (const line of text.split("\n")) {
      if (!line.trim()) { list = nested = lastItem = null; continue; }
      const m = line.match(/^(\s*)- (.*)$/);
      if (m && m[1].length >= 2 && lastItem) {
        if (!nested) { nested = h("ul"); lastItem.append(nested); }
        nested.append(h("li", null, inline(m[2])));
      } else if (m) {
        if (!list) { list = h("ul"); root.append(list); }
        nested = null;
        lastItem = h("li", null, inline(m[2]));
        list.append(lastItem);
      } else {
        list = nested = lastItem = null;
        root.append(h("p", null, inline(line)));
      }
    }
    return root;
  }

  // --- status chips -----------------------------------------------------------------------
  async function loadStatus() {
    try {
      state.status = await api("/api/status");
    } catch (err) {
      state.status = null;
      $("#status-chips").replaceChildren(h("span", { class: "chip bad" }, h("span", { class: "dot" }), "Server unreachable"));
      return;
    }
    renderChips();
    const select = $("#apply-template");
    const current = select.value || "classic";
    select.replaceChildren(...state.status.templates.map((t) => h("option", { value: t, selected: t === current }, t)));
  }

  function renderChips() {
    const s = state.status;
    const chips = [];
    if (s.provider_error) {
      chips.push(h("span", { class: "chip bad", title: s.provider_error }, h("span", { class: "dot" }), "LLM misconfigured"));
    } else if (s.provider === "dry-run") {
      const why = s.provider_implicit
        ? "No LLM provider configured: documents use deterministic, fact-only text."
        : "JOBPIPE_PROVIDER=dry-run: documents use deterministic, fact-only text.";
      chips.push(h("span", { class: "chip warn", title: why }, h("span", { class: "dot" }), "Dry run"));
    } else {
      chips.push(h("span", { class: "chip good", title: "LLM provider" }, h("span", { class: "dot" }), `LLM: ${s.provider}`));
    }
    const p = s.profile;
    if (!p.ok) {
      chips.push(h("button", { class: "chip bad", type: "button", onclick: () => showTab("profile") }, h("span", { class: "dot" }), "Profile not loaded"));
    } else {
      const gaps = p.placeholders.length;
      chips.push(h("button", { class: `chip ${gaps ? "warn" : "good"}`, type: "button", title: "Open profile", onclick: () => showTab("profile") },
        h("span", { class: "dot" }), `${p.name} · ${gaps ? plural(gaps, "gap") : "complete"}`));
    }
    $("#status-chips").replaceChildren(...chips);
  }

  // --- tabs -------------------------------------------------------------------------------
  function showTab(name, { focus = false } = {}) {
    if (!TABS.includes(name)) name = "apply";
    state.tab = name;
    for (const tab of TABS) {
      const on = tab === name;
      const button = $(`#tab-btn-${tab}`);
      button.setAttribute("aria-selected", String(on));
      button.tabIndex = on ? 0 : -1;
      $(`#tab-${tab}`).hidden = !on;
    }
    if (focus) $(`#tab-btn-${name}`).focus();
    if (location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
    if (name === "tracker") loadTracker();
    if (name === "profile") renderProfile();
  }

  function bindTabs() {
    for (const button of document.querySelectorAll(".tab")) {
      button.addEventListener("click", () => showTab(button.dataset.tab));
      button.addEventListener("keydown", (ev) => {
        const index = TABS.indexOf(button.dataset.tab);
        let next = null;
        if (ev.key === "ArrowRight") next = TABS[(index + 1) % TABS.length];
        if (ev.key === "ArrowLeft") next = TABS[(index - 1 + TABS.length) % TABS.length];
        if (ev.key === "Home") next = TABS[0];
        if (ev.key === "End") next = TABS[TABS.length - 1];
        if (next) { ev.preventDefault(); showTab(next, { focus: true }); }
      });
    }
    window.addEventListener("hashchange", () => showTab(location.hash.slice(1)));
  }

  // --- report view ------------------------------------------------------------------------
  function ring(score, grade) {
    const NS = "http://www.w3.org/2000/svg";
    const radius = 48;
    const circumference = 2 * Math.PI * radius;
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", "0 0 112 112");
    svg.setAttribute("width", "112");
    svg.setAttribute("height", "112");
    svg.setAttribute("aria-hidden", "true");
    for (const [cls, length] of [["track", circumference], [`value ${gradeClass(grade)}`, (circumference * score) / 100]]) {
      const c = document.createElementNS(NS, "circle");
      for (const [k, v] of Object.entries({ cx: 56, cy: 56, r: radius, fill: "none", "stroke-width": 10, class: cls })) c.setAttribute(k, v);
      c.setAttribute("stroke-dasharray", `${length} ${circumference}`);
      if (cls !== "track") c.setAttribute("stroke-linecap", "round");
      svg.append(c);
    }
    return h("div", { class: "ring", role: "img", "aria-label": `ATS score ${score} out of 100, grade ${grade}` },
      svg, h("div", { class: "ring-value" }, h("span", { class: "ring-number" }, score), h("span", { class: "ring-sub" }, `of 100 · ${grade}`)));
  }

  const GRADE_TEXT = {
    A: "Excellent. Rare on this strict scale.",
    B: "Safe to send. Review the fixes below for an edge.",
    C: "Usable, but fix the red flags before sending.",
    D: "Weak. Several fixes needed before sending.",
    F: "Not ready to send.",
  };

  function downloadButtons(files) {
    const labels = { resume: "Resume (.docx)", cover: "Cover letter (.docx)", report: "ATS report (.md)" };
    const buttons = Object.entries(labels)
      .filter(([kind]) => files && files[kind])
      .map(([kind, label]) => h("a", { class: kind === "report" ? "btn btn-secondary" : "btn btn-primary", href: files[kind].url, download: files[kind].name },
        h("span", { class: "icon", "aria-hidden": "true" }, "↓"), label));
    return buttons.length ? h("div", { class: "downloads" }, buttons) : null;
  }

  function keywordGroups(keywords, seniority) {
    const scored = keywords.filter((k) => k.category !== "seniority");
    const groups = [
      ["Exact phrase found", scored.filter((k) => k.resume === "exact"), "exact"],
      ["Synonym only (use the posting's exact phrase if it is true)", scored.filter((k) => k.resume === "synonym"), "synonym"],
      ["Missing must-haves", scored.filter((k) => k.resume === "missing" && k.weight >= 2), "missing"],
      ["Missing others", scored.filter((k) => k.resume === "missing" && k.weight < 2), "neutral"],
    ].filter(([, items]) => items.length);
    const table = h("table", null,
      h("thead", null, h("tr", null, ["Keyword", "Type", "Weight", "Resume", "Cover letter"].map((t, i) => h("th", { class: i === 2 ? "num" : null, scope: "col" }, t)))),
      h("tbody", null, keywords.map((k) => h("tr", null,
        h("td", null, k.phrase), h("td", null, k.category), h("td", { class: "num" }, k.weight),
        h("td", null, h("span", { class: `kw ${k.resume}` }, k.resume)),
        h("td", null, k.cover ? h("span", { class: `kw ${k.cover}` }, k.cover) : "–")))));
    return [
      h("div", { class: "kw-groups" }, groups.map(([label, items, cls]) => h("div", null,
        h("div", { class: "kw-group-label" }, `${label} (${items.length})`),
        h("div", { class: "kw-chips" }, items.map((k) => h("span", { class: `kw ${cls}` }, k.phrase)))))),
      seniority.length ? h("p", { class: "hint" }, `Seniority signals in the posting (not scored): ${seniority.join(", ")}`) : null,
      h("p", { class: "hint" }, "Add a missing keyword only if it is true: put the evidence in profile.yaml first, then regenerate."),
      h("details", null, h("summary", null, "All keywords"), h("div", { class: "table-wrap" }, table)),
    ];
  }

  function reportView(report, { title, files, coverLetter, extraDownload } = {}) {
    const head = h("div", { class: "card" },
      h("div", { class: "score-head" },
        ring(report.overall, report.grade),
        h("div", { class: "score-text" },
          h("h2", null, title || "ATS report"),
          h("p", null, `${report.overall}/100 (${report.grade}). ${GRADE_TEXT[report.grade]}`),
          report.hard_violations ? h("p", null, h("strong", null, `${plural(report.hard_violations, "hard-rule violation")}: the score is capped at 59.`)) : null)),
      downloadButtons(files), extraDownload || null);

    const categories = h("div", { class: "card" },
      h("h3", { class: "section-title" }, "Categories"),
      h("div", { class: "cats" }, report.categories.map((c) => h("div", { class: "cat" },
        h("span", null, c.label),
        h("div", { class: "bar", role: "img", "aria-label": `${c.label}: ${c.score} out of 100` }, h("span", { class: gradeClass(c.grade), style: `width: ${Math.max(2, c.score)}%` })),
        h("span", { class: "cat-score" }, c.score),
        h("span", { class: `grade ${gradeClass(c.grade)}` }, c.grade)))),
      h("details", null, h("summary", null, "Category notes"),
        h("div", { class: "md" }, report.categories.map((c) => [
          h("p", null, h("strong", null, `${c.label} (${c.score}, ${c.grade})`)),
          h("ul", null, (c.notes.length ? c.notes : ["No deductions."]).map((n) => h("li", null, n))),
        ]))));

    const flags = h("div", { class: "card" },
      h("h3", { class: "section-title" }, `Red flags (${report.red_flags.length})`),
      report.red_flags.length
        ? h("ul", { class: "flags" }, report.red_flags.map((f) => h("li", null, f)))
        : h("ul", { class: "flags none" }, h("li", null, "None found.")));

    const fixes = report.findings.length ? h("div", { class: "card" },
      h("h3", { class: "section-title" }, `Line-level fixes (${report.findings.length})`),
      h("ol", { class: "fixes" }, report.findings.map((f) => h("li", { class: "fix" },
        h("div", { class: "fix-where" }, f.where),
        h("div", { class: "fix-excerpt" }, f.excerpt),
        h("dl", null, h("dt", null, "Problem"), h("dd", null, f.problem), h("dt", null, "Fix"), h("dd", null, f.fix)))))) : null;

    const keywords = report.keywords.length ? h("div", { class: "card" },
      h("h3", { class: "section-title" }, "Keyword match"), keywordGroups(report.keywords, report.seniority)) : null;

    const letter = coverLetter ? h("div", { class: "card" },
      h("details", { class: "flush" }, h("summary", null, "Cover letter text"), h("div", { class: "letter" }, coverLetter))) : null;

    const sections = report.sections.length ? h("div", { class: "card" },
      report.sections.map((s, i) => h("details", { open: s.heading === "Claims check" || s.heading === "Profile gaps", class: i === 0 ? "flush" : null },
        h("summary", null, s.heading), markdown(s.body)))) : null;

    return h("div", { class: "stack" }, head, categories, flags, fixes, keywords, letter, sections);
  }

  // --- new application ----------------------------------------------------------------------
  function bindApplyForm() {
    $("#apply-posting-file").addEventListener("change", (ev) => {
      const file = ev.target.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = () => { $("#apply-posting").value = String(reader.result); toast(`Loaded ${file.name}`); };
      reader.onerror = () => toast(`Could not read ${file.name}`);
      reader.readAsText(file);
      ev.target.value = "";
    });

    $("#apply-form").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      showFormError("apply", "");
      const company = $("#apply-company");
      const posting = $("#apply-posting");
      const missing = [[company, company.value.trim()], [posting, posting.value.trim()]].filter(([, v]) => !v).map(([el]) => el);
      for (const el of [company, posting]) el.setAttribute("aria-invalid", String(missing.includes(el)));
      if (missing.length) {
        showFormError("apply", "Company and job posting are required.");
        missing[0].focus();
        return;
      }
      const payload = {
        company: company.value.trim(),
        posting: posting.value,
        role: $("#apply-role").value.trim() || null,
        url: $("#apply-url").value.trim() || null,
        template: $("#apply-template").value || "classic",
      };
      setBusy("apply", true);
      try {
        const data = await api("/api/apply", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
        renderApplyResult(data);
      } catch (err) {
        showFormError("apply", err.message);
      } finally {
        setBusy("apply", false);
      }
    });
  }

  function renderApplyResult(data) {
    const banner = data.blocked
      ? alertBox("bad", h("strong", null, "Not logged. "), "These documents break a hard rule, so the application was not added to the tracker. See the red flags below.")
      : alertBox("good", h("strong", null, `Logged as application #${data.app_id}`), ` (status: ${data.status}). `,
        h("a", { href: "#tracker" }, "Open tracker"));
    const providerNote = data.provider === "dry-run"
      ? alertBox("warn", "Dry run: no LLM was called. Text comes from your profile wording and the deterministic fallback letter.")
      : null;
    const box = $("#apply-result");
    box.replaceChildren(banner, providerNote || "", reportView(data.report, {
      title: `${data.company} – ${data.role}`, files: data.files, coverLetter: data.cover_letter,
    }));
    revealResult(box);
  }

  // --- tracker --------------------------------------------------------------------------------
  async function loadTracker() {
    const body = $("#tracker-body");
    if (!body.childElementCount) body.replaceChildren(h("p", { class: "muted" }, "Loading…"));
    let rows;
    try {
      rows = await api("/api/applications");
    } catch (err) {
      body.replaceChildren(alertBox("bad", err.message));
      return;
    }
    $("#tracker-count").textContent = rows.length ? plural(rows.length, "application") : "";
    if (!rows.length) {
      body.replaceChildren(h("div", { class: "empty" },
        h("p", { class: "empty-title" }, "No applications yet"),
        h("p", { class: "hint" }, "Generated applications are logged here automatically."),
        h("p", { style: "margin-top: 12px" }, h("a", { class: "btn btn-primary", href: "#apply" }, "New application"))));
      return;
    }
    const statuses = (state.status && state.status.statuses) || FALLBACK_STATUSES;
    const docLinks = (files) => {
      const links = [["resume", "Resume"], ["cover", "Letter"], ["report", "Report"]]
        .filter(([k]) => files[k]).map(([k, label]) => h("a", { href: files[k].url, download: files[k].name }, label));
      return links.length ? h("span", { class: "doc-links" }, links) : h("span", { class: "muted" }, "–");
    };
    const table = h("table", null,
      h("thead", null, h("tr", null,
        h("th", { class: "num", scope: "col" }, "#"), h("th", { scope: "col" }, "Date"), h("th", { scope: "col" }, "Company"),
        h("th", { scope: "col" }, "Role"), h("th", { scope: "col" }, "Status"), h("th", { class: "num", scope: "col" }, "ATS"),
        h("th", { scope: "col" }, "Documents"))),
      h("tbody", null, [...rows].reverse().map((row) => {
        const select = h("select", { "aria-label": `Status of application ${row.id}` },
          statuses.map((s) => h("option", { value: s, selected: s === row.status }, s)));
        select.dataset.previous = row.status;
        select.addEventListener("change", async () => {
          select.disabled = true;
          try {
            await api(`/api/applications/${row.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status: select.value }) });
            select.dataset.previous = select.value;
            toast(`Application #${row.id}: ${select.value}`);
          } catch (err) {
            select.value = select.dataset.previous;
            toast(err.message);
          } finally {
            select.disabled = false;
          }
        });
        const score = row.ats_score;
        return h("tr", null,
          h("td", { class: "num" }, row.id),
          h("td", null, row.date),
          h("td", null, row.company),
          h("td", null, row.url ? h("a", { href: row.url, target: "_blank", rel: "noopener noreferrer" }, row.role || row.url) : row.role || "–"),
          h("td", null, select),
          h("td", { class: "num" }, score === null ? "–" : [String(score), " ", h("span", { class: `grade ${gradeClass(gradeOf(score))}` }, gradeOf(score))]),
          h("td", null, docLinks(row.files)));
      })));
    body.replaceChildren(h("div", { class: "table-wrap" }, table));
  }

  // --- score documents --------------------------------------------------------------------------
  function bindScoreForm() {
    $("#score-form").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      showFormError("score", "");
      const resume = $("#score-resume");
      const cover = $("#score-cover");
      const file = resume.files[0];
      resume.setAttribute("aria-invalid", String(!file));
      if (!file) {
        showFormError("score", "Choose a resume (.docx) to score.");
        resume.focus();
        return;
      }
      const form = new FormData();
      form.append("resume", file);
      if (cover.files[0]) form.append("cover", cover.files[0]);
      form.append("posting", $("#score-posting").value);
      setBusy("score", true);
      try {
        const data = await api("/api/score", { method: "POST", body: form });
        const url = URL.createObjectURL(new Blob([data.markdown], { type: "text/markdown" }));
        const name = `${file.name.replace(/\.docx$/i, "")}_ats_report.md`;
        const box = $("#score-result");
        box.replaceChildren(reportView(data.report, {
          title: file.name,
          extraDownload: h("div", { class: "downloads" }, h("a", { class: "btn btn-secondary", href: url, download: name },
            h("span", { class: "icon", "aria-hidden": "true" }, "↓"), "ATS report (.md)")),
        }));
        revealResult(box);
      } catch (err) {
        showFormError("score", err.message);
      } finally {
        setBusy("score", false);
      }
    });
  }

  // --- profile ----------------------------------------------------------------------------------
  function renderProfile() {
    const body = $("#profile-body");
    const s = state.status;
    if (!s) {
      body.replaceChildren(h("div", { class: "card" }, h("p", { class: "muted" }, "Loading profile…")));
      return;
    }
    const p = s.profile;
    const refresh = h("button", {
      class: "btn btn-secondary", type: "button",
      onclick: async () => { await loadStatus(); renderProfile(); toast("Profile reloaded"); },
    }, "Reload profile");
    if (!p.ok) {
      body.replaceChildren(h("div", { class: "card" },
        h("div", { class: "card-head" }, h("h1", { class: "card-title" }, "Profile"), refresh),
        alertBox("bad", p.error),
        h("p", { class: "hint" }, "Create your profile from the template, fill in real facts, then reload:"),
        h("p", null, h("code", null, "cp profile/profile.template.yaml profile/profile.yaml"))));
      return;
    }
    const facts = [
      ["Full name", p.full_name], ["Headline", p.headline], ["Location", p.location],
      ["Contact", p.contact.join(" · ")], ["Work authorisation", p.work_authorisation],
      ["Profile file", p.path],
    ].filter(([, v]) => v);
    const gaps = p.placeholders;
    const chips = (items, cls = "neutral") => (items.length
      ? h("div", { class: "kw-chips" }, items.map((x) => h("span", { class: `kw ${cls}` }, x)))
      : h("p", { class: "hint" }, "None yet."));

    body.replaceChildren(h("div", { class: "stack" },
      h("div", { class: "card" },
        h("div", { class: "card-head" }, h("h1", { class: "card-title" }, p.name), refresh),
        h("dl", { class: "facts" }, facts.map(([k, v]) => [h("dt", null, k), h("dd", null, v)])),
        p.summary ? h("p", { class: "hint", style: "margin-top: 12px" }, p.summary) : null),
      h("div", { class: "card" },
        h("h2", { class: "section-title" }, `Profile gaps (${gaps.length})`),
        gaps.length
          ? [alertBox("warn", "These fields still hold TODO placeholders, so they are left out of every document. Fill them in ",
            h("code", null, p.path), " with real facts only, then reload."),
          h("ul", { class: "list gap-list" }, gaps.map((g) => h("li", null, g)))]
          : alertBox("good", "No placeholders left. Every field is filled in.")),
      h("div", { class: "profile-grid" },
        h("div", { class: "card" },
          h("h2", { class: "section-title" }, "Experience"),
          p.roles.length ? h("ul", { class: "list" }, p.roles.map((r) => h("li", null,
            h("strong", null, `${r.title}, ${r.company}`), r.dates ? ` (${r.dates})` : "", " · ",
            h("span", { class: r.bullets >= 3 ? "muted" : "" }, plural(r.bullets, "bullet")),
            r.bullets < 3 ? h("span", { class: "grade g-warn", style: "margin-left: 6px" }, "add more") : null)))
            : h("p", { class: "hint" }, "No roles yet.")),
        h("div", { class: "card" },
          h("h2", { class: "section-title" }, "Education & certifications"),
          h("ul", { class: "list" }, p.education.map((e) => h("li", null, e))),
          p.certifications.length ? h("p", { class: "hint", style: "margin-top: 10px" },
            p.certification_hours ? `${p.certification_hours} hours of certifications:` : "Certifications:") : null,
          h("ul", { class: "list" }, p.certifications.map((c) => h("li", null, c)))),
        h("div", { class: "card" },
          h("h2", { class: "section-title" }, "Skills"), chips(p.skills),
          h("h2", { class: "section-title", style: "margin-top: 16px" }, "Languages"), chips(p.languages)),
        h("div", { class: "card" },
          h("h2", { class: "section-title" }, "Target roles"), chips(p.target_roles),
          h("p", { class: "hint", style: "margin-top: 10px" }, "Target roles guide your search but are never used as claims in your documents."))),
      h("p", { class: "hint" }, "The web interface never edits your profile. profile.yaml stays the single source of truth for every fact.")));
  }

  // --- start ------------------------------------------------------------------------------------
  bindTabs();
  bindApplyForm();
  bindScoreForm();
  showTab(location.hash.slice(1) || "apply");
  loadStatus().then(() => { if (state.tab === "profile") renderProfile(); });
})();
