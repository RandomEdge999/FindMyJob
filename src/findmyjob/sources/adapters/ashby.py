from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import httpx

from findmyjob.apply.browser import PlaywrightSubmitter
from findmyjob.apply.forms import extract_questions_from_html, extract_questions_from_lever_fields, merge_extraction_results
from findmyjob.core.enums import ArtifactKind, PolicyMode, QuestionType, SourceKind, SourceRisk
from findmyjob.core.filtering import evaluate_job_against_query
from findmyjob.core.retry import http_get_with_retry, http_post_with_retry
from findmyjob.core.types import ArtifactBinding, FormFieldBinding, NormalizedJobPosting, SourceCapabilities, SubmissionCapturePolicy, SubmissionPlan, SubmissionResult
from findmyjob.sources.base import (
    SourceAdapter,
    _artifact_companion_satisfied,
    _binding_metadata,
    _build_captcha_solver_from_notes,
    _generated_text_binding,
    _is_helper_question,
    _preferred_artifact_kinds_for_question,
    _resolve_option_values,
)
from findmyjob.sources.contracts import DiscoveryQuery, ExtractionResult
from findmyjob.sources.normalizer import build_normalized_job

ASHBY_BOARD_QUERY = """
query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) {
  jobBoard: jobBoardWithTeams(organizationHostedJobsPageName: $organizationHostedJobsPageName) {
    teams {
      id
      name
      externalName
      parentTeamId
    }
    jobPostings {
      id
      title
      employmentType
      locationName
      locationAddress
      workplaceType
      compensationTierSummary
      teamId
    }
  }
}
"""

ASHBY_DETAIL_QUERY = """
query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) {
  jobPosting(
    organizationHostedJobsPageName: $organizationHostedJobsPageName
    jobPostingId: $jobPostingId
  ) {
    id
    title
    departmentName
    departmentExternalName
    teamNames
    locationName
    locationAddress
    workplaceType
    employmentType
    descriptionHtml
    publishedDate
    secondaryLocationNames
    compensationTierSummary
    compensationTiers {
      id
      title
      tierSummary
    }
    scrapeableCompensationSalarySummary
    linkedData
  }
}
"""

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_COMPENSATION_RANGE_RE = re.compile(
    r"(?P<minimum_currency>[$€£])?\s*(?P<minimum>\d+(?:\.\d+)?)\s*(?P<minimum_suffix>[KMB]?)\s*[^\dA-Za-z]{1,3}\s*(?P<maximum_currency>[$€£])?\s*(?P<maximum>\d+(?:\.\d+)?)\s*(?P<maximum_suffix>[KMB]?)",
    re.IGNORECASE,
)
_CURRENCY_MAP = {"$": "USD", "€": "EUR", "£": "GBP"}
_MAGNITUDE_MAP = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}


def _ashby_posting_url(board: str, job_id: str) -> str:
    return f"https://jobs.ashbyhq.com/{board}/{job_id}"


def _ashby_application_url(raw_url: str | None, *, board: str | None = None, job_id: str | None = None) -> str:
    value = str(raw_url or "").strip()
    parsed = urlparse(value) if value else None
    path = str(parsed.path or "").rstrip("/") if parsed is not None else ""
    if path.endswith("/application"):
        return urlunparse(parsed._replace(fragment="")) if parsed is not None else value
    if path and parsed is not None:
        return urlunparse(parsed._replace(path=f"{path}/application", fragment=""))
    board_token = str(board or "").strip().strip("/")
    job_token = str(job_id or "").strip().strip("/")
    return f"{_ashby_posting_url(board_token, job_token)}/application"


def _html_to_text(value: str | None) -> str:
    if not value:
        return ""
    text = _HTML_TAG_RE.sub(" ", value)
    return _WHITESPACE_RE.sub(" ", text).strip()


def _scaled_amount(raw_value: str, suffix: str) -> int | None:
    try:
        numeric = float(str(raw_value).strip())
    except ValueError:
        return None
    multiplier = _MAGNITUDE_MAP.get(str(suffix or "").strip().upper(), 1)
    return int(numeric * multiplier)


def _compensation_rows_from_summary(summary: str | None) -> list[dict[str, object]]:
    text = str(summary or "").strip()
    if not text:
        return []
    rows: list[dict[str, object]] = []
    for match in _COMPENSATION_RANGE_RE.finditer(text):
        minimum = _scaled_amount(match.group("minimum"), match.group("minimum_suffix"))
        maximum = _scaled_amount(match.group("maximum"), match.group("maximum_suffix"))
        currency = _CURRENCY_MAP.get(match.group("minimum_currency") or match.group("maximum_currency") or "")
        if minimum is None and maximum is None:
            continue
        rows.append(
            {
                "min": minimum,
                "max": maximum,
                "currency": currency,
                "interval": "yearly",
            }
        )
    return rows


