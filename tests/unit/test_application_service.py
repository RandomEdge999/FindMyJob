from types import SimpleNamespace

from findmyjob.apply.service import ApplicationService
from findmyjob.core.enums import QuestionType


def test_missing_required_questions_skips_file_inputs() -> None:
    service = ApplicationService(SimpleNamespace(duplicate_exists=lambda *args, **kwargs: False), SimpleNamespace())
    question = SimpleNamespace(required=True, question_type=QuestionType.FILE, prompt_text="Resume")
    missing = service.missing_required_questions([(question, None)])
    assert missing == []
