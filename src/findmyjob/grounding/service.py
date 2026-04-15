from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from findmyjob.core.filtering import CANADA_PROVINCE_CODES, US_STATE_CODES, normalize_region_code
from findmyjob.core.enums import ModelRole, QuestionType, VerificationStatus
from findmyjob.core.policies import SENSITIVE_QUESTION_KEYWORDS
from findmyjob.core.types import ClaimEvidence, GroundedAnswer, ProfileFact
from findmyjob.model_router.router import ModelRouter
from findmyjob.sources.normalizer import slugify

WORD_RE = re.compile(r"[a-z0-9]+")
STOPWORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "for",
    "from",
    "i",
    "in",
    "is",
    "my",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
    "your",
}
REGION_CODE_TO_NAME = {code: name.title() for name, code in {**US_STATE_CODES, **CANADA_PROVINCE_CODES}.items()}


class GroundingService:
    def __init__(self, router: ModelRouter | None = None) -> None:
        self.router = router

    def canonicalize_question(self, question: str, normalized_key: str | None = None) -> str:
        return normalized_key or slugify(question)

    def _normalized_question_text(self, text: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(text or "").casefold()).strip()

    def _has_phrase(self, normalized_text: str, phrase: str) -> bool:
        token = self._normalized_question_text(phrase)
        if not token:
            return False
        return f" {token} " in f" {normalized_text} "

    def _has_any_phrase(self, normalized_text: str, phrases: Sequence[str]) -> bool:
        return any(self._has_phrase(normalized_text, phrase) for phrase in phrases)

    def classify_question(self, question: str, options: Sequence[str] | None = None) -> QuestionType:
        lowered = question.lower()
        if any(keyword in lowered for keyword in SENSITIVE_QUESTION_KEYWORDS):
            return QuestionType.SENSITIVE
        if options:
            if set(option.lower() for option in options) <= {"yes", "no"}:
                return QuestionType.BOOLEAN
            return QuestionType.SELECT
        if any(keyword in lowered for keyword in ("resume", "cover letter", "attachment", "upload")):
            return QuestionType.FILE
        if any(keyword in lowered for keyword in ("email", "phone", "linkedin", "portfolio", "website", "name")):
            return QuestionType.DETERMINISTIC
        if lowered.startswith("do you") or lowered.startswith("are you"):
            return QuestionType.BOOLEAN
        if any(keyword in lowered for keyword in ("how many", "years of", "gpa", "salary")):
            return QuestionType.NUMERIC
        if any(keyword in lowered for keyword in ("date", "when did", "start date")):
            return QuestionType.DATE
        if any(keyword in lowered for keyword in ("why", "describe", "tell us", "experience", "accomplishment", "project")):
            return QuestionType.NARRATIVE
        return QuestionType.UNKNOWN

    async def answer_question(
        self,
        question: str,
        facts: Sequence[ProfileFact],
        *,
        options: Sequence[str] | None = None,
        normalized_key: str | None = None,
        answer_memory: Sequence[dict] | None = None,
        memory_context: dict[str, Any] | None = None,
        allow_sensitive_fallback: bool = True,
    ) -> GroundedAnswer:
        canonical_question = self.canonicalize_question(question, normalized_key)
        question_type = self.classify_question(question, options)
        memory_hit = self._find_memory(canonical_question, answer_memory or [], memory_context or {}, options=options)
        if memory_hit is not None:
            return GroundedAnswer(
                question=question,
                canonical_question=canonical_question,
                question_type=question_type,
                answer=memory_hit["answer_text"],
                confidence=1.0,
                reason="answer_memory_hit",
                used_fact_ids=list(memory_hit.get("grounded_fact_ids", [])),
                provenance="answer_memory",
                verification_status=VerificationStatus.VERIFIED,
            )

        answer = GroundedAnswer(question=question, canonical_question=canonical_question, question_type=question_type)
        lowered = question.lower()

        if question_type == QuestionType.SENSITIVE:
            if options:
                direct_select = self._deterministic_select_answer(question, options, facts)
                if direct_select is not None:
                    direct_select.question = question
                    direct_select.canonical_question = canonical_question
                    direct_select.question_type = question_type
                    return direct_select
            direct_sensitive = self._direct_sensitive_answer(question, facts)
            if direct_sensitive is not None:
                direct_sensitive.question = question
                direct_sensitive.canonical_question = canonical_question
                direct_sensitive.question_type = question_type
                return direct_sensitive
            if allow_sensitive_fallback:
                decline_sensitive = self._decline_sensitive_answer(question, options or [])
                if decline_sensitive is not None:
                    decline_sensitive.question = question
                    decline_sensitive.canonical_question = canonical_question
                    decline_sensitive.question_type = question_type
                    return decline_sensitive
            return answer

        if question_type == QuestionType.FILE:
            answer.verification_status = VerificationStatus.REVIEW_REQUIRED
            return answer

        direct = self._rule_based_answer(question, lowered, facts, question_type, options)
        if direct is None:
            direct = self._direct_fact_answer(question, facts, question_type)
        if direct is not None:
            direct.question = question
            direct.canonical_question = canonical_question
            direct.question_type = question_type
            return direct

        if question_type == QuestionType.SELECT and options:
            return await self._structured_select_answer(
                question, canonical_question, question_type, options, facts, allow_sensitive_fallback=allow_sensitive_fallback,
            )

        if question_type == QuestionType.NARRATIVE:
            company_motivation = self._company_motivation_answer(question, facts)
            if company_motivation is not None:
                company_motivation.question = question
                company_motivation.canonical_question = canonical_question
                company_motivation.question_type = question_type
                return company_motivation
            return await self._narrative_answer(question, canonical_question, facts)

        if question_type == QuestionType.BOOLEAN:
            model_answer = await self._model_boolean_answer(question, canonical_question, facts)
            if model_answer is not None:
                model_answer.question = question
                model_answer.canonical_question = canonical_question
                model_answer.question_type = question_type
                return model_answer

        return answer

    async def _structured_select_answer(
        self,
        question: str,
        canonical_question: str,
        question_type: QuestionType,
        options: Sequence[str],
        facts: Sequence[ProfileFact],
        *,
        allow_sensitive_fallback: bool,
    ) -> GroundedAnswer:
        """Answer a SELECT/radio/checkbox question by choosing from actual options.

        Strategy:
        1. Exact fact-in-option matching (deterministic)
        2. Structured LLM selection constrained to actual options
        3. Return uncertainty if neither works
        """
        direct = self._deterministic_select_answer(question, options, facts)
        if direct is not None:
            direct.question = question
            direct.canonical_question = canonical_question
            direct.question_type = question_type
            return direct

        # --- Step 1: deterministic fact-in-option matching ---
        for option in options:
            option_lower = option.lower().strip()
            if option_lower in {"yes", "no"}:
                continue
            for fact in facts:
                if not fact.allowed_for_generation or fact.disallowed:
                    continue
                haystack = " ".join(str(value) for value in fact.payload.values()).lower()
                if option_lower in haystack:
                    return GroundedAnswer(
                        question=question,
                        canonical_question=canonical_question,
                        question_type=question_type,
                        answer=option,
                        selected_option_values=[option],
                        confidence=0.85,
                        reason=f"option '{option}' matched in fact {fact.fact_id}",
                        used_fact_ids=[fact.fact_id],
                        claim_evidence=[ClaimEvidence(text=option, fact_id=fact.fact_id)],
                        verification_status=VerificationStatus.REVIEW_REQUIRED,
                    )

        # --- Step 2: structured LLM selection ---
        if self.router is not None:
            try:
                options_list = [str(opt).strip() for opt in options if str(opt).strip()]
                fact_summaries = []
                for fact in facts:
                    if not fact.allowed_for_generation or fact.disallowed:
                        continue
                    summary = " ".join(str(v) for v in fact.payload.values() if str(v).strip())
                    if summary.strip():
                        fact_summaries.append(f"- {fact.fact_id}: {summary[:200]}")
                prompt = (
                    "Select the best option to answer the application question. "
                    "You MUST choose ONLY from the provided options. "
                    "If none of the options match the candidate's profile, set selected_option to null and uncertain to true.\n\n"
                    f"Question: {question}\n"
                    f"Options: {options_list}\n"
                    "Profile facts:\n" + "\n".join(fact_summaries[:10]) + "\n\n"
                    "Return JSON: {\"selected_option\": \"exact option text or null\", "
                    "\"confidence\": 0.0-1.0, \"reason\": \"brief explanation\", \"uncertain\": boolean}"
                )
                payload, profile_name = await self.router.generate_json_with_profile(
                    ModelRole.QUESTION_ANSWERER,
                    prompt,
                    system_prompt=(
                        "You are answering a job application form question. "
                        "You MUST select from the provided options list exactly as written. "
                        "Do NOT generate free-text answers for select/radio/checkbox fields. "
                        "Return only valid JSON."
                    ),
                )
                selected = payload.get("selected_option")
                uncertain = bool(payload.get("uncertain", False))
                model_confidence = float(payload.get("confidence", 0.0))
                model_reason = str(payload.get("reason", ""))

                if selected and not uncertain and selected in options_list:
                    return GroundedAnswer(
                        question=question,
                        canonical_question=canonical_question,
                        question_type=question_type,
                        answer=selected,
                        selected_option_values=[selected],
                        confidence=min(model_confidence, 0.9),
                        reason=f"model:{profile_name} - {model_reason}",
                        provenance=f"model:{profile_name}",
                        verification_status=VerificationStatus.REVIEW_REQUIRED,
                    )
                # Model was uncertain or selected invalid option
            except Exception:
                pass

        if allow_sensitive_fallback:
            fallback = self._privacy_select_fallback(question, options)
            if fallback is not None:
                fallback.question = question
                fallback.canonical_question = canonical_question
                fallback.question_type = question_type
                return fallback

        # --- Step 3: return uncertainty ---
        return GroundedAnswer(
            question=question,
            canonical_question=canonical_question,
            question_type=question_type,
            confidence=0.0,
            reason="no_option_matched_profile",
        )

    def _find_memory(
        self,
        canonical_question: str,
        answer_memory: Sequence[dict],
        memory_context: dict[str, Any],
        *,
        options: Sequence[str] | None = None,
    ) -> dict | None:
        candidates: list[tuple[int, int, int, int, dict]] = []
        for index, record in enumerate(answer_memory):
            if record.get("canonical_question") != canonical_question or not record.get("approved"):
                continue
            constraints = record.get("context_constraints") or {}
            if all(self._memory_constraint_matches(constraints, key, value) for key, value in memory_context.items()):
                overlap = self._memory_constraint_overlap(constraints, memory_context)
                option_match = self._memory_option_match_score(str(record.get("answer_text") or ""), options or [])
                candidates.append((option_match, overlap, len(constraints), index, record))
        if not candidates:
            return None
        if options:
            matching_candidates = [candidate for candidate in candidates if candidate[0] > 0]
            if not matching_candidates:
                return None
            candidates = matching_candidates
        candidates.sort(reverse=True)
        return candidates[0][4]

    def _memory_constraint_matches(self, constraints: dict[str, Any], key: str, value: Any) -> bool:
        aliases = (key,)
        if key == "source":
            aliases = ("source", "source_adapter")
        elif key == "source_adapter":
            aliases = ("source_adapter", "source")
        if not any(alias in constraints for alias in aliases):
            return True
        return any(constraints.get(alias) == value for alias in aliases)

    def _memory_constraint_overlap(self, constraints: dict[str, Any], memory_context: dict[str, Any]) -> int:
        overlap = 0
        for key, value in memory_context.items():
            aliases = (key,)
            if key == "source":
                aliases = ("source", "source_adapter")
            elif key == "source_adapter":
                aliases = ("source_adapter", "source")
            present_aliases = [alias for alias in aliases if alias in constraints]
            if present_aliases and any(constraints.get(alias) == value for alias in present_aliases):
                overlap += 1
        return overlap

    def _memory_option_match_score(self, answer_text: str, options: Sequence[str]) -> int:
        normalized_options = {self._normalize_option(str(option or "")) for option in options if str(option or "").strip()}
        if not normalized_options:
            return 0
        normalized_answer = self._normalize_option(answer_text)
        if normalized_answer and normalized_answer in normalized_options:
            return 3
        chunks = [
            self._normalize_option(part)
            for part in str(answer_text or "").replace("|", ",").replace(";", ",").split(",")
            if str(part).strip()
        ]
        if chunks and all(chunk in normalized_options for chunk in chunks):
            return 2 + min(len(chunks), 3)
        return 0

    def _rule_based_answer(
        self,
        question: str,
        lowered: str,
        facts: Sequence[ProfileFact],
        question_type: QuestionType,
        options: Sequence[str] | None,
    ) -> GroundedAnswer | None:
        _ = lowered
        normalized_question = self._normalized_question_text(question)
        contact = self._first_fact_payload(facts, 'contact')
        location = self._first_fact_payload(facts, 'location')
        authorization = self._first_fact_payload(facts, 'authorization')
        education = self._primary_education_payload(facts)

        name_value = str((contact or {}).get('name') or '').strip()
        name_parts = [part for part in name_value.split() if part]
        first_name = str(
            (contact or {}).get('first_name')
            or (contact or {}).get('given_name')
            or (contact or {}).get('legal_first_name')
            or ''
        ).strip()
        last_name = str(
            (contact or {}).get('last_name')
            or (contact or {}).get('family_name')
            or (contact or {}).get('surname')
            or (contact or {}).get('legal_last_name')
            or ''
        ).strip()
        preferred_name = str(
            (contact or {}).get('preferred_name')
            or (contact or {}).get('preferred_first_name')
            or (contact or {}).get('nickname')
            or ''
        ).strip()
        if not first_name and name_parts:
            first_name = name_parts[0]
        if not last_name and len(name_parts) > 1:
            last_name = ' '.join(name_parts[1:])

        if question_type in {QuestionType.DETERMINISTIC, QuestionType.UNKNOWN}:
            if self._has_any_phrase(normalized_question, ('preferred name', 'preferred first name', 'nickname', 'chosen name')) and (preferred_name or first_name):
                return self._grounded_value(preferred_name or first_name, question_type, contact)
            if self._has_any_phrase(normalized_question, ('first name', 'given name', 'legal first name')) and first_name:
                return self._grounded_value(first_name, question_type, contact)
            if self._has_any_phrase(normalized_question, ('last name', 'family name', 'surname', 'legal last name')) and last_name:
                return self._grounded_value(last_name, question_type, contact)
            if (
                self._has_any_phrase(normalized_question, ('full name', 'legal name'))
                or (self._has_phrase(normalized_question, 'name') and not self._has_any_phrase(normalized_question, ('company name', 'school name', 'manager name')) and name_value)
            ):
                return self._grounded_value(name_value, question_type, contact)
            for field, tokens in (
                ('email', ('email', 'e mail')),
                ('phone', ('phone', 'mobile', 'telephone')),
                ('linkedin', ('linkedin',)),
                ('github', ('github',)),
                ('portfolio', ('portfolio',)),
                ('website', ('website', 'site', 'url')),
            ):
                if self._has_any_phrase(normalized_question, tokens):
                    value = self._contact_value(contact, field)
                    if value:
                        return self._grounded_value(value, question_type, contact)
            if self._has_any_phrase(normalized_question, ('city', 'town')):
                value = str((location or {}).get('city') or '').strip()
                if value:
                    return self._grounded_value(value, question_type, location)
            if self._has_any_phrase(normalized_question, ('zip', 'zip code', 'zipcode', 'postal code')):
                value = self._postal_code_value(location)
                if value:
                    return self._grounded_value(value, question_type, location)
            if self._has_any_phrase(normalized_question, ('state', 'province', 'region')):
                value = str((location or {}).get('region') or (location or {}).get('region_code') or '').strip()
                if value:
                    return self._grounded_value(value, question_type, location)
            if self._has_any_phrase(normalized_question, ('country', 'citizenship', 'nationality')):
                value = self._country_display_name((authorization or {}).get('country_code') or (location or {}).get('country_code'))
                if value:
                    fact_payload = authorization or location
                    return self._grounded_value(value, question_type, fact_payload)
            if self._has_phrase(normalized_question, 'location'):
                value = str((location or {}).get('display') or '').strip()
                if not value:
                    pieces = [str((location or {}).get(key) or '').strip() for key in ('city', 'region_code', 'country_code')]
                    value = ', '.join(piece for piece in pieces if piece)
                if value:
                    return self._grounded_value(value, question_type, location)
            certification_answer = self._certification_text_answer(normalized_question, question_type, contact)
            if certification_answer is not None:
                return certification_answer
            education_answer = self._education_rule_answer(normalized_question, question_type, education)
            if education_answer is not None:
                return education_answer

        if question_type == QuestionType.BOOLEAN:
            boolean_answer = self._deterministic_boolean_answer(question, authorization, facts)
            if boolean_answer is not None:
                return boolean_answer
            if self._has_any_phrase(normalized_question, ('contact your current employer', 'contact current employer', 'may we contact your current employer')):
                return self._yes_no_answer(False, VerificationStatus.REVIEW_REQUIRED, reason='rule_based_boolean')
            if self._has_phrase(normalized_question, 'reasonable accommodation') and self._has_any_phrase(normalized_question, ('perform these essential functions', 'essential functions of the job', 'can you perform')):
                return self._yes_no_answer(True, VerificationStatus.REVIEW_REQUIRED, reason='rule_based_boolean')
            prior_employment = self._prior_employment_answer(question, facts)
            if prior_employment is not None:
                return prior_employment
            consent_answer = self._consent_answer(question, options)
            if consent_answer is not None:
                return consent_answer

        if question_type == QuestionType.SELECT and options:
            return self._deterministic_select_answer(question, options, facts)
        return None

    def _deterministic_boolean_answer(self, question: str, authorization: dict[str, Any] | None, facts: Sequence[ProfileFact]) -> GroundedAnswer | None:
        if authorization is None:
            authorization = {}
        normalized_question = self._normalized_question_text(question)
        authorized_tokens = (
            'authorized',
            'work authorization',
            'legally authorized',
            'eligible to work',
            'employment authorization',
        )
        sponsorship_tokens = (
            'sponsor',
            'sponsorship',
            'visa',
            'immigration support',
            'work permit',
        )
        if self._has_any_phrase(normalized_question, authorized_tokens) and 'is_authorized' in authorization:
            return self._yes_no_answer(bool(authorization['is_authorized']), VerificationStatus.REVIEW_REQUIRED)
        if self._has_any_phrase(normalized_question, sponsorship_tokens) and 'requires_future_sponsorship' in authorization:
            requires_sponsorship = bool(authorization['requires_future_sponsorship'])
            if self._has_any_phrase(normalized_question, ('without sponsorship', 'do not require sponsorship', 'does not require sponsorship')):
                requires_sponsorship = not requires_sponsorship
            return self._yes_no_answer(requires_sponsorship, VerificationStatus.REVIEW_REQUIRED)
        if self._has_any_phrase(
            normalized_question,
            (
                'open to working in person',
                'open to working in-person',
                'open to work in person',
                'work in person',
                'working in-person',
                'working in person',
                'one of our offices',
                'in our offices',
                'hybrid policy',
            ),
        ):
            return self._yes_no_answer(True, VerificationStatus.REVIEW_REQUIRED, reason='preference_rule')
        if self._has_any_phrase(
            normalized_question,
            (
                'open to relocation',
                'open to relocate',
                'willing to relocate',
                'relocation for this role',
                'open to relocating',
            ),
        ):
            return self._yes_no_answer(True, VerificationStatus.REVIEW_REQUIRED, reason='preference_rule')
        if self._has_any_phrase(
            normalized_question,
            (
                'have you ever interviewed',
                'interviewed at',
                'interviewed with',
                'previously interviewed',
                'interviewed here before',
            ),
        ):
            return self._yes_no_answer(False, VerificationStatus.REVIEW_REQUIRED, reason='history_rule')
        if self._has_any_phrase(
            normalized_question,
            (
                'fluent in english',
                'english fluency',
                'fluent english',
                'english proficient',
                'proficient in english',
                'english language',
                'speak english',
                'written english',
                'spoken english',
            ),
        ) and self._has_language_fact(facts, 'English'):
            return self._yes_no_answer(True, VerificationStatus.REVIEW_REQUIRED, reason='language_rule')
        return None

    def _primary_education_payload(self, facts: Sequence[ProfileFact]) -> dict[str, Any] | None:
        for fact in facts:
            if getattr(fact.kind, 'value', fact.kind) != 'education' or fact.disallowed:
                continue
            return fact.payload
        return None

    def _education_rule_answer(
        self,
        normalized_question: str,
        question_type: QuestionType,
        education: dict[str, Any] | None,
    ) -> GroundedAnswer | None:
        if education is None or question_type not in {QuestionType.DETERMINISTIC, QuestionType.UNKNOWN, QuestionType.NUMERIC, QuestionType.DATE}:
            return None
        school = str(education.get('school') or '').strip()
        degree_text = str(education.get('degree') or education.get('summary') or '').strip()
        summary = str(education.get('summary') or '').strip()
        date_label = str(education.get('date_label') or education.get('end_year') or '').strip()
        degree_level = self._education_degree_level(degree_text)
        discipline = self._education_discipline(degree_text)
        minor = self._education_minor(degree_text)
        gpa = self._education_gpa(summary)

        if self._has_any_phrase(normalized_question, ('school', 'university', 'college', 'institution')):
            if school:
                return self._grounded_value(school, question_type, education)
        if self._has_any_phrase(normalized_question, ('major', 'discipline', 'field of study', 'program', 'concentration')):
            if discipline:
                return self._grounded_value(discipline, question_type, education)
        if self._has_phrase(normalized_question, 'minor'):
            if minor:
                return self._grounded_value(minor, question_type, education)
        if self._has_any_phrase(normalized_question, ('degree level', 'degree type', 'degree')):
            if degree_level:
                return self._grounded_value(degree_level, question_type, education)
            if degree_text:
                return self._grounded_value(degree_text, question_type, education)
        if self._has_any_phrase(normalized_question, ('graduation', 'graduate', 'expected graduation', 'graduation date', 'expected grad', 'completion')):
            if date_label:
                return self._grounded_value(date_label, question_type, education)
        if self._has_any_phrase(normalized_question, ('gpa', 'grade point average')):
            if gpa:
                return self._grounded_value(gpa, question_type, education)
        return None

    def _education_degree_level(self, degree_text: str) -> str | None:
        text = str(degree_text or '').strip()
        if not text:
            return None
        patterns = (
            (r"\bb\.?\s*s\.?\b", "Bachelor's"),
            (r"\bbachelor'?s\b", "Bachelor's"),
            (r"\bm\.?\s*s\.?\b", "Master's"),
            (r"\bmaster'?s\b", "Master's"),
            (r"\bph\.?\s*d\.?\b", "PhD"),
            (r"\bdoctorate\b", "Doctorate"),
            (r"\bassociate'?s\b", "Associate's"),
        )
        lowered = text.casefold()
        for pattern, normalized in patterns:
            if re.search(pattern, lowered, flags=re.IGNORECASE):
                return normalized
        return None

    def _education_discipline(self, degree_text: str) -> str | None:
        text = str(degree_text or '').strip()
        if not text:
            return None
        for pattern in (r"\bin\s+([^;,]+)", r"^[^,]+,\s*([^;,]+)"):
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return None

    def _education_minor(self, degree_text: str) -> str | None:
        text = str(degree_text or '').strip()
        if not text:
            return None
        match = re.search(r"minor\s+in\s+([^;,]+)", text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
        return None

    def _education_gpa(self, summary: str) -> str | None:
        text = str(summary or '').strip()
        if not text:
            return None
        match = re.search(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", text)
        if match:
            return f"{match.group(1)}/{match.group(2)}"
        return None

    def _deterministic_select_answer(self, question: str, options: Sequence[str], facts: Sequence[ProfileFact]) -> GroundedAnswer | None:
        normalized_question = self._normalized_question_text(question)
        authorization = self._first_fact_payload(facts, 'authorization')
        location = self._first_fact_payload(facts, 'location')

        single_option = self._single_option_acknowledgement(normalized_question, options)
        if single_option is not None:
            return single_option

        demographic_answer = self._demographic_select_answer(question, options, facts)
        if demographic_answer is not None:
            return demographic_answer

        if authorization is not None and self._has_any_phrase(normalized_question, ('work authorization', 'authorized to work', 'legally authorized', 'eligible to work', 'employment authorization', 'sponsorship', 'visa')):
            if bool(authorization.get('is_authorized')) and not bool(authorization.get('requires_future_sponsorship')):
                selected = self._match_option(
                    options,
                    [
                        'I am authorized to work for any employer in the country in which this position is based.',
                        'I am authorized to work for any employer',
                        'authorized to work for any employer',
                        'do not require sponsorship',
                        'without sponsorship',
                    ],
                )
                if selected:
                    return self._selected_option_answer(selected, authorization, VerificationStatus.REVIEW_REQUIRED)
            if bool(authorization.get('requires_future_sponsorship')):
                selected = self._match_option(
                    options,
                    [
                        'I require/will require sponsorship',
                        'I require or will require sponsorship',
                        'require sponsorship',
                        'will require sponsorship',
                        'visa sponsorship',
                    ],
                )
                if selected:
                    return self._selected_option_answer(selected, authorization, VerificationStatus.REVIEW_REQUIRED)

        if self._has_any_phrase(
            normalized_question,
            (
                'english fluency',
                'fluent in english',
                'english language',
                'english proficiency',
                'spoken english',
                'written english',
            ),
        ) and self._has_language_fact(facts, 'English'):
            selected = self._match_option(
                options,
                [
                    'Fluent',
                    'Full professional proficiency',
                    'Professional working proficiency',
                    'Native or bilingual proficiency',
                    'Yes',
                    'English',
                ],
            )
            if selected:
                return self._selected_option_answer(selected, None, VerificationStatus.REVIEW_REQUIRED)

        boolean_answer = self._deterministic_boolean_answer(question, authorization, facts)
        if boolean_answer is not None:
            selected = self._match_option(options, [boolean_answer.answer or ''])
            if selected:
                boolean_answer.answer = selected
                boolean_answer.selected_option_values = [selected]
                return boolean_answer

        consent_answer = self._consent_answer(question, options)
        if consent_answer is not None:
            selected = self._match_option(options, [consent_answer.answer or ''])
            if selected:
                consent_answer.answer = selected
                consent_answer.selected_option_values = [selected]
                return consent_answer

        if self._has_any_phrase(normalized_question, ('citizenship', 'nationality', 'country region', 'country')):
            country_code = str((authorization or {}).get('country_code') or (location or {}).get('country_code') or '').strip()
            selected = self._match_option(options, [country_code, self._country_display_name(country_code)])
            if selected:
                return self._selected_option_answer(selected, authorization or location, VerificationStatus.REVIEW_REQUIRED)

        if self._has_any_phrase(normalized_question, ('state', 'province', 'region')):
            region_candidates = [
                str((location or {}).get('region') or '').strip(),
                str((location or {}).get('region_code') or '').strip(),
            ]
            selected = self._match_option(options, region_candidates)
            if selected:
                return self._selected_option_answer(selected, location, VerificationStatus.VERIFIED)

        if self._has_any_phrase(normalized_question, ('relocate', 'relocating', 'relocation', 'commutable proximity')):
            selected = self._match_option(
                options,
                [
                    'I am willing to relocate before starting employment.',
                    'I am willing to relocate',
                    'open to relocating',
                    'yes',
                ],
            )
            if selected:
                return self._selected_option_answer(selected, location, VerificationStatus.REVIEW_REQUIRED)

        return None

    def _privacy_select_fallback(self, question: str, options: Sequence[str]) -> GroundedAnswer | None:
        normalized_question = self._normalized_question_text(question)
        privacy_tokens = (
            'pronoun',
            'pronouns',
            'gender',
            'ethnicity',
            'race',
            'veteran',
            'disability',
            'sexual orientation',
            'lgbtq',
        )
        if not self._has_any_phrase(normalized_question, privacy_tokens):
            return None
        selected = self._match_option(
            options,
            [
                'I prefer not to say',
                'Prefer not to say',
                'Prefer not to answer',
                'Decline to answer',
                'Decline to self-identify',
            ],
        )
        if not selected:
            return None
        return GroundedAnswer(
            question='',
            question_type=QuestionType.SELECT,
            answer=selected,
            selected_option_values=[selected],
            confidence=0.8,
            reason='privacy_fallback',
            provenance='rules',
            verification_status=VerificationStatus.REVIEW_REQUIRED,
        )

    def _has_language_fact(self, facts: Sequence[ProfileFact], language_name: str) -> bool:
        target = self._normalize_option(language_name)
        if not target:
            return False
        for fact in facts:
            if getattr(fact.kind, 'value', fact.kind) != 'skill' or fact.disallowed:
                continue
            payload = fact.payload or {}
            if self._normalize_option(str(payload.get('category') or '')) != 'language':
                continue
            for candidate in (payload.get('name'), payload.get('summary')):
                if self._normalize_option(str(candidate or '')) == target:
                    return True
        return False

    def _is_high_risk_boolean_question(self, normalized_question: str) -> bool:
        return self._has_any_phrase(
            normalized_question,
            (
                'work authorization',
                'authorized to work',
                'legally authorized',
                'visa',
                'sponsorship',
                'salary',
                'compensation',
                'criminal',
                'background check',
                'security clearance',
                'drug test',
                'disability',
                'veteran',
                'gender',
                'race',
                'ethnicity',
                'citizenship',
            ),
        )

    async def _model_boolean_answer(
        self,
        question: str,
        canonical_question: str,
        facts: Sequence[ProfileFact],
    ) -> GroundedAnswer | None:
        if self.router is None:
            return None
        normalized_question = self._normalized_question_text(question)
        if self._is_high_risk_boolean_question(normalized_question):
            return None
        fact_summaries: list[str] = []
        used_fact_ids: list[str] = []
        for fact in facts:
            if fact.disallowed or not fact.allowed_for_generation:
                continue
            summary = " ".join(str(value).strip() for value in fact.payload.values() if str(value).strip())
            if not summary:
                continue
            fact_summaries.append(f"- {fact.fact_id}: {summary[:240]}")
            used_fact_ids.append(fact.fact_id)
            if len(fact_summaries) >= 12:
                break
        if not fact_summaries:
            return None
        try:
            payload, profile_name = await self.router.generate_json_with_profile(
                ModelRole.QUESTION_ANSWERER,
                (
                    "Answer the job application question with Yes or No using only the provided profile facts. "
                    "If the facts do not support either answer clearly, return null and set uncertain=true.\n\n"
                    f"Question: {question}\n"
                    "Profile facts:\n" + "\n".join(fact_summaries) + "\n\n"
                    'Return JSON: {"answer":"Yes"|"No"|null,"confidence":0.0-1.0,"reason":"brief explanation","uncertain":boolean}'
                ),
                system_prompt=(
                    "You answer non-sensitive job application boolean questions. "
                    "Use only the provided profile facts. "
                    "If the answer is not clearly supported, return null and uncertain=true. "
                    "Return only valid JSON."
                ),
            )
        except Exception:
            return None
        answer_value = str(payload.get('answer') or '').strip().title()
        uncertain = bool(payload.get('uncertain', False))
        confidence = float(payload.get('confidence', 0.0) or 0.0)
        reason = str(payload.get('reason') or '').strip()
        if uncertain or answer_value not in {'Yes', 'No'} or confidence < 0.75:
            return None
        return GroundedAnswer(
            question=question,
            canonical_question=canonical_question,
            question_type=QuestionType.BOOLEAN,
            answer=answer_value,
            selected_option_values=[answer_value],
            confidence=min(confidence, 0.9),
            reason=f"model_boolean:{reason}" if reason else 'model_boolean',
            provenance=f"model:{profile_name}",
            used_fact_ids=used_fact_ids,
            verification_status=VerificationStatus.REVIEW_REQUIRED,
        )

    def _consent_answer(self, question: str, options: Sequence[str] | None = None) -> GroundedAnswer | None:
        normalized_question = self._normalized_question_text(question)
        if self._has_any_phrase(
            normalized_question,
            (
                'consent',
                'agree',
                'acknowledge',
                'privacy notice',
                'privacy policy',
                'terms of use',
                'terms and conditions',
                'data processing',
                'use of ai',
                'ai policy',
                'candidate ai guidance',
            ),
        ):
            yes_value = self._match_option(options or ['Yes', 'No'], ['yes', 'i agree', 'agree', 'accept']) or 'Yes'
            return GroundedAnswer(
                question='',
                question_type=QuestionType.BOOLEAN,
                answer=yes_value,
                selected_option_values=[yes_value],
                confidence=1.0,
                reason='consent_rule',
                provenance='rules',
                verification_status=VerificationStatus.VERIFIED,
            )
        return None

    def _first_fact_payload(self, facts: Sequence[ProfileFact], kind_name: str) -> dict[str, Any] | None:
        for fact in facts:
            if getattr(fact.kind, 'value', fact.kind) != kind_name or fact.disallowed:
                continue
            return fact.payload
        return None

    def _contact_value(self, contact: dict[str, Any] | None, field: str) -> str | None:
        if not contact:
            return None
        aliases = {
            'portfolio': ('portfolio', 'website'),
            'website': ('website', 'portfolio'),
        }
        candidates = aliases.get(field, (field,))
        for candidate in candidates:
            value = str(contact.get(candidate) or '').strip()
            if value:
                return value
        return None

    def _grounded_value(self, value: str, question_type: QuestionType, payload: dict[str, Any] | None) -> GroundedAnswer:
        _ = payload
        return GroundedAnswer(
            question='',
            question_type=question_type,
            answer=value,
            confidence=1.0,
            reason='fact_rule',
            provenance='rules',
            used_fact_ids=[],
            claim_evidence=[],
            verification_status=VerificationStatus.VERIFIED,
        )

    def _yes_no_answer(self, value: bool, status: VerificationStatus, payload: dict[str, Any] | None = None, *, reason: str = 'authorization_rule') -> GroundedAnswer:
        answer_value = 'Yes' if value else 'No'
        return GroundedAnswer(
            question='',
            question_type=QuestionType.BOOLEAN,
            answer=answer_value,
            selected_option_values=[answer_value],
            confidence=1.0,
            reason=reason,
            provenance='rules',
            verification_status=status,
        )

    def _selected_option_answer(self, selected: str, payload: dict[str, Any] | None, status: VerificationStatus) -> GroundedAnswer:
        return GroundedAnswer(
            question='',
            question_type=QuestionType.SELECT,
            answer=selected,
            selected_option_values=[selected],
            confidence=1.0,
            reason='option_rule',
            provenance='rules',
            verification_status=status,
        )

    def _single_option_acknowledgement(self, normalized_question: str, options: Sequence[str]) -> GroundedAnswer | None:
        visible_options = [str(option).strip() for option in options if str(option).strip()]
        if len(visible_options) != 1:
            return None
        only_option = visible_options[0]
        if self._has_any_phrase(
            normalized_question,
            (
                'review the linked document',
                'privacy notice',
                'privacy policy',
                'acknowledge',
                'employment and military service',
            ),
        ) or self._normalize_option(only_option) in {'thankyou', 'thanks'}:
            return self._selected_option_answer(only_option, None, VerificationStatus.REVIEW_REQUIRED)
        return None

    def _postal_code_value(self, location: dict[str, Any] | None) -> str | None:
        if not location:
            return None
        for key in ('postal_code', 'zip', 'zipcode'):
            value = str(location.get(key) or '').strip()
            if value:
                return value
        return None

    def _certification_text_answer(self, normalized_question: str, question_type: QuestionType, contact: dict[str, Any] | None) -> GroundedAnswer | None:
        name_value = str((contact or {}).get('name') or '').strip()
        if not name_value:
            return None
        if self._has_any_phrase(normalized_question, ('i certify that', 'electronic signature', 'type your full name', 'sign here')):
            return self._grounded_value(name_value, question_type, contact)
        return None

    def _extract_company_from_employment_question(self, question: str) -> str | None:
        patterns = [
            r'employed by\s+(.+?)(?:,\s+or|\s+or any subsidiary|\s+in the past|\(|\?)',
            r'worked for\s+(.+?)(?:\s+before|\?|,)',
        ]
        for pattern in patterns:
            match = re.search(pattern, question, flags=re.IGNORECASE)
            if match:
                company = match.group(1).strip(' ,.')
                if company:
                    return company
        return None

    def _has_company_in_work_history(self, company_name: str, facts: Sequence[ProfileFact]) -> bool:
        target = self._normalize_option(company_name)
        if not target:
            return False
        for fact in facts:
            if getattr(fact.kind, 'value', fact.kind) != 'work' or fact.disallowed:
                continue
            company = self._normalize_option(str(fact.payload.get('company') or ''))
            if company and (company == target or company in target or target in company):
                return True
        return False

    def _prior_employment_answer(self, question: str, facts: Sequence[ProfileFact]) -> GroundedAnswer | None:
        normalized_question = self._normalized_question_text(question)
        if not self._has_any_phrase(normalized_question, ('have you been employed by', 'have you worked for', 'been employed by')):
            return None
        company_name = self._extract_company_from_employment_question(question)
        employed = self._has_company_in_work_history(company_name, facts) if company_name else False
        return self._yes_no_answer(employed, VerificationStatus.REVIEW_REQUIRED, reason='rule_based_boolean')

    def _match_option(self, options: Sequence[str], candidates: Sequence[str | None]) -> str | None:
        normalized_options = {self._normalize_option(option): option for option in options if str(option).strip()}
        candidate_values: list[str] = []
        for candidate in candidates:
            text = str(candidate or '').strip()
            if not text:
                continue
            candidate_values.append(text)
            if text.upper() == 'US':
                candidate_values.extend(['United States', 'United States of America', 'USA'])
            region_code = normalize_region_code(text)
            if region_code:
                candidate_values.append(region_code)
                region_name = REGION_CODE_TO_NAME.get(region_code)
                if region_name:
                    candidate_values.append(region_name)
        for candidate in candidate_values:
            normalized = self._normalize_option(candidate)
            if normalized in normalized_options:
                return normalized_options[normalized]
        return None

    def _normalize_option(self, value: str) -> str:
        return re.sub(r'[^a-z0-9]+', '', str(value or '').strip().lower())

    def _decline_sensitive_answer(self, question: str, options: Sequence[str]) -> GroundedAnswer | None:
        selected = self._preferred_sensitive_decline_option(options)
        if not selected:
            selected = self._default_sensitive_decline_option(question, options)
        if not selected:
            return None
        return self._selected_option_answer(selected, None, VerificationStatus.REVIEW_REQUIRED)

    def _default_sensitive_decline_option(self, question: str, options: Sequence[str]) -> str | None:
        if any(str(option).strip() for option in options):
            return None
        normalized_question = self._normalized_question_text(question)
        if self._has_any_phrase(normalized_question, ("gender", "race", "ethnicity", "hispanic", "latino")):
            return "Decline To Self Identify"
        if self._has_phrase(normalized_question, "veteran"):
            return "I don't wish to answer"
        if self._has_phrase(normalized_question, "disability"):
            return "I do not want to answer"
        if self._has_any_phrase(normalized_question, ("sexual orientation", "lgbtq", "pronoun", "pronouns")):
            return "I don't wish to answer"
        return None

    def _preferred_sensitive_decline_option(self, options: Sequence[str]) -> str | None:
        preferred_tokens = (
            "decline",
            "prefer not",
            "don't wish",
            "do not wish",
            "not wish",
            "not answer",
            "do not want to answer",
            "choose not",
            "self identify",
            "self-identify",
        )
        visible_options = [str(option).strip() for option in options if str(option).strip()]
        for option in visible_options:
            normalized = str(option).casefold()
            if any(token in normalized for token in preferred_tokens):
                return option
        return None

    def _country_display_name(self, country_code: str | None) -> str | None:
        code = str(country_code or '').strip().upper()
        if not code:
            return None
        if code == 'US':
            return 'United States'
        return code

    def _demographic_fact_value(self, facts: Sequence[ProfileFact], *labels: str) -> tuple[str, ProfileFact] | None:
        wanted = {self._normalize_option(label) for label in labels if str(label).strip()}
        for fact in facts:
            if getattr(fact.kind, 'value', fact.kind) != 'personal' or fact.disallowed or not fact.allowed_for_generation:
                continue
            payload = fact.payload or {}
            if self._normalize_option(str(payload.get('category') or '')) != 'demographic':
                continue
            fact_label = self._normalize_option(str(payload.get('label') or ''))
            if fact_label not in wanted:
                continue
            value = str(payload.get('value') or '').strip()
            if value:
                return value, fact
        return None

    def _demographic_select_answer(self, question: str, options: Sequence[str], facts: Sequence[ProfileFact]) -> GroundedAnswer | None:
        normalized_question = self._normalized_question_text(question)

        def _answer_from_fact(selected: str, fact: ProfileFact, *, reason: str) -> GroundedAnswer:
            return GroundedAnswer(
                question='',
                question_type=QuestionType.SELECT,
                answer=selected,
                selected_option_values=[selected],
                confidence=1.0,
                reason=reason,
                provenance='rules',
                used_fact_ids=[fact.fact_id],
                claim_evidence=[ClaimEvidence(text=selected, fact_id=fact.fact_id)],
                verification_status=VerificationStatus.REVIEW_REQUIRED,
            )

        if self._has_phrase(normalized_question, 'gender'):
            match = self._demographic_fact_value(facts, 'gender', 'gender_identity')
            if match is not None:
                value, fact = match
                selected = self._match_option(options, [value, 'Male', 'Man'])
                if selected:
                    return _answer_from_fact(selected, fact, reason='demographic_gender_rule')

        if self._has_any_phrase(normalized_question, ('transgender', 'transgender experience')):
            match = self._demographic_fact_value(facts, 'transgender_experience', 'transgender')
            if match is not None:
                value, fact = match
                selected = self._match_option(
                    options,
                    [value, 'No', 'Yes', 'I am not a person of transgender experience', 'I am a person of transgender experience'],
                )
                if selected:
                    return _answer_from_fact(selected, fact, reason='demographic_transgender_rule')

        if self._has_any_phrase(normalized_question, ('sexual orientation', 'orientation')):
            match = self._demographic_fact_value(facts, 'sexual_orientation', 'orientation')
            if match is not None:
                value, fact = match
                selected = self._match_option(options, [value, 'Heterosexual', 'Straight'])
                if selected:
                    return _answer_from_fact(selected, fact, reason='demographic_sexual_orientation_rule')

        if self._has_any_phrase(normalized_question, ('hispanic', 'latino')):
            match = self._demographic_fact_value(facts, 'hispanic_ethnicity', 'hispanic', 'latino')
            if match is not None:
                value, fact = match
                selected = self._match_option(options, [value, 'Yes', 'No'])
                if selected:
                    return _answer_from_fact(selected, fact, reason='demographic_hispanic_rule')

        if self._has_any_phrase(normalized_question, ('race', 'ethnicity')) and not self._has_any_phrase(normalized_question, ('hispanic', 'latino')):
            match = self._demographic_fact_value(facts, 'race_ethnicity', 'race', 'ethnicity')
            if match is not None:
                value, fact = match
                selected = self._match_option(
                    options,
                    [value, 'Asian', 'Asian (Not Hispanic or Latino)', 'Asian or Pacific Islander'],
                )
                if selected:
                    return _answer_from_fact(selected, fact, reason='demographic_race_rule')

        if self._has_phrase(normalized_question, 'veteran'):
            match = self._demographic_fact_value(facts, 'veteran_status', 'veteran')
            if match is not None:
                value, fact = match
                selected = self._match_option(
                    options,
                    [value, 'I am not a protected veteran', 'Not a protected veteran', 'No, I am not a protected veteran'],
                )
                if selected:
                    return _answer_from_fact(selected, fact, reason='demographic_veteran_rule')

        if self._has_phrase(normalized_question, 'disability'):
            match = self._demographic_fact_value(facts, 'disability_status', 'disability')
            if match is not None:
                value, fact = match
                selected = self._match_option(
                    options,
                    [value, 'No', 'No, I do not have a disability and have not had one in the past'],
                )
                if selected:
                    return _answer_from_fact(selected, fact, reason='demographic_disability_rule')

        return None

    def _direct_sensitive_answer(self, question: str, facts: Sequence[ProfileFact]) -> GroundedAnswer | None:
        normalized_question = self._normalized_question_text(question)
        if self._has_phrase(normalized_question, 'gender'):
            match = self._demographic_fact_value(facts, 'gender', 'gender_identity')
            if match is not None:
                value, fact = match
                return GroundedAnswer(
                    question='',
                    question_type=QuestionType.SENSITIVE,
                    answer=value,
                    confidence=1.0,
                    reason='demographic_gender_rule',
                    provenance='rules',
                    used_fact_ids=[fact.fact_id],
                    claim_evidence=[ClaimEvidence(text=value, fact_id=fact.fact_id)],
                    verification_status=VerificationStatus.REVIEW_REQUIRED,
                )
        if self._has_any_phrase(normalized_question, ('transgender', 'transgender experience')):
            match = self._demographic_fact_value(facts, 'transgender_experience', 'transgender')
            if match is not None:
                value, fact = match
                return GroundedAnswer(
                    question='',
                    question_type=QuestionType.SENSITIVE,
                    answer=value,
                    confidence=1.0,
                    reason='demographic_transgender_rule',
                    provenance='rules',
                    used_fact_ids=[fact.fact_id],
                    claim_evidence=[ClaimEvidence(text=value, fact_id=fact.fact_id)],
                    verification_status=VerificationStatus.REVIEW_REQUIRED,
                )
        if self._has_any_phrase(normalized_question, ('sexual orientation', 'orientation')):
            match = self._demographic_fact_value(facts, 'sexual_orientation', 'orientation')
            if match is not None:
                value, fact = match
                return GroundedAnswer(
                    question='',
                    question_type=QuestionType.SENSITIVE,
                    answer=value,
                    confidence=1.0,
                    reason='demographic_sexual_orientation_rule',
                    provenance='rules',
                    used_fact_ids=[fact.fact_id],
                    claim_evidence=[ClaimEvidence(text=value, fact_id=fact.fact_id)],
                    verification_status=VerificationStatus.REVIEW_REQUIRED,
                )
        if self._has_any_phrase(normalized_question, ('hispanic', 'latino')):
            match = self._demographic_fact_value(facts, 'hispanic_ethnicity', 'hispanic', 'latino')
            if match is not None:
                value, fact = match
                return GroundedAnswer(
                    question='',
                    question_type=QuestionType.SENSITIVE,
                    answer=value,
                    confidence=1.0,
                    reason='demographic_hispanic_rule',
                    provenance='rules',
                    used_fact_ids=[fact.fact_id],
                    claim_evidence=[ClaimEvidence(text=value, fact_id=fact.fact_id)],
                    verification_status=VerificationStatus.REVIEW_REQUIRED,
                )
        if self._has_any_phrase(normalized_question, ('race', 'ethnicity')) and not self._has_any_phrase(normalized_question, ('hispanic', 'latino')):
            match = self._demographic_fact_value(facts, 'race_ethnicity', 'race', 'ethnicity')
            if match is not None:
                value, fact = match
                return GroundedAnswer(
                    question='',
                    question_type=QuestionType.SENSITIVE,
                    answer=value,
                    confidence=1.0,
                    reason='demographic_race_rule',
                    provenance='rules',
                    used_fact_ids=[fact.fact_id],
                    claim_evidence=[ClaimEvidence(text=value, fact_id=fact.fact_id)],
                    verification_status=VerificationStatus.REVIEW_REQUIRED,
                )
        if self._has_phrase(normalized_question, 'veteran'):
            match = self._demographic_fact_value(facts, 'veteran_status', 'veteran')
            if match is not None:
                value, fact = match
                return GroundedAnswer(
                    question='',
                    question_type=QuestionType.SENSITIVE,
                    answer=value,
                    confidence=1.0,
                    reason='demographic_veteran_rule',
                    provenance='rules',
                    used_fact_ids=[fact.fact_id],
                    claim_evidence=[ClaimEvidence(text=value, fact_id=fact.fact_id)],
                    verification_status=VerificationStatus.REVIEW_REQUIRED,
                )
        if self._has_phrase(normalized_question, 'disability'):
            match = self._demographic_fact_value(facts, 'disability_status', 'disability')
            if match is not None:
                value, fact = match
                return GroundedAnswer(
                    question='',
                    question_type=QuestionType.SENSITIVE,
                    answer=value,
                    confidence=1.0,
                    reason='demographic_disability_rule',
                    provenance='rules',
                    used_fact_ids=[fact.fact_id],
                    claim_evidence=[ClaimEvidence(text=value, fact_id=fact.fact_id)],
                    verification_status=VerificationStatus.REVIEW_REQUIRED,
                )
        for fact in facts:
            if fact.kind.value != "authorization" or fact.disallowed:
                continue
            payload = fact.payload
            if self._has_any_phrase(normalized_question, ('authorized', 'work authorization', 'legally authorized', 'eligible to work')) and 'is_authorized' in payload:
                value = 'Yes' if payload['is_authorized'] else 'No'
                return GroundedAnswer(
                    question='',
                    question_type=QuestionType.SENSITIVE,
                    answer=value,
                    confidence=1.0,
                    reason='authorization_rule',
                    provenance='rules',
                    used_fact_ids=[fact.fact_id],
                    claim_evidence=[ClaimEvidence(text=value, fact_id=fact.fact_id)],
                    verification_status=VerificationStatus.REVIEW_REQUIRED,
                )
            if self._has_any_phrase(normalized_question, ('sponsor', 'sponsorship', 'visa')) and 'requires_future_sponsorship' in payload:
                requires_sponsorship = bool(payload['requires_future_sponsorship'])
                if self._has_any_phrase(normalized_question, ('without sponsorship', 'do not require sponsorship', 'does not require sponsorship')):
                    requires_sponsorship = not requires_sponsorship
                value = 'Yes' if requires_sponsorship else 'No'
                return GroundedAnswer(
                    question='',
                    question_type=QuestionType.SENSITIVE,
                    answer=value,
                    confidence=1.0,
                    reason='authorization_rule',
                    provenance='rules',
                    used_fact_ids=[fact.fact_id],
                    claim_evidence=[ClaimEvidence(text=value, fact_id=fact.fact_id)],
                    verification_status=VerificationStatus.REVIEW_REQUIRED,
                )
        return None
    def _direct_fact_answer(self, question: str, facts: Sequence[ProfileFact], question_type: QuestionType) -> GroundedAnswer | None:
        normalized_question = self._normalized_question_text(question)
        for fact in facts:
            if not fact.allowed_for_generation or fact.disallowed:
                continue
            payload = fact.payload
            if question_type == QuestionType.DETERMINISTIC:
                for field, phrases in (
                    ("email", ("email", "e mail")),
                    ("phone", ("phone", "mobile", "telephone")),
                    ("linkedin", ("linkedin",)),
                    ("portfolio", ("portfolio",)),
                    ("website", ("website", "site", "url")),
                    ("github", ("github",)),
                    ("name", ("name",)),
                ):
                    if self._has_any_phrase(normalized_question, phrases) and payload.get(field):
                        value = str(payload[field])
                        return GroundedAnswer(
                            question="",
                            question_type=question_type,
                            answer=value,
                            used_fact_ids=[fact.fact_id],
                            claim_evidence=[ClaimEvidence(text=value, fact_id=fact.fact_id)],
                            verification_status=VerificationStatus.VERIFIED,
                        )
                if self._has_any_phrase(normalized_question, ("location", "city", "state", "country", "zip code", "postal code")):
                    location_value = payload.get("display") or payload.get("city") or payload.get("postal_code") or payload.get("zip") or payload.get("country_code")
                    if location_value:
                        value = str(location_value)
                        return GroundedAnswer(
                            question="",
                            question_type=question_type,
                            answer=value,
                            used_fact_ids=[fact.fact_id],
                            claim_evidence=[ClaimEvidence(text=value, fact_id=fact.fact_id)],
                            verification_status=VerificationStatus.VERIFIED,
                        )
                if self._has_any_phrase(normalized_question, ("school", "university", "college", "degree", "major", "gpa", "graduation")):
                    for field in ("school", "degree", "summary", "date_label"):
                        if payload.get(field):
                            value = str(payload[field])
                            return GroundedAnswer(
                                question="",
                                question_type=question_type,
                                answer=value,
                                used_fact_ids=[fact.fact_id],
                                claim_evidence=[ClaimEvidence(text=value, fact_id=fact.fact_id)],
                                verification_status=VerificationStatus.REVIEW_REQUIRED,
                            )
            if question_type == QuestionType.BOOLEAN and fact.kind.value == "authorization":
                if self._has_any_phrase(normalized_question, ("authorized", "work authorization", "eligible to work")) and "is_authorized" in payload:
                    value = "Yes" if payload["is_authorized"] else "No"
                    return GroundedAnswer(
                        question="",
                        question_type=question_type,
                        answer=value,
                        used_fact_ids=[fact.fact_id],
                        claim_evidence=[ClaimEvidence(text=value, fact_id=fact.fact_id)],
                        verification_status=VerificationStatus.REVIEW_REQUIRED,
                    )
                if self._has_any_phrase(normalized_question, ("sponsor", "sponsorship", "visa")) and "requires_future_sponsorship" in payload:
                    value = "Yes" if payload["requires_future_sponsorship"] else "No"
                    return GroundedAnswer(
                        question="",
                        question_type=question_type,
                        answer=value,
                        used_fact_ids=[fact.fact_id],
                        claim_evidence=[ClaimEvidence(text=value, fact_id=fact.fact_id)],
                        verification_status=VerificationStatus.REVIEW_REQUIRED,
                    )
            if question_type == QuestionType.NUMERIC:
                for field in ("years_experience", "gpa", "salary_expectation"):
                    if self._has_phrase(normalized_question, field.replace("_", " ")) and payload.get(field) is not None:
                        value = str(payload[field])
                        return GroundedAnswer(
                            question="",
                            question_type=question_type,
                            answer=value,
                            used_fact_ids=[fact.fact_id],
                            claim_evidence=[ClaimEvidence(text=value, fact_id=fact.fact_id)],
                            verification_status=VerificationStatus.REVIEW_REQUIRED,
                        )
            if question_type == QuestionType.DATE:
                for field in ("available_start_date", "graduation_date", "start_date", "end_date"):
                    if self._has_phrase(normalized_question, field.replace("_", " ")) and payload.get(field):
                        value = str(payload[field])
                        return GroundedAnswer(
                            question="",
                            question_type=question_type,
                            answer=value,
                            used_fact_ids=[fact.fact_id],
                            claim_evidence=[ClaimEvidence(text=value, fact_id=fact.fact_id)],
                            verification_status=VerificationStatus.REVIEW_REQUIRED,
                        )
        return None

    def _company_motivation_answer(self, question: str, facts: Sequence[ProfileFact]) -> GroundedAnswer | None:
        normalized_question = self._normalized_question_text(question)
        if not (
            normalized_question.startswith("why ")
            or self._has_any_phrase(
                normalized_question,
                ("why us", "why this company", "why do you want to work here", "why do you want to join"),
            )
        ):
            return None
        for fact in facts:
            if fact.disallowed or not fact.allowed_for_generation:
                continue
            payload = fact.payload or {}
            company = str(payload.get("company") or "").strip()
            role = str(payload.get("title") or "").strip()
            summary = str(payload.get("summary") or "").strip()
            bullets = [
                str(item).strip()
                for item in list(payload.get("bullets") or [])
                if str(item).strip()
            ]
            if not company:
                continue
            if company.casefold() not in normalized_question and not self._has_any_phrase(
                normalized_question,
                ("why us", "why this company", "why do you want to work here", "why do you want to join"),
            ):
                continue
            cleaned_summary = re.sub(r"^Target application:[^.]+\.\s*", "", summary).strip()
            cleaned_summary = re.sub(r"^Location:[^.]+\.\s*", "", cleaned_summary).strip()
            lead_reason = bullets[0] if bullets else cleaned_summary
            if not lead_reason:
                continue
            role_prefix = f"the {role}" if role else "this role"
            answer_text = (
                f"I'm excited about {company} because {role_prefix} aligns closely with my background in forecasting, "
                f"evaluation-first machine learning, and Python-based analytics systems. {lead_reason}"
            ).strip()
            return GroundedAnswer(
                question="",
                question_type=QuestionType.NARRATIVE,
                answer=answer_text,
                confidence=0.9,
                reason="company_motivation_rule",
                provenance="rules",
                verification_status=VerificationStatus.REVIEW_REQUIRED,
            )
        return None

    async def _narrative_answer(self, question: str, canonical_question: str, facts: Sequence[ProfileFact]) -> GroundedAnswer:
        question_tokens = self._content_tokens(question)
        supporting: list[tuple[int, str, str]] = []
        for fact in facts:
            if not fact.allowed_for_generation or fact.disallowed:
                continue
            fragments: list[str] = []
            for field in ('name', 'title', 'summary', 'description', 'achievement'):
                value = fact.payload.get(field)
                if value:
                    fragments.append(str(value))
            bullets = fact.payload.get('bullets')
            if isinstance(bullets, Sequence) and not isinstance(bullets, (str, bytes)):
                fragments.extend(str(item) for item in bullets if str(item).strip())
            summary = ' '.join(fragment.strip() for fragment in fragments if str(fragment).strip())
            if not summary:
                continue
            overlap = len(question_tokens & self._content_tokens(summary))
            supporting.append((overlap, fact.fact_id, summary))
        if not supporting:
            return GroundedAnswer(question=question, canonical_question=canonical_question, question_type=QuestionType.NARRATIVE)

        supporting.sort(key=lambda item: (item[0] > 0, item[0], len(item[2])), reverse=True)
        relevant = [(fact_id, text) for overlap, fact_id, text in supporting if overlap > 0]
        if not relevant:
            return GroundedAnswer(question=question, canonical_question=canonical_question, question_type=QuestionType.NARRATIVE)
        snippets = relevant[:3]
        answer_text = ' '.join(text for _fact_id, text in snippets)
        provenance = 'generated'
        if self.router is not None:
            try:
                prompt = (
                    'Answer the application question using only the supplied facts. Do not invent details. '
                    'If the supplied facts address the question, answer concisely by paraphrasing only those facts. '
                    'Return NEEDS_USER_INPUT only when the supplied facts do not address the question.\n'
                    f'Question: {question}\nFacts:\n' + '\n'.join(f'- {fact_id}: {text}' for fact_id, text in snippets)
                )
                answer_text, profile_name = await self.router.generate_text_with_profile(
                    ModelRole.QUESTION_ANSWERER,
                    prompt,
                    system_prompt='Use only the provided facts. If the facts address the question, answer directly. If they do not, return NEEDS_USER_INPUT.',
                )
                provenance = f'model:{profile_name}'
                if 'NEEDS_USER_INPUT' in answer_text:
                    return GroundedAnswer(question=question, canonical_question=canonical_question, question_type=QuestionType.NARRATIVE)
            except Exception:
                answer_text = ' '.join(text for _fact_id, text in snippets)
                provenance = 'generated'

        verification_status, unsupported = await self.verify_narrative(answer_text, question, snippets)
        confidence = 0.8 if verification_status == VerificationStatus.REVIEW_REQUIRED else 0.3
        return GroundedAnswer(
            question=question,
            canonical_question=canonical_question,
            question_type=QuestionType.NARRATIVE,
            answer=answer_text,
            confidence=confidence,
            reason=f"narrative_from_{len(snippets)}_facts" if not unsupported else f"unsupported_segments:{len(unsupported)}",
            used_fact_ids=[fact_id for fact_id, _text in snippets],
            unsupported_segments=unsupported,
            claim_evidence=[ClaimEvidence(text=text, fact_id=fact_id) for fact_id, text in snippets],
            provenance=provenance,
            verification_status=verification_status,
        )

    async def verify_narrative(self, answer_text: str, question: str, snippets: Sequence[tuple[str, str]]) -> tuple[VerificationStatus, list[str]]:
        if self.router is not None:
            try:
                payload, _profile_name = await self.router.generate_json_with_profile(
                    ModelRole.VERIFIER,
                    (
                        "Verify whether each sentence in the answer is supported by the supplied facts. "
                        "Return JSON with keys supported:boolean and unsupported_segments:list[string].\n"
                        f"Question: {question}\n"
                        f"Answer: {answer_text}\n"
                        "Facts:\n" + "\n".join(f"- {fact_id}: {text}" for fact_id, text in snippets)
                    ),
                    system_prompt="Only return valid JSON.",
                )
                unsupported_segments = [str(item).strip() for item in payload.get("unsupported_segments", []) if str(item).strip()]
                if unsupported_segments or not payload.get("supported", False):
                    return VerificationStatus.NEEDS_USER_INPUT, unsupported_segments or [answer_text]
                return VerificationStatus.REVIEW_REQUIRED, []
            except Exception:
                pass

        unsupported: list[str] = []
        fact_tokens = {fact_id: self._content_tokens(text) for fact_id, text in snippets}
        fact_bigrams = {fact_id: self._bigrams(tokens) for fact_id, tokens in fact_tokens.items()}
        for sentence in [part.strip() for part in re.split(r"[.!?]", answer_text) if part.strip()]:
            sentence_tokens = self._content_tokens(sentence)
            if not sentence_tokens:
                continue
            supported = False
            for fact_id, tokens in fact_tokens.items():
                if sentence.lower() in snippets[[item[0] for item in snippets].index(fact_id)][1].lower():
                    supported = True
                    break
                if sentence_tokens.issubset(tokens):
                    supported = True
                    break
                overlap = len(sentence_tokens & tokens) / max(len(sentence_tokens), 1)
                sentence_bigrams = self._bigrams(sentence_tokens)
                bigram_overlap = len(sentence_bigrams & fact_bigrams[fact_id]) / max(len(sentence_bigrams), 1) if sentence_bigrams else 0.0
                if overlap >= 0.8 and bigram_overlap >= 0.5:
                    supported = True
                    break
            if not supported:
                unsupported.append(sentence)
        if unsupported:
            return VerificationStatus.NEEDS_USER_INPUT, unsupported
        return VerificationStatus.REVIEW_REQUIRED, []

    def _content_tokens(self, text: str) -> set[str]:
        return {token for token in WORD_RE.findall(text.lower()) if token not in STOPWORDS}

    def _bigrams(self, tokens: set[str]) -> set[tuple[str, str]]:
        ordered = sorted(tokens)
        return set(zip(ordered, ordered[1:]))