def _team_lookup(teams: object) -> dict[str, str]:
    if not isinstance(teams, list):
        return {}
    lookup: dict[str, str] = {}
    for item in teams:
        if not isinstance(item, dict):
            continue
        team_id = str(item.get("id") or "").strip()
        label = str(item.get("externalName") or item.get("name") or "").strip()
        if team_id and label:
            lookup[team_id] = label
    return lookup


def _capabilities_payload() -> dict[str, object]:
    return SourceCapabilities(
        adapter_name="ashby",
        source_kind=SourceKind.ASHBY.value,
        policy_mode=PolicyMode.HUMAN_IN_LOOP_SUBMIT,
        risk=SourceRisk.MEDIUM,
        supports_discovery=True,
        supports_apply=True,
        supports_auto_submit=True,
        supported_filters=[
            "title_keywords",
            "locations",
            "countries",
            "regions",
            "cities",
            "workplace_types",
            "employment_types",
            "location_scopes",
            "experience_levels",
            "posted_within_days",
            "compensation_min",
            "compensation_currency",
            "company_size_buckets",
            "remote_only",
            "requires_future_sponsorship",
        ],
        supported_question_types=[
            QuestionType.DETERMINISTIC,
            QuestionType.BOOLEAN,
            QuestionType.SELECT,
            QuestionType.NUMERIC,
            QuestionType.DATE,
            QuestionType.FILE,
        ],
    ).model_dump(mode="json")


def _brief_posting_payload(item: dict, *, board: str, team_name: str | None) -> tuple[NormalizedJobPosting, dict]:
    job_id = str(item.get("id") or "")
    posting_url = _ashby_posting_url(board, job_id)
    compensation_rows = _compensation_rows_from_summary(item.get("compensationTierSummary"))
    payload = dict(item)
    payload["teamName"] = team_name
    payload["compensation"] = compensation_rows or None
    posting = build_normalized_job(
        company_name=board,
        title=item.get("title", "Untitled role"),
        source="ashby",
        source_kind=SourceKind.ASHBY.value,
        source_job_id=job_id,
        posting_url=posting_url,
        apply_url=_ashby_application_url(posting_url),
        location_raw=item.get("locationName"),
        employment_type=item.get("employmentType"),
        compensation=compensation_rows or None,
        description="",
        notes={
            "board": board,
            "team": team_name,
            "capabilities": _capabilities_payload(),
        },
    )
    return posting, payload


async def _fetch_job_detail(client: httpx.AsyncClient, board: str, job_id: str) -> dict:
    response = await http_post_with_retry(
        client,
        "https://jobs.ashbyhq.com/api/non-user-graphql",
        json={
            "query": ASHBY_DETAIL_QUERY,
            "variables": {
                "organizationHostedJobsPageName": board,
                "jobPostingId": job_id,
            },
            "operationName": "ApiJobPosting",
        },
    )
    payload = response.json()
    error_detail = _ashby_error_detail(payload)
    if error_detail:
        raise RuntimeError(f"Ashby job `{board}/{job_id}` returned GraphQL errors: {error_detail}")
    detail = payload.get("data", {}).get("jobPosting")
    if detail is None:
        raise RuntimeError(f"Ashby job `{board}/{job_id}` returned no detail payload.")
    if not isinstance(detail, dict):
        raise RuntimeError(f"Ashby job `{board}/{job_id}` returned an invalid detail payload.")
    return detail


def _enriched_posting_payload(
    brief_item: dict,
    detail: dict,
    *,
    board: str,
    team_name: str | None,
) -> tuple[NormalizedJobPosting, dict]:
    job_id = str(detail.get("id") or brief_item.get("id") or "")
    posting_url = _ashby_posting_url(board, job_id)
    team_names = [str(value).strip() for value in detail.get("teamNames") or [] if str(value).strip()]
    department_name = str(detail.get("departmentExternalName") or detail.get("departmentName") or "").strip() or None
    compensation_rows: list[dict[str, object]] = []
    compensation_rows.extend(_compensation_rows_from_summary(detail.get("scrapeableCompensationSalarySummary")))
    compensation_rows.extend(_compensation_rows_from_summary(detail.get("compensationTierSummary")))
    for tier in detail.get("compensationTiers") or []:
        if isinstance(tier, dict):
            compensation_rows.extend(_compensation_rows_from_summary(tier.get("tierSummary")))
    description_text = _html_to_text(detail.get("descriptionHtml"))
    payload = dict(brief_item)
    payload.update(detail)
    payload["teamName"] = ", ".join(team_names) if team_names else team_name
    payload["compensation"] = compensation_rows or None
    posting = build_normalized_job(
        company_name=board,
        title=detail.get("title") or brief_item.get("title") or "Untitled role",
        source="ashby",
        source_kind=SourceKind.ASHBY.value,
        source_job_id=job_id,
        posting_url=posting_url,
        apply_url=_ashby_application_url(posting_url),
        location_raw=detail.get("locationName") or brief_item.get("locationName"),
        employment_type=detail.get("employmentType") or brief_item.get("employmentType"),
        compensation=compensation_rows or None,
        description=description_text,
        posted_at=detail.get("publishedDate"),
        notes={
            "board": board,
            "workplace_type": detail.get("workplaceType") or brief_item.get("workplaceType"),
            "department": department_name,
            "team": ", ".join(team_names) if team_names else team_name,
            "secondary_locations": detail.get("secondaryLocationNames") or [],
            "capabilities": _capabilities_payload(),
        },
    )
    return posting, payload


