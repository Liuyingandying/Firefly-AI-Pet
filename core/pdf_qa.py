"""PDF QA — answer questions about a processed PDF using the AI Router.

Architecture:
  1. PdfProcessor extracts text from PDF
  2. PdfQa feeds relevant text chunks to AI Router for answering
  3. Supports term explanation, formula explanation, paragraph summary,
     concept card generation, and recommended follow-up questions

Dependencies:
  - core.pdf_processor (this project)
  - core.ai_router (this project, for LLM calls)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

from core.pdf_processor import PdfProcessor, PdfResult

log = logging.getLogger("firefly.pdf_qa")


@dataclass
class ExplainEntry:
    """A single explain box entry (term, formula, chart, etc.)."""

    kind: str  # "term", "formula", "chart", "paragraph", "section"
    label: str  # display label
    explanation: str  # human-readable explanation
    source_page: int = -1
    source_text: str = ""


@dataclass
class PdfQaResult:
    """Result of a PDF QA operation."""

    answer: str = ""
    explains: list[ExplainEntry] = field(default_factory=list)
    recommended_questions: list[str] = field(default_factory=list)
    summary: str = ""
    terms: list[str] = field(default_factory=list)


class PdfQa:
    """PDF question answering with explain box integration.

    Uses the existing AI Router (core.ai_router.chat) for LLM calls.
    Delegates text extraction to PdfProcessor.
    """

    # Max characters of PDF text to send per query (to avoid token limits)
    MAX_CONTEXT_CHARS = 6000

    def __init__(self, chat_handler=None) -> None:
        self._processor = PdfProcessor()
        self._chat = chat_handler

    def _get_chat(self):
        """Lazy-import chat handler to avoid circular imports."""
        if self._chat is None:
            from core.ai_router import chat
            self._chat = chat
        return self._chat

    def process(self, file_path: str | Path) -> PdfResult:
        """Process a PDF file (text extraction + OCR fallback)."""
        return self._processor.process(file_path)

    def answer(
        self,
        file_path: str | Path,
        question: str,
        *,
        top_k_pages: int = 3,
    ) -> PdfQaResult:
        """Answer a question about a PDF.

        Args:
            file_path: Path to the PDF.
            question: User's question.
            top_k_pages: How many pages of context to include.

        Returns:
            PdfQaResult with answer, explanations, and follow-up questions.
        """
        result = self.process(file_path)
        context = self._build_context(result, top_k_pages=top_k_pages)

        prompt = self._build_question_prompt(question, context)

        chat = self._get_chat()
        messages = [{"role": "user", "content": prompt}]

        try:
            response = chat(messages, temperature=0.3)
            answer = self._extract_content(response, default="")
        except Exception as exc:
            log.warning("[PdfQa] AI chat failed: %s", exc)
            answer = f"无法回答该问题：{exc}"

        # Extract terms for concept cards
        terms = self._extract_terms(result)

        return PdfQaResult(
            answer=answer,
            terms=terms,
        )

    def explain_term(
        self,
        file_path: str | Path,
        term: str,
    ) -> ExplainEntry:
        """Explain a specific term found in a PDF.

        Args:
            file_path: Path to the PDF.
            term: The term to explain.

        Returns:
            ExplainEntry with explanation.
        """
        result = self.process(file_path)
        context = self._find_term_context(result, term)

        prompt = (
            f"你是 Firefly，一个友好的学术助手。请解释以下 PDF 论文中的术语。"
            f"\n\n术语：{term}"
            f"\n\n上下文：{context[:2000]}"
            f"\n\n请用简洁的中文解释这个术语，包括："
            f"1. 术语的定义"
            f"2. 在论文中的作用"
            f"3. 相关背景知识（如果适用）"
        )

        chat = self._get_chat()
        messages = [{"role": "user", "content": prompt}]

        try:
            response = chat(messages, temperature=0.2)
            explanation = self._extract_content(response, default="")
        except Exception as exc:
            log.warning("[PdfQa] AI chat failed for term '%s': %s", term, exc)
            explanation = f"无法解释术语：{exc}"

        return ExplainEntry(
            kind="term",
            label=term,
            explanation=explanation,
        )

    def summarize(self, file_path: str | Path) -> PdfQaResult:
        """Generate a summary of a PDF.

        Args:
            file_path: Path to the PDF.

        Returns:
            PdfQaResult with summary and recommended questions.
        """
        result = self.process(file_path)
        full_text = result.full_text[:15000]  # Limit for token budget

        prompt = (
            "你是 Firefly，一个友好的学术助手。请阅读以下 PDF 论文内容，"
            "并生成一份简洁的中文摘要。"
            "\n\n论文内容：\n"
            f"{full_text[:5000]}\n\n"
            "请提供：\n"
            "1. 论文的核心问题和方法（1-2句）\n"
            "2. 主要贡献（3-5个要点）\n"
            "3. 关键结论（2-3句）\n"
            "4. 3个推荐的深入问题"
        )

        chat = self._get_chat()
        messages = [{"role": "user", "content": prompt}]

        try:
            response = chat(messages, temperature=0.3)
            summary_text = self._extract_content(response, default="")
        except Exception as exc:
            log.warning("[PdfQa] AI chat failed for summary: %s", exc)
            summary_text = f"无法生成摘要：{exc}"

        # Parse summary and recommended questions
        summary = summary_text
        recommended_questions = self._parse_recommended_questions(summary_text)

        return PdfQaResult(
            summary=summary,
            recommended_questions=recommended_questions,
        )

    def extract_concepts(self, file_path: str | Path) -> list[str]:
        """Extract key concepts from a PDF for PageLens concept chips.

        Args:
            file_path: Path to the PDF.

        Returns:
            List of concept names (top 8).
        """
        result = self.process(file_path)
        context = result.full_text[:8000]

        prompt = (
            "你是 Firefly，一个学术概念提取助手。从以下 PDF 论文内容中，"
            "提取最重要的 8 个专业概念/术语。"
            "\n\n只返回概念名称，用中文逗号分隔，不要其他文字。"
            "\n\n内容：\n"
            f"{context[:4000]}\n"
        )

        chat = self._get_chat()
        messages = [{"role": "user", "content": prompt}]

        try:
            response = chat(messages, temperature=0.2)
            concepts_text = self._extract_content(response, default="")
            # Parse comma-separated concepts
            concepts = [c.strip() for c in concepts_text.replace("，", ",").split(",") if c.strip()]
            return concepts[:8]
        except Exception as exc:
            log.warning("[PdfQa] AI chat failed for concepts: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_context(self, result: PdfResult, *, top_k_pages: int = 3) -> str:
        """Build context string from top-k most relevant pages."""
        # Simple heuristic: use pages with most text (likely most informative)
        sorted_pages = sorted(result.pages, key=lambda p: len(p.text), reverse=True)
        selected = sorted_pages[:top_k_pages]
        return "\n\n".join(
            f"[Page {p.page_index + 1}]\n{p.text}" for p in selected
        )

    def _find_term_context(self, result: PdfResult, term: str) -> str:
        """Find context around a term in the PDF."""
        contexts = []
        for page in result.pages:
            idx = page.text.find(term)
            if idx >= 0:
                start = max(0, idx - 200)
                end = min(len(page.text), idx + len(term) + 400)
                contexts.append(page.text[start:end])
        return "\n\n".join(contexts[:3]) if contexts else "未找到该术语的上下文。"

    def _build_question_prompt(self, question: str, context: str) -> str:
        return (
            "你是 Firefly，一个友好的学术助手。请根据以下 PDF 论文内容回答用户的问题。"
            "\n\n如果答案不在上下文中，请诚实地说不知道，不要编造。"
            "\n\n用户问题：{question}"
            "\n\n论文内容：\n{context}"
        ).format(question=question, context=context)

    def _extract_content(self, response: dict, *, default: str = "") -> str:
        """Extract content string from AI Router response."""
        if not isinstance(response, dict):
            return default
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            return default
        message = choices[0].get("message", {})
        if not isinstance(message, dict):
            return default
        content = message.get("content", default)
        return content if isinstance(content, str) else default

    def _extract_terms(self, result: PdfResult) -> list[str]:
        """Extract candidate terms from PDF for concept cards."""
        # Simple heuristic: look for capitalized words, technical terms
        terms = set()
        for page in result.pages:
            for word in page.text.split():
                # Filter: 2-30 chars, not pure numbers, not common words
                w = word.strip(".,;:!?()[]{}\"'")
                if 2 <= len(w) <= 30 and not w.isdigit():
                    terms.add(w)
        return sorted(terms, key=len, reverse=True)[:20]

    def _parse_recommended_questions(self, text: str) -> list[str]:
        """Parse recommended questions from AI response text."""
        questions = []
        for line in text.split("\n"):
            line = line.strip()
            # Match patterns like "4. 问题..." or "- 问题..." or "4、问题..."
            if (line.startswith(("4.", "4、", "- ", "* "))
                    or (len(questions) >= 2 and any(c.isdigit() for c in line[:2]))):
                # Clean up numbering
                cleaned = line.lstrip("0123456789.、- ")
                if cleaned and len(cleaned) > 5:
                    questions.append(cleaned)
        return questions[:5]


__all__ = [
    "PdfQa",
    "PdfQaResult",
    "ExplainEntry",
]
