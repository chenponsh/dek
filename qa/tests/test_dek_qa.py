import asyncio
import hashlib
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
from qa.dek_qa.mcp_server import TOOLS, _reply, load_knowledge_base
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
            '---\nsource_url: "https://official.example/notice"\nsource_name: 测试材料\nsource_type: 第三方整理\n---\n\n来源全文', encoding="utf-8"
        )
        (self.root / "source" / "CPC" / "应排除_排除").mkdir()
        (self.root / "source" / "CPC" / "应排除_排除" / "坏.md").write_text(
            '---\nsource_url: "https://excluded.example"\n---\n', encoding="utf-8"
        )
        (self.root / "wiki" / "01_注册" / "0101-0001.md").write_text(
            '---\ndate: 2026-09-05\nquestion: 药品注册如何申报？\nsource: 官方通知\n---\n\n通过注册系统提交。\n\n![[source/CPC/官方通知]]', encoding="utf-8"
        )
        self.index = self.root / "runtime" / "index.json"
        build_index(self.root, self.index)
        self.kb = KnowledgeBase(self.index)

    def tearDown(self):
        self.temp.cleanup()

    def test_index_contains_only_wiki_and_neutral_source_metadata(self):
        data = json.loads(self.index.read_text(encoding="utf-8"))
        self.assertEqual(len(data["documents"]), 1)
        doc = data["documents"][0]
        self.assertTrue(doc["path"].startswith("wiki/"))
        self.assertEqual(doc["source_urls"], ["https://official.example/notice"])
        self.assertEqual(doc["source_names"], ["测试材料"])
        self.assertEqual(doc["source_types"], ["第三方整理"])
        self.assertNotIn("official_urls", doc)
        self.assertNotIn("来源全文", json.dumps(data, ensure_ascii=False))
        self.assertNotIn("excluded.example", json.dumps(data))

    def test_live_mcp_load_emits_proof_for_exact_loaded_bytes(self):
        proof_path = self.root / "run" / "generation.json"
        kb, loaded = load_knowledge_base(self.index, proof_path, boot_nonce="boot-1", release_sha256="f" * 64, pid=4321)
        self.index.write_text('{"version":4,"documents":[]}', encoding="utf-8")
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
        self.assertEqual(proof["index_sha256"], hashlib.sha256(loaded).hexdigest())
        self.assertEqual(Path(proof["index_path"]), self.index.resolve())
        self.assertEqual(proof["release"], str(self.index.resolve().parent))
        self.assertEqual(proof["boot_nonce"], "boot-1")
        self.assertEqual(proof["release_sha256"], "f" * 64)
        self.assertEqual(proof["pid"], 4321)
        self.assertTrue(kb.dek_kb_search("药品注册"))
        self.assertEqual(proof_path.stat().st_mode & 0o777, 0o640)

    def test_wiki_wikilink_with_source_stem_is_not_a_source_mapping(self):
        note = self.root / "wiki" / "01_注册" / "0101-0001.md"
        note.write_text(
            "---\nquestion: 药品注册如何申报？\n---\n\n参见 [[官方通知]]。",
            encoding="utf-8",
        )

        payload = build_index(self.root, self.index)

        self.assertEqual(payload["documents"][0]["source_urls"], [])
        self.assertEqual(payload["documents"][0]["source_status"], "none")

    def test_source_url_requires_valid_host_and_no_credentials(self):
        source = self.root / "source" / "CPC" / "官方通知.md"
        source.write_text(
            '---\nsource_url: "https://user:secret@official.example/notice"\n---\n',
            encoding="utf-8",
        )

        payload = build_index(self.root, self.index)

        self.assertEqual(payload["documents"][0]["source_urls"], [])
        self.assertEqual(payload["documents"][0]["source_status"], "unknown")

    def test_path_qualified_source_does_not_fall_back_to_same_stem(self):
        note = self.root / "wiki" / "01_注册" / "0101-0001.md"
        note.write_text(
            "---\nquestion: 药品注册如何申报？\nsource: [[source/错误目录/官方通知]]\n---\n\n见来源。",
            encoding="utf-8",
        )
        build_index(self.root, self.index)
        document = json.loads(self.index.read_text(encoding="utf-8"))["documents"][0]
        self.assertEqual(document["source_urls"], [])
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

    def test_unmapped_wiki_urls_are_not_promoted_to_source_links(self):
        note = self.root / "wiki" / "01_注册" / "0101-0001.md"
        note.write_text(
            "---\nquestion: 药品注册如何申报？\nsource: 官方通知\n---\n\n"
            "正文链接 https://unverified.example/page\n\n![[source/CPC/官方通知]]",
            encoding="utf-8",
        )

        payload = build_index(self.root, self.index)

        self.assertEqual(payload["documents"][0]["source_urls"], ["https://official.example/notice"])

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
        self.assertEqual(first["metadata"]["builder_version"], "4")
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

    def test_bare_source_wikilink_resolves_legacy_url_field(self):
        source = self.root / "source" / "CDE" / "受理共性问题.md"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            "---\nurl: https://official.example/cde/questions\n---\n",
            encoding="utf-8",
        )
        note = self.root / "wiki" / "01_注册" / "0101-0003.md"
        note.write_text(
            "---\nquestion: 如何准备资料？\nsource: '[[受理共性问题]]'\n---\n\n按要求准备。",
            encoding="utf-8",
        )
        build_index(self.root, self.index)
        kb = KnowledgeBase(self.index)
        result = kb.dek_kb_search("准备资料", 5)
        document = kb.dek_kb_get(result[0]["id"])
        self.assertEqual(document["source_status"], "verified")
        self.assertEqual(
            document["source_urls"],
            ["https://official.example/cde/questions"],
        )

    def test_legacy_indexes_are_normalized_to_neutral_source_urls(self):
        current = json.loads(self.index.read_text(encoding="utf-8"))
        for version in (2, 3):
            with self.subTest(version=version):
                legacy = json.loads(json.dumps(current))
                legacy["version"] = version
                for document in legacy["documents"]:
                    document["official_urls"] = document.pop("source_urls")
                    document.pop("source_names", None)
                    document.pop("source_types", None)
                self.index.write_text(json.dumps(legacy), encoding="utf-8")

                document = KnowledgeBase(self.index).dek_kb_get(legacy["documents"][0]["id"])

                self.assertIsNotNone(document)
                self.assertEqual(document["source_urls"], ["https://official.example/notice"])
                self.assertNotIn("official_urls", document)
                build_index(self.root, self.index)

    def test_recent_distinguishes_publication_date_from_git_update(self):
        recent = self.kb.dek_kb_recent(days=7, as_of="2026-09-09")
        self.assertEqual(recent["knowledge_base_update_count"], 0)
        self.assertEqual(recent["publication_count"], 1)
        item = recent["recent_publications"][0]
        self.assertEqual(item["path"], "wiki/01_注册/0101-0001.md")
        self.assertEqual(item["source_urls"], ["https://official.example/notice"])
        self.assertEqual(item["source_names"], ["测试材料"])
        self.assertEqual(item["source_types"], ["第三方整理"])
        self.assertEqual(item["source_status"], "verified")

    def test_recent_uses_last_git_commit_date_for_formal_wiki_updates(self):
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "-c", "user.name=QA", "-c", "user.email=qa@example.invalid", "add", "wiki", "source"],
            cwd=self.root,
            check=True,
        )
        env = os.environ.copy()
        env["GIT_AUTHOR_DATE"] = "2026-09-07T08:00:00+08:00"
        env["GIT_COMMITTER_DATE"] = "2026-09-07T08:00:00+08:00"
        subprocess.run(
            ["git", "-c", "user.name=QA", "-c", "user.email=qa@example.invalid", "commit", "-qm", "fixture"],
            cwd=self.root,
            env=env,
            check=True,
        )
        build_index(self.root, self.index)
        recent = KnowledgeBase(self.index).dek_kb_recent(days=7, as_of="2026-09-09")
        self.assertEqual(recent["knowledge_base_update_count"], 1)
        item = recent["knowledge_base_updates"][0]
        self.assertTrue(item["updated_at"].startswith("2026-09-07"))
        self.assertEqual(item["source_urls"], ["https://official.example/notice"])
        self.assertEqual(item["source_names"], ["测试材料"])
        self.assertEqual(item["source_types"], ["第三方整理"])
        self.assertEqual(item["source_status"], "verified")

    def test_search_requires_meaningful_token_without_crossing_boundaries(self):
        note = self.root / "wiki" / "01_注册" / "0101-0002.md"
        note.write_text("---\nquestion: 甲。乙\n---\n\n甲。乙", encoding="utf-8")
        build_index(self.root, self.index)
        kb = KnowledgeBase(self.index)

        self.assertEqual(kb.dek_kb_search("甲乙"), [])
        self.assertEqual(kb.dek_kb_search("药"), [])

    def test_search_prefers_complete_query_phrase_in_title_over_body_only_match(self):
        generic = self.root / "wiki" / "01_注册" / "0101-0002.md"
        generic.write_text(
            "---\nquestion: 常规注册检验如何办理？\n---\n\n本文泛化讨论辅料变更。",
            encoding="utf-8",
        )
        direct = self.root / "wiki" / "01_注册" / "0101-0003.md"
        direct.write_text(
            "---\nquestion: 辅料变更如何申报？\n---\n\n请按变更要求准备资料。",
            encoding="utf-8",
        )
        build_index(self.root, self.index)

        hits = KnowledgeBase(self.index).dek_kb_search("辅料变更")

        self.assertEqual(hits[0]["title"], "辅料变更如何申报？")

    def test_search_weights_title_and_parent_categories_above_saturated_body(self):
        category = self.root / "wiki" / "16_上市后变更" / "1609_辅料变更"
        category.mkdir(parents=True)
        (category / "1609-0001.md").write_text(
            "---\nquestion: 如何准备申报材料？\n---\n\n按要求准备。",
            encoding="utf-8",
        )
        title_match = self.root / "wiki" / "01_注册" / "0101-0002.md"
        title_match.write_text(
            "---\nquestion: 辅料变更申报\n---\n\n按要求准备。",
            encoding="utf-8",
        )
        body_once = self.root / "wiki" / "01_注册" / "0101-0003.md"
        body_once.write_text(
            "---\nquestion: 通用变更问题甲\n---\n\n辅料变更。",
            encoding="utf-8",
        )
        body_repeated = self.root / "wiki" / "01_注册" / "0101-0004.md"
        body_repeated.write_text(
            "---\nquestion: 通用变更问题乙\n---\n\n" + "辅料变更。" * 50,
            encoding="utf-8",
        )
        build_index(self.root, self.index)

        hits = KnowledgeBase(self.index).dek_kb_search("辅料变更", limit=10)
        positions = {hit["title"]: position for position, hit in enumerate(hits)}
        scores = {hit["title"]: hit["score"] for hit in hits}

        self.assertLess(positions["辅料变更申报"], positions["通用变更问题甲"])
        self.assertLess(positions["如何准备申报材料？"], positions["通用变更问题甲"])
        self.assertEqual(scores["通用变更问题甲"], scores["通用变更问题乙"])

    def test_search_excludes_tag_page_candidates_but_keeps_them_indexed(self):
        category = self.root / "wiki" / "16_上市后变更" / "1609_辅料变更"
        category.mkdir(parents=True)
        tag_page = category / "1609_辅料变更.md"
        tag_page.write_text(
            "---\naliases:\n  - '#16_上市后变更/1609_辅料变更'\n---\n\n```dataview\nTABLE file.link\n```",
            encoding="utf-8",
        )
        answer = category / "1609-0001.md"
        answer.write_text(
            "---\nquestion: 辅料变更应如何申报？\n---\n\n按指导原则准备申报资料。",
            encoding="utf-8",
        )

        payload = build_index(self.root, self.index)
        hits = KnowledgeBase(self.index).dek_kb_search("辅料变更", limit=10)

        indexed_paths = {doc["path"] for doc in payload["documents"]}
        self.assertIn("wiki/16_上市后变更/1609_辅料变更/1609_辅料变更.md", indexed_paths)
        self.assertNotIn(tag_page.relative_to(self.root).as_posix(), {hit["path"] for hit in hits})
        self.assertIn(answer.relative_to(self.root).as_posix(), {hit["path"] for hit in hits})

    def test_complete_phrase_bonus_does_not_cross_punctuation_segments(self):
        punctuated = self.root / "wiki" / "01_注册" / "0101-0002.md"
        punctuated.write_text(
            "---\nquestion: 辅料，变更（料变）\n---\n\n按要求准备。",
            encoding="utf-8",
        )
        contiguous = self.root / "wiki" / "01_注册" / "0101-0003.md"
        contiguous.write_text(
            "---\nquestion: 辅料变更\n---\n\n按要求准备。",
            encoding="utf-8",
        )
        build_index(self.root, self.index)

        hits = KnowledgeBase(self.index).dek_kb_search("辅料变更", limit=10)
        positions = {hit["title"]: position for position, hit in enumerate(hits)}

        self.assertLess(positions["辅料变更"], positions["辅料，变更（料变）"])

    def test_search_keeps_typo_and_omission_recall_for_answerable_category_notes(self):
        category = self.root / "wiki" / "16_上市后变更" / "1609_辅料变更"
        category.mkdir(parents=True)
        answer = category / "1609-0001.md"
        answer.write_text(
            "---\nquestion: 申报资料应如何准备？\n---\n\n按指导原则准备。",
            encoding="utf-8",
        )
        generic = self.root / "wiki" / "01_注册" / "0101-0002.md"
        generic.write_text(
            "---\nquestion: 常见问题示例\n---\n\n辅科变更；辅料变。",
            encoding="utf-8",
        )
        build_index(self.root, self.index)
        kb = KnowledgeBase(self.index)

        for query in ("辅科变更", "辅料变"):
            with self.subTest(query=query):
                hits = kb.dek_kb_search(query, limit=10)
                self.assertEqual(hits[0]["path"], answer.relative_to(self.root).as_posix())

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

    def test_only_three_read_only_mcp_tools_are_exposed(self):
        self.assertEqual(
            {tool["name"] for tool in TOOLS},
            {"dek_kb_search", "dek_kb_get", "dek_kb_recent"},
        )
        response = _reply({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, self.kb)
        self.assertEqual(len(response["result"]["tools"]), 3)
        recent = next(tool for tool in TOOLS if tool["name"] == "dek_kb_recent")
        self.assertEqual(set(recent["inputSchema"]["properties"]), {"days", "limit"})
        self.assertFalse(recent["inputSchema"]["additionalProperties"])

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

    def test_mcp_tools_list_accepts_standard_optional_cursor(self):
        response = _reply(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {"cursor": None, "_meta": {}},
            },
            self.kb,
        )
        self.assertEqual(
            [tool["name"] for tool in response["result"]["tools"]],
            ["dek_kb_search", "dek_kb_get", "dek_kb_recent"],
        )

    def test_mcp_call_accepts_standard_optional_meta(self):
        response = _reply(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "dek_kb_search",
                    "arguments": {"query": "注册申报", "limit": 1},
                    "_meta": {},
                },
            },
            self.kb,
        )
        self.assertFalse(response["result"]["isError"])

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

    def test_mcp_recent_is_exposed_and_called_with_defaults(self):
        class RecordingKnowledgeBase:
            def __init__(self):
                self.calls = []

            def dek_kb_recent(self, **kwargs):
                self.calls.append(kwargs)
                return {
                    "publication_count": 0,
                    "knowledge_base_update_count": 0,
                    "recent_publications": [],
                    "knowledge_base_updates": [],
                }

        kb = RecordingKnowledgeBase()
        response = _reply(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {"name": "dek_kb_recent", "arguments": {}},
            },
            kb,
        )
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(kb.calls, [{"days": 7, "limit": 20}])
        result = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(result["publication_count"], 0)

    def test_mcp_recent_validates_parameters(self):
        invalid_arguments = [
            {"days": True},
            {"days": 0},
            {"days": 366},
            {"days": 7.0},
            {"limit": False},
            {"limit": 0},
            {"limit": 51},
            {"limit": "20"},
            {"as_of": "2026-09-09"},
            {"extra": True},
        ]
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                response = _reply(
                    {
                        "jsonrpc": "2.0",
                        "id": 10,
                        "method": "tools/call",
                        "params": {"name": "dek_kb_recent", "arguments": arguments},
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
        self.assertEqual(len(responses[4]["result"]["tools"]), 3)

    def test_unknown_mcp_tool_is_rejected(self):
        response = _reply({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "shell", "arguments": {}}}, self.kb)
        self.assertEqual(response["error"]["code"], -32601)

    def test_systemd_unit_limits_writes_to_profile_state(self):
        unit = (Path(__file__).parents[2] / "deploy" / "systemd" / "dek-qa.service").read_text(encoding="utf-8")
        self.assertIn("ReadWritePaths=/var/lib/dek-qa/hermes", unit)
        self.assertIn("ReadOnlyPaths=/var/lib/dek-activate/control/active.json /var/lib/dek-activate/releases /var/lib/dek-qa/secrets", unit)
        self.assertIn("Environment=HERMES_DISABLE_LAZY_INSTALLS=1", unit)
        self.assertNotIn("ReadWritePaths=/var/lib/dek-qa\n", unit)

    def test_qa_activation_synchronously_opens_and_proves_selected_index(self):
        unit = (Path(__file__).parents[2] / "deploy" / "systemd" / "dek-qa.service").read_text(encoding="utf-8")
        self.assertNotIn("ExecStartPre=", unit)
        config = (Path(__file__).parents[1] / "config" / "config.yaml").read_text(encoding="utf-8")
        self.assertIn("--generation-proof", config)

    def test_profile_config_delegates_users_to_dingtalk_and_exposes_only_mcp(self):
        config = (Path(__file__).parents[1] / "config" / "config.yaml").read_text(encoding="utf-8")
        self.assertIn("/var/lib/dek-activate/control/active.json", config)
        self.assertIn("/var/lib/dek-activate/releases", config)
        self.assertIn("enabled: false", config)
        self.assertIn('allowed_users:\n        - "*"', config)
        self.assertIn("allowed_chats: []", config)
        self.assertIn("dingtalk: []", config)
        self.assertIn("tool_search:\n    enabled: off", config)
        for forbidden in ("terminal", "browser", "session_search", "cronjob"):
            self.assertNotIn(f"- {forbidden}", config)

    def test_system_prompt_requires_citations_and_no_guessing(self):
        prompt = (Path(__file__).parents[1] / "config" / "SOUL.md").read_text(encoding="utf-8")
        self.assertIn("wiki/...", prompt)
        self.assertIn(INSUFFICIENT_EVIDENCE, prompt)
        self.assertIn("其他知识问答必须先且最多调用一轮知识库搜索工具", prompt)
        self.assertIn("在下一轮并行读取最相关的 1–3 篇笔记", prompt)
        self.assertIn("source_status=verified", prompt)
        self.assertIn("source_status=unknown", prompt)
        self.assertIn("source_urls", prompt)
        self.assertIn("来源链接", prompt)
        self.assertNotIn("official_urls", prompt)
        self.assertNotIn("官方 URL", prompt)
        self.assertNotIn("官方来源链接", prompt)
        self.assertIn("不显示内部 `wiki/...` 路径", prompt)
        self.assertIn("用户明确要求内部追溯信息", prompt)
        self.assertIn("知识库查询失败，请联系管理员检查工具状态。", prompt)
        self.assertIn("不得将工具故障伪装成知识库无相关内容", prompt)
        self.assertIn("最近几天/一周/月新增或更新了什么", prompt)
        self.assertIn("默认查看 `recent_publications`，按来源发布日期回答", prompt)
        self.assertIn("查看 `knowledge_base_updates`，按正式 wiki 的 Git 更新时间回答", prompt)
        self.assertIn("逐字使用工具返回的 `title`", prompt)
        self.assertIn("不得改写、润色、补充或删减标题", prompt)
        self.assertIn("每个列出的条目都必须按该条目的来源证据处理", prompt)
        self.assertIn("展示工具返回的来源名称（若有）和至少一个公开来源链接", prompt)
        self.assertIn("只有工具结果为 `source_status=verified` 且 `source_urls` 非空时", prompt)
        self.assertIn("多个条目共享同一已确认来源", prompt)
        self.assertIn("明确说明适用于哪些条目或全部条目", prompt)
        self.assertIn("来源链接尚未确认", prompt)
        self.assertIn("未提供来源链接", prompt)
        self.assertIn("两者均不得补造链接", prompt)
        self.assertIn("即使只报告数量和标题，也必须提供上述来源证据", prompt)
        self.assertIn("数量为 0 必须明确回答 0", prompt)
        self.assertIn("调用失败或结果不可解析属于查询失败，不得表述为 0", prompt)
        self.assertIn("即使用户明确要求内部追溯信息，也不显示内部路径", prompt)
        self.assertIn("最多调用一轮知识库搜索工具", prompt)


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
