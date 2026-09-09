import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


HERMES_SOURCE = Path(os.environ.get("DEK_QA_HERMES_SOURCE", "/opt/dek-qa/hermes-agent"))
if HERMES_SOURCE.is_dir():
    sys.path.insert(0, str(HERMES_SOURCE))


@unittest.skipUnless(HERMES_SOURCE.is_dir(), "independent Hermes source is not available")
class HermesDingTalkBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self):
        from gateway.config import PlatformConfig
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter

        return DingTalkAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "allowed_users": ["allowed-sender", "allowed-staff"],
                    "allowed_chats": ["allowed-chat"],
                    "require_mention": True,
                },
            )
        )

    @staticmethod
    def message(*, sender_id="denied-sender", staff_id="denied-staff", chat_id="allowed-chat"):
        return SimpleNamespace(
            message_id="synthetic-message",
            conversation_id=chat_id,
            conversation_type="2",
            sender_id=sender_id,
            sender_staff_id=staff_id,
            sender_nick="synthetic",
            text={"content": "synthetic"},
            is_in_at_list=True,
        )

    def test_repository_template_routes_dingtalk_gates_into_adapter_extra(self):
        import yaml
        from gateway.config import PlatformConfig
        from plugins.platforms.dingtalk.adapter import DingTalkAdapter

        template_path = Path(__file__).parents[1] / "config" / "config.yaml"
        raw = yaml.safe_load(template_path.read_text(encoding="utf-8"))
        dingtalk = raw["platforms"]["dingtalk"]
        parsed = PlatformConfig.from_dict(dingtalk)
        adapter = DingTalkAdapter(parsed)

        self.assertIn("allowed_users", parsed.extra)
        self.assertIn("allowed_chats", parsed.extra)
        self.assertTrue(parsed.extra["require_mention"])
        self.assertEqual(adapter._allowed_users, set())

    def test_actual_gateway_authorization_remains_default_deny(self):
        from gateway.authz_mixin import GatewayAuthorizationMixin
        from gateway.config import Platform
        from gateway.session import SessionSource

        class Runner(GatewayAuthorizationMixin):
            pass

        runner = Runner()
        runner.adapters = {}
        runner.pairing_store = None
        runner.pairing_stores = {}
        runner.active_profile = "dek-qa"
        source = SessionSource(
            platform=Platform.DINGTALK,
            chat_id="synthetic-chat",
            chat_type="dm",
            user_id="synthetic-allowed",
        )
        names = (
            "DINGTALK_ALLOWED_USERS",
            "DINGTALK_ALLOW_ALL_USERS",
            "GATEWAY_ALLOWED_USERS",
            "GATEWAY_ALLOW_ALL_USERS",
        )
        previous = {name: os.environ.get(name) for name in names}
        try:
            os.environ["DINGTALK_ALLOWED_USERS"] = "synthetic-allowed"
            os.environ["DINGTALK_ALLOW_ALL_USERS"] = "false"
            os.environ.pop("GATEWAY_ALLOWED_USERS", None)
            os.environ.pop("GATEWAY_ALLOW_ALL_USERS", None)
            self.assertTrue(runner._is_user_authorized(source))
            source.user_id = "synthetic-denied"
            self.assertFalse(runner._is_user_authorized(source))
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    async def test_actual_adapter_rejects_user_before_gateway_dispatch(self):
        adapter = self.adapter()
        dispatched = False

        async def forbidden_dispatch(_event):
            nonlocal dispatched
            dispatched = True

        adapter.handle_message = forbidden_dispatch
        await adapter._on_message(self.message())
        self.assertFalse(dispatched)

    async def test_actual_adapter_rejects_unlisted_group_before_gateway_dispatch(self):
        adapter = self.adapter()
        dispatched = False

        async def forbidden_dispatch(_event):
            nonlocal dispatched
            dispatched = True

        adapter.handle_message = forbidden_dispatch
        await adapter._on_message(
            self.message(sender_id="allowed-sender", staff_id="allowed-staff", chat_id="denied-chat")
        )
        self.assertFalse(dispatched)


if __name__ == "__main__":
    unittest.main()
