#!/usr/bin/env python3
"""
Convert a LinkedIn "Save to PDF" export into a clean, single-column,
ATS-friendly CV PDF.

Usage:
    python3 generate_ats_cv.py <input.pdf> <output.pdf> [target_role]

Approach:
  1. LinkedIn's export renders a dark sidebar (Contact/Skills/Languages/
     Certifications/Honors) next to a main column (Name/Summary/Experience/
     Education). A naive PDF-to-text pass interleaves both columns line by
     line into gibberish. We split each page by x-coordinate first, then
     extract text per column so the reading order stays correct.
  2. The sidebar and main column are parsed into sections using LinkedIn's
     standard headers.
  3. Rule-based cleanup removes noisy/irrelevant fields, de-duplicates
     repeated buzzwords, trims an over-stuffed headline, and builds a single
     explicit Skills list from both the sidebar and each role's tech stack.
  4. The result is rendered as plain single-column HTML (no tables, no text
     boxes, no icons-as-content) and converted to PDF with WeasyPrint, which
     produces text-selectable, parser-friendly PDF output.
"""
import re
import sys
from pathlib import Path

import pdfplumber
from weasyprint import HTML

SIDEBAR_SPLIT_X = 190
MAX_HEADLINE_SEGMENTS = 3
DROP_SECTIONS = {"certifications", "honors-awards"}

DATE_RANGE_RE = re.compile(
    r"^[A-Za_]*[A-Za-z]+\s+\d{4}\s*-\s*(Present|[A-Za-z]+\s+\d{4})\s*\(.*\)\s*$"
)
YEARS_ONLY_RE = re.compile(r"^\d+\s+years?(\s+\d+\s+months?)?$")
PAGE_FOOTER_RE = re.compile(r"^Page \d+ of \d+$")
# LinkedIn's PDF export sometimes renders a bullet glyph (e.g. a sparkle icon)
# as a stray 1-2 letter text fragment on its own line; drop those.
GLYPH_ARTIFACT_RE = re.compile(r"^[A-Za-z]{1,2}$")


def join_wrapped(buf, addition):
    """Join a line-wrapped continuation, collapsing a trailing soft hyphen
    (e.g. "non-" + "performing" -> "non-performing" instead of "non- performing")."""
    addition = addition.strip()
    if buf.endswith("-") and addition[:1].islower():
        return buf + addition
    return f"{buf} {addition}" if buf else addition


def extract_columns(pdf_path):
    """Return (sidebar_text, main_text) preserving each column's reading order."""
    sidebar_lines, main_lines = [], []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            w, h = page.width, page.height
            left = page.crop((0, 0, SIDEBAR_SPLIT_X, h)).extract_text() or ""
            right = page.crop((SIDEBAR_SPLIT_X, 0, w, h)).extract_text() or ""
            sidebar_lines += [
                ln.strip()
                for ln in left.splitlines()
                if ln.strip() and not GLYPH_ARTIFACT_RE.match(ln.strip())
            ]
            main_lines += [
                ln.strip()
                for ln in right.splitlines()
                if ln.strip()
                and not PAGE_FOOTER_RE.match(ln.strip())
                and not GLYPH_ARTIFACT_RE.match(ln.strip())
            ]
    return sidebar_lines, main_lines


def split_sections(lines, headers):
    """Split a flat list of lines into {header: [lines]} using exact-match header lines."""
    sections, current = {}, None
    header_set = {h.lower() for h in headers}
    for ln in lines:
        key = ln.strip().lower()
        if key in header_set:
            current = key
            sections[current] = []
        elif current:
            sections[current].append(ln.strip())
    return sections


def parse_sidebar(lines):
    sections = split_sections(
        lines, ["Contact", "Top Skills", "Languages", "Certifications", "Honors-Awards"]
    )

    contact_lines = sections.get("contact", [])
    email = next((l for l in contact_lines if "@" in l), "")
    links = [l for l in contact_lines if l.startswith(("www.", "github.com", "http"))]

    def rejoin(raw_lines):
        """Merge a sidebar's word-wrapped bullet list back into single entries."""
        items, buf = [], ""
        for l in raw_lines:
            open_parens = buf.count("(") - buf.count(")")
            if l.startswith("(") or l[:1].islower() or open_parens > 0:
                buf = join_wrapped(buf, l)
            else:
                if buf:
                    items.append(buf.strip())
                buf = l
        if buf:
            items.append(buf.strip())
        return items

    top_skills = rejoin(sections.get("top skills", []))
    languages = rejoin(sections.get("languages", []))
    return {"email": email, "links": links, "top_skills": top_skills, "languages": languages}


