import os
import stat
import tempfile
import unittest
from pathlib import Path

from deploy.fsutil import atomic_write_bytes, atomic_write_json, fsync_dir, read_bounded_regular


class AtomicWriteBytesTests(unittest.TestCase):
    def test_writes_content_mode_and_leaves_no_temp_file_behind(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "out.bin"
            atomic_write_bytes(target, b"hello", mode=0o600)
            self.assertEqual(target.read_bytes(), b"hello")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(sorted(os.listdir(temporary)), ["out.bin"])

    def test_replace_preserves_old_content_if_crash_happens_before_replace(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "out.bin"
            target.write_bytes(b"original")
            real_replace = os.replace

            def boom(*args, **kwargs):
                raise OSError("simulated crash before replace")

            os.replace = boom
            try:
                with self.assertRaises(OSError):
                    atomic_write_bytes(target, b"new", mode=0o600)
            finally:
                os.replace = real_replace
            self.assertEqual(target.read_bytes(), b"original")
            # the tempfile must be cleaned up even though the write failed
            self.assertEqual(sorted(os.listdir(temporary)), ["out.bin"])

    def test_ensure_parent_mode_creates_missing_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "nested" / "out.bin"
            atomic_write_bytes(target, b"x", mode=0o600, ensure_parent_mode=0o700)
            self.assertEqual(target.read_bytes(), b"x")
            self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o700)

    def test_parent_no_follow_false_allows_a_dir_fd_magic_symlink_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            real = Path(temporary) / "real"; real.mkdir()
            descriptor = os.open(real, os.O_RDONLY | os.O_DIRECTORY)
            try:
                target = Path(f"/proc/self/fd/{descriptor}") / "out.bin"
                with self.assertRaises(OSError):
                    atomic_write_bytes(target, b"x", mode=0o600)
                atomic_write_bytes(target, b"x", mode=0o600, parent_no_follow=False)
                self.assertEqual((real / "out.bin").read_bytes(), b"x")
            finally:
                os.close(descriptor)


class AtomicWriteJsonTests(unittest.TestCase):
    def test_writes_canonical_sorted_json_with_trailing_newline(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "out.json"
            atomic_write_json(target, {"b": 1, "a": 2}, mode=0o640)
            self.assertEqual(target.read_text(encoding="utf-8"), '{"a":2,"b":1}\n')
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)


class FsyncDirTests(unittest.TestCase):
    def test_refuses_to_follow_a_symlinked_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            real = Path(temporary) / "real"; real.mkdir()
            link = Path(temporary) / "link"; link.symlink_to(real)
            with self.assertRaises(OSError):
                fsync_dir(link)

    def test_no_follow_false_allows_a_dir_fd_magic_symlink_path(self):
        """Callers that pin a root via os.open(..., O_DIRECTORY) and address
        it afterwards as /proc/self/fd/N (itself always a symlink) need to
        opt out of O_NOFOLLOW, since that path is trusted by construction."""
        with tempfile.TemporaryDirectory() as temporary:
            real = Path(temporary) / "real"; real.mkdir()
            descriptor = os.open(real, os.O_RDONLY | os.O_DIRECTORY)
            try:
                magic = Path(f"/proc/self/fd/{descriptor}")
                with self.assertRaises(OSError):
                    fsync_dir(magic)
                fsync_dir(magic, no_follow=False)
            finally:
                os.close(descriptor)


class ReadBoundedRegularTests(unittest.TestCase):
    def test_reads_exact_bytes_within_bounds(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "in.bin"
            source.write_bytes(b"payload")
            self.assertEqual(read_bounded_regular(source, maximum=1024), b"payload")

    def test_rejects_a_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            real = Path(temporary) / "real.bin"; real.write_bytes(b"x")
            link = Path(temporary) / "link.bin"; link.symlink_to(real)
            with self.assertRaises(OSError):
                read_bounded_regular(link, maximum=1024)

    def test_rejects_a_hardlinked_file_by_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "in.bin"; source.write_bytes(b"x")
            os.link(source, Path(temporary) / "second-link")
            with self.assertRaises(OSError):
                read_bounded_regular(source, maximum=1024)

    def test_require_nlink1_false_allows_a_hardlinked_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "in.bin"; source.write_bytes(b"x")
            os.link(source, Path(temporary) / "second-link")
            self.assertEqual(read_bounded_regular(source, maximum=1024, require_nlink1=False), b"x")

    def test_rejects_content_over_the_maximum(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "in.bin"; source.write_bytes(b"x" * 10)
            with self.assertRaises(OSError):
                read_bounded_regular(source, maximum=5)

    def test_rejects_content_under_the_minimum(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "in.bin"; source.write_bytes(b"x")
            with self.assertRaises(OSError):
                read_bounded_regular(source, maximum=1024, minimum=2)

    def test_require_uid_and_mode_are_enforced_when_given(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "in.bin"; source.write_bytes(b"x")
            os.chmod(source, 0o440)
            self.assertEqual(read_bounded_regular(source, maximum=1024, require_mode=0o440), b"x")
            with self.assertRaises(OSError):
                read_bounded_regular(source, maximum=1024, require_mode=0o400)
            with self.assertRaises(OSError):
                read_bounded_regular(source, maximum=1024, require_uid=os.getuid() + 1)


if __name__ == "__main__":
    unittest.main()
