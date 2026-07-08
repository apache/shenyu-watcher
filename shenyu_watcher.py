#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
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
#
"""
shenyu-watcher-python: Pre-build dependency license checker.

Checks that every Maven dependency (including transitive) is properly
declared in the project's LICENSE file — without needing to build or
package the project first.

Usage:
    python shenyu_watcher.py /path/to/maven/project [/path/to/LICENSE]

If LICENSE path is omitted, it defaults to <project>/LICENSE.
"""

import os
import re
import subprocess
import sys
import glob
import zipfile
import datetime
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Dependency:
    """A Maven dependency with resolved coordinates."""
    group_id: str
    artifact_id: str
    version: str
    classifier: str = ""  # e.g. "linux-x86_64", "jakarta"
    scope: str = ""

    @property
    def coordinate(self) -> str:
        if self.classifier:
            return f"{self.group_id}:{self.artifact_id}:{self.version}:{self.classifier}"
        return f"{self.group_id}:{self.artifact_id}:{self.version}"

    @property
    def jar_name(self) -> str:
        if self.classifier:
            return f"{self.artifact_id}-{self.version}-{self.classifier}.jar"
        return f"{self.artifact_id}-{self.version}.jar"

    def __hash__(self):
        return hash((self.group_id, self.artifact_id, self.version, self.classifier))

    def __eq__(self, other):
        if not isinstance(other, Dependency):
            return False
        return (self.group_id == other.group_id
                and self.artifact_id == other.artifact_id
                and self.version == other.version
                and self.classifier == other.classifier)


@dataclass
class FixEntry:
    """A single entry written to the LICENSE file by the fix action."""
    section: str          # LICENSE section header, e.g. "MIT licenses"
    dep: Dependency
    entry_line: str       # the formatted line written to LICENSE
    license_name: str     # display license name from the POM


@dataclass
class CheckResult:
    """Result of a license check run."""
    total: int = 0
    checked: int = 0
    skipped_project: int = 0
    matched: List[Dependency] = field(default_factory=list)
    unmatched: List[Dependency] = field(default_factory=list)
    skipped_scope: List[Dependency] = field(default_factory=list)
    parse_failures: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Maven dependency resolution
# ---------------------------------------------------------------------------

def _find_maven() -> str:
    """Locate the mvn executable."""
    # 1. Check common wrapper locations
    for wrapper in ("./mvnw", "mvnw"):
        if os.path.isfile(wrapper) and os.access(wrapper, os.X_OK):
            return wrapper

    # 2. Check PATH via shutil.which
    from shutil import which
    found = which("mvn")
    if found:
        return found

    # 3. Probe common installation paths (subprocess can't see shell functions)
    candidates = [
        os.path.expanduser("~/.local/share/maven-no-jansi/libexec/bin/mvn"),
        os.path.expanduser("~/apache-maven/bin/mvn"),
        "/usr/local/bin/mvn",
        "/opt/homebrew/bin/mvn",
        "/usr/bin/mvn",
    ]
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path

    # 4. Try M2_HOME / MAVEN_HOME env vars
    for env_var in ("M2_HOME", "MAVEN_HOME"):
        home = os.environ.get(env_var)
        if home:
            mvn_bin = os.path.join(home, "bin", "mvn")
            if os.path.isfile(mvn_bin) and os.access(mvn_bin, os.X_OK):
                return mvn_bin

    # 5. Fallback — let subprocess try and produce a clear error
    return "mvn"


