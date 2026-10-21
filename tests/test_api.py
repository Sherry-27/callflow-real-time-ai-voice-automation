import json
import os
import tempfile
import unittest
from pathlib import Path

_TEMP_DIR = tempfile.TemporaryDirectory()
os.environ["DATABASE_PATH"] = str(Path(_TEMP_DIR.name) / "api-test.db")
os.environ["VALIDATE_TWILIO_SIGNATURE"] = "false"
os.environ["GROQ_API_KEY"] = ""
os.environ["N8N_POST_CALL_WEBHOOK"] = ""
os.environ["API_KEY"] = "test-api-key"
os.environ["PUBLIC_BASE_URL"] = "https://voice.example"
os.environ["PUBLIC_WS_URL"] = "wss://voice.example/ws/conversation"
os.environ["HUMAN_AGENT_PHONE"] = "+15550109999"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app, store  # noqa: E402


class ApiIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_health_and_protected_simulator(self):
        dashboard = self.client.get("/")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("CallFlow AI", dashboard.text)
        self.assertEqual(self.client.get("/styles.css").status_code, 200)
        self.assertEqual(self.client.get("/app.js").status_code, 200)

        health = self.client.get("/api/v1/health")
        self.assertEqual(health.status_code, 200)
        self.assertFalse(health.json()["groq_enabled"])

        denied = self.client.post("/api/v1/simulate", json={"text": "hello"})
        self.assertEqual(denied.status_code, 401)

        response = self.client.post(
            "/api/v1/simulate",
            headers={"X-API-Key": "test-api-key"},
            json={
                "session_id": "api-customer-demo",
                "caller": "+15550101001",
                "text": "Please look up my account",
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["action"], "lookup_customer")
        self.assertNotIn("+15550101001", json.dumps(payload))

    def test_incoming_call_and_warm_handoff_callbacks(self):
        incoming = self.client.post("/voice/incoming", data={"From": "+15550101001"})
        self.assertEqual(incoming.status_code, 200)
        self.assertIn('speechModel="flux"', incoming.text)
        self.assertIn('interruptible="any"', incoming.text)

        session_id = "api-warm-handoff"
        store.start_call(session_id, "CA-api-warm", "+15550101001")
        store.request_handoff(session_id, "explicit_human_request")
        store.update_call_summary(session_id, "Caller wants help with an existing booking.")
        handoff_data = json.dumps(
            {
                "reasonCode": "live-agent-handoff",
                "sessionId": session_id,
                "reason": "explicit_human_request",
            }
        )
        connect = self.client.post(
            "/voice/connect-action",
            data={"HandoffData": handoff_data, "SessionStatus": "completed"},
        )
        self.assertEqual(connect.status_code, 200)
        self.assertIn("/voice/dial-action?", connect.text)
        self.assertNotIn("callback request has been saved", connect.text)

        dial = self.client.post(
            f"/voice/dial-action?session_id={session_id}&reason=explicit_human_request",
            data={"DialCallStatus": "completed"},
        )
        self.assertEqual(dial.status_code, 200)
        self.assertEqual(store.get_call(session_id)["outcome"], "handoff_completed")

    def test_unanswered_dial_creates_callback_before_confirmation(self):
        session_id = "api-no-answer"
        store.start_call(session_id, "CA-api-no-answer", "+15550101001")
        store.request_handoff(session_id, "explicit_human_request")
        response = self.client.post(
            f"/voice/dial-action?session_id={session_id}&reason=explicit_human_request",
            data={"DialCallStatus": "no-answer"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("callback request has been saved", response.text)
        self.assertEqual(store.get_call(session_id)["outcome"], "callback_requested")
        self.assertEqual(len(store.list_tickets(session_id)), 1)

    def test_conversationrelay_websocket_stream_and_handoff(self):
        with self.client.websocket_connect("/ws/conversation") as websocket:
            websocket.send_json(
                {
                    "type": "setup",
                    "sessionId": "api-websocket",
                    "callSid": "CA-api-websocket",
                    "from": "+15550101001",
                }
            )
            websocket.send_json(
                {"type": "prompt", "voicePrompt": "Hello", "lang": "en", "last": True}
            )
            while True:
                message = websocket.receive_json()
                self.assertEqual(message["type"], "text")
                if message["last"]:
                    break

            websocket.send_json(
                {
                    "type": "prompt",
                    "voicePrompt": "Please connect me to a human agent",
                    "lang": "en",
                    "last": True,
                }
            )
            outbound = []
            while True:
                message = websocket.receive_json()
                outbound.append(message)
                if message["type"] == "end":
                    break

        self.assertTrue(any(item["type"] == "text" for item in outbound))
        end = next(item for item in outbound if item["type"] == "end")
        handoff = json.loads(end["handoffData"])
        self.assertEqual(handoff["reasonCode"], "live-agent-handoff")
        call = store.get_call("api-websocket")
        self.assertEqual(call["status"], "handoff_requested")
        self.assertIn("human agent", call["summary"])


if __name__ == "__main__":
    unittest.main()
