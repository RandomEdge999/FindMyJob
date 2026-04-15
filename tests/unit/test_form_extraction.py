from findmyjob.apply.forms import extract_questions_from_greenhouse_payload, extract_questions_from_html, extract_questions_from_lever_fields, merge_extraction_results


def test_extract_questions_from_html_parses_basic_fields() -> None:
    html = """
    <form>
      <label for='email'>Email Address</label>
      <input id='email' name='email' type='email' required />
      <label for='visa'>Will you require sponsorship?</label>
      <select id='visa' name='visa'>
        <option>Yes</option>
        <option>No</option>
      </select>
      <label for='resume'>Resume</label>
      <input id='resume' name='resume' type='file' accept='.pdf' required />
    </form>
    """
    result = extract_questions_from_html(html, handoff_url="https://example.com/apply")
    assert len(result.questions) == 3
    assert result.questions[0].normalized_key == "email-address"
    assert result.questions[1].sensitive is True
    assert result.questions[1].options == ["Yes", "No"]
    assert result.questions[2].question_type.value == "file"


def test_extract_questions_from_greenhouse_payload_tolerates_list_shaped_sections() -> None:
    payload = {
        "questions": [
            {
                "label": "Email Address",
                "fields": [{"name": "email", "type": "input_text", "required": True}],
            },
            "ignore-me",
        ],
        "compliance": [
            {
                "label": "Resume/CV",
                "required": True,
                "fields": [{"name": "resume", "type": "input_file"}],
            }
        ],
        "demographic_questions": [
            {
                "id": "work_auth",
                "label": "Are you legally authorized to work in the United States?",
                "type": "multi_value_single_select",
                "answer_options": ["Yes", "No"],
            }
        ],
    }

    result = extract_questions_from_greenhouse_payload(payload, handoff_url="https://example.com/apply")

    assert result.questions[0].normalized_key == "email-address"
    assert result.questions[1].question_type.value == "file"
    assert result.questions[2].normalized_key == "are-you-legally-authorized-to-work-in-the-united-states"
    assert result.questions[2].options == ["Yes", "No"]
    assert any(question.source_field_name == "first_name" for question in result.questions)
    assert any(question.source_field_name == "last_name" for question in result.questions)
    assert any(question.source_field_name == "resume" for question in result.questions)


def test_extract_questions_from_greenhouse_payload_adds_standard_greenhouse_fields_when_missing() -> None:
    payload = {
        "questions": [
            {
                "label": "Do you hold an AWS certification?",
                "required": True,
                "fields": [{"name": "aws_certification", "type": "multi_value_single_select"}],
                "answer_options": ["Yes", "No"],
            }
        ]
    }

    result = extract_questions_from_greenhouse_payload(payload, handoff_url="https://example.com/apply")
    by_name = {question.source_field_name: question for question in result.questions}

    assert "aws_certification" in by_name
    assert "first_name" in by_name
    assert "last_name" in by_name
    assert "preferred_name" in by_name
    assert "email" in by_name
    assert "phone" in by_name
    assert "location" in by_name
    assert "resume" in by_name
    assert "cover_letter" in by_name
    assert by_name["first_name"].required is True
    assert by_name["resume"].question_type.value == "file"
    assert by_name["cover_letter"].required is False
    assert by_name["first_name"].source_snapshot_ref == "greenhouse:synthetic:first_name"


def test_merge_extraction_results_prefers_rendered_location_field_over_payload_location() -> None:
    primary = extract_questions_from_greenhouse_payload(
        {
            "location_questions": [
                {
                    "label": "Location",
                    "required": True,
                    "fields": [{"name": "location", "type": "input_text"}],
                }
            ]
        },
        handoff_url="https://example.com/apply",
    )
    fallback = extract_questions_from_html(
        """
        <form>
          <label for='candidate-location'>Location (City)*</label>
          <input id='candidate-location' name='candidate-location' type='text' required />
        </form>
        """,
        handoff_url="https://example.com/apply",
    )

    merged = merge_extraction_results(primary, fallback)
    location_questions = [question for question in merged.questions if "location" in question.prompt_text.lower()]

    assert len(location_questions) == 1
    assert location_questions[0].source_field_name == "candidate-location"
    assert location_questions[0].normalized_key == "location-city"


def test_merge_extraction_results_replaces_synthetic_greenhouse_baseline_with_rendered_dom_field() -> None:
    primary = extract_questions_from_greenhouse_payload({}, handoff_url="https://example.com/apply")
    fallback = extract_questions_from_html(
        """
        <form>
          <label for='email'>Email Address</label>
          <input id='email' name='email' type='email' required />
        </form>
        """,
        handoff_url="https://example.com/apply",
    )

    merged = merge_extraction_results(primary, fallback)
    email_questions = [question for question in merged.questions if question.source_field_name == "email"]

    assert len(email_questions) == 1
    assert email_questions[0].prompt_text == "Email Address"
    assert email_questions[0].required is True
    assert email_questions[0].source_snapshot_ref == "html:input:email"


