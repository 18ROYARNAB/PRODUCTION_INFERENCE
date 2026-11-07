import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from evaluation.run_deepeval import collect_chat_results, load_questions


class DeepEvalDatasetTests(unittest.TestCase):
    def test_loads_project_question_set(self) -> None:
        questions = load_questions(
            Path(__file__).parents[1] / "evaluation" / "rag_questions.jsonl"
        )

        self.assertEqual(len(questions), 14)
        self.assertEqual(
            sum(
                question["metadata"]["expected_behavior"] == "abstain"
                for question in questions
            ),
            1,
        )

    def test_rejects_duplicate_ids(self) -> None:
        question = {
            "id": "duplicate",
            "user_input": "Question?",
            "reference": "Answer.",
            "reference_contexts": ["Evidence."],
            "metadata": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            dataset = Path(directory) / "questions.jsonl"
            dataset.write_text(
                json.dumps(question) + "\n" + json.dumps(question) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "duplicate question id"):
                load_questions(dataset)

    def test_collects_actual_answer_and_retrieved_context(self) -> None:
        question = {
            "id": "sample",
            "user_input": "What does this CVE affect?",
            "reference": "A sample component.",
            "reference_contexts": ["The sample component is affected."],
            "metadata": {},
        }
        response = SimpleNamespace(
            is_error=False,
            json=lambda: {
                "model_output": "The sample component is affected.",
                "safe_context": ["The sample component is affected."],
                "role_applied": "SecOps_Lead",
            },
        )
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(return_value=response)

        async def collect() -> list[dict[str, object]]:
            with patch(
                "evaluation.run_deepeval.httpx.AsyncClient",
                return_value=client,
            ):
                return await collect_chat_results(
                    [question],
                    api_url="http://localhost:8000",
                    bearer_token="local-dev-token",
                    request_delay=0,
                )

        results = asyncio.run(collect())

        self.assertEqual(results[0]["actual_output"], "The sample component is affected.")
        self.assertEqual(
            results[0]["retrieved_contexts"],
            ["The sample component is affected."],
        )
        self.assertEqual(results[0]["role_applied"], "SecOps_Lead")
        client.post.assert_awaited_once_with(
            "/chat",
            json={"prompt": "What does this CVE affect?"},
        )


if __name__ == "__main__":
    unittest.main()
