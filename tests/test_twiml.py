import unittest

from app.twiml import (
    agent_whisper_twiml,
    call_result_twiml,
    conversation_relay_twiml,
    warm_handoff_twiml,
)


class TwimlTests(unittest.TestCase):
    def test_conversation_relay_enables_natural_turn_taking(self):
        xml = conversation_relay_twiml(
            "wss://voice.example/ws/conversation",
            "https://voice.example/voice/connect-action",
            "+923001234567",
        )
        self.assertIn("<ConversationRelay", xml)
        self.assertIn('speechModel="flux"', xml)
        self.assertIn('interruptible="any"', xml)
        self.assertIn('reportInputDuringAgentSpeech="any"', xml)
        self.assertIn('ignoreBackchannel="true"', xml)
        self.assertIn('transcriptionLanguage="multi"', xml)

    def test_warm_handoff_uses_answer_on_bridge_and_whisper(self):
        xml = warm_handoff_twiml(
            "+923009999999",
            "https://voice.example/voice/agent-whisper?id=abc",
            "https://voice.example/voice/dial-action?id=abc",
            "callback saved",
        )
        self.assertIn('answerOnBridge="true"', xml)
        self.assertIn('action="https://voice.example/voice/dial-action?id=abc"', xml)
        self.assertIn("<Number url=", xml)
        self.assertIn("+923009999999", xml)
        self.assertNotIn("callback saved", xml)

    def test_no_agent_and_dial_result_messages_are_explicit(self):
        missing_agent = warm_handoff_twiml("", "", "", "Please call again")
        self.assertIn("Please call again", missing_agent)
        self.assertIn("<Hangup/>", missing_agent)
        self.assertIn("Callback saved", call_result_twiml("Callback saved"))

    def test_xml_values_are_escaped(self):
        xml = agent_whisper_twiml("Alice & Bob <priority>", "refund & complaint")
        self.assertIn("Alice &amp; Bob &lt;priority&gt;", xml)
        self.assertNotIn("<priority>", xml)


if __name__ == "__main__":
    unittest.main()
