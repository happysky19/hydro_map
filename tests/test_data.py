import io
import hashlib
import json
import stat
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

from hydro_map.data import download_dataset
from hydro_map import data


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


def flow_archive(layer, *, missing=False, symlink=False, extra=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_STORED) as output:
        if not missing:
            member = zipfile.ZipInfo(f"hyd_na_{layer}_15s.tif")
            member.create_system = 3
            member.external_attr = ((stat.S_IFLNK if symlink else stat.S_IFREG) | 0o644) << 16
            output.writestr(member, f"original-{layer}".encode())
        output.writestr("HydroSHEDS_TechDoc_v1_4.pdf", b"documentation")
        if extra:
            output.writestr(extra, b"must not extract")
    return stream.getvalue()


class FlowDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cache = Path(self.temporary.name)

    def download(self, *contents):
        with patch("hydro_map.data.urlopen", side_effect=[io.BytesIO(content) for content in contents]):
            return data.download_flow_dataset("na", self.cache)

    def test_downloads_exact_rasters_and_reuses_verified_cache(self):
        paths = self.download(flow_archive("dir", extra="../escape"), flow_archive("aca"))
        for layer, path in zip(("dir", "aca"), paths):
            stem = f"hyd_na_{layer}_15s"
            self.assertEqual(path, self.cache / "hydrosheds" / stem / f"{stem}.tif")
            self.assertEqual(path.read_bytes(), f"original-{layer}".encode())
            manifest = json.loads((path.parent / "manifest.json").read_text())
            self.assertEqual(manifest["source_url"],
                             f"https://data.hydrosheds.org/file/hydrosheds-v1-{layer}/{stem}.zip")
            for name, digest in manifest["sha256"].items():
                self.assertEqual(hashlib.sha256((path.parent / name).read_bytes()).hexdigest(), digest)
            self.assertEqual(set(p.name for p in path.parent.iterdir()),
                             {f"{stem}.tif", f"{stem}.zip", "manifest.json"})
        self.assertFalse((self.cache / "hydrosheds" / "escape").exists())
        with patch("hydro_map.data.urlopen", side_effect=AssertionError("verified cache must work offline")):
            self.assertEqual(data.download_flow_dataset("na", self.cache), paths)

    def test_adopts_existing_archives_without_trusting_extracted_rasters(self):
        root = self.cache / "hydrosheds"
        root.mkdir()
        for layer in ("dir", "aca"):
            stem = f"hyd_na_{layer}_15s"
            (root / f"{stem}.zip").write_bytes(flow_archive(layer))
            (root / stem).mkdir()
            (root / stem / f"{stem}.tif").write_bytes(b"unverified old raster")
        with patch("hydro_map.data.urlopen", side_effect=AssertionError("existing archives should work offline")):
            paths = data.download_flow_dataset("na", self.cache)
        self.assertEqual([p.read_bytes() for p in paths], [b"original-dir", b"original-aca"])

    def test_modified_cache_is_repaired_before_reuse(self):
        paths = self.download(flow_archive("dir"), flow_archive("aca"))
        for name in (paths[0].name, "hyd_na_dir_15s.zip", "manifest.json"):
            with self.subTest(name=name):
                (paths[0].parent / name).write_bytes(b"truncated")
                repaired = self.download(flow_archive("dir"))
                self.assertEqual(repaired[0].read_bytes(), b"original-dir")

    def test_invalid_archives_do_not_publish_a_manifest(self):
        damaged = flow_archive("dir").replace(b"original-dir", b"tampered-dir", 1)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            duplicate = flow_archive("dir", extra="hyd_na_dir_15s.tif")
        for content in (flow_archive("dir", missing=True), flow_archive("dir", symlink=True),
                        duplicate, damaged, b"not a zip"):
            with self.subTest(content=content[:20]):
                with self.assertRaisesRegex(ValueError, "archive"):
                    self.download(content)
                self.assertFalse((self.cache / "hydrosheds" / "hyd_na_dir_15s" / "manifest.json").exists())

    def test_failed_download_does_not_publish_partial_cache(self):
        class BrokenStream(io.BytesIO):
            def read(self, size=-1):
                raise OSError("connection lost")

        with patch("hydro_map.data.urlopen", return_value=BrokenStream(b"partial")):
            with self.assertRaisesRegex(OSError, "download"):
                data.download_flow_dataset("na", self.cache)
        self.assertFalse((self.cache / "hydrosheds" / "hyd_na_dir_15s" / "manifest.json").exists())
        self.assertEqual(list((self.cache / "hydrosheds").iterdir()), [])

    def test_unsupported_region_and_symlink_cache_do_not_access_network(self):
        with patch("hydro_map.data.urlopen", side_effect=AssertionError("invalid inputs must not download")):
            with self.assertRaises(ValueError):
                data.download_flow_dataset("../na", self.cache)
            (self.cache / "hydrosheds").symlink_to(self.cache, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                data.download_flow_dataset("na", self.cache)


if __name__ == "__main__":
    unittest.main()
