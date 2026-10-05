from __future__ import annotations

import unittest

from chatgpt_orchestrator.edge_adapter import EdgeChatGPTAdapter


class EdgeAdapterTests(unittest.TestCase):
    def test_extract_latest_response(self):
        texts = [
            "Bạn đã nói:",
            "First prompt",
            "ChatGPT đã nói:",
            "First answer",
            "Bạn đã nói:",
            "Second prompt",
            "ChatGPT đã nói:",
            "Second answer line 1",
            "Second answer line 2",
            "ChatGPT có thể mắc lỗi. Hãy kiểm tra thông tin quan trọng.",
            "Hỏi ChatGPT",
        ]
        self.assertEqual(
            EdgeChatGPTAdapter._extract_latest_response(texts, "Second prompt"),
            "Second answer line 1\nSecond answer line 2",
        )

    def test_feedback_survey_is_not_part_of_answer(self):
        texts = [
            "Bạn đã nói:",
            "Prompt",
            "ChatGPT đã nói:",
            "Answer",
            "Bạn có thích tính cách này không?",
            "Hỏi ChatGPT",
        ]
        self.assertEqual(
            EdgeChatGPTAdapter._extract_latest_response(texts, "Prompt"),
            "Answer",
        )

    def test_conversation_helpfulness_survey_is_not_part_of_answer(self):
        texts = [
            "Bạn đã nói:",
            "Prompt",
            "ChatGPT đã nói:",
            "Answer",
            "Cho đến lúc này, cuộc hội thoại này có hữu ích không?",
            "Hỏi ChatGPT",
        ]
        self.assertEqual(
            EdgeChatGPTAdapter._extract_latest_response(texts, "Prompt"),
            "Answer",
        )

    def test_progress_marker_is_ignored(self):
        texts = [
            "Bạn đã nói:",
            "Prompt",
            "ChatGPT đã nói:",
            "ChatGPT đang phản hồi",
        ]
        self.assertEqual(
            EdgeChatGPTAdapter._extract_latest_response(texts, "Prompt"),
            "",
        )

    def test_extract_returns_empty_until_prompt_is_present(self):
        self.assertEqual(
            EdgeChatGPTAdapter._extract_latest_response(
                ["Bạn đã nói:", "other prompt"], "prompt"
            ),
            "",
        )

    def test_extract_falls_back_to_turn_markers_when_long_prompt_is_split(self):
        texts = [
            "You said:",
            "long prompt part 1",
            "long prompt part 2",
            "ChatGPT said:",
            "ORCH_UIA_FINAL_OK",
            "Ask ChatGPT",
        ]
        self.assertEqual(
            EdgeChatGPTAdapter._extract_latest_response(
                texts, "long prompt part 1 long prompt part 2"
            ),
            "ORCH_UIA_FINAL_OK",
        )

    def test_extract_fallback_stays_empty_without_assistant_turn(self):
        self.assertEqual(
            EdgeChatGPTAdapter._extract_latest_response(
                ["You said:", "split prompt fragment"], "full prompt"
            ),
            "",
        )

    def test_parse_edge_session(self):
        self.assertEqual(EdgeChatGPTAdapter._parse_session("edge:12345"), 12345)
        with self.assertRaises(ValueError):
            EdgeChatGPTAdapter._parse_session("sim:12345")


if __name__ == "__main__":
    unittest.main()
