# Eval Mode

Return JSON with these keys:
- company
- role
- archetype
- score (0.0 to 5.0)
- grade (A-F)
- summary
- keywords (list of 10-20 strings)
- fit_reasons (list of strings)
- gaps (list of strings)
- report_markdown
- resume_headline
- resume_summary_lines
- selected_work_fact_ids
- selected_project_fact_ids
- selected_skill_fact_ids
- custom_bullets
- cover_letter_paragraphs

The markdown report should contain:
- title
- role summary
- fit reasons
- gaps and mitigation
- resume strategy
- interview angles

Additional rules:
- `cover_letter_paragraphs` must contain exactly 3 body paragraphs.
- `cover_letter_paragraphs` must be in FIRST PERSON voice (`I/my/me`). Never use third person.
- `resume_summary_lines` must describe the candidate's ACTUAL skills and experience, not the job requirements.
- Do not echo raw job-description claims into `resume_summary_lines` or `cover_letter_paragraphs`.