def _ashby_error_detail(payload: dict) -> str | None:
    errors = payload.get("errors")
    if not isinstance(errors, list):
        return None
    messages = []
    for item in errors:
        if isinstance(item, dict):
            message = str(item.get("message") or item.get("detail") or "").strip()
            if message:
                messages.append(message)
        elif item:
            messages.append(str(item).strip())
    return "; ".join(message for message in messages if message) or None


class AshbyAdapter(SourceAdapter):
    source_kind = SourceKind.ASHBY
    policy_mode = PolicyMode.HUMAN_IN_LOOP_SUBMIT
    adapter_name = "ashby"

    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(
            adapter_name=self.adapter_name,
            source_kind=self.source_kind.value,
            policy_mode=self.policy_mode,
            risk=SourceRisk.MEDIUM,
            supports_discovery=True,
            supports_apply=True,
            supports_auto_submit=True,
            supported_filters=[
                "title_keywords",
                "locations",
                "countries",
                "regions",
                "cities",
                "workplace_types",
                "employment_types",
                "location_scopes",
                "experience_levels",
                "posted_within_days",
                "compensation_min",
                "compensation_currency",
                "company_size_buckets",
                "remote_only",
                "requires_future_sponsorship",
            ],
            supported_question_types=[QuestionType.DETERMINISTIC, QuestionType.BOOLEAN, QuestionType.SELECT, QuestionType.NUMERIC, QuestionType.DATE, QuestionType.FILE],
        )

    async def discover(self, client: httpx.AsyncClient, query: DiscoveryQuery) -> list[tuple[NormalizedJobPosting, dict]]:
        jobs: list[tuple[NormalizedJobPosting, dict]] = []
        for board in self.boards:
            response = await http_post_with_retry(
                client,
                "https://jobs.ashbyhq.com/api/non-user-graphql",
                json={
                    "query": ASHBY_BOARD_QUERY,
                    "variables": {"organizationHostedJobsPageName": board},
                    "operationName": "ApiJobBoardWithTeams",
                },
            )
            payload = response.json()
            error_detail = _ashby_error_detail(payload)
            if error_detail:
                raise RuntimeError(f"Ashby board `{board}` returned GraphQL errors: {error_detail}")
            board_data = payload.get("data", {}).get("jobBoard") or {}
            if not board_data:
                raise RuntimeError(f"Ashby board `{board}` returned no job board payload.")
            postings = board_data.get("jobPostings") or []
            if not isinstance(postings, list):
                raise RuntimeError(f"Ashby board `{board}` returned an invalid job posting list.")
            teams = _team_lookup(board_data.get("teams"))
            for item in postings:
                team_name = teams.get(str(item.get("teamId") or "").strip())
                posting, raw_payload = _brief_posting_payload(item, board=board, team_name=team_name)
                if not self._matches_query(posting, query):
                    continue
                job_id = str(item.get("id") or "").strip()
                if not job_id:
                    continue
                try:
                    detail = await _fetch_job_detail(client, board, job_id)
                except Exception as exc:
                    raw_payload["detail_fetch_error"] = str(exc)
                    jobs.append((posting, raw_payload))
                    continue
                jobs.append(_enriched_posting_payload(item, detail, board=board, team_name=team_name))
        return jobs

    async def load_application_contract(self, client: httpx.AsyncClient, job: NormalizedJobPosting) -> ExtractionResult:
        board = str(job.notes.get("board") or job.company_key)
        url = _ashby_application_url(str(job.apply_url or job.posting_url), board=board, job_id=job.source_job_id)
        response = await http_get_with_retry(client, url)
        result = extract_questions_from_html(response.text, handoff_url=url)
        try:
            submitter = self._submitter_for_job(job)
            inspected_rows = await submitter.inspect_form(url)
            if inspected_rows:
                rendered = extract_questions_from_lever_fields(inspected_rows, handoff_url=url)
                result = merge_extraction_results(result, rendered)
        except Exception:
            pass
        result.handoff_url = url
        return result

    def bind_answers(self, job: NormalizedJobPosting, question_answers, artifacts_by_kind: dict[str, str]) -> SubmissionPlan:
        fields: list[FormFieldBinding] = []
        missing: list[str] = []
        notes: list[str] = []
        for question, answer in question_answers:
            binding = self._binding_for_question(question, answer, artifacts_by_kind)
            if binding is None:
                if question.required and not _is_helper_question(question) and not _artifact_companion_satisfied(question, artifacts_by_kind):
                    missing.append(question.prompt_text)
                continue
            fields.append(binding)
        if missing:
            notes.append("Missing required bound fields")
        board = str(job.notes.get("board") or job.company_key)
        return SubmissionPlan(
            source_kind=job.source_kind,
            application_url=_ashby_application_url(str(job.apply_url or job.posting_url), board=board, job_id=job.source_job_id),
            fields=fields,
            missing_required_fields=missing,
            notes=notes,
        )

    async def execute_submission(self, job: NormalizedJobPosting, plan: SubmissionPlan, output_dir: Path) -> SubmissionResult:
        submitter = self._submitter_for_job(job)
        board = str(job.notes.get("board") or job.company_key)
        application_url = _ashby_application_url(str(job.apply_url or job.posting_url), board=board, job_id=job.source_job_id)
        return await submitter.submit_generic_form(application_url, plan, output_dir)

    async def preview_submission(
        self,
        job: NormalizedJobPosting,
        plan: SubmissionPlan,
        output_dir: Path,
        *,
        keep_browser_open: bool = False,
    ) -> SubmissionResult:
        submitter = self._submitter_for_job(job)
        board = str(job.notes.get("board") or job.company_key)
        application_url = _ashby_application_url(str(job.apply_url or job.posting_url), board=board, job_id=job.source_job_id)
        return await submitter.preview_generic_form(application_url, plan, output_dir, keep_browser_open=keep_browser_open)

    def _submitter_for_job(self, job: NormalizedJobPosting) -> PlaywrightSubmitter:
        captcha_strategy, captcha_solver = _build_captcha_solver_from_notes(job.notes)
        return PlaywrightSubmitter(
            timeout_seconds=int(job.notes.get("browser_timeout_seconds") or 30),
            capture_policy=SubmissionCapturePolicy.model_validate(job.notes.get("capture_policy") or {}),
            browser_attach_enabled=bool(job.notes.get("browser_attach_enabled", False)),
            browser_cdp_url=job.notes.get("browser_cdp_url"),
            browser_mode=str(job.notes.get("browser_mode") or "headless"),
            max_open_tabs=int(job.notes.get("max_open_tabs") or 10),
            captcha_strategy=captcha_strategy,
            captcha_solver=captcha_solver,
        )

    def _binding_for_question(self, question, answer, artifacts_by_kind: dict[str, str]) -> FormFieldBinding | None:
        option_details = list(getattr(question, "option_details", None) or [])
        section = getattr(question, "section", None)
        sensitive = getattr(question, "sensitive", None)
        metadata = _binding_metadata(question, option_details, section, bool(sensitive))

        if question.question_type.value == "file":
            preferred_kind, fallback_kind = _preferred_artifact_kinds_for_question(question)
            artifact_path = artifacts_by_kind.get(preferred_kind.value) or artifacts_by_kind.get(fallback_kind.value)
            if not artifact_path:
                return None
            mime_type = "application/pdf" if artifact_path.endswith(".pdf") else "text/plain"
            return FormFieldBinding(
                source_field_name=question.source_field_name,
                widget_type=getattr(question, "widget_type", "file"),
                prompt_text=question.prompt_text,
                required=question.required,
                artifact_binding=ArtifactBinding(
                    artifact_kind=preferred_kind if artifact_path.endswith(".pdf") else fallback_kind,
                    source_artifact_kind=preferred_kind,
                    path=artifact_path,
                    mime_type=mime_type,
                ),
                metadata=metadata,
            )
        generated_text_binding = _generated_text_binding(question, artifacts_by_kind, option_details, section, bool(sensitive))
        if generated_text_binding is not None:
            return generated_text_binding
        if answer is None or answer.needs_user_input:
            return None
        raw_answer = answer.candidate_answer or ""
        option_values, labels = _resolve_option_values(raw_answer, option_details)
        return FormFieldBinding(
            source_field_name=question.source_field_name,
            widget_type=getattr(question, "widget_type", "text"),
            prompt_text=question.prompt_text,
            required=question.required,
            value=raw_answer,
            values=labels if len(labels) > 1 else [],
            option_value=option_values[0] if len(option_values) == 1 else None,
            option_values=option_values if len(option_values) > 1 else [],
            metadata=metadata,
        )

    def _matches_query(self, job: NormalizedJobPosting, query: DiscoveryQuery) -> bool:
        return evaluate_job_against_query(job, query).matched
