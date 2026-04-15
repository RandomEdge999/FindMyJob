# Screen Mode

Return JSON only. Use this exact schema:
- approved (boolean)
- reasons (list of strings)
- confidence (0.0 to 1.0)
- internship_like (boolean)
- seniority_too_high (boolean)
- years_experience_signal (string or null)
- notes (string or null)

Return JSON only. No thinking, reasoning, or explanation outside the JSON object.

The deterministic filter already removes obvious title rejects. Your job is to classify the remaining jobs conservatively but not narrowly.

Hard reject signals:
- Reject if the title contains: senior, sr, staff, principal, lead, architect, director, vp, head, chief, manager, distinguished, fellow.
- Reject internships, apprenticeships, co-ops, and fellowships.
- Reject only if the posting explicitly requires 7+ years of experience.
- Reject only if the posting explicitly requires a graduate degree: PhD required or Masters required. Do not reject when those degrees are only preferred.

Approve signals:
- Approve any role without an explicit seniority marker when the title or description matches any of these target keywords: engineer, developer, software, ml, machine learning, data, python, research.
- If the title does NOT contain a seniority marker (senior, sr, staff, principal, lead, architect, director, manager, vp, head, chief), default to approved=true.
- The candidate is applying broadly to early-career AND mid-level roles.
- A job requiring 2-5 years of experience IS appropriate for this candidate.

Confidence rules:
- Set confidence to 0.8 or higher when the decision is clear.
- Do not use 0.2 unless you genuinely cannot tell from the posting.
- Keep the booleans and reasons internally consistent.
- Low-confidence rejections must stay rejected and be flagged for human review; do not auto-approve them.

Approved example:
{
  "approved": true,
  "reasons": ["Title and description match software/data targets with no seniority marker."],
  "confidence": 0.92,
  "internship_like": false,
  "seniority_too_high": false,
  "years_experience_signal": "2-5 years",
  "notes": "Broad early-career and mid-level fit."
}

Rejected example:
{
  "approved": false,
  "reasons": ["Posting explicitly requires 8+ years of experience."],
  "confidence": 0.9,
  "internship_like": false,
  "seniority_too_high": true,
  "years_experience_signal": "8+ years",
  "notes": "Explicit seniority requirement."
}
