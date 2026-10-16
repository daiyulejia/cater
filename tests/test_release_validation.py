"""Offline adversarial fixtures for the Cater wheel release boundary."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import stat
import struct
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts import release_validation as validation
from scripts.release_validation import ReleaseValidationError, validate_wheel


class ReleaseValidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.wheel = self.root / "cater-0.1.0-py3-none-any.whl"
        self.prefix = "cater-0.1.0.dist-info/"
        self.license = b"MIT License\nfixture copyright\n"
        self.expected = {
            "cater/__init__.py": b'__version__ = "0.1.0"\n',
            "cater/cli.py": b"def main():\n    return 0\n",
            "cater/packs/skills/catalog.json": b'{"skills": []}\n',
            "cater/static/workroom/assets/app-new123.js": b"window.fixture = true;\n",
            # An arbitrary manifest cannot grant permission to package old files.
            "cater/static/workroom/workroom-manifest.json": (
                b'{"extra": {"assets/app-old999.js": "trusted"}}\n'
            ),
        }
        self.files = self.expected | {
            self.prefix + "METADATA": (
                b"Metadata-Version: 2.4\nName: cater\nVersion: 0.1.0\n"
                b"Requires-Python: >=3.11\nLicense-Expression: MIT\n"
                b"License-File: LICENSE\n\nA fixture, not imported.\n"
            ),
            self.prefix + "WHEEL": (
                b"Wheel-Version: 1.0\nGenerator: fixture\n"
                b"Root-Is-Purelib: true\nTag: py3-none-any\n\n"
            ),
            self.prefix + "entry_points.txt": b"[console_scripts]\ncater = cater.cli:main\n",
            self.prefix + "top_level.txt": b"cater\n",
            self.prefix + "licenses/LICENSE": self.license,
        }
        self.files[self.prefix + "RECORD"] = self.record(self.files)

    def record(self, files):
        rows = []
        for path, data in files.items():
            if path == self.prefix + "RECORD":
                continue
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
            rows.append([path, "sha256=" + digest.decode("ascii"), str(len(data))])
        rows.append([self.prefix + "RECORD", "", ""])
        return self.csv_bytes(rows)

    @staticmethod
    def csv_bytes(rows):
        buffer = io.StringIO(newline="")
        csv.writer(buffer, lineterminator="\n").writerows(rows)
        return buffer.getvalue().encode("utf-8")

    def write_wheel(self, files=None, *, rebuild_record=True, extras=()):
        files = dict(self.files if files is None else files)
        if rebuild_record and self.prefix + "RECORD" in files:
            files[self.prefix + "RECORD"] = self.record(files)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(self.wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path, data in files.items():
                    archive.writestr(path, data)
                for member, data in extras:
                    archive.writestr(member, data)
        return files

    def validate(self, **kwargs):
        arguments = {"name": "cater", "version": "0.1.0", "license_bytes": self.license}
        arguments.update(kwargs)
        return validate_wheel(self.wheel, self.expected, **arguments)

    def assert_invalid(self, files=None, *, message=None, rebuild_record=True, extras=()):
        self.write_wheel(files, rebuild_record=rebuild_record, extras=extras)
        with self.assertRaisesRegex(ReleaseValidationError, message or ".+"):
            self.validate()

    def test_valid_wheel_reports_only_independently_measured_facts(self):
        files = self.write_wheel()
        before = self.wheel.read_bytes()
        result = self.validate()
        self.assertEqual(result, {
            "name": "cater", "version": "0.1.0",
            "wheel_sha256": hashlib.sha256(before).hexdigest(),
            "package_files": len(self.expected),
            "total_bytes": sum(map(len, files.values())),
            "files": {path: hashlib.sha256(data).hexdigest() for path, data in self.expected.items()},
        })
        self.assertEqual(self.wheel.read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [self.wheel])

    def test_optional_top_level_and_explicit_directories(self):
        files = dict(self.files)
        del files[self.prefix + "top_level.txt"]
        self.write_wheel(files, extras=[("cater/", b""), (self.prefix, b""), (self.prefix + "licenses/", b"")])
        self.validate()

    def test_expected_python_files_are_compared_without_import_or_execution(self):
        self.expected["cater/cli.py"] = b"\xffnot valid Python\x00"
        self.write_wheel(self.files | {"cater/cli.py": self.expected["cater/cli.py"]})
        self.validate()

    def test_explicit_directory_local_headers_are_also_verified(self):
        self.write_wheel(extras=[("cater/", b"")])
        content = bytearray(self.wheel.read_bytes())
        # Change only the directory's local name, leaving the central entry safe.
        central = content.index(b"PK\x01\x02")
        local_name = content.rfind(b"cater/", 0, central)
        self.assertGreater(local_name, 0)
        content[local_name:local_name + 6] = b"other/"
        self.wheel.write_bytes(content)
        with self.assertRaises(ReleaseValidationError):
            self.validate()

    def test_canonical_static_versions(self):
        for version in ("1.2.3", "2.0rc1", "1.4.post2", "3.0.dev1", "1.0+local.2"):
            with self.subTest(version=version):
                files = {
                    path.replace(self.prefix, f"cater-{version}.dist-info/"): (
                        data.replace(b"Version: 0.1.0\n", f"Version: {version}\n".encode())
                        if path.endswith("/METADATA") else data
                    ) for path, data in self.files.items() if not path.endswith("/RECORD")
                }
                original = self.prefix
                self.prefix = f"cater-{version}.dist-info/"
                files[self.prefix + "RECORD"] = self.record(files)
                self.write_wheel(files)
                self.prefix = original
                self.validate(version=version)

    def test_stale_resource_pyc_unknown_package_and_metadata_are_rejected(self):
        for path in (
            "cater/static/workroom/assets/app-old999.js", "cater/old_resource.json",
            "cater/__pycache__/cli.cpython-312.pyc", "other/__init__.py",
            "cater-0.1.0.data/scripts/cater", self.prefix + "extra.json",
            self.prefix + "direct_url.json", self.prefix + "licenses/OTHER",
            "cater-0.0.9.dist-info/METADATA",
        ):
            with self.subTest(path=path):
                self.assert_invalid(self.files | {path: b"old or unexpected"}, message="unexpected file")

    def test_every_expected_file_and_required_metadata_must_exist(self):
        for path in self.files:
            if path == self.prefix + "top_level.txt":
                continue
            with self.subTest(path=path):
                files = dict(self.files)
                del files[path]
                self.assert_invalid(files, message="missing")

    def test_package_body_changes_fail_even_with_consistent_record(self):
        for path in self.expected:
            with self.subTest(path=path):
                self.assert_invalid(self.files | {path: b"modified"}, message="content differs")

    def test_metadata_single_values_identity_license_and_dependencies(self):
        key = self.prefix + "METADATA"
        original = self.files[key]
        replacements = (
            (b"Name: cater", b"Name: other"),
            (b"Name: cater", b"Name: Cater"),
            (b"Version: 0.1.0", b"Version: 0.2.0"),
            (b"Requires-Python: >=3.11", b"Requires-Python: >=3.10"),
            (b"License-Expression: MIT", b"License-Expression: Apache-2.0"),
            (b"License-File: LICENSE", b"License-File: licenses/LICENSE"),
            (b"License-File: LICENSE\n", b""),
            (b"Name: cater", b"Name: cater\nName: cater"),
            (b"Version: 0.1.0", b"Version: 0.1.0\nVersion: 0.1.0"),
            (b"Requires-Python: >=3.11", b"Requires-Python: >=3.11\nRequires-Python: >=3.11"),
            (b"License-Expression: MIT", b"License-Expression: MIT\nLicense-Expression: MIT"),
            (b"License-File: LICENSE", b"License-File: LICENSE\nLicense-File: LICENSE"),
            (b"\n\n", b"\nRequires-Dist: requests>=2\n\n"),
            (b"\n\n", b"\nRequires-Dist:\n\n"),
            (b"\n\n", b"\nRequires-Dist: requests; extra == 'optional'\n\n"),
        )
        for old, new in replacements:
            with self.subTest(new=new):
                self.assert_invalid(self.files | {key: original.replace(old, new)})

    def test_license_body_and_top_level(self):
        self.assert_invalid(self.files | {self.prefix + "licenses/LICENSE": b"different"}, message="License content")
        for data in (b"other\n", b"cater\nother\n", b"Cater", b"\xff"):
            with self.subTest(data=data):
                self.assert_invalid(self.files | {self.prefix + "top_level.txt": data})

    def test_wheel_headers_require_one_pure_python_tag(self):
        key = self.prefix + "WHEEL"
        original = self.files[key]
        for old, new in (
            (b"Wheel-Version: 1.0", b"Wheel-Version: 2.0"),
            (b"Wheel-Version: 1.0", b"Wheel-Version: 1.0\nWheel-Version: 1.0"),
            (b"Root-Is-Purelib: true", b"Root-Is-Purelib: false"),
            (b"Root-Is-Purelib: true", b"Root-Is-Purelib: true\nRoot-Is-Purelib: true"),
            (b"Tag: py3-none-any", b"Tag: cp312-cp312-win_amd64"),
            (b"Tag: py3-none-any", b"Tag: py3-none-any\nTag: py2-none-any"),
            (b"Tag: py3-none-any", b"Tag: py3-none-any\nTag: py3-none-any"),
            (b"Tag: py3-none-any\n", b""),
        ):
            with self.subTest(new=new):
                self.assert_invalid(self.files | {key: original.replace(old, new)})

    def test_entry_points_are_case_sensitive_and_exclusive(self):
        for data in (
            b"[console_scripts]\ncater = other:main\n",
            b"[console_scripts]\nCater = cater.cli:main\n",
            b"[Console_Scripts]\ncater = cater.cli:main\n",
            b"[console_scripts]\ncater = cater.cli:Main\n",
            b"[console_scripts]\ncater = cater.cli:main\nother = cater.cli:main\n",
            b"[console_scripts]\ncater = cater.cli:main\n[gui_scripts]\n",
            b"[DEFAULT]\n[console_scripts]\ncater = cater.cli:main\n",
            b"[DEFAULT]\ncater = cater.cli:main\n[console_scripts]\n",
            b"[console_scripts]\ncater = cater.cli:main\ncater = cater.cli:main\n",
            b"[console_scripts]\ncater = cater.cli:main\n[console_scripts]\n",
            b"[console_scripts]\ncater = cater.cli:main [extra]\n", b"\xff",
        ):
            with self.subTest(data=data):
                self.assert_invalid(self.files | {self.prefix + "entry_points.txt": data})

    def test_record_missing_duplicate_unknown_and_wrong_column_counts(self):
        original = list(csv.reader(io.StringIO(self.files[self.prefix + "RECORD"].decode())))
        for rows in (
            original[1:], original + [original[0]], original + [["unknown", "", ""]],
            [row[:2] for row in original], original + [["", "", "", ""]], original + [[]],
        ):
            with self.subTest(rows=rows[-1:]):
                self.assert_invalid(self.files | {self.prefix + "RECORD": self.csv_bytes(rows)}, rebuild_record=False)

    def test_record_hash_and_decimal_size_are_checked_against_real_bytes(self):
        original = list(csv.reader(io.StringIO(self.files[self.prefix + "RECORD"].decode())))
        for column, values in (
            (1, ("", "md5=deadbeef", "sha256=wrong", original[0][1] + "=")),
            (2, ("", "0", "-1", "+1", "1.0", "999", "0" + original[0][2], " " + original[0][2])),
        ):
            for value in values:
                with self.subTest(column=column, value=value):
                    rows = [row[:] for row in original]
                    rows[0][column] = value
                    self.assert_invalid(self.files | {self.prefix + "RECORD": self.csv_bytes(rows)}, rebuild_record=False)
        for column in (1, 2):
            rows = [row[:] for row in original]
            rows[-1][column] = "1"
            self.assert_invalid(self.files | {self.prefix + "RECORD": self.csv_bytes(rows)}, rebuild_record=False)

    def test_record_does_not_accept_stale_metadata_hash(self):
        key = self.prefix + "METADATA"
        self.assert_invalid(self.files | {key: self.files[key] + b"changed readme\n"},
                            rebuild_record=False, message="RECORD sha256")

    def test_record_csv_quoting_handles_real_package_paths(self):
        path = "cater/packs/a,b.json"
        self.expected[path] = b"{}"
        self.write_wheel(self.files | {path: b"{}"})
        self.validate()

    def test_invalid_csv_and_metadata_parsers_do_not_echo_content(self):
        secret = "PRIVATE_FIXTURE_MARKER"
        cases = {
            self.prefix + "RECORD": b'"' + secret.encode() + b'"unclosed',
            self.prefix + "entry_points.txt": secret.encode(),
            self.prefix + "METADATA": secret.encode() + b"\nno headers\n",
        }
        for path, data in cases.items():
            with self.subTest(path=path):
                self.write_wheel(self.files | {path: data}, rebuild_record=False)
                with self.assertRaises(ReleaseValidationError) as caught:
                    self.validate()
                self.assertNotIn(secret, str(caught.exception))

    def test_path_traversal_absolute_windows_and_component_aliases(self):
        for path in (
            "../outside", "/absolute", "//server/share", "cater/../outside",
            "cater/./cli.py", "cater//cli.py", "C:/outside",
            "C:outside", "cater/file:stream", "cater/trailing. ", "cater/trailing.",
            "cater/trailing ", "cater/NUL", "cater/COM1.txt", "cater/LPT9",
            "cater/a?.json", "cater/a*.json", "cater/a\nb.json",
            "cater/\u202ehidden", "cater/\uff0e\uff0e/outside", "cater/\uff23\uff2f\uff2e",
            "cater/\uff0foutside", "cater/\uff3coutside", "cater/\uff1astream",
        ):
            with self.subTest(path=path):
                self.assert_invalid(extras=[(path, b"bad")], message="Unsafe")

    def test_raw_zip_backslashes_are_rejected_on_windows(self):
        self.write_wheel(extras=[("cater/Xcli.py", b"bad")])
        content = self.wheel.read_bytes().replace(b"cater/Xcli.py", b"cater\\Xcli.py")
        self.wheel.write_bytes(content)
        with self.assertRaisesRegex(ReleaseValidationError, "Unsafe"):
            self.validate()

    def test_nul_truncation_is_rejected(self):
        self.write_wheel(extras=[("cater/nulXname", b"bad")])
        content = self.wheel.read_bytes().replace(b"cater/nulXname", b"cater/nul\0name")
        self.wheel.write_bytes(content)
        with self.assertRaisesRegex(ReleaseValidationError, "Unsafe"):
            self.validate()

    def test_duplicate_path_case_unicode_and_implicit_directory_aliases(self):
        self.assert_invalid(extras=[("cater/cli.py", b"duplicate")], message="duplicate path")
        for path in ("cater/CLI.py", "cater/PACKS/other.json", "\uff43ater/cli.py"):
            with self.subTest(path=path):
                self.assert_invalid(extras=[(path, b"alias")], message="alias conflicts")
        for left, right in (("caf\u00e9.json", "cafe\u0301.json"), ("ss.json", "\u00df.json")):
            self.expected["cater/" + left] = b"{}"
            self.assert_invalid(self.files | {"cater/" + left: b"{}"},
                                extras=[("cater/" + right, b"{}")], message="alias conflicts")
            del self.expected["cater/" + left]

    def test_symlinks_special_files_and_mismatched_directory_types(self):
        for kind in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFDIR):
            member = zipfile.ZipInfo("cater/cli.py")
            member.create_system = 3
            member.external_attr = (kind | 0o644) << 16
            files = dict(self.files)
            del files["cater/cli.py"]
            with self.subTest(kind=kind):
                self.assert_invalid(files, extras=[(member, b"target")], message="link or special")
        member = zipfile.ZipInfo("cater/")
        member.external_attr = (stat.S_IFREG | 0o644) << 16
        self.assert_invalid(extras=[(member, b"")], message="link or special")

    def test_windows_reparse_points_and_directory_attributes_are_rejected(self):
        for attribute in (0x400, 0x10):
            member = zipfile.ZipInfo("cater/cli.py")
            member.create_system = 0
            member.external_attr = attribute
            files = dict(self.files)
            del files["cater/cli.py"]
            with self.subTest(attribute=attribute):
                self.assert_invalid(files, extras=[(member, b"target")], message="reparse point or directory alias")

    def test_encryption_flags_and_unsupported_compression_are_rejected(self):
        self.write_wheel()
        original = self.wheel.read_bytes()
        central = original.index(b"PK\x01\x02")
        for offset, value, message in ((8, 1, "Encrypted"), (10, 99, "compression")):
            content = bytearray(original)
            struct.pack_into("<H", content, central + offset, value)
            self.wheel.write_bytes(content)
            with self.subTest(offset=offset), self.assertRaisesRegex(ReleaseValidationError, message):
                self.validate()

    def test_unexpected_nonempty_duplicate_and_file_directory_paths(self):
        self.assert_invalid(extras=[("unknown/", b"")], message="unexpected directory")
        self.assert_invalid(extras=[("cater/", b"body")], message="unexpected directory")
        self.assert_invalid(extras=[("cater/", b""), ("cater/", b"")], message="duplicate path")
        self.assert_invalid(extras=[("cater/cli.py/", b"")], message="duplicate path")
        path = "cater/packs"
        self.expected[path] = b"file"
        self.assert_invalid(self.files | {path: b"file"}, message="both file and directory")

    def test_limits_apply_to_file_count_individual_and_total_including_metadata(self):
        # Small injected ceilings exercise the same boundary without big fixtures.
        files = self.write_wheel()
        count = len(files)
        total = sum(map(len, files.values()))
        largest = max(map(len, files.values()))
        for field, ceiling, message in (
            ("MAX_FILES", count - 1, "too many"),
            ("MAX_FILE_BYTES", largest - 1, "size limit"),
            ("MAX_TOTAL_BYTES", total - 1, "total uncompressed"),
        ):
            with self.subTest(field=field), patch.object(validation, field, ceiling):
                with self.assertRaisesRegex(ReleaseValidationError, message):
                    self.validate()
        with patch.object(validation, "MAX_FILES", count), \
                patch.object(validation, "MAX_FILE_BYTES", largest), \
                patch.object(validation, "MAX_TOTAL_BYTES", total):
            self.validate()
        self.assertEqual((validation.MAX_FILES, validation.MAX_TOTAL_BYTES, validation.MAX_FILE_BYTES),
                         (2048, 64 * 1024 * 1024, 16 * 1024 * 1024))

    def test_declared_oversize_rejected_before_decompression(self):
        self.write_wheel()
        content = bytearray(self.wheel.read_bytes())
        central = content.index(b"PK\x01\x02")
        struct.pack_into("<I", content, central + 24, validation.MAX_FILE_BYTES + 1)
        self.wheel.write_bytes(content)
        with patch.object(zipfile.ZipFile, "open", side_effect=AssertionError("must not read")):
            with self.assertRaisesRegex(ReleaseValidationError, "size limit"):
                self.validate()

    def test_stream_reads_are_bounded_even_if_source_exceeds_declaration(self):
        member = zipfile.ZipInfo("cater/cli.py")
        member.file_size = 10

        class OversizedSource(io.BytesIO):
            def read(self, size=-1):
                self.test.assertLessEqual(size, validation._CHUNK_BYTES)
                self.test.assertGreater(size, 0)
                return super().read(size)

        source = OversizedSource(b"a" * 11)
        source.test = self
        archive = unittest.mock.Mock()
        archive.open.return_value = source
        with self.assertRaisesRegex(ReleaseValidationError, "declared or allowed size"):
            validation._read_member(archive, member)

    def test_bad_crc_truncated_and_invalid_archives_are_validation_errors(self):
        self.write_wheel()
        original = self.wheel.read_bytes()
        central = original.index(b"PK\x01\x02")
        bad_crc = bytearray(original)
        struct.pack_into("<I", bad_crc, central + 16, 0)
        for content in (b"not zip", original[:-30], bytes(bad_crc)):
            with self.subTest(length=len(content)):
                self.wheel.write_bytes(content)
                with self.assertRaises(ReleaseValidationError):
                    self.validate()

    def test_corrupt_deflate_stream_is_a_validation_error(self):
        self.write_wheel()
        content = bytearray(self.wheel.read_bytes())
        local = content.index(b"PK\x03\x04")
        name_length, extra_length = struct.unpack_from("<HH", content, local + 26)
        # A deflate block type of 3 is reserved and invalid.
        content[local + 30 + name_length + extra_length] = 0x07
        self.wheel.write_bytes(content)
        with self.assertRaises(ReleaseValidationError):
            self.validate()

    def test_missing_wheel_and_invalid_expected_arguments(self):
        with self.assertRaises(ReleaseValidationError):
            self.validate()
        self.write_wheel()
        for arguments in ({"name": "other"}, {"version": "../0.1.0"},
                          {"version": "0.1.0/path"}, {"license_bytes": "MIT"}):
            with self.subTest(arguments=arguments), self.assertRaises(ReleaseValidationError):
                self.validate(**arguments)
        for expected in ({}, {"other/cli.py": b""}, {"cater/cli.py": "text"},
                         {"cater/__pycache__/cli.pyc": b""}, {"cater/cli.pyo": b""}):
            with self.subTest(expected=list(expected)), self.assertRaises(ReleaseValidationError):
                validate_wheel(self.wheel, expected, name="cater", version="0.1.0", license_bytes=self.license)


if __name__ == "__main__":
    unittest.main()