def parse_main(lines):
    # Find "Summary" header to split off the profile header block (name/headline/location)
    try:
        summary_idx = next(i for i, l in enumerate(lines) if l.strip().lower() == "summary")
    except StopIteration:
        summary_idx = len(lines)

    header_block = lines[:summary_idx]
    name = header_block[0] if header_block else ""
    location = header_block[-1] if len(header_block) > 1 else ""
    headline_lines = header_block[1:-1] if len(header_block) > 2 else []
    headline_raw = " ".join(headline_lines).replace("  ", " ").strip()

    body = lines[summary_idx:]
    sections = split_sections(body, ["Summary", "Experience", "Education"])
    summary = ""
    for l in sections.get("summary", []):
        summary = join_wrapped(summary, l)
    summary = summary.strip()
    experience_lines = sections.get("experience", [])
    education_lines = sections.get("education", [])

    return {
        "name": name,
        "headline_raw": headline_raw,
        "location": location,
        "summary": summary,
        "experience_lines": experience_lines,
        "education_lines": education_lines,
    }


def clean_headline(headline_raw):
    segments = [s.strip(" |") for s in headline_raw.split("|") if s.strip(" |")]
    trimmed = segments[:MAX_HEADLINE_SEGMENTS]
    return " | ".join(trimmed)


def clean_summary(summary):
    text = summary.replace("&amp;", "&")
    text = re.sub(r"\s+", " ", text).strip()
    # Collapse repeated "AI-Augmented"/"AI-augmented" style buzzword drumming to one mention
    matches = list(re.finditer(r"AI[- ]?[Aa]ugmented", text))
    if len(matches) > 1:
        keep_end = matches[0].end()
        first_part = text[:keep_end]
        rest = text[keep_end:]
        rest = re.sub(r"\bAI[- ]?[Aa]ugmented\b", "AI-assisted", rest)
        text = first_part + rest
    return text


TECH_LABEL_RE = re.compile(
    r"^(Languages? & Frameworks|Databases?|Infrastructure(?:\s*&\s*Tools)?|Stack)\s*:\s*(.+)$",
    re.IGNORECASE,
)
TECH_HEADER_RE = re.compile(
    r"^Tech(nology)?\s*Stack(\s*(&|and)\s*Tools)?\s*:?\s*$", re.IGNORECASE
)


def is_tech_header(line):
    # e.g. "✨ Tech Stack & Tools:" -- strip the leading emoji/symbol first so
    # we only match the standalone section header, not a bullet title like
    # "Tech Stack Evolution:" which merely contains the same words.
    stripped = re.sub(r"^[^A-Za-z]+", "", line).strip()
    return bool(TECH_HEADER_RE.match(stripped))


def parse_experience(lines):
    """Split the Experience section into individual role entries."""
    date_idx = [i for i, l in enumerate(lines) if DATE_RANGE_RE.match(l)]
    roles = []

    def looks_like_company(line):
        # A real company/header line is Title-Case-ish and not a stray tail
        # end of the previous role's closing paragraph (which ends in "." or
        # continues mid-sentence in lowercase).
        return bool(line) and not line.endswith(".") and not line[:1].islower()

    for n, d in enumerate(date_idx):
        title = lines[d - 1] if d - 1 >= 0 else ""
        back = d - 2
        company = ""
        if back >= 0 and YEARS_ONLY_RE.match(lines[back]):
            back -= 1
        if back >= 0 and looks_like_company(lines[back]):
            company = lines[back]
        elif roles:
            # Second+ role grouped under one company header (LinkedIn only
            # prints the company name once for consecutive roles there).
            company = roles[-1]["company"]

        loc = lines[d + 1] if d + 1 < len(lines) else ""
        body_start = d + 2
        body_end = date_idx[n + 1] - 1 if n + 1 < len(date_idx) else len(lines)
        # walk back from body_end to exclude the next role's company/title lines already claimed
        if n + 1 < len(date_idx):
            next_d = date_idx[n + 1]
            back2 = next_d - 2
            if back2 >= 0 and YEARS_ONLY_RE.match(lines[back2]):
                back2 -= 1
            body_end = min(body_end, back2)

        raw_body = [l for l in lines[body_start:body_end] if l]

        bullets, tech = [], []
        buf = ""
        # intro: free-text paragraph before the first bullet (dropped, bullets
        # already restate the same impact) -> bullets -> techblock (once a
        # "Tech Stack" style line appears, the rest of the role is tech/fluff)
        mode = "intro"
        for l in raw_body:
            stripped = l.lstrip("-•* ").strip()
            if is_tech_header(l):
                mode = "techblock"
                continue
            m = TECH_LABEL_RE.match(stripped)
            if m:
                mode = "techblock"
                tech.extend(part.strip() for part in m.group(2).split(",") if part.strip())
                continue
            if mode == "techblock":
                # short orphan continuation of a wrapped tech line, e.g. "(Feature Flags)"
                if tech and len(l) < 40 and not l.endswith("."):
                    tech[-1] = tech[-1] + " " + l.strip()
                continue
            if l.startswith("- "):
                mode = "bullets"
                if buf:
                    bullets.append(buf.strip())
                buf = l[2:].strip()
            elif mode == "bullets" and buf:
                buf = join_wrapped(buf, l)
        if buf:
            bullets.append(buf.strip())

        roles.append(
            {
                "company": company,
                "title": title,
                "dates": lines[d],
                "location": loc,
                "bullets": [b for b in bullets if b],
                "tech": tech,
            }
        )
    return roles


