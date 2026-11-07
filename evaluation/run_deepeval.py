#!/usr/bin/env python3
"""Run DeepEval RAG metrics against the application's authenticated chat API."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from openai import RateLimitError
from tenacity import RetryError

DEFAULT_DATASET = Path(__file__).with_name("rag_questions.jsonl")
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_GROQ_JUDGE_MODEL = "openai/gpt-oss-120b"


def load_questions(path: Path) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    required_fields = {
        "id",
        "user_input",
        "reference",
        "reference_contexts",
        "metadata",
    }

    with path.open(encoding="utf-8") as dataset:
        for line_number, line in enumerate(dataset, start=1):
            if not line.strip():
                continue
            try:
                question = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"{path}:{line_number}: invalid JSONL: {error.msg}"
                ) from error

            if not isinstance(question, dict) or not required_fields <= question.keys():
                raise ValueError(
                    f"{path}:{line_number}: question must include "
                    f"{', '.join(sorted(required_fields))}"
                )
            if not all(
                isinstance(question[field], str) and question[field].strip()
                for field in ("id", "user_input", "reference")
            ):
                raise ValueError(
                    f"{path}:{line_number}: id, user_input, and reference "
                    "must be non-empty strings"
                )
            if (
                not isinstance(question["reference_contexts"], list)
                or not question["reference_contexts"]
                or not all(
                    isinstance(context, str) and context.strip()
                    for context in question["reference_contexts"]
                )
            ):
                raise ValueError(
                    f"{path}:{line_number}: reference_contexts must be "
                    "a non-empty list of strings"
                )
            if question["id"] in seen_ids:
                raise ValueError(
                    f"{path}:{line_number}: duplicate question id "
                    f"{question['id']!r}"
                )
            seen_ids.add(question["id"])
            questions.append(question)

    if not questions:
        raise ValueError(f"No questions found in {path}")
    return questions


def resolve_bearer_token() -> str:
    token = os.getenv("EVAL_BEARER_TOKEN")
    if token:
        return token

    if (
        os.getenv("APP_ENV", "").lower() == "development"
        and os.getenv("AUTH_DEV_MODE", "").lower() == "true"
        and os.getenv("AUTH_DEV_TOKEN")
    ):
        return os.environ["AUTH_DEV_TOKEN"]

    raise RuntimeError(
        "Set EVAL_BEARER_TOKEN to an API bearer token, or explicitly enable "
        "development auth with APP_ENV=development and AUTH_DEV_MODE=true."
    )


async def collect_chat_results(
    questions: list[dict[str, Any]],
    *,
    api_url: str,
    bearer_token: str,
    request_delay: float,
) -> list[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {bearer_token}"}
    results: list[dict[str, Any]] = []
    timeout = httpx.Timeout(120.0)

    async with httpx.AsyncClient(
        base_url=api_url.rstrip("/"),
        headers=headers,
        timeout=timeout,
    ) as client:
        for index, question in enumerate(questions):
            response = await client.post(
                "/chat",
                json={"prompt": question["user_input"]},
            )
            if response.is_error:
                raise RuntimeError(
                    f"Chat request for {question['id']} failed with HTTP "
                    f"{response.status_code}: {response.text[:500]}"
                )
            payload = response.json()
            answer = payload.get("model_output")
            contexts = payload.get("safe_context")
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError(
                    f"Chat response for {question['id']} has no model_output"
                )
            if not isinstance(contexts, list) or not all(
                isinstance(context, str) for context in contexts
            ):
                raise ValueError(
                    f"Chat response for {question['id']} has invalid safe_context"
                )

            results.append(
                {
                    "question": question,
                    "actual_output": answer,
                    "retrieved_contexts": contexts,
                    "role_applied": payload.get("role_applied"),
                }
            )
            print(f"Collected {index + 1}/{len(questions)}: {question['id']}")
            if request_delay > 0 and index + 1 < len(questions):
                await asyncio.sleep(request_delay)

    return results


def evaluate_results(
    chat_results: list[dict[str, Any]],
    *,
    judge_model_name: str,
    groq_api_key: str,
    metric_delay: float,
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    try:
        from deepeval.metrics import (
            AnswerRelevancyMetric,
            ContextualPrecisionMetric,
            ContextualRecallMetric,
            FaithfulnessMetric,
            GEval,
        )
        from deepeval.models import OpenAIModel
        from deepeval.test_case import LLMTestCase, SingleTurnParams
    except ImportError as error:
        raise RuntimeError(
            "DeepEval is not installed. Install the evaluation extra with "
            "`uv sync --extra evaluation`."
        ) from error

    judge_model = OpenAIModel(
        model=judge_model_name,
        api_key=groq_api_key,
        base_url=GROQ_BASE_URL,
    )
    metrics = [
        ("answer_relevancy", AnswerRelevancyMetric(model=judge_model)),
        ("faithfulness", FaithfulnessMetric(model=judge_model)),
        ("contextual_precision", ContextualPrecisionMetric(model=judge_model)),
        ("contextual_recall", ContextualRecallMetric(model=judge_model)),
        (
            "answer_correctness",
            GEval(
                name="Answer Correctness",
                criteria=(
                    "Assess whether the actual answer correctly and completely "
                    "answers the input according to the expected answer. Do not "
                    "reward claims unsupported by the retrieved context. When "
                    "the expected answer says the requested fact is unavailable "
                    "in the retrieved context, the answer should abstain clearly "
                    "and must not invent that fact."
                ),
                evaluation_params=[
                    SingleTurnParams.INPUT,
                    SingleTurnParams.ACTUAL_OUTPUT,
                    SingleTurnParams.EXPECTED_OUTPUT,
                    SingleTurnParams.RETRIEVAL_CONTEXT,
                ],
                model=judge_model,
            ),
        ),
    ]

    detailed_results: list[dict[str, Any]] = []
    totals = {name: 0.0 for name, _ in metrics}
    for item in chat_results:
        question = item["question"]
        test_case = LLMTestCase(
            input=question["user_input"],
            actual_output=item["actual_output"],
            expected_output=question["reference"],
            retrieval_context=item["retrieved_contexts"],
        )
        scores: dict[str, dict[str, Any]] = {}
        for name, metric in metrics:
            for attempt in range(4):
                try:
                    metric.measure(test_case)
                    break
                except (RateLimitError, RetryError) as error:
                    cause = (
                        error.last_attempt.exception()
                        if isinstance(error, RetryError)
                        else error
                    )
                    if not isinstance(cause, RateLimitError) or attempt == 3:
                        raise
                    delay = min(10 * (2**attempt), 60)
                    print(
                        f"Groq rate limit while scoring {question['id']} "
                        f"({name}); retrying in {delay}s"
                    )
                    time.sleep(delay)
            score = float(metric.score)
            totals[name] += score
            scores[name] = {
                "score": score,
                "reason": metric.reason,
            }
            if metric_delay > 0:
                time.sleep(metric_delay)

        detailed_results.append(
            {
                "id": question["id"],
                "category": question["metadata"].get("category"),
                "expected_behavior": question["metadata"].get("expected_behavior"),
                "user_input": question["user_input"],
                "reference": question["reference"],
                "actual_output": item["actual_output"],
                "reference_contexts": question["reference_contexts"],
                "retrieved_contexts": item["retrieved_contexts"],
                "role_applied": item["role_applied"],
                "metrics": scores,
            }
        )

    count = len(detailed_results)
    averages = {name: total / count for name, total in totals.items()}
    return detailed_results, averages


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=DEFAULT_DATASET,
        help=f"DeepEval JSONL question set (default: {DEFAULT_DATASET})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Path for the detailed JSON evaluation report",
    )
    parser.add_argument(
        "--api-url",
        default=os.getenv("EVAL_API_URL", "http://127.0.0.1:8000"),
        help="Base URL for the running application API",
    )
    parser.add_argument(
        "--judge-model",
        default=os.getenv("DEEPEVAL_MODEL", DEFAULT_GROQ_JUDGE_MODEL),
        help=f"Groq judge model (default: {DEFAULT_GROQ_JUDGE_MODEL})",
    )
    parser.add_argument(
        "--request-delay",
        type=float,
        default=4.0,
        help="Seconds between chat requests to stay within the app rate limit",
    )
    parser.add_argument(
        "--metric-delay",
        type=float,
        default=10.0,
        help="Seconds between Groq metric requests to reduce judge rate limits",
    )
    args = parser.parse_args()
    if args.request_delay < 0:
        parser.error("--request-delay must be non-negative")
    if args.metric_delay < 0:
        parser.error("--metric-delay must be non-negative")
    return args


async def async_main(args: argparse.Namespace) -> None:
    load_dotenv()
    questions = load_questions(args.dataset)
    groq_api_key = os.getenv("GROP_API_KEY")
    if not groq_api_key:
        raise RuntimeError(
            "GROP_API_KEY is not set. Add the Groq API key to the environment "
            "or the project .env file."
        )
    bearer_token = resolve_bearer_token()
    chat_results = await collect_chat_results(
        questions,
        api_url=args.api_url,
        bearer_token=bearer_token,
        request_delay=args.request_delay,
    )
    detailed_results, averages = await asyncio.to_thread(
        evaluate_results,
        chat_results,
        judge_model_name=args.judge_model,
        groq_api_key=groq_api_key,
        metric_delay=args.metric_delay,
    )

    report = {
        "evaluated_at": datetime.now(UTC).isoformat(),
        "api_url": args.api_url,
        "judge_provider": "Groq",
        "judge_model": args.judge_model,
        "dataset": str(args.dataset),
        "question_count": len(detailed_results),
        "average_scores": averages,
        "results": detailed_results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"\nAverage DeepEval scores ({len(detailed_results)} questions):")
    for name, score in averages.items():
        print(f"  {name}: {score:.3f}")
    print(f"\nDetailed report saved to {args.output}")


def main() -> None:
    load_dotenv()
    args = parse_args()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
