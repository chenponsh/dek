import asyncio
import io
import importlib.util
import json
import logging
import os
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from qa.dek_qa.access import INSUFFICIENT_EVIDENCE, Message, ReadOnlyQa
from qa.dek_qa.index import KnowledgeBase, build_index
from qa.dek_qa.mcp_server import TOOLS, _reply
from qa.dek_qa.stream_id_collector import (
    CandidateCollector,
    CollectorLimits,
    ack_only_process,
    configure_safe_logging,
    install_stop_signal_handlers,
    pairing_message,
    redact_log_text,
    remove_stop_signal_handlers,
    run_managed_session,
)


class DekQaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "wiki" / "01_注册").mkdir(parents=True)
        (self.root / "source" / "CPC").mkdir(parents=True)
        (self.root / "source" / "CPC" / "官方通知.md").write_text(
            '---\nsource_url: "https://official.example/notice"\n---\n\n来源全文', encoding="utf-8"
        )
        (self.root / "source" / "CPC" / "应排除_排除").mkdir()
        (self.root / "source" / "CPC" / "应排除_排除" / "坏.md").write_text(
            '---\nsource_url: "https://excluded.example"\n---\n', encoding="utf-8"
        )
        (self.root / "wiki" / "01_注册" / "0101-0001.md").write_text(
            '---\nquestion: 药品注册如何申报？\nsource: 官方通知\n---\n\n通过注册系统提交。\n\n![[source/CPC/官方通知]]', encoding="utf-8"
        )
        self.index = self.root / "runtime" / "index.json"
        build_index(self.root, self.index)
        self.kb = KnowledgeBase(self.index)

    def tearDown(self):
        self.temp.cleanup()

    def test_index_contains_only_wiki_and_official_source_url(self):
        data = json.loads(self.index.read_text(encoding="utf-8"))
        self.assertEqual(len(data["documents"]), 1)
        doc = data["documents"][0]
        self.assertTrue(doc["path"].startswith("wiki/"))
        self.assertEqual(doc["official_urls"], ["https://official.example/notice"])
        self.assertNotIn("来源全文", json.dumps(data, ensure_ascii=False))
        self.assertNotIn("excluded.example", json.dumps(data))

    def test_wiki_wikilink_with_source_stem_is_not_a_source_mapping(self):
        note = self.root / "wiki" / "01_注册" / "0101-0001.md"
        note.write_text(
            "---\nquestion: 药品注册如何申报？\n---\n\n参见 [[官方通知]]。",
            encoding="utf-8",
        )

        payload = build_index(self.root, self.index)

        self.assertEqual(payload["documents"][0]["official_urls"], [])
        self.assertEqual(payload["documents"][0]["source_status"], "none")

    def test_source_url_requires_valid_host_and_no_credentials(self):
        source = self.root / "source" / "CPC" / "官方通知.md"
        source.write_text(
            '---\nsource_url: "https://user:secret@official.example/notice"\n---\n',
            encoding="utf-8",
        )

        payload = build_index(self.root, self.index)

        self.assertEqual(payload["documents"][0]["official_urls"], [])
        self.assertEqual(payload["documents"][0]["source_status"], "unknown")

    def test_path_qualified_source_does_not_fall_back_to_same_stem(self):
        note = self.root / "wiki" / "01_注册" / "0101-0001.md"
        note.write_text(
            "---\nquestion: 药品注册如何申报？\nsource: [[source/错误目录/官方通知]]\n---\n\n见来源。",
            encoding="utf-8",
        )
        build_index(self.root, self.index)
        document = json.loads(self.index.read_text(encoding="utf-8"))["documents"][0]
        self.assertEqual(document["official_urls"], [])
        self.assertEqual(document["source_status"], "unknown")

    def test_duplicate_source_stems_fail_closed(self):
        duplicate_dir = self.root / "source" / "另一区域"
        duplicate_dir.mkdir()
        (duplicate_dir / "官方通知.md").write_text(
            "---\ntitle: 同名但未审核来源\n---\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "ambiguous source stem"):
            build_index(self.root, self.index)

    def test_unmapped_wiki_urls_are_not_promoted_to_official(self):
        note = self.root / "wiki" / "01_注册" / "0101-0001.md"
        note.write_text(
            "---\nquestion: 药品注册如何申报？\nsource: 官方通知\n---\n\n"
            "正文链接 https://unverified.example/page\n\n![[source/CPC/官方通知]]",
            encoding="utf-8",
        )

        payload = build_index(self.root, self.index)

        self.assertEqual(payload["documents"][0]["official_urls"], ["https://official.example/notice"])

    def test_index_excludes_any_wiki_path_marked_excluded(self):
        excluded = self.root / "wiki" / "01_注册" / "待复核_排除" / "0101-9999.md"
        excluded.parent.mkdir()
        excluded.write_text(
            "---\nquestion: 不应收录\nsource: 官方通知\n---\n\n排除内容",
            encoding="utf-8",
        )

        payload = build_index(self.root, self.index)

        self.assertEqual([doc["path"] for doc in payload["documents"]], ["wiki/01_注册/0101-0001.md"])
        self.assertNotIn("排除内容", json.dumps(payload, ensure_ascii=False))

    def test_index_records_deterministic_build_metadata(self):
        first = json.loads(self.index.read_text(encoding="utf-8"))
        second_index = self.root / "index-second.json"
        second = build_index(self.root, second_index)

        self.assertEqual(first["metadata"], second["metadata"])
        self.assertEqual(first["metadata"]["builder_version"], "2")
        self.assertRegex(first["metadata"]["input_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(first["metadata"]["builder_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(first["metadata"]["document_count"], len(first["documents"]))

    def test_index_created_private(self):
        self.assertEqual(stat.S_IMODE(self.index.stat().st_mode), 0o600)

    def test_search_and_get_use_opaque_id(self):
        hits = self.kb.dek_kb_search("药品注册申报")
        self.assertEqual(len(hits), 1)
        self.assertNotIn("content", hits[0])
        self.assertIn("通过注册系统", self.kb.dek_kb_get(hits[0]["id"])["content"])
        self.assertIsNone(self.kb.dek_kb_get("../../source/secret"))

    def test_search_requires_meaningful_token_without_crossing_boundaries(self):
        note = self.root / "wiki" / "01_注册" / "0101-0002.md"
        note.write_text("---\nquestion: 甲。乙\n---\n\n甲。乙", encoding="utf-8")
        build_index(self.root, self.index)
        kb = KnowledgeBase(self.index)

        self.assertEqual(kb.dek_kb_search("甲乙"), [])
        self.assertEqual(kb.dek_kb_search("药"), [])

    def test_insufficient_evidence_does_not_guess(self):
        qa = ReadOnlyQa(self.kb, {"u1"}, {"c1"})
        result = qa.handle(Message("u1", "c1", "direct", "完全不存在的主题XYZ"))
        self.assertEqual(result["answer"], INSUFFICIENT_EVIDENCE)
        self.assertEqual(result["citations"], [])

    def test_default_deny_user_and_chat(self):
        qa = ReadOnlyQa(self.kb, {"u1"}, {"c1"})
        self.assertEqual(qa.handle(Message("u2", "c1", "direct", "注册"))["status"], "denied")
        self.assertEqual(qa.handle(Message("u1", "c2", "group", "注册"))["status"], "denied")

    def test_empty_allowlist_rejected(self):
        with self.assertRaises(ValueError):
            ReadOnlyQa(self.kb, set(), {"c1"})

    def test_sessions_isolated_by_user_chat_and_type(self):
        qa = ReadOnlyQa(self.kb, {"u1", "u2"}, {"c1", "c2"})
        first = Message("u1", "c1", "group", "注册")
        others = [Message("u2", "c1", "group", "申报"), Message("u1", "c2", "group", "系统"), Message("u1", "c1", "direct", "药品")]
        qa.handle(first)
        for message in others:
            qa.handle(message)
        self.assertEqual(qa.session_messages(first), ("注册",))
        self.assertTrue(all(qa.session_messages(message) == (message.text,) for message in others))

    def test_only_two_mcp_tools_are_exposed(self):
        self.assertEqual({tool["name"] for tool in TOOLS}, {"dek_kb_search", "dek_kb_get"})
        response = _reply({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, self.kb)
        self.assertEqual(len(response["result"]["tools"]), 2)

    def test_mcp_client_sdk_is_installed_for_runtime_discovery(self):
        self.assertIsNotNone(
            importlib.util.find_spec("mcp"),
            "MCP SDK is required; without it Hermes silently skips MCP discovery",
        )

    def test_mcp_tools_list_rejects_unexpected_parameters(self):
        response = _reply(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/list",
                "params": {"unexpected": True},
            },
            self.kb,
        )
        self.assertEqual(response["error"]["code"], -32602)

    def test_mcp_non_object_request_returns_controlled_error(self):
        response = _reply([], self.kb)
        self.assertEqual(response["error"]["code"], -32600)
        self.assertEqual(response["id"], None)

    def test_mcp_search_rejects_invalid_parameters(self):
        invalid_arguments = [
            {},
            {"query": "注册", "extra": True},
            {"query": 1},
            {"query": "注册", "limit": 0},
            {"query": "注册", "limit": 11},
        ]
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                response = _reply(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "dek_kb_search", "arguments": arguments},
                    },
                    self.kb,
                )
                self.assertTrue(response["result"]["isError"])
                self.assertNotIn("error", response)

    def test_mcp_call_envelope_rejects_non_object_arguments(self):
        response = _reply(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "dek_kb_search", "arguments": []},
            },
            self.kb,
        )
        self.assertEqual(response["error"]["code"], -32602)

    def test_mcp_get_rejects_invalid_parameters(self):
        invalid_arguments = [{}, {"document_id": "../../secret"}, {"document_id": "a" * 24, "extra": True}]
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                response = _reply(
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {"name": "dek_kb_get", "arguments": arguments},
                    },
                    self.kb,
                )
                self.assertTrue(response["result"]["isError"])
                self.assertNotIn("error", response)

    def test_mcp_stdio_survives_invalid_input(self):
        requests = [
            [],
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": []},
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "dek_kb_search",
                    "arguments": {"query": "注册", "limit": 99},
                },
            },
            {"jsonrpc": "2.0", "id": 7, "method": "tools/list"},
        ]
        input_text = "{malformed-json\n" + "".join(
            json.dumps(request) + "\n" for request in requests
        )
        completed = subprocess.run(
            [sys.executable, "-m", "qa.dek_qa.mcp_server", "--index", str(self.index)],
            input=input_text,
            text=True,
            capture_output=True,
            cwd=Path(__file__).parents[2],
            check=False,
        )
        responses = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(
            [response.get("error", {}).get("code") for response in responses[:3]],
            [-32700, -32600, -32602],
        )
        self.assertTrue(responses[3]["result"]["isError"])
        self.assertEqual(len(responses[4]["result"]["tools"]), 2)

    def test_unknown_mcp_tool_is_rejected(self):
        response = _reply({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "shell", "arguments": {}}}, self.kb)
        self.assertEqual(response["error"]["code"], -32601)

    def test_systemd_unit_limits_writes_to_profile_state(self):
        unit = (Path(__file__).parents[2] / "deploy" / "systemd" / "dek-qa.service").read_text(encoding="utf-8")
        self.assertIn("ReadWritePaths=/var/lib/dek-qa/hermes", unit)
        self.assertIn("ReadOnlyPaths=/var/lib/dek-qa/index /var/lib/dek-qa/secrets", unit)
        self.assertNotIn("ReadWritePaths=/var/lib/dek-qa\n", unit)

    def test_profile_config_is_default_deny_and_only_mcp(self):
        config = (Path(__file__).parents[1] / "config" / "config.yaml").read_text(encoding="utf-8")
        self.assertIn("enabled: false", config)
        self.assertIn("allowed_users: []", config)
        self.assertIn("allowed_chats: []", config)
        self.assertIn("dingtalk: []", config)
        self.assertIn("tool_search:\n    enabled: off", config)
        for forbidden in ("terminal", "browser", "session_search", "cronjob"):
            self.assertNotIn(f"- {forbidden}", config)

    def test_system_prompt_requires_citations_and_no_guessing(self):
        prompt = (Path(__file__).parents[1] / "config" / "SOUL.md").read_text(encoding="utf-8")
        self.assertIn("wiki/...", prompt)
        self.assertIn(INSUFFICIENT_EVIDENCE, prompt)
        self.assertIn("每个知识问答都必须先调用知识库搜索工具", prompt)
        self.assertIn("搜索命中后，再调用知识库读取工具", prompt)
        self.assertIn("source_status=verified", prompt)
        self.assertIn("source_status=unknown", prompt)
        self.assertIn("知识库查询失败，请联系管理员检查工具状态。", prompt)
        self.assertIn("不得将工具故障伪装成知识库无相关内容", prompt)


class StreamCollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.output = Path(self.temp.name) / "private" / "candidates.json"
        self.now = datetime(2026, 9, 8, 1, 0, tzinfo=timezone.utc)
        self.completed = 0

    def tearDown(self):
        self.temp.cleanup()

    def collector(self, *, seconds=60, max_events=2):
        return CandidateCollector(
            "AbCdEf12_3", self.output,
            CollectorLimits(self.now + timedelta(seconds=seconds), max_events),
            on_complete=lambda: setattr(self, "completed", self.completed + 1),
            now=lambda: self.now,
        )

    def payload(self, text=None, *, conversation="cid-private", kind="1", staff="staff-1"):
        return {
            "msgtype": "text",
            "text": {"content": text or pairing_message("AbCdEf12_3")},
            "senderStaffId": staff,
            "senderId": "sender-1",
            "conversationId": conversation,
            "conversationType": kind,
            "senderCorpId": "corp-1",
            "chatbotCorpId": "corp-1",
            "sessionWebhook": "https://secret.invalid",
            "senderNick": "Must Not Persist",
        }

    def test_correct_code_collects_only_minimum_ids_in_private_file(self):
        collector = self.collector()
        self.assertTrue(collector.process_payload(self.payload()))
        saved = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.output.parent.stat().st_mode), 0o700)
        serialized = json.dumps(saved)
        self.assertNotIn("Must Not Persist", serialized)
        self.assertNotIn("secret.invalid", serialized)
        self.assertFalse(saved["authorization_granted"])

    def test_wrong_code_and_unrelated_message_are_ignored(self):
        collector = self.collector()
        self.assertFalse(collector.process_payload(self.payload("DEK-QA PAIR wrongcode")))
        self.assertFalse(collector.process_payload(self.payload("hello")))
        self.assertEqual(collector.candidates, ())

    def test_group_mention_text_can_contain_exact_pairing_message(self):
        collector = self.collector()
        text = "@知识库机器人 " + pairing_message("AbCdEf12_3")
        self.assertTrue(collector.process_payload(self.payload(text)))

    def test_ack_contains_no_reply_body(self):
        collector = self.collector()
        self.assertEqual(ack_only_process(collector, self.payload("unrelated")), (200, ""))
        self.assertEqual(collector.candidates, ())

    def test_non_text_message_is_ignored(self):
        collector = self.collector()
        payload = self.payload()
        payload["msgtype"] = "picture"
        self.assertFalse(collector.process_payload(payload))

    def test_expired_code_is_ignored(self):
        collector = self.collector(seconds=30)
        self.now += timedelta(seconds=30)
        self.assertFalse(collector.process_payload(self.payload()))
        self.assertEqual(collector.candidates, ())

    def test_event_limit_requests_exit_and_duplicate_does_not_count(self):
        collector = self.collector(max_events=2)
        private = self.payload()
        group = self.payload(conversation="cid-group", kind="2")
        self.assertTrue(collector.process_payload(private))
        self.assertTrue(collector.process_payload(private))
        self.assertEqual(self.completed, 0)
        self.assertTrue(collector.process_payload(group))
        self.assertEqual(self.completed, 1)
        saved = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "complete")
        self.assertEqual(len(saved["candidates"]), 2)

    def test_duplicate_after_completion_does_not_repeat_callback(self):
        collector = self.collector(max_events=1)
        event = self.payload()

        self.assertTrue(collector.process_payload(event))
        self.assertTrue(collector.process_payload(event))

        self.assertEqual(self.completed, 1)

    def test_sensitive_sdk_log_values_are_redacted(self):
        fictional = (
            "endpoint={'endpoint':'wss://stream.example.invalid',"
            "'ticket':'FICTIONAL_TICKET_123'} token=FICTIONAL_TOKEN_456 "
            "clientSecret=FICTIONAL_SECRET_789 "
            "sessionWebhook=https://oapi.dingtalk.com/robot/send?access_token=FICTIONAL"
        )
        redacted = redact_log_text(fictional)
        for value in ("FICTIONAL_TICKET_123", "FICTIONAL_TOKEN_456", "FICTIONAL_SECRET_789", "access_token=FICTIONAL"):
            self.assertNotIn(value, redacted)
        self.assertIn("stream.example.invalid", redacted)

    def test_logging_filter_redacts_exception_text(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            configure_safe_logging()
            logging.getLogger("dingtalk_stream.client").error(
                "failed ticket=%s", "FICTIONAL_TICKET_123"
            )
        finally:
            root.removeHandler(handler)
        self.assertNotIn("FICTIONAL_TICKET_123", stream.getvalue())
        self.assertIn("[REDACTED]", stream.getvalue())


class StreamLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_cancels_and_cleans_session(self):
        cleaned = asyncio.Event()
        timed_out = []

        async def session(_stop):
            try:
                await asyncio.Future()
            finally:
                cleaned.set()

        await run_managed_session(session, asyncio.Event(), 0.01, lambda: timed_out.append(True))
        self.assertTrue(cleaned.is_set())
        self.assertEqual(timed_out, [True])

    async def test_ctrl_c_stop_event_cleans_without_reconnect(self):
        stop = asyncio.Event()
        cleaned = asyncio.Event()
        starts = 0

        async def session(_stop):
            nonlocal starts
            starts += 1
            try:
                await asyncio.Future()
            finally:
                cleaned.set()

        task = asyncio.create_task(run_managed_session(session, stop, 60, lambda: None))
        await asyncio.sleep(0)
        stop.set()
        await task
        self.assertTrue(cleaned.is_set())
        self.assertEqual(starts, 1)

    async def test_two_sigint_signals_are_idempotent_and_clean(self):
        stop = asyncio.Event()
        cleaned = asyncio.Event()

        async def session(_stop):
            try:
                await asyncio.Future()
            finally:
                cleaned.set()

        loop = asyncio.get_running_loop()
        install_stop_signal_handlers(loop, stop)
        try:
            task = asyncio.create_task(run_managed_session(session, stop, 60, lambda: None))
            await asyncio.sleep(0)
            os.kill(os.getpid(), signal.SIGINT)
            os.kill(os.getpid(), signal.SIGINT)
            await task
        finally:
            remove_stop_signal_handlers(loop)
        self.assertTrue(cleaned.is_set())


if __name__ == "__main__":
    unittest.main()
