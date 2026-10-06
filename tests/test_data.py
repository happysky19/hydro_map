import io
import json
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from hydro_map.data import download_dataset


STEM = "hybas_na_lev12_v1c"
EXTENSIONS = ("shp", "shx", "dbf", "prj")


def archive(*, missing=None, symlink=False, extra=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_STORED) as output:
        for extension in EXTENSIONS:
            if extension == missing:
                continue
            name = f"{STEM}.{extension}"
            if symlink and extension == "shp":
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                output.writestr(info, "/etc/passwd")
            else:
                output.writestr(name, f"original-{extension}".encode())
        if extra:
            output.writestr(extra, "must not extract")
    return stream.getvalue()


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cache = Path(self.temporary.name)

    def download(self, content):
        with patch("hydro_map.data.urlopen", return_value=io.BytesIO(content)) as request:
            result = download_dataset("na", 12, "1c", self.cache)
        return result, request

    def test_download_and_verified_cache_reuse(self):
        result, request = self.download(archive())
        self.assertEqual(result, self.cache / STEM / f"{STEM}.shp")
        self.assertEqual(result.read_bytes(), b"original-shp")
        http_request = request.call_args.args[0]
        self.assertEqual(http_request.full_url,
                         f"https://data.hydrosheds.org/file/hydrobasins/standard/{STEM}.zip")
        self.assertTrue(http_request.get_header("User-agent").startswith("hydro-map/"))
        manifest = json.loads((result.parent / "manifest.json").read_text())
        self.assertEqual(manifest["source_url"], http_request.full_url)
        self.assertEqual(len(manifest["sha256"][f"{STEM}.zip"]), 64)
        with patch("hydro_map.data.urlopen") as request:
            self.assertEqual(download_dataset("na", 12, "1c", self.cache), result)
            request.assert_not_called()

    def test_modified_or_interrupted_cache_is_downloaded_again(self):
        for target in (f"{STEM}.shp", f"{STEM}.zip", "manifest.json"):
            with self.subTest(target=target):
                result, _ = self.download(archive())
                (result.parent / target).write_bytes(b"truncated")
                result, request = self.download(archive())
                request.assert_called_once()
                self.assertEqual(result.read_bytes(), b"original-shp")
        (result.parent / "manifest.json").unlink()
        _, request = self.download(archive())
        request.assert_called_once()

    def test_missing_required_member_and_bad_crc_are_rejected(self):
        damaged = archive().replace(b"original-shp", b"tampered-shp", 1)
        for content in (archive(missing="prj"), damaged, b"not a zip"):
            with self.subTest(content=content[:20]):
                with self.assertRaisesRegex(ValueError, "archive"):
                    self.download(content)
                self.assertFalse((self.cache / STEM / "manifest.json").exists())

    def test_only_exact_members_are_extracted_and_symlinks_rejected(self):
        result, _ = self.download(archive(extra="../escape"))
        self.assertFalse((self.cache / "escape").exists())
        self.assertFalse((result.parent / "escape").exists())
        (result.parent / "manifest.json").unlink()
        with self.assertRaisesRegex(ValueError, "archive"):
            self.download(archive(symlink=True))

    def test_failed_stream_does_not_publish_partial_cache(self):
        class BrokenStream(io.BytesIO):
            def read(self, size=-1):
                raise OSError("connection lost")

        with patch("hydro_map.data.urlopen", return_value=BrokenStream(b"partial")):
            with self.assertRaisesRegex(OSError, "download"):
                download_dataset("na", 12, "1c", self.cache)
        self.assertFalse((self.cache / STEM / "manifest.json").exists())
        result, _ = self.download(archive())
        self.assertTrue(result.is_file())

    def test_invalid_inputs_do_not_access_network(self):
        with patch("hydro_map.data.urlopen") as request:
            for region, level, version in (("../na", 12, "1c"), ("na", 0, "1c"),
                                           ("na", 13, "1c"), ("na", True, "1c"),
                                           ("na", 12, "2")):
                with self.subTest(region=region, level=level, version=version):
                    with self.assertRaises(ValueError):
                        download_dataset(region, level, version, self.cache)
            request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
