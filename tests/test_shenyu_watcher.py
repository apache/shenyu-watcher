#!/usr/bin/env python3
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Tuple


ROOT = Path(__file__).resolve().parents[1]
WATCHER = ROOT / "shenyu_watcher.py"
sys.path.insert(0, str(ROOT))
import shenyu_watcher  # noqa: E402


def write_jar(path: Path, group_id: str = "com.example", artifact_id: str = "demo",
              version: str = "1.0.0") -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"META-INF/maven/{group_id}/{artifact_id}/pom.properties",
            f"groupId={group_id}\nartifactId={artifact_id}\nversion={version}\n",
        )


class WatcherExitStatusTest(unittest.TestCase):
    def make_project(self) -> Tuple[Path, Path]:
        root = Path(tempfile.mkdtemp())
        lib = root / "target" / "demo-bin" / "lib"
        lib.mkdir(parents=True)
        (root / "LICENSE").write_text("Apache 2.0 licenses\n", encoding="utf-8")
        return root, lib

    def test_missing_declaration_returns_failure(self):
        root, lib = self.make_project()
        write_jar(lib / "demo-1.0.0.jar")
        result = shenyu_watcher.run_check(str(root), lib_dir=str(lib))
        self.assertEqual(len(result.unmatched), 1)
        completed = subprocess.run(
            [sys.executable, str(WATCHER), str(root)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(completed.returncode, 1)

    def test_unparseable_jar_returns_failure(self):
        root, lib = self.make_project()
        (lib / "vendor.jar").write_bytes(b"not a zip archive")
        result = shenyu_watcher.run_check(str(root), lib_dir=str(lib))
        self.assertEqual(result.parse_failures, ["vendor.jar"])
        completed = subprocess.run(
            [sys.executable, str(WATCHER), str(root)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(completed.returncode, 1)

    def test_declared_dependency_returns_success(self):
        root, lib = self.make_project()
        write_jar(lib / "demo-1.0.0.jar")
        (root / "LICENSE").write_text(
            "Apache 2.0 licenses\n    demo 1.0.0: https://example.com, Apache 2.0\n",
            encoding="utf-8",
        )
        completed = subprocess.run(
            [sys.executable, str(WATCHER), str(root)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
