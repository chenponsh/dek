import gzip
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from web.personal_documents import DocumentError, SharedDocumentStore, extract_text, store_from_environment


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

    def test_documents_are_shared_and_context_preserves_source_label(self):
        with tempfile.TemporaryDirectory() as temporary:
            storage = MemoryStorage()
            store = SharedDocumentStore(Path(temporary), storage, clock=lambda: 123)
            first = store.add("u1", "方案.txt", "生产场地变更研究方案".encode())
            self.assertEqual(store.list()[0]["name"], "方案.txt")
            context = store.context("场地变更")
            self.assertIn("权重相同", context)
            self.assertIn("共享资料：方案.txt", context)
            self.assertIn("生产场地变更研究方案", context)
            item, raw = store.download(first["id"])
            self.assertEqual((item["name"], raw),("方案.txt","生产场地变更研究方案".encode()))
            store.remove(first["id"])
            self.assertEqual(store.list(), [])
            self.assertEqual(storage.values, {})

    def test_configuration_is_off_by_default_and_requires_private_complete_bucket(self):
        self.assertIsNone(store_from_environment({}))
        with self.assertRaises(DocumentError):
            store_from_environment({"QINIU_ACCESS_KEY": "a"})

    def test_metadata_contains_no_user_identifier_or_document_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SharedDocumentStore(Path(temporary), MemoryStorage())
            store.add("private-user-id", "资料.md", b"private body")
            files = list(Path(temporary).iterdir())
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].name,"shared.json")
            raw = files[0].read_text(encoding="utf-8")
            self.assertNotIn("private-user-id", raw)
            self.assertNotIn("private body", raw)


if __name__ == "__main__": unittest.main()
