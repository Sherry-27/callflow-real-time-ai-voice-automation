"""Regression checks for the mirrored browser voice-demo surfaces."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SURFACES = (ROOT / "dist", ROOT / "app" / "static")


class BrowserVoiceDemoTests(unittest.TestCase):
    def test_voice_controls_exist_on_both_surfaces(self) -> None:
        for surface in SURFACES:
            with self.subTest(surface=surface):
                html = (surface / "index.html").read_text(encoding="utf-8")
                self.assertIn('id="voiceButton"', html)
                self.assertIn('id="voiceStatus"', html)
                self.assertIn('id="voiceOutputToggle"', html)
                self.assertIn("CallFlow receives the transcript only and does not store audio", html)

    def test_voice_runtime_and_states_exist_on_both_surfaces(self) -> None:
        for surface in SURFACES:
            with self.subTest(surface=surface):
                javascript = (surface / "app.js").read_text(encoding="utf-8")
                stylesheet = (surface / "styles.css").read_text(encoding="utf-8")

                self.assertIn("window.SpeechRecognition || window.webkitSpeechRecognition", javascript)
                self.assertIn("new SpeechSynthesisUtterance", javascript)
                self.assertIn("elements.form.requestSubmit()", javascript)
                self.assertIn("stopSpeaking();", javascript)
                self.assertIn('.voice-status[data-state="listening"]', stylesheet)
                self.assertIn('.voice-status[data-state="speaking"]', stylesheet)


if __name__ == "__main__":
    unittest.main()