def test_extract_questions_from_lever_fields_uses_group_label_for_yes_no_radios() -> None:
    result = extract_questions_from_lever_fields(
        [
            {
                "tag": "input",
                "type": "radio",
                "field_type": "radio",
                "widget_type": "radio_group",
                "name": "work_auth",
                "id": "work_auth_yes",
                "required": True,
                "label": "Yes",
                "group_label": "Are you legally authorized to work in the United States?",
                "option_label": "Yes",
                "option_value": "Yes",
                "source_snapshot_ref": "radio:work_auth",
            },
            {
                "tag": "input",
                "type": "radio",
                "field_type": "radio",
                "widget_type": "radio_group",
                "name": "work_auth",
                "id": "work_auth_no",
                "required": True,
                "label": "No",
                "group_label": "Are you legally authorized to work in the United States?",
                "option_label": "No",
                "option_value": "No",
                "source_snapshot_ref": "radio:work_auth",
            },
        ],
        handoff_url="https://example.com/apply",
    )

    assert len(result.questions) == 1
    assert result.questions[0].prompt_text == "Are you legally authorized to work in the United States?"
    assert result.questions[0].normalized_key == "are-you-legally-authorized-to-work-in-the-united-states"
    assert result.questions[0].options == ["Yes", "No"]


def test_extract_questions_from_lever_fields_groups_checkbox_sets_by_group_label() -> None:
    result = extract_questions_from_lever_fields(
        [
            {
                "tag": "input",
                "type": "checkbox",
                "field_type": "checkbox",
                "widget_type": "checkbox_group",
                "name": "LinkedIn",
                "id": "heard_linkedin",
                "required": False,
                "label": "How did you hear about this opportunity? (select all that apply)",
                "group_label": "How did you hear about this opportunity? (select all that apply)",
                "option_label": "LinkedIn",
                "option_value": "LinkedIn",
                "source_snapshot_ref": "checkbox:LinkedIn",
            },
            {
                "tag": "input",
                "type": "checkbox",
                "field_type": "checkbox",
                "widget_type": "checkbox_group",
                "name": "Notion Website",
                "id": "heard_website",
                "required": False,
                "label": "How did you hear about this opportunity? (select all that apply)",
                "group_label": "How did you hear about this opportunity? (select all that apply)",
                "option_label": "Notion Website",
                "option_value": "Notion Website",
                "source_snapshot_ref": "checkbox:Notion Website",
            },
        ],
        handoff_url="https://example.com/apply",
    )

    assert len(result.questions) == 1
    assert result.questions[0].prompt_text == "How did you hear about this opportunity? (select all that apply)"
    assert result.questions[0].options == ["LinkedIn", "Notion Website"]


def test_extract_questions_from_lever_fields_marks_captcha_tokens_as_helper_fields() -> None:
    result = extract_questions_from_lever_fields(
        [
            {
                "tag": "textarea",
                "type": "",
                "field_type": "textarea",
                "widget_type": "textarea",
                "name": "g-recaptcha-response",
                "id": "g-recaptcha-response",
                "required": False,
                "label": "g-recaptcha-response",
                "source_snapshot_ref": "textarea:g-recaptcha-response",
            }
        ],
        handoff_url="https://example.com/apply",
    )

    assert len(result.questions) == 1
    assert result.questions[0].input_role == "helper"
    assert result.questions[0].visible_to_operator is False


def test_extract_questions_from_lever_fields_skips_uuid_only_choice_groups() -> None:
    result = extract_questions_from_lever_fields(
        [
            {
                "tag": "input",
                "type": "checkbox",
                "field_type": "checkbox",
                "widget_type": "checkbox_group",
                "name": "e01a85db-feaa-42b3-a9ad-69b1dcbbab3f",
                "label": "e01a85db-feaa-42b3-a9ad-69b1dcbbab3f",
                "group_label": "e01a85db-feaa-42b3-a9ad-69b1dcbbab3f",
                "option_label": "e01a85db-feaa-42b3-a9ad-69b1dcbbab3f",
                "option_value": "e01a85db-feaa-42b3-a9ad-69b1dcbbab3f",
            }
        ],
        handoff_url="https://example.com/apply",
    )

    assert result.questions == []


def test_extract_questions_from_lever_fields_collapses_polluted_select_labels() -> None:
    result = extract_questions_from_lever_fields(
        [
            {
                "tag": "select",
                "type": "",
                "field_type": "select",
                "widget_type": "select",
                "name": "eeoc_race",
                "label": "RaceSelect ...Hispanic or LatinoWhite (Not Hispanic or Latino)Decline to self-identify",
                "required": False,
                "options": ["Asian", "White"],
            },
            {
                "tag": "select",
                "type": "",
                "field_type": "select",
                "widget_type": "select",
                "name": "opportunityLocationId",
                "label": "opportunityLocationId",
                "required": True,
                "options": ["San Francisco", "Remote"],
            },
        ],
        handoff_url="https://example.com/apply",
    )

    assert len(result.questions) == 2
    assert result.questions[0].prompt_text == "Race"
    assert result.questions[1].prompt_text == "Location"