def build_skills(sidebar_skills, roles):
    skills = []
    seen = set()
    for s in sidebar_skills + [t for r in roles for t in r["tech"]]:
        key = s.lower().strip()
        if key and key not in seen and len(s) < 40:
            seen.add(key)
            skills.append(s)
    return skills


def render_html(data, target_role=""):
    def esc(s):
        return (
            (s or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    roles_html = ""
    for r in data["roles"]:
        bullets_html = "".join(f"<li>{esc(b)}</li>" for b in r["bullets"])
        roles_html += f"""
        <div class="role">
          <div class="role-line"><span class="company">{esc(r['company'])}</span> &mdash; {esc(r['title'])}</div>
          <div class="meta">{esc(r['dates'])} | {esc(r['location'])}</div>
          <ul>{bullets_html}</ul>
        </div>
        """

    education_html = "<br>".join(esc(l) for l in data["education_lines"])
    languages_html = ", ".join(esc(l) for l in data["languages"])
    skills_html = ", ".join(esc(s) for s in data["skills"])
    links_html = " | ".join(esc(l) for l in data["links"])
    target_note = f"<div class=\"target\">Target role: {esc(target_role)}</div>" if target_role else ""

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<style>
  @page {{ size: A4; margin: 2cm; }}
  body {{ font-family: Arial, Helvetica, sans-serif; font-size: 10.5pt; color: #111; line-height: 1.35; }}
  h1 {{ font-size: 18pt; margin: 0 0 2px 0; }}
  .headline {{ font-size: 11pt; color: #333; margin: 0 0 2px 0; }}
  .contact {{ font-size: 9.5pt; color: #333; margin-bottom: 10px; }}
  h2 {{ font-size: 12pt; text-transform: uppercase; letter-spacing: 0.5px;
        border-bottom: 1px solid #999; margin: 14px 0 6px 0; padding-bottom: 2px; }}
  .role {{ margin-bottom: 10px; }}
  .role-line {{ font-weight: bold; }}
  .meta {{ color: #555; font-size: 9.5pt; margin-bottom: 3px; }}
  ul {{ margin: 2px 0 0 18px; padding: 0; }}
  li {{ margin-bottom: 2px; }}
  .target {{ font-size: 9pt; color: #666; font-style: italic; margin-bottom: 6px; }}
</style></head>
<body>
  <h1>{esc(data['name'])}</h1>
  <div class="headline">{esc(data['headline'])}</div>
  <div class="contact">{esc(data['location'])}{' | ' + esc(data['email']) if data['email'] else ''}{' | ' + links_html if links_html else ''}</div>
  {target_note}

  <h2>Summary</h2>
  <p>{esc(data['summary'])}</p>

  <h2>Skills</h2>
  <p>{skills_html}</p>

  <h2>Experience</h2>
  {roles_html}

  <h2>Education</h2>
  <p>{education_html}</p>

  {"<h2>Languages</h2><p>" + languages_html + "</p>" if languages_html else ""}
</body></html>"""


def main():
    if len(sys.argv) < 3:
        print("Usage: generate_ats_cv.py <input.pdf> <output.pdf> [target_role]", file=sys.stderr)
        sys.exit(1)

    input_pdf, output_pdf = sys.argv[1], sys.argv[2]
    target_role = sys.argv[3] if len(sys.argv) > 3 else ""

    sidebar_lines, main_lines = extract_columns(input_pdf)
    sidebar = parse_sidebar(sidebar_lines)
    main = parse_main(main_lines)
    roles = parse_experience(main["experience_lines"])

    data = {
        "name": main["name"],
        "headline": clean_headline(main["headline_raw"]),
        "location": main["location"],
        "email": sidebar["email"],
        "links": sidebar["links"],
        "summary": clean_summary(main["summary"]),
        "skills": build_skills(sidebar["top_skills"], roles),
        "roles": roles,
        "education_lines": main["education_lines"],
        "languages": sidebar["languages"],
    }

    html = render_html(data, target_role)
    Path(output_pdf).parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html).write_pdf(output_pdf)
    print(f"Wrote {output_pdf}")


if __name__ == "__main__":
    main()
