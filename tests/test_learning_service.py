import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from learning_service import DeepSeekError, LearningService, calculate_feedback, validate_generated_word
from storage import JsonVocabularyRepository


NOW = datetime(2026, 9, 27, 13, 0, tzinfo=timezone(timedelta(hours=8)))
WORD = {
    "schema_version": 1,
    "word": "predict",
    "phonetic": "/prɪˈdɪkt/",
    "morphology": {"parts": [], "analysis": ["predict = pre- + dict"]},
    "meanings": [
        {
            "id": "meaning_1",
            "meaning_en": "to say what will happen",
            "meaning_zh": "预测",
            "examples": ["Experts predict more rain."],
        },
        {
            "id": "meaning_2",
            "meaning_en": "to estimate a result",
            "meaning_zh": "预先判断结果",
            "examples": ["Can you predict the result?"],
        },
    ],
}


class LearningServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = JsonVocabularyRepository(Path(self.temporary.name))
        self.repository.save_word(WORD)
        self.repository.save_learning_plan(
            {
                "schema_version": 1,
                "generated_at": NOW.isoformat(),
                "status": "in_progress",
                "words": ["predict"],
                "queue": ["predict"],
                "current_index": 0,
                "completed_words": [],
            }
        )
        self.service = LearningService(self.repository, now=lambda: NOW)

    def tearDown(self):
        self.temporary.cleanup()

    def test_wrong_answer_appends_word_to_end(self):
        result = self.service.record_answer(
            "learning",
            {
                "word": "predict",
                "meaning_results": {"meaning_1": True, "meaning_2": False},
            },
        )
        self.assertEqual(result["result"]["feedback"], "again")
        self.assertEqual(result["session"]["current_word"], "predict")
        self.assertEqual(result["session"]["completed"], 0)
        self.assertEqual(result["session"]["total"], 1)
        self.assertEqual(self.repository.get_learning_plan()["queue"], ["predict", "predict"])

    def test_all_meanings_must_be_correct(self):
        result = self.service.record_answer(
            "learning",
            {
                "word": "predict",
                "meaning_results": {"meaning_1": True, "meaning_2": True},
            },
        )
        self.assertTrue(result["result"]["correct"])
        self.assertEqual(result["result"]["feedback"], "hard")
        self.assertEqual(result["session"]["completed"], 1)
        self.assertEqual(result["session"]["total"], 1)
        self.assertEqual(
            result["result"]["next_review_time"],
            (NOW + timedelta(days=1)).isoformat(timespec="seconds"),
        )

    def test_feedback_progression(self):
        records = []
        expected = ["hard", "good", "easy", "know_well"]
        for feedback in expected:
            actual = calculate_feedback(records, True)
            self.assertEqual(actual, feedback)
            records.append({"correct": True})
        self.assertEqual(calculate_feedback(records, False), "again")

    def test_generated_meaning_must_have_exactly_one_example(self):
        invalid = dict(WORD)
        invalid["meanings"] = [dict(WORD["meanings"][0])]
        invalid["meanings"][0]["examples"] = ["First example.", "Second example."]
        with self.assertRaises(DeepSeekError):
            validate_generated_word(invalid)


if __name__ == "__main__":
    unittest.main()
