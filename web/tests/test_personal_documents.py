import gzip
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from web.personal_documents import DocumentError, PersonalDocumentStore, extract_text, store_from_environment


class MemoryStorage:
    def __init__(self): self.values = {}
    def upload(self, key, data, mime): self.values[key] = data
    def download(self, key, maximum):
        value = self.values[key]
        if len(value) > maximum: raise DocumentError("large")
        return value
    def delete(self, key): self.values.pop(key, None)


class PersonalDocumentTests(unittest.TestCase):
    def test_text_and_docx_extraction_are_bounded_to_supported_types(self):
        self.assertEqual(extract_text("a.txt", "法规资料".encode()), "法规资料")
        value = io.BytesIO()
        with zipfile.ZipFile(value, "w") as archive:
            archive.writestr("word/document.xml", '<w:document xmlns:w="x"><w:p><w:r><w:t>场地变更</w:t></w:r></w:p></w:document>')
        self.assertEqual(extract_text("a.docx", value.getvalue()), "场地变更")
        with self.assertRaises(DocumentError): extract_text("a.exe", b"data")

    def test_documents_are_isolated_by_user_and_context_is_labeled_unreviewed(self):
        with tempfile.TemporaryDirectory() as temporary:
            storage = MemoryStorage()
            store = PersonalDocumentStore(Path(temporary), storage, b"s" * 32, clock=lambda: 123)
            first = store.add("u1", "方案.txt", "生产场地变更研究方案".encode())
            self.assertEqual(store.list("u2"), [])
            self.assertEqual(store.list("u1")[0]["name"], "方案.txt")
            context = store.context("u1", [first["id"]], "场地变更")
            self.assertIn("未经 DEK 审核", context)
            self.assertIn("我的资料：方案.txt", context)
            self.assertIn("生产场地变更研究方案", context)
            store.remove("u1", first["id"])
            self.assertEqual(store.list("u1"), [])
            self.assertEqual(storage.values, {})

    def test_configuration_is_off_by_default_and_requires_private_complete_bucket(self):
        self.assertIsNone(store_from_environment({}))
        with self.assertRaises(DocumentError):
            store_from_environment({"QINIU_ACCESS_KEY": "a"})

    def test_metadata_contains_no_user_identifier_or_document_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = PersonalDocumentStore(Path(temporary), MemoryStorage(), b"s" * 32)
            store.add("private-user-id", "资料.md", b"private body")
            files = list(Path(temporary).iterdir())
            self.assertEqual(len(files), 1)
            self.assertNotIn("private-user-id", files[0].name)
            raw = files[0].read_text(encoding="utf-8")
            self.assertNotIn("private-user-id", raw)
            self.assertNotIn("private body", raw)


if __name__ == "__main__": unittest.main()