def _run_maven_dependency_list(project_dir: str) -> str:
    """Run `mvn dependency:list` and return the output."""
    mvn = _find_maven()

    cmd = [
        mvn,
        "dependency:list",
        "-DincludeScope=runtime",
        "-f", project_dir,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300
        )
    except FileNotFoundError:
        print(f"❌ Maven not found at '{mvn}'. Install Maven or add mvnw to the project.", file=sys.stderr)
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print("❌ Maven dependency resolution timed out (5 min).", file=sys.stderr)
        sys.exit(1)

    if result.returncode != 0:
        print(f"❌ Maven failed (exit {result.returncode}):", file=sys.stderr)
        # Maven writes its [ERROR] diagnostics to STDOUT, not stderr — print both
        # so the actual failure reason is visible (stderr is often empty).
        if result.stdout:
            print(result.stdout, file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        sys.exit(1)

    return result.stdout


def _parse_dependency_list(output: str) -> List[Dependency]:
    """
    Parse `mvn dependency:list` output.

    Maven outputs lines in two formats depending on whether a classifier exists:

    Without classifier:
        [INFO]    groupId:artifactId:jar:version:scope
        e.g. org.apache.commons:commons-compress:jar:1.26.0:compile

    With classifier:
        [INFO]    groupId:artifactId:jar:classifier:version:scope
        e.g. io.netty:netty-tcnative-boringssl-static:jar:linux-x86_64:2.0.65.Final:compile

    We detect the classifier by checking if the 4th field looks like a version
    (starts with a digit). If it does, there's no classifier; if not, it's a classifier
    and the 5th field is the version.
    """
    deps = []
    # Match lines with [INFO] prefix — 5 or 6 colon-separated fields
    pattern_info = re.compile(
        r"^\s*\[INFO\]\s+"
        r"([a-zA-Z0-9_.-]+):([a-zA-Z0-9_.-]+):([a-zA-Z0-9_.-]+)"
        r":([a-zA-Z0-9_.-]+)(?::([a-zA-Z0-9_.-]+))?:(\w+)"
    )
    # Match lines without [INFO] prefix (e.g. from outputFile)
    pattern_plain = re.compile(
        r"^\s*"
        r"([a-zA-Z0-9_.-]+):([a-zA-Z0-9_.-]+):([a-zA-Z0-9_.-]+)"
        r":([a-zA-Z0-9_.-]+)(?::([a-zA-Z0-9_.-]+))?:(\w+)"
    )
    seen = set()

    for line in output.splitlines():
        m = pattern_info.match(line) or pattern_plain.match(line)
        if not m:
            continue

        group_id = m.group(1)
        artifact_id = m.group(2)
        # packaging = m.group(3)  # not used
        field4 = m.group(4)  # version or classifier
        field5 = m.group(5)  # version (if classifier present) or None
        scope = m.group(6)

        # If field5 is present, field4 is the classifier and field5 is the version.
        # If field5 is None, field4 is the version and there is no classifier.
        if field5 is not None:
            classifier = field4
            version = field5
        else:
            # Disambiguate: if field4 starts with a digit, it's a version (no classifier).
            # If field4 does NOT start with a digit, it's a classifier and version is missing
            # (shouldn't happen in practice, but handle gracefully).
            if field4 and field4[0].isdigit():
                version = field4
                classifier = ""
            else:
                classifier = field4
                version = ""

        dep = Dependency(
            group_id=group_id,
            artifact_id=artifact_id,
            version=version,
            classifier=classifier,
            scope=scope,
        )
        if dep not in seen:
            seen.add(dep)
            deps.append(dep)

    return deps


def resolve_dependencies(project_dir: str) -> List[Dependency]:
    """Resolve all runtime dependencies for a Maven project."""
    print("📦 Resolving Maven dependencies...")
    output = _run_maven_dependency_list(project_dir)
    deps = _parse_dependency_list(output)
    print(f"   Found {len(deps)} unique runtime dependencies.")
    return deps


def resolve_dependencies_from_pom(project_dir: str) -> List[Dependency]:
    """
    Fallback: parse pom.xml for direct dependencies only.

    This does NOT resolve transitive dependencies, so it's less accurate
    than using `mvn dependency:list`. Used when Maven is not available.
    Uses regex instead of XML parser for broader compatibility.
    """
    pom_path = os.path.join(project_dir, "pom.xml")
    if not os.path.isfile(pom_path):
        print(f"❌ pom.xml not found at {pom_path}", file=sys.stderr)
        sys.exit(1)

    try:
        with open(pom_path, "r", encoding="utf-8") as f:
            content = f.read()
    except IOError as e:
        print(f"❌ Failed to read pom.xml: {e}", file=sys.stderr)
        sys.exit(1)

    deps = []
    seen = set()

    # Find all <dependency> blocks
    dep_pattern = re.compile(
        r"<dependency>\s*(.*?)\s*</dependency>", re.DOTALL
    )
    for match in dep_pattern.finditer(content):
        block = match.group(1)

        def _extract(tag):
            m = re.search(rf"<{tag}>\s*([^<]+?)\s*</{tag}>", block)
            return m.group(1).strip() if m else ""

        group_id = _extract("groupId")
        artifact_id = _extract("artifactId")
        version = _extract("version")
        classifier = _extract("classifier")
        scope = _extract("scope") or "compile"

        if not group_id or not artifact_id:
            continue

        dep = Dependency(
            group_id=group_id,
            artifact_id=artifact_id,
            version=version,
            classifier=classifier,
            scope=scope,
        )
        if dep not in seen:
            seen.add(dep)
            deps.append(dep)

    return deps


# ---------------------------------------------------------------------------
# Dependency resolution from an assembled lib/ directory
# ---------------------------------------------------------------------------
#
# For distribution modules (e.g. shenyu-admin-dist) the project cannot always
# be resolved with `mvn dependency:list` — a pom-packaged aggregator may depend
# on sibling reactor modules that aren't installed in the local repo. But the
# assembled distribution already ships a `lib/` directory containing the exact
# set of runtime jars that will be released, so scanning it is both faster and
# more accurate than running Maven.
#
# Each jar's GAV is read preferentially from META-INF/maven/<gid>/<aid>/pom.properties
# (exact, zero ambiguity). Jars without pom.properties fall back to filename
# parsing — pom.properties-first is essential because the filename can be
# misleading, e.g. `jakarta.mail-2.0.3.jar` is really `jakarta.mail-api:2.1.3`,
# and `guava-32.0.0-jre.jar` has version `32.0.0-jre` (no classifier). The
# classifier is never recorded in pom.properties, so it is derived from the
# filename in both paths.


def _parse_pom_properties(text: str) -> Tuple[str, str, str]:
    """
    Parse a Java Properties-format pom.properties string.

    Returns (groupId, artifactId, version), empty strings for any missing key.
    Skips blank lines and full-line `#` comments (Java Properties format).
    """
    gid = aid = ver = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key == "groupId":
            gid = value
        elif key == "artifactId":
            aid = value
        elif key == "version":
            ver = value
    return gid, aid, ver


def _derive_classifier(filename: str, artifact_id: str, version: str) -> str:
    """
    Derive the classifier from a jar filename given a known artifactId and version
    (used in the pom.properties path, where the classifier isn't recorded).

    Filename layout: `<artifactId>-<version>[-<classifier>].jar`
    Returns "" when there is no classifier or when the version doesn't match the
    filename (e.g. jakarta.mail-2.0.3.jar with pom.properties version 2.1.3).
    """
    stem = filename[:-4] if filename.endswith(".jar") else filename  # strip .jar
    prefix = artifact_id + "-"
    if not stem.startswith(prefix):
        return ""
    rest = stem[len(prefix):]  # "version" or "version-classifier"
    if rest == version:
        return ""
    if version and rest.startswith(version + "-"):
        return rest[len(version) + 1:]
    return ""  # version mismatch — cannot reliably derive classifier


def _parse_jar_filename(filename: str) -> Tuple[str, str, str]:
    """
    Filename-only fallback for jars without pom.properties.

    Splits on `-`: the first segment starting with a digit is the version,
    segments before it (joined by `-`) are the artifactId, segments after it
    (joined by `-`) are the classifier. Returns (artifactId, version, classifier);
    on failure to locate a version segment, returns ("", stem, "").
    """
    stem = filename[:-4] if filename.endswith(".jar") else filename
    segments = stem.split("-")
    vidx = None
    for i, seg in enumerate(segments):
        if seg and seg[0].isdigit():
            vidx = i
            break
    if vidx is None:
        return "", stem, ""
    artifact_id = "-".join(segments[:vidx])
    version = segments[vidx]
    classifier = "-".join(segments[vidx + 1:]) if vidx + 1 < len(segments) else ""
    return artifact_id, version, classifier


def _extract_gav_from_jar(jar_path: str) -> Optional[Tuple[str, str, str, str]]:
    """
    Extract (groupId, artifactId, version, classifier) from a jar.

    Prefers META-INF/maven/*/pom.properties (exact GAV; classifier derived from
    the filename). Falls back to filename parsing with groupId="" when no
    pom.properties is present or the jar cannot be read as a zip.
    """
    filename = os.path.basename(jar_path)
    pom_text = None
    try:
        with zipfile.ZipFile(jar_path) as zf:
            for name in zf.namelist():
                if name.startswith("META-INF/maven/") and name.endswith("/pom.properties"):
                    pom_text = zf.read(name).decode("utf-8", "replace")
                    break
    except (zipfile.BadZipFile, OSError):
        pom_text = None

    if pom_text:
        gid, aid, ver = _parse_pom_properties(pom_text)
        if aid and ver:
            classifier = _derive_classifier(filename, aid, ver)
            return gid, aid, ver, classifier

    # Fallback: parse the filename. groupId is unknown.
    aid, ver, classifier = _parse_jar_filename(filename)
    return "", aid, ver, classifier


def resolve_dependencies_from_lib(lib_dir: str) -> List[Dependency]:
    """
    Resolve dependencies by scanning the jars in an assembled lib/ directory.

    Each jar's GAV is read from its embedded pom.properties when available,
    otherwise parsed from the filename (groupId unknown in that case).
    """
    lib_dir = os.path.abspath(lib_dir)
    if not os.path.isdir(lib_dir):
        print(f"❌ lib directory not found: {lib_dir}", file=sys.stderr)
        sys.exit(1)

    jars = sorted(glob.glob(os.path.join(lib_dir, "*.jar")))
    if not jars:
        print(f"⚠️  No .jar files found in {lib_dir}", file=sys.stderr)

    print("📦 Scanning JARs in lib directory...")
    deps: List[Dependency] = []
    seen = set()
    n_props = 0
    n_filename = 0

    for jar_path in jars:
        gav = _extract_gav_from_jar(jar_path)
        if gav is None:
            continue
        gid, aid, ver, classifier = gav
        if not aid or not ver:
            # Could not parse at all — skip rather than emit garbage.
            continue

        if gid:
            n_props += 1
        else:
            n_filename += 1

        dep = Dependency(
            group_id=gid,
            artifact_id=aid,
            version=ver,
            classifier=classifier,
            scope="compile",  # lib/ ships only runtime jars
        )
        if dep not in seen:
            seen.add(dep)
            deps.append(dep)

    print(f"   Found {len(deps)} unique dependencies "
          f"({n_props} from pom.properties, {n_filename} from filename).")
    if n_filename:
        print(f"   ({n_filename} jars had no pom.properties — resolved from filename, "
              f"groupId unknown; license auto-fix will skip these.)")
    return deps


def resolve_lib_dir(project_dir: str, explicit_lib: Optional[str]) -> Optional[str]:
    """
    Determine the lib/ directory to scan.

    If an explicit path is given, validate and use it. Otherwise auto-detect
    under <project_dir>/target/*-bin/lib then target/*/lib (version-agnostic,
    matching distribution output dirs like apache-shenyu-2.7.1-admin-bin/lib).
    Returns None when no lib dir is found.
    """
    if explicit_lib:
        p = os.path.abspath(explicit_lib)
        if not os.path.isdir(p):
            print(f"❌ --lib path is not a directory: {p}", file=sys.stderr)
            sys.exit(1)
        return p

    for pattern in ("*-bin", "*"):
        for cand in glob.glob(os.path.join(project_dir, "target", pattern, "lib")):
            if os.path.isdir(cand):
                return cand
    return None


# ---------------------------------------------------------------------------
# License file handling
# ---------------------------------------------------------------------------

def _find_license_file(project_dir: str) -> Optional[str]:
    """
    Auto-locate the LICENSE file in common locations.

    Search order:
      1. <project_dir>/LICENSE
      2. <project_dir>/src/main/release-docs/LICENSE   (ShenYu dist layout)
      3. <project_dir>/target/maven-shared-archive-resources/META-INF/LICENSE
      4. Any LICENSE file found under src/
    """
    candidates = [
        os.path.join(project_dir, "LICENSE"),
        os.path.join(project_dir, "src", "main", "release-docs", "LICENSE"),
        os.path.join(project_dir, "target", "maven-shared-archive-resources",
                      "META-INF", "LICENSE"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path

    # Last resort: find any LICENSE under src/
    for root, dirs, files in os.walk(os.path.join(project_dir, "src")):
        if "LICENSE" in files:
            return os.path.join(root, "LICENSE")

    return None


def read_license(license_path: str) -> str:
    """Read the LICENSE file content."""
    if not os.path.isfile(license_path):
        print(f"❌ LICENSE file not found at {license_path}", file=sys.stderr)
        sys.exit(1)

    with open(license_path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def extract_declaration_section(license_content: str) -> str:
    """
    Extract only the dependency declaration section from a LICENSE file.

    Apache-style LICENSE files have a standard structure:
      - Top: Apache License 2.0 full text (boilerplate)
      - Bottom: "Subcomponents" / "Third-party" declarations

    The boilerplate contains common English words like "annotations",
    "distribution", "license" etc. that can cause false matches.

    We find the declaration section by looking for common section markers.
    If none found, we return the full content (for non-Apache projects).
    """
    # Common markers that indicate the start of dependency declarations
    markers = [
        "=======================================================================\n",
        "Subcomponents:",
        "Third-party",
        "third-party",
        "3rd party",
        "The following",
        "This product includes",
        "This product bundles",
    ]

    best_pos = len(license_content)

    for marker in markers:
        pos = license_content.find(marker)
        if pos != -1 and pos < best_pos:
            best_pos = pos

    # If we found a marker, return from that point onward
    if best_pos < len(license_content):
        return license_content[best_pos:]

    # No marker found — return the full content
    return license_content


# ---------------------------------------------------------------------------
# License matching
# ---------------------------------------------------------------------------

def check_dependency_in_license(dep: Dependency, license_content: str) -> bool:
    """
    Check whether a dependency is declared in the LICENSE file.

    Uses the declaration section only (skipping license boilerplate) to
    avoid false positives from common English words in the license text.

    Matching strategies (in order of precision):
      1. "groupId:artifactId:version"           (Maven coordinate — most precise)
      2. "groupId artifactId version"           (space-separated coordinate)
      3. "artifactId-version-classifier.jar"    (jar filename with classifier)
      4. "artifactId version-classifier"        (version+classifier together)
      5. "artifactId-version.jar"               (jar filename without classifier)
      6. "artifactId version"                   (what the Java tool used)
    """
    # Only search in the declaration section to avoid false positives
    search_area = extract_declaration_section(license_content)
    normalized_area = " ".join(search_area.split())

    # Strategy 1: Maven coordinate (groupId:artifactId:version)
    coord = f"{dep.group_id}:{dep.artifact_id}:{dep.version}"
    if _literal_in(coord, normalized_area):
        return True

    # Strategy 2: groupId + artifactId + version (space-separated)
    gav = f"{dep.group_id} {dep.artifact_id} {dep.version}"
    if _literal_in(gav, normalized_area):
        return True

    # Strategy 3: jar filename with classifier (artifactId-version-classifier.jar)
    if dep.classifier:
        if _literal_in(dep.jar_name, normalized_area):
            return True

        # Strategy 4: artifactId + version-classifier (e.g. "shiro-core 1.13.0-jakarta")
        av_classifier = f"{dep.artifact_id} {dep.version}-{dep.classifier}"
        if _literal_in(av_classifier, normalized_area):
            return True

        # Strategy 4b: artifactId + classifier + version (alternate format)
        av_alt = f"{dep.artifact_id} {dep.classifier} {dep.version}"
        if _literal_in(av_alt, normalized_area):
            return True

    # Strategy 5: jar filename without classifier (artifactId-version.jar)
    jar_no_classifier = f"{dep.artifact_id}-{dep.version}.jar"
    if _literal_in(jar_no_classifier, normalized_area):
        return True

    # Strategy 6: artifactId + version (original Java tool's approach)
    av = f"{dep.artifact_id} {dep.version}"
    if _literal_in(av, normalized_area):
        return True

    return False


def _literal_in(target: str, normalized_text: str) -> bool:
    """Check if target appears in pre-normalized text as a literal string."""
    normalized_target = " ".join(target.split())
    return normalized_target in normalized_text


# ---------------------------------------------------------------------------
# POM license resolution via Maven Central
# ---------------------------------------------------------------------------

MAVEN_CENTRAL_BASE = "https://repo1.maven.org/maven2"

# In-memory cache: (groupId, artifactId, version) -> license_info dict
_pom_cache: Dict[Tuple[str, str, str], dict] = {}


def _group_id_to_path(group_id: str) -> str:
    """Convert groupId to Maven repo path: org.apache.commons -> org/apache/commons"""
    return group_id.replace(".", "/")


def _fetch_url(url: str, timeout: int = 10) -> Optional[str]:
    """Fetch a URL and return its text content, or None on failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "shenyu-watcher/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        return None


# In-memory cache: artifactId -> resolved groupId (for jars without pom.properties)
_group_id_cache: Dict[Tuple[str, str], Optional[str]] = {}


def _pom_exists(group_id: str, artifact_id: str, version: str) -> bool:
    """Check whether a POM exists on Maven Central for the given coordinates."""
    gpath = _group_id_to_path(group_id)
    url = (f"{MAVEN_CENTRAL_BASE}/{gpath}/{artifact_id}/{version}/"
           f"{artifact_id}-{version}.pom")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "shenyu-watcher/1.0"},
                                     method="HEAD")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        return False


def _resolve_group_id_from_central(artifact_id: str, version: str) -> Optional[str]:
    """
    Reverse-lookup a dependency's groupId on Maven Central when only the
    artifactId + version are known (jar had no pom.properties).

    Two-tier strategy:
      1. Search by `a:<artifactId> AND v:<version>` — precise when indexed.
      2. If that misses (Maven Central's search index lags behind newly
         published versions), list candidates by `a:<artifactId>` and verify
         each candidate groupId by checking the POM exists at that version.
    Returns the resolved groupId, or None if it can't be determined.
    """
    cache_key = (artifact_id, version)
    if cache_key in _group_id_cache:
        return _group_id_cache[cache_key]

    import json as _json

    def _search(query: str) -> List[str]:
        url = (f"https://search.maven.org/solrsearch/select?q={query}"
               f"&rows=20&wt=json")
        text = _fetch_url(url)
        if not text:
            return []
        try:
            docs = _json.loads(text).get("response", {}).get("docs", [])
            return [d.get("g") for d in docs if d.get("g")]
        except (ValueError, AttributeError):
            return []

    # Tier 1: exact artifactId + version
    candidates = _search(f"a:{artifact_id}+AND+v:{version}")
    if candidates:
        _group_id_cache[cache_key] = candidates[0]
        return candidates[0]

    # Tier 2: list by artifactId, verify each candidate's POM exists at this version
    for gid in _search(f"a:{artifact_id}"):
        if gid and _pom_exists(gid, artifact_id, version):
            _group_id_cache[cache_key] = gid
            return gid

    _group_id_cache[cache_key] = None
    return None


def _parse_pom_licenses(pom_text: str) -> List[dict]:
    """Extract <licenses> section from POM XML text using regex."""
    licenses = []
    # Find all <license> blocks
    for m in re.finditer(r"<license>\s*(.*?)\s*</license>", pom_text, re.DOTALL):
        block = m.group(1)
        name = ""
        url = ""
        for tag in ("name", "url"):
            tm = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", block, re.DOTALL)
            if tm:
                val = tm.group(1).strip()
                if tag == "name":
                    name = val
                else:
                    url = val
        if name:
            licenses.append({"name": name, "url": url})
    return licenses


def _parse_pom_url(pom_text: str) -> str:
    """Extract <url> from POM (project URL, not license URL)."""
    m = re.search(r"<url>\s*(.*?)\s*</url>", pom_text)
    if m:
        return m.group(1).strip()
    return ""


def _parse_pom_scm_url(pom_text: str) -> str:
    """Extract <scm><url> from POM."""
    m = re.search(r"<scm>.*?<url>\s*(.*?)\s*</url>.*?</scm>", pom_text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return ""


def _parse_pom_parent(pom_text: str) -> Optional[Tuple[str, str, str]]:
    """Extract <parent><groupId><artifactId><version> from POM."""
    parent_match = re.search(r"<parent>\s*(.*?)\s*</parent>", pom_text, re.DOTALL)
    if not parent_match:
        return None
    block = parent_match.group(1)
    gid = aid = ver = ""
    for tag, attr in [("groupId", "gid"), ("artifactId", "aid"), ("version", "ver")]:
        tm = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", block)
        if tm:
            val = tm.group(1).strip()
            if attr == "gid":
                gid = val
            elif attr == "aid":
                aid = val
            else:
                ver = val
    if gid and aid and ver:
        return (gid, aid, ver)
    return None


def _is_likely_fork_url(url: str, group_id: str, artifact_id: str) -> bool:
    """
    Heuristic: detect if a GitHub URL likely points to a fork rather than the
    canonical upstream project.

    A URL is considered a likely fork when the GitHub owner/organization
    doesn't correspond to the project name or groupId. For example:
      - at.yawk.lz4:lz4-java → https://github.com/yawkat/lz4-java (fork)
      - org.lz4:lz4-java → https://github.com/lz4/lz4-java (canonical)

    Returns True if the URL looks like a fork.
    """
    m = re.match(r"https?://github\.com/([^/]+)/([^/\s?#]+)", url)
    if not m:
        return False
    owner = m.group(1).lower()
    repo = m.group(2).lower()
    gid_lower = group_id.lower()
    aid_lower = artifact_id.lower()

    # The owner should relate to the groupId or artifactId.
    # Common patterns for canonical projects:
    #   groupId "org.lz4" → owner "lz4"
    #   groupId "com.google.guava" → owner "google"
    #   groupId "io.netty" → owner "netty"
    # A fork typically has a personal username that doesn't match.

    # Extract the last segment of groupId (e.g., "lz4" from "at.yawk.lz4")
    gid_last = gid_lower.rsplit(".", 1)[-1]
    # Also extract second-to-last if exists (e.g., "yawk" from "at.yawk.lz4")
    gid_parts = gid_lower.split(".")

    # If owner matches any part of groupId or artifactId, it's likely canonical
    if owner in gid_parts or owner == gid_last or owner == aid_lower:
        return False

    # If owner matches the repo name (common for orgs like netty/netty, lz4/lz4-java)
    if owner == repo.split("-")[0].replace(".", ""):
        return False

    # If owner is a known org domain segment (google, apache, spring-projects, etc.)
    known_orgs = {
        "google", "apache", "spring-projects", "eclipse", "netty",
        "alibaba", "facebook", "twitter", "linkedin", "netflix",
        "oracle", "microsoft", "aws", "cloudflare",
    }
    if owner in known_orgs:
        return False

    # Otherwise, it's likely a personal fork
    return True


def fetch_pom_license(dep: Dependency, max_parent_depth: int = 3) -> dict:
    """
    Fetch license info for a dependency from Maven Central POM.

    Resolves parent POMs recursively (up to max_parent_depth levels).
    Returns dict with keys: license_name, license_url, project_url
    """
    cache_key = (dep.group_id, dep.artifact_id, dep.version)
    if cache_key in _pom_cache:
        return _pom_cache[cache_key]

    # Without a groupId we cannot build a Maven Central URL (the jar had no
    # pom.properties and was resolved from its filename). Return a sentinel so
    # the caller can skip auto-append rather than silently defaulting to Apache 2.0.
    if not dep.group_id:
        result = {
            "license_name": "Unknown (no groupId — manual lookup required)",
            "license_url": "",
            "project_url": f"https://mvnrepository.com/artifact/{dep.artifact_id}",
        }
        _pom_cache[cache_key] = result
        return result

    result = {
        "license_name": "Apache 2.0",  # sensible default
        "license_url": "",
        "project_url": f"https://mvnrepository.com/artifact/{dep.group_id}/{dep.artifact_id}",
    }

    pom_text = None
    parent_info = None

    # Try to fetch the artifact's own POM
    gpath = _group_id_to_path(dep.group_id)
    pom_url = f"{MAVEN_CENTRAL_BASE}/{gpath}/{dep.artifact_id}/{dep.version}/{dep.artifact_id}-{dep.version}.pom"
    pom_text = _fetch_url(pom_url)

    if pom_text:
        # Extract project URL and SCM URL
        # Prefer <url> (project homepage) over <scm><url> (may point to a fork)
        project_url = _parse_pom_url(pom_text)
        scm_url = _parse_pom_scm_url(pom_text)
        if scm_url:
            # Clean up SCM URLs (git scm:git:https://... -> https://...)
            scm_url = re.sub(r"^scm:git:", "", scm_url)
            if scm_url.endswith(".git"):
                scm_url = scm_url[:-4]

        # Pick the best URL: <url> preferred, <scm><url> as fallback
        chosen_url = project_url or scm_url or ""

        # Heuristic: if the GitHub URL owner doesn't look like the canonical project,
        # fall back to mvnrepository which always shows the correct upstream link.
        # E.g. at.yawk.lz4:lz4-java has <url>https://github.com/yawkat/lz4-java</url>
        # but the upstream is https://github.com/lz4/lz4-java
        mvn_url = f"https://mvnrepository.com/artifact/{dep.group_id}/{dep.artifact_id}"
        if chosen_url and _is_likely_fork_url(chosen_url, dep.group_id, dep.artifact_id):
            chosen_url = mvn_url

        if chosen_url:
            result["project_url"] = chosen_url

        # Extract licenses
        licenses = _parse_pom_licenses(pom_text)
        if licenses:
            result["license_name"] = licenses[0]["name"]
            result["license_url"] = licenses[0].get("url", "")
            _pom_cache[cache_key] = result
            return result

        # No licenses in this POM — try parent
        parent_info = _parse_pom_parent(pom_text)

    # Resolve parent POMs
    depth = 0
    while parent_info and depth < max_parent_depth:
        p_gid, p_aid, p_ver = parent_info
        p_cache_key = (p_gid, p_aid, p_ver)
        if p_cache_key in _pom_cache:
            parent_result = _pom_cache[p_cache_key]
            result["license_name"] = parent_result["license_name"]
            result["license_url"] = parent_result["license_url"]
            if not result["project_url"].startswith("https://mvnrepository.com"):
                pass  # keep existing project_url
            else:
                result["project_url"] = parent_result.get("project_url", result["project_url"])
            break

        p_gpath = _group_id_to_path(p_gid)
        p_pom_url = f"{MAVEN_CENTRAL_BASE}/{p_gpath}/{p_aid}/{p_ver}/{p_aid}-{p_ver}.pom"
        p_pom_text = _fetch_url(p_pom_url)

        if not p_pom_text:
            break

        # Extract licenses from parent
        p_licenses = _parse_pom_licenses(p_pom_text)
        if p_licenses:
            result["license_name"] = p_licenses[0]["name"]
            result["license_url"] = p_licenses[0].get("url", "")
            # Also try to get project URL from parent
            if result["project_url"].startswith("https://mvnrepository.com"):
                p_project_url = _parse_pom_url(p_pom_text)
                p_scm_url = _parse_pom_scm_url(p_pom_text)
                if p_scm_url:
                    p_scm_url = re.sub(r"^scm:git:", "", p_scm_url)
                    if p_scm_url.endswith(".git"):
                        p_scm_url = p_scm_url[:-4]
                # Prefer project <url> over <scm><url>
                if p_project_url:
                    result["project_url"] = p_project_url
                elif p_scm_url:
                    result["project_url"] = p_scm_url
            _pom_cache[p_cache_key] = {
                "license_name": p_licenses[0]["name"],
                "license_url": p_licenses[0].get("url", ""),
                "project_url": result["project_url"],
            }
            break

        # Try next parent level
        parent_info = _parse_pom_parent(p_pom_text)
        depth += 1

    _pom_cache[cache_key] = result
    return result


# ---------------------------------------------------------------------------
# License section mapping
# ---------------------------------------------------------------------------

# Mapping: keyword in POM license name -> LICENSE section header
# Order matters: more specific patterns first, then general ones as fallback.
LICENSE_SECTION_MAP = [
    ("bouncy castle", "Bouncy Castle licenses"),
    ("cddl", "CDDL licenses"),
    ("cc0", "CC0 licenses"),
    ("creative commons cc0", "CC0 licenses"),
    ("eclipse distribution license", "EDL License"),
    ("edl", "EDL License"),
    ("epl", "EPL licenses"),
    ("eclipse public license", "EPL licenses"),
    ("isc", "ISC licenses"),
    ("mpl", "MPL licenses"),
    ("mozilla public license", "MPL licenses"),
    ("public domain", "Public Domain licenses"),
    ("wtfpl", "WTFPL License"),
    ("xpp", "XPP License"),
    ("xml pull parser", "XPP License"),
    # "Go License" is used by re2j and other Google projects — it is BSD 3-Clause
    ("go license", "BSD licenses"),
    ("lgpl", "LGPL licenses"),
    ("lesser general public license", "LGPL licenses"),
    ("gpl", "GPL licenses"),
    ("general public license", "GPL licenses"),
    ("bsd", "BSD licenses"),
    ("mit", "MIT licenses"),
    ("apache", "Apache 2.0 licenses"),
]


def map_license_to_section(license_name: str) -> str:
    """Map a POM license name to the LICENSE section header."""
    lower = license_name.lower()
    for keyword, section in LICENSE_SECTION_MAP:
        if keyword in lower:
            return section
    # Unknown license — warn and default to Apache 2.0 (most common in Java ecosystem)
    print(f"  {YELLOW}⚠️  Unknown license '{license_name}', defaulting to Apache 2.0 — please verify manually{RESET}")
    return "Apache 2.0 licenses"


# ---------------------------------------------------------------------------
# LICENSE file fix logic
# ---------------------------------------------------------------------------

def format_license_entry(dep: Dependency, license_info: dict) -> str:
    """
    Format a single LICENSE entry line.

    Format: "    <artifactId> <version>[-<classifier>]: <url>, <LicenseName>"
    """
    version_display = f"{dep.version}-{dep.classifier}" if dep.classifier else dep.version
    project_url = license_info.get("project_url", "")
    license_name = license_info.get("license_name", "Apache 2.0")

    # Clean up license name for display
    # Common POM names -> shorter LICENSE-friendly forms
    name_cleanups = {
        "Apache License, Version 2.0": "Apache 2.0",
        "The Apache Software License, Version 2.0": "Apache 2.0",
        "Apache License 2.0": "Apache 2.0",
        "Apache License Version 2.0": "Apache 2.0",
        "Apache-2.0": "Apache 2.0",
        "The MIT License": "MIT",
        "MIT License": "MIT",
        "SPDX-License-Identifier: MIT": "MIT",
        "BSD-2-Clause": "BSD 2-Clause",
        "BSD-3-Clause": "BSD 3-Clause",
        "The BSD License": "BSD",
        "Go License": "BSD 3-Clause",
        "Eclipse Public License - v 2.0": "EPL 2.0",
        "Eclipse Public License v1.0": "EPL 1.0",
        "Eclipse Distribution License - v 1.0": "EDL 1.0",
        "Mozilla Public License, Version 2.0": "MPL 2.0",
        "COMMON DEVELOPMENT AND DISTRIBUTION LICENSE (CDDL) Version 1.0": "CDDL 1.0",
        "Bouncy Castle Licence": "Bouncy Castle",
        "Public Domain, per Creative Commons CC0": "CC0 1.0",
    }

    display_name = name_cleanups.get(license_name, license_name)

    if project_url:
        return f"    {dep.artifact_id} {version_display}: {project_url}, {display_name}"
    else:
        mvn_url = f"https://mvnrepository.com/artifact/{dep.group_id}/{dep.artifact_id}"
        return f"    {dep.artifact_id} {version_display}: {mvn_url}, {display_name}"


def find_section_range(license_content: str, section_header: str) -> Optional[Tuple[int, int]]:
    """
    Find the start and end line indices of a section in the LICENSE file.

    A section is delimited by lines of '=' characters with the header text between.
    Returns (section_start_line, section_end_line) or None if not found.
    """
    lines = license_content.split("\n")
    section_start = None
    separator_pattern = re.compile(r"^={10,}$")

    # Find the section header line
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == section_header:
            # The header line is between two separator lines
            # section_start is the line of the first '=' separator
            section_start = i - 1  # the '=' line above the header
            break

    if section_start is None or section_start < 0:
        return None

    # Find the end: the next separator line after the header's closing separator
    # The header line is at section_start + 1, closing separator at section_start + 2
    # Content starts at section_start + 3 (after blank line)
    # End is the next line starting with '=' or EOF
    section_end = len(lines)  # default to EOF
    for i in range(section_start + 3, len(lines)):
        if separator_pattern.match(lines[i].strip()):
            section_end = i
            break

    return (section_start, section_end)


def fix_license_file(license_path: str, result: CheckResult) -> List[FixEntry]:
    """
    Automatically append missing dependency entries to the LICENSE file.

    For each unmatched dependency:
    1. Fetches license info from Maven Central POM
    2. Maps the license to the correct LICENSE section
    3. Formats and appends the entry

    Returns the list of entries actually written (excluding duplicates that
    were already present). Callers (e.g. the --pr flow) use this to build the
    commit message and PR body.
    """
    if not result.unmatched:
        print(f"\n{GREEN}No missing entries to fix.{RESET}")
        return []

    # Read current LICENSE
    license_content = read_license(license_path)

    # Group unmatched deps by their target section
    from collections import defaultdict
    section_deps = defaultdict(list)  # section_header -> [(dep, entry_line)]

    print(f"🔍 Resolving license info for {len(result.unmatched)} missing dependencies...")

    for dep in sorted(result.unmatched, key=lambda d: (d.group_id, d.artifact_id)):
        if not dep.group_id:
            # The jar had no pom.properties — try to recover the groupId from
            # Maven Central by artifactId + version so we can still auto-fix it.
            resolved = _resolve_group_id_from_central(dep.artifact_id, dep.version)
            if resolved:
                print(f"   {dep.artifact_id} {dep.version}: groupId resolved from Maven Central "
                      f"-> {resolved}")
                dep.group_id = resolved
            else:
                print(f"  {YELLOW}⚠️  Skipping auto-fix for {dep.artifact_id} {dep.version}: "
                      f"groupId unknown (no pom.properties in jar, and not found on Maven Central). "
                      f"Add manually.{RESET}")
                continue
        license_info = fetch_pom_license(dep)
        section = map_license_to_section(license_info["license_name"])
        entry_line = format_license_entry(dep, license_info)
        section_deps[section].append((dep, entry_line, license_info))
        license_display = license_info.get("license_name", "unknown")
        print(f"   {dep.artifact_id} {dep.version} → {section} ({license_display})")

    if not section_deps:
        print(f"\n{YELLOW}No entries could be auto-fixed (all missing deps lack a groupId).{RESET}")
        return []

    # Now modify the LICENSE file
    lines = license_content.split("\n")
    total_added = 0
    written: List[FixEntry] = []

    # Process sections — we need to insert from bottom to top to preserve line numbers
    # First, collect all insertion points
    insertions = []  # (line_index, entries_to_add)

    for section_header, dep_entries in sorted(section_deps.items()):
        section_range = find_section_range(license_content, section_header)

        if section_range is None:
            # Section doesn't exist yet — we'll need to create it
            # Append at the end of the file
            insert_point = len(lines)
            insertions.append((insert_point, section_header, dep_entries, True))
        else:
            # Find the last non-empty line within the section
            start, end = section_range
            # Find last content line (skip trailing blank lines)
            last_content = end - 1
            while last_content > start and lines[last_content].strip() == "":
                last_content -= 1
            insertions.append((last_content + 1, section_header, dep_entries, False))

    # Sort insertions by line index descending (insert from bottom to top)
    insertions.sort(key=lambda x: x[0], reverse=True)

    for insert_idx, section_header, dep_entries, is_new_section in insertions:
        new_lines = []
        if is_new_section:
            # Create a new section at the end
            # Derive a clean license name from the section header by stripping a
            # trailing " licenses"/" License" suffix (e.g. "LGPL licenses" -> "LGPL").
            # Use suffix removal, NOT str.rstrip (which strips a character set and
            # would mangle "LGPL licenses" into "LGPL l").
            lic_name = section_header
            for suffix in (" licenses", " License", " licenses:"):
                if lic_name.endswith(suffix):
                    lic_name = lic_name[:-len(suffix)]
                    break
            new_lines.append("")  # blank line before section
            new_lines.append("=" * 72)
            new_lines.append(section_header)
            new_lines.append("=" * 72)
            new_lines.append("")
            new_lines.append(f"The following components are provided under a {lic_name} license. "
                           f"See project link for details.")
            new_lines.append("")

        for dep, entry_line, license_info in dep_entries:
            # Check if this exact entry already exists (avoid duplicates)
            if entry_line.strip() not in license_content:
                new_lines.append(entry_line)
                total_added += 1
                written.append(FixEntry(
                    section=section_header,
                    dep=dep,
                    entry_line=entry_line,
                    license_name=license_info.get("license_name", ""),
                ))

        if is_new_section and new_lines:
            # Ensure we add at the end
            lines.extend(new_lines)
        elif new_lines:
            # Insert at the correct position
            for i, line in enumerate(new_lines):
                lines.insert(insert_idx + i, line)

    # Write updated LICENSE
    updated_content = "\n".join(lines)
    # Clean up trailing whitespace but preserve final newline
    updated_content = updated_content.rstrip("\n") + "\n"

    with open(license_path, "w", encoding="utf-8") as f:
        f.write(updated_content)

    print(f"\n{GREEN}{BOLD}✅ Added {total_added} entr{'y' if total_added == 1 else 'ies'} to LICENSE{RESET}")
    print(f"   File updated: {license_path}")

    # Show a summary of what was added
    print(f"\n{BOLD}Added entries by section:{RESET}")
    for section_header, dep_entries in sorted(section_deps.items()):
        print(f"  {section_header}:")
        for dep, entry_line, license_info in dep_entries:
            print(f"    + {entry_line.strip()}")

    return written


# ---------------------------------------------------------------------------
# Main check logic
# ---------------------------------------------------------------------------


def run_check(project_dir: str, license_path: Optional[str] = None,
              lib_dir: Optional[str] = None, no_lib: bool = False) -> CheckResult:
    """
    Run the full license check.

    Args:
        project_dir: Path to the Maven project root (containing pom.xml).
        license_path: Path to the LICENSE file. Defaults to <project_dir>/LICENSE.
        lib_dir: Explicit path to an assembled lib/ directory to scan instead of
            running Maven. When None and no_lib is False, auto-detected under
            <project_dir>/target/*-bin/lib.
        no_lib: If True, disable lib-directory scanning and force Maven.

    Returns:
        CheckResult with matched/unmatched/skipped details.
    """
    project_dir = os.path.abspath(project_dir)

    # 1. Resolve dependencies — prefer an assembled lib/ dir (fast, accurate,
    #    and works for distribution modules Maven can't resolve), then Maven,
    #    then pom.xml parsing as a last-resort fallback.
    resolved_lib = None if no_lib else resolve_lib_dir(project_dir, lib_dir)
    if resolved_lib:
        deps = resolve_dependencies_from_lib(resolved_lib)
    else:
        try:
            deps = resolve_dependencies(project_dir)
        except SystemExit:
            print("⚠️  Maven resolution failed, falling back to pom.xml parsing (direct deps only).")
            deps = resolve_dependencies_from_pom(project_dir)
        except Exception:
            print("⚠️  Maven resolution failed, falling back to pom.xml parsing (direct deps only).")
            deps = resolve_dependencies_from_pom(project_dir)

    if not deps:
        print("⚠️  No dependencies found.", file=sys.stderr)
        return CheckResult()

    # 2. Locate and read LICENSE
    if license_path is not None:
        license_path = os.path.abspath(license_path)
    else:
        license_path = _find_license_file(project_dir)
        if license_path is None:
            print(f"❌ LICENSE file not found in {project_dir}", file=sys.stderr)
            print(f"   Searched: LICENSE, src/main/release-docs/LICENSE, target/.../META-INF/LICENSE",
                  file=sys.stderr)
            print(f"   Hint: specify the LICENSE path explicitly as the 2nd argument.", file=sys.stderr)
            sys.exit(1)
    print(f"📄 Using LICENSE: {license_path}")
    license_content = read_license(license_path)

    # 3. Skip project's own modules by artifactId keyword (e.g. "shenyu")
    #    This avoids false positives from broad groupIds like "org.apache"
    skip_keywords = {"shenyu"}

    # 4. Check each dependency
    result = CheckResult(total=len(deps))

    for dep in deps:
        # Skip project's own modules (by artifactId keyword, e.g. "shenyu")
        if any(kw in dep.artifact_id for kw in skip_keywords):
            result.skipped_project += 1
            continue

        # Skip test/provided/system scopes (not shipped in lib/)
        if dep.scope in ("test", "provided", "system"):
            result.skipped_scope.append(dep)
            continue

        result.checked += 1

        if check_dependency_in_license(dep, license_content):
            result.matched.append(dep)
        else:
            result.unmatched.append(dep)

    return result


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

# ANSI colors
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def print_result(result: CheckResult):
    """Print a human-readable check result."""
    print()
    print(f"{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}  License Check Report{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}")
    print()
    print(f"  Total dependencies:      {result.total}")
    print(f"  Checked:                 {result.checked}")
    print(f"  Skipped (project):       {result.skipped_project}")
    print(f"  Skipped (test/provided): {len(result.skipped_scope)}")
    print(f"  {GREEN}Matched in LICENSE:       {len(result.matched)}{RESET}")
    print(f"  {RED}NOT matched in LICENSE:    {len(result.unmatched)}{RESET}")
    print()

    if result.unmatched:
        # Group unmatched by groupId for a concise summary
        from collections import defaultdict
        by_group = defaultdict(list)
        for dep in result.unmatched:
            by_group[dep.group_id].append(dep)

        print(f"{RED}{BOLD}❌ {len(result.unmatched)} dependency(ies) NOT declared in LICENSE{RESET}")
        print(f"{RED}   (grouped by groupId, {len(by_group)} groups){RESET}")
        print(f"{RED}{'─' * 60}{RESET}")
        for gid in sorted(by_group):
            deps_in_group = sorted(by_group[gid], key=lambda d: d.artifact_id)
            print(f"  {BOLD}{gid}{RESET} ({len(deps_in_group)} artifact(s))")
            for dep in deps_in_group:
                version_display = f"{dep.version}-{dep.classifier}" if dep.classifier else dep.version
                print(f"    {RED}• {dep.artifact_id}:{version_display}{RESET}")
        print()

        # Provide actionable suggestions — grouped by groupId
        print(f"{YELLOW}💡 Suggested LICENSE entries to add (grouped):{RESET}")
        print(f"{YELLOW}{'─' * 60}{RESET}")
        for gid in sorted(by_group):
            print(f"  {gid}")
            for dep in sorted(by_group[gid], key=lambda d: d.artifact_id):
                version_display = f"{dep.version}-{dep.classifier}" if dep.classifier else dep.version
                print(f"    {dep.artifact_id} {version_display}")
        print()

    if result.skipped_scope:
        print(f"{CYAN}ℹ️  Skipped (test/provided/system scope, not shipped):{RESET}")
        for dep in sorted(result.skipped_scope, key=lambda d: d.coordinate):
            print(f"  • {dep.coordinate} [{dep.scope}]")
        print()

    if not result.unmatched:
        print(f"{GREEN}{BOLD}✅ All runtime dependencies are properly declared in LICENSE!{RESET}")
        print()

    # Summary
    if result.unmatched:
        print(f"{RED}{BOLD}Result: FAIL — {len(result.unmatched)} dependency(ies) missing from LICENSE{RESET}")
        return False
    else:
        print(f"{GREEN}{BOLD}Result: PASS{RESET}")
        return True


# ---------------------------------------------------------------------------
# Git / GitHub PR helpers
# ---------------------------------------------------------------------------
#
# The --pr flow turns an in-place LICENSE edit into a reviewable pull request:
# after --fix rewrites the target project's LICENSE, these helpers create a
# branch, commit only that file, push, and open a PR via the `gh` CLI. All git
# calls go through _run_git so failures surface a uniform message.
#
# Design note: the branch is created BEFORE fix_license_file writes to disk,
# so the only commit on the new branch is the LICENSE change. The commit is
# path-scoped (git commit -- <relpath>) so unrelated dirty files in the working
# tree — common in a monorepo with build artifacts — are never swept in.


class GitError(RuntimeError):
    """Raised when a git command fails with check=True."""


def _run_git(args: List[str], cwd: str, check: bool = True,
             timeout: int = 120) -> "subprocess.CompletedProcess":
    """Run a git command and return the CompletedProcess.

    On non-zero exit with check=True, raises GitError carrying the combined
    stdout/stderr so the caller can print a useful message.
    """
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise GitError(f"git {' '.join(args)} failed (exit {proc.returncode}): {detail}")
    return proc


def _find_gh() -> Optional[str]:
    """Locate the gh executable, or None if not installed."""
    from shutil import which
    return which("gh")


def find_git_root(project_dir: str) -> Optional[str]:
    """Return the git toplevel containing project_dir, or None if not a repo."""
    proc = _run_git(["rev-parse", "--show-toplevel"], cwd=project_dir, check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def verify_remote(git_root: str, remote: str) -> bool:
    """True if the named remote exists in the repo."""
    return _run_git(["remote", "get-url", remote], cwd=git_root,
                    check=False).returncode == 0


def license_relpath(git_root: str, license_path: str) -> str:
    """LICENSE path relative to git_root. Raises if it lies outside the repo.

    Both paths are realpath-normalized first, because git reports the resolved
    toplevel (e.g. /private/tmp on macOS where /tmp is a symlink) while the
    caller's paths may still carry the symlink prefix — without normalization
    a legitimately in-repo file can look like it escapes the repo.
    """
    root_real = os.path.realpath(git_root)
    lic_real = os.path.realpath(license_path)
    rel = os.path.relpath(lic_real, root_real)
    if rel == "." or os.path.isabs(rel) or rel.startswith(".." + os.sep):
        raise GitError(f"LICENSE file ({license_path}) is outside the git repository "
                       f"({git_root})")
    return rel


def license_is_dirty(git_root: str, relpath: str) -> bool:
    """True if the LICENSE file already has uncommitted changes."""
    proc = _run_git(["status", "--porcelain", "--", relpath], cwd=git_root, check=False)
    return proc.stdout.strip() != ""


def current_ref(git_root: str) -> Tuple[str, bool]:
    """Return (ref, is_detached). ref is the branch name, or the SHA if detached."""
    name = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=git_root).stdout.strip()
    if name == "HEAD":
        sha = _run_git(["rev-parse", "HEAD"], cwd=git_root).stdout.strip()
        return (sha, True)
    return (name, False)


def _branch_exists(git_root: str, branch: str) -> bool:
    """True if a local branch named `branch` already exists."""
    return _run_git(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                    cwd=git_root, check=False).returncode == 0


def unique_branch_name(git_root: str, preferred: Optional[str]) -> str:
    """Resolve a branch name to use for the PR.

    If `preferred` is given, use it as-is and error if it already exists.
    Otherwise generate license-fix-<YYYYMMDD-HHMMSS>, suffixing -2, -3, ... until
    a free name is found.
    """
    if preferred:
        if _branch_exists(git_root, preferred):
            raise GitError(f"branch '{preferred}' already exists; pick another "
                           f"or omit --branch")
        return preferred

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"license-fix-{stamp}"
    name = base
    suffix = 1
    while _branch_exists(git_root, name):
        suffix += 1
        name = f"{base}-{suffix}"
    return name


@dataclass
class PRContext:
    """State captured during PR pre-flight, carried through the commit/push/open steps."""
    git_root: str
    license_relpath: str
    remote: str
    base: Optional[str]        # None => gh uses the repo's default branch
    branch: str                # resolved (unique) branch name
    original_ref: str          # branch name OR sha (if detached) to restore on abort
    is_detached: bool


def prepare_pr_context(project_dir: str, license_path: str,
                       branch: Optional[str], base: Optional[str],
                       remote: str) -> PRContext:
    """Run all read-only pre-flight checks for the --pr flow. No writes.

    Exits with a clear message on any precondition failure. Does NOT create the
    branch — that happens later, just before fix_license_file writes.
    """
    gh = _find_gh()
    if not gh:
        print("❌ GitHub CLI (`gh`) not found. Install it from https://cli.github.com "
              "and run `gh auth login` to use --pr.", file=sys.stderr)
        sys.exit(2)

    git_root = find_git_root(project_dir)
    if not git_root:
        print(f"❌ {project_dir} is not inside a git repository; --pr requires one.",
              file=sys.stderr)
        sys.exit(2)

    if not verify_remote(git_root, remote):
        print(f"❌ git remote '{remote}' not found in {git_root}.", file=sys.stderr)
        print(f"   Add it, or pass --remote <name>.", file=sys.stderr)
        sys.exit(2)

    try:
        rel = license_relpath(git_root, license_path)
    except GitError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(2)

    if license_is_dirty(git_root, rel):
        print(f"❌ {rel} already has uncommitted changes. Stash or commit them first.",
              file=sys.stderr)
        sys.exit(2)

    original_ref, detached = current_ref(git_root)
    try:
        bname = unique_branch_name(git_root, branch)
    except GitError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(2)

    print(f"🔧 PR target: {git_root}")
    print(f"   branch: {bname} (from {original_ref})  remote: {remote}  "
          f"base: {base or '<repo default>'}")
    return PRContext(git_root=git_root, license_relpath=rel, remote=remote,
                     base=base, branch=bname, original_ref=original_ref,
                     is_detached=detached)


def create_branch(ctx: PRContext) -> None:
    """Create and check out the PR branch from the current HEAD."""
    try:
        _run_git(["checkout", "-b", ctx.branch], cwd=ctx.git_root)
    except GitError as e:
        abort_pr_branch(ctx)
        print(f"❌ Failed to create branch {ctx.branch}: {e}", file=sys.stderr)
        sys.exit(2)


def commit_license(ctx: PRContext, added_entries: List[FixEntry]) -> None:
    """Stage and commit only the LICENSE file (path-scoped)."""
    _run_git(["add", "--", ctx.license_relpath], cwd=ctx.git_root)
    _run_git(["commit", "-m", _commit_message(added_entries), "--", ctx.license_relpath],
             cwd=ctx.git_root)


def push_branch(ctx: PRContext) -> None:
    """Push the branch to the remote with upstream tracking (-u)."""
    try:
        _run_git(["push", "-u", ctx.remote, ctx.branch], cwd=ctx.git_root, timeout=180)
    except GitError as e:
        print(f"❌ Push to '{ctx.remote}' failed: {e}", file=sys.stderr)
        print(f"   If you lack push access, fork the repo "
              f"(`gh repo fork --remote --remote-name fork`) and re-run with "
              f"`--remote fork`.", file=sys.stderr)
        abort_pr_branch(ctx)
        sys.exit(2)


def open_pr(ctx: PRContext, added_entries: List[FixEntry],
            result: CheckResult, project_dir: str) -> str:
    """Open a GitHub PR via `gh pr create`. Returns the PR URL."""
    title = _pr_title(added_entries)
    body = _pr_body(added_entries, result, project_dir)
    cmd = ["gh", "pr", "create", "--title", title, "--body-file", "-"]
    if ctx.base:
        cmd += ["--base", ctx.base]
    proc = subprocess.run(cmd, cwd=ctx.git_root, input=body,
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        print(f"❌ `gh pr create` failed: {detail}", file=sys.stderr)
        print(f"   The branch {ctx.branch} is pushed; you can open the PR manually with:",
              file=sys.stderr)
        print(f"     gh pr create --title {title!r} --base {ctx.base or 'main'}",
              file=sys.stderr)
        sys.exit(2)
    # gh prints the PR URL on the last non-empty line of stdout.
    url = proc.stdout.strip().splitlines()[-1].strip() if proc.stdout.strip() else ""
    return url


def abort_pr_branch(ctx: PRContext) -> None:
    """Best-effort cleanup: restore the original ref and delete the new branch."""
    _run_git(["checkout", ctx.original_ref], cwd=ctx.git_root, check=False)
    _run_git(["branch", "-D", ctx.branch], cwd=ctx.git_root, check=False)


def _commit_message(added_entries: List[FixEntry]) -> str:
    lines = ["chore(license): add missing dependency license declarations", ""]
    lines.append(f"Auto-generated by shenyu-watcher (--fix --pr).")
    lines.append("")
    lines.append(f"Added {len(added_entries)} entr{'y' if len(added_entries) == 1 else 'ies'} to LICENSE:")
    by_section: Dict[str, List[FixEntry]] = {}
    for e in added_entries:
        by_section.setdefault(e.section, []).append(e)
    for section in sorted(by_section):
        lines.append(f"  {section}:")
        for e in by_section[section]:
            lines.append(f"    {e.dep.artifact_id} {e.dep.version}")
    return "\n".join(lines)


def _pr_title(added_entries: List[FixEntry]) -> str:
    n = len(added_entries)
    return f"chore(license): add {n} missing dependency license declaration{'s' if n != 1 else ''}"


def _pr_body(added_entries: List[FixEntry], result: CheckResult,
             project_dir: str) -> str:
    lines: List[str] = []
    lines.append("## Summary")
    lines.append("")
    lines.append(f"This PR adds {len(added_entries)} missing dependency license "
                 f"declaration(s) to the LICENSE file, discovered by "
                 f"`shenyu-watcher` (pre-build license checker) while checking "
                 f"`{os.path.basename(os.path.abspath(project_dir))}`.")
    lines.append("")
    lines.append("## Added entries")
    lines.append("")
    by_section: Dict[str, List[FixEntry]] = {}
    for e in added_entries:
        by_section.setdefault(e.section, []).append(e)
    for section in sorted(by_section):
        lines.append(f"### {section}")
        for e in by_section[section]:
            license_name = e.license_name or "unknown"
            lines.append(f"- `{e.entry_line.strip()}` *({license_name})*")
        lines.append("")
    lines.append("## Check context")
    lines.append("")
    lines.append(f"- Total dependencies checked: {result.total}")
    lines.append(f"- Matched in LICENSE (before fix): {len(result.matched)}")
    lines.append(f"- Unmatched (before fix): {len(result.unmatched)}")
    lines.append(f"- Entries added by this PR: {len(added_entries)}")
    lines.append("")
    lines.append("## Verification")
    lines.append("")
    lines.append("The fix was re-verified by re-running the license check after the edit.")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("Generated by `shenyu_watcher.py --fix --pr`.")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Check and fix Maven dependency license declarations in LICENSE file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python %(prog)s /path/to/project                  # Check only (lib/ auto-detected, else Maven)
  python %(prog)s /path/to/project /path/to/LICENSE # Check with custom LICENSE path
  python %(prog)s --fix /path/to/project            # Check and auto-fix
  python %(prog)s -f /path/to/project               # Same as --fix
  python %(prog)s --lib /path/to/lib /path/to/project  # Scan a specific lib/ dir of jars
  python %(prog)s --no-lib /path/to/project         # Force Maven instead of lib/ scanning
  python %(prog)s --pr /path/to/project             # Fix and open a GitHub PR (implies --fix)
""",
    )
    parser.add_argument("project_dir", help="Path to Maven project (containing pom.xml)")
    parser.add_argument("license_file", nargs="?", default=None,
                        help="Path to LICENSE file (default: auto-detect)")
    parser.add_argument("-f", "--fix", action="store_true",
                        help="Automatically append missing entries to the LICENSE file")
    parser.add_argument("--lib", dest="lib_dir", default=None,
                        help="Scan JARs from this lib/ directory instead of running Maven "
                             "(default: auto-detect <project>/target/*-bin/lib)")
    parser.add_argument("--no-lib", action="store_true",
                        help="Disable lib-directory scanning; force Maven dependency resolution")
    parser.add_argument("--pr", action="store_true",
                        help="After --fix, create a git branch, commit the LICENSE change, "
                             "push it, and open a GitHub PR via `gh`. Implies --fix.")
    parser.add_argument("--branch", default=None,
                        help="Branch name for the PR (default: license-fix-<timestamp>). "
                             "Must not already exist.")
    parser.add_argument("--base", default=None,
                        help="PR base branch (default: repository default branch).")
    parser.add_argument("--remote", default="origin",
                        help="Git remote to push the branch to (default: origin; use a fork "
                             "remote, e.g. `--remote fork`, if you lack push access to origin)")

    args = parser.parse_args()

    if not os.path.isdir(args.project_dir):
        print(f"❌ Not a directory: {args.project_dir}", file=sys.stderr)
        sys.exit(1)

    # Run check (suppress exit on fail so we can still fix)
    try:
        result = run_check(args.project_dir, args.license_file,
                           lib_dir=args.lib_dir, no_lib=args.no_lib)
    except SystemExit as e:
        # run_check calls sys.exit(1) on Maven failure — let it propagate
        raise

    print_result(result)

    # --pr implies --fix (a PR needs a LICENSE change to exist).
    do_fix = args.fix or args.pr

    if do_fix and result.unmatched:
        # Resolve the license path for the fix.
        if args.license_file is not None:
            license_path = os.path.abspath(args.license_file)
        else:
            license_path = _find_license_file(os.path.abspath(args.project_dir))
            if license_path is None:
                print(f"\n❌ Cannot fix: LICENSE file not found.", file=sys.stderr)
                sys.exit(1)

        # PR pre-flight (read-only) and branch creation happen BEFORE the fix
        # writes to disk, so the only commit on the new branch is the LICENSE
        # change.
        pr_ctx = None
        if args.pr:
            pr_ctx = prepare_pr_context(
                os.path.abspath(args.project_dir), license_path,
                branch=args.branch, base=args.base, remote=args.remote)
            create_branch(pr_ctx)

        # Write the LICENSE change (on the new branch if --pr).
        added_entries = fix_license_file(license_path, result)

        if pr_ctx is not None:
            if not added_entries:
                # All entries were duplicates or skipped — no LICENSE change.
                abort_pr_branch(pr_ctx)
                print(f"\n{YELLOW}No changes to LICENSE; PR skipped.{RESET}")
            else:
                commit_license(pr_ctx, added_entries)
                push_branch(pr_ctx)
                pr_url = open_pr(pr_ctx, added_entries, result, args.project_dir)
                print(f"\n{GREEN}{BOLD}✅ Pull request opened: {pr_url}{RESET}")
                print(f"   branch: {pr_ctx.branch}  base: {pr_ctx.base or '<repo default>'}")

        # Re-run check to verify
        print(f"\n{'=' * 60}")
        print(f"{BOLD}  Re-checking after fix...{RESET}")
        print(f"{'=' * 60}")
        license_content = read_license(license_path)
        new_result = CheckResult(
            total=result.total,
            checked=result.checked,
            skipped_project=result.skipped_project,
            matched=result.matched,
            unmatched=[],
            skipped_scope=result.skipped_scope,
        )
        for dep in result.unmatched:
            if check_dependency_in_license(dep, license_content):
                new_result.matched.append(dep)
            else:
                new_result.unmatched.append(dep)
        new_result.checked = len(new_result.matched) + len(new_result.unmatched)

        if new_result.unmatched:
            print(f"\n{YELLOW}⚠️  {len(new_result.unmatched)} deps still unmatched after fix:{RESET}")
            for dep in sorted(new_result.unmatched, key=lambda d: d.coordinate):
                print(f"    • {dep.coordinate}")
            if pr_ctx is not None:
                print(f"\n{YELLOW}   These remain unmatched and need manual review — see the PR body.{RESET}")
        else:
            print(f"\n{GREEN}{BOLD}✅ All dependencies now properly declared in LICENSE!{RESET}")


if __name__ == "__main__":
    main()
