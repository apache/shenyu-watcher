# Apache ShenYu Watcher

License check tool for Apache ShenYu releases. Ensures every runtime dependency is properly declared in the project's LICENSE file.

## Overview

Two tools are provided:

| Tool | Language | When to use | Dependencies |
|---|---|---|---|
| `shenyu_watcher.py` | Python 3 | **Before packaging** — check directly from Maven project | Maven (optional, falls back to pom.xml parsing) |
| `ShenyuWatcher.java` | Java 8 | After packaging — check from a `.tar.gz` release archive | Python ≥ 3.8, Java 8 |

The Python tool is recommended for development and CI workflows — it catches missing license declarations **before** you build, without needing to create a release archive.

---

## Python Tool (`shenyu_watcher.py`)

### Features

- **Pre-build checking** — resolves all runtime dependencies (including transitive) via `mvn dependency:list`, no packaging needed
- **Auto-fix mode** (`--fix`) — fetches license info from Maven Central POMs and appends missing entries to the LICENSE file in the correct section
- **PR mode** (`--pr`) — after fixing, creates a git branch, commits only the LICENSE change, pushes, and opens a GitHub PR via the `gh` CLI (implies `--fix`)
- **Smart LICENSE discovery** — auto-locates LICENSE files in `LICENSE`, `src/main/release-docs/LICENSE`, etc.
- **Classifier support** — correctly handles Maven classifiers (e.g., `shiro-core:jakarta:1.13.0`, `netty-tcnative-boringssl-static:linux-x86_64:2.0.65.Final`)
- **Precise matching** — literal string matching (not regex), avoids false positives from license boilerplate text
- **Fork detection** — when a POM's GitHub URL points to a personal fork rather than the canonical upstream, falls back to mvnrepository for accurate attribution
- **Lib-directory scanning** — for distribution modules that Maven can't resolve (e.g. pom-packaged aggregators depending on uninstalled reactor siblings), reads the exact shipped jar set from an assembled `lib/` directory instead. GAV is read from each jar's embedded `pom.properties`, with filename parsing as a fallback.
- **Fallback** — works without Maven by parsing pom.xml directly (direct dependencies only)
- **Zero dependencies** — pure Python standard library, no pip install needed

### Requirements

- Python 3.6+
- Maven 3.x (recommended; falls back to pom.xml parsing if unavailable). Not required when scanning an assembled `lib/` directory.
- GitHub CLI (`gh`) installed and authenticated, when using `--pr`.

### Usage

```bash
# Check only — report missing dependencies.
# An assembled lib/ dir is auto-detected under <project>/target/*-bin/lib and
# scanned in preference to running Maven; otherwise Maven is used.
python shenyu_watcher.py /path/to/maven/project

# Check with explicit LICENSE path
python shenyu_watcher.py /path/to/maven/project /path/to/LICENSE

# Check and auto-fix — append missing entries to LICENSE
python shenyu_watcher.py --fix /path/to/maven/project

# Short form
python shenyu_watcher.py -f /path/to/maven/project

# Scan a specific lib/ directory of jars instead of running Maven
python shenyu_watcher.py --lib /path/to/lib /path/to/maven/project

# Force Maven dependency resolution (disable lib/ auto-detection)
python shenyu_watcher.py --no-lib /path/to/maven/project

# Fix and open a GitHub PR for the LICENSE change (implies --fix)
python shenyu_watcher.py --pr /path/to/maven/project

# Fix + PR with a custom branch and base
python shenyu_watcher.py --pr --branch fix/admin-license --base main /path/to/maven/project

# Fix + PR pushing to a fork remote (when you lack push access to origin)
python shenyu_watcher.py --pr --remote fork /path/to/maven/project
```

### Examples

Check the shenyu-admin-dist module:

```bash
python shenyu_watcher.py /path/to/shenyu/shenyu-dist/shenyu-admin-dist
```

Output (the dist module's assembled `lib/` is auto-detected and scanned):

```
📦 Scanning JARs in lib directory...
   Found 324 unique dependencies (214 from pom.properties, 110 from filename).
   (110 jars had no pom.properties — resolved from filename, groupId unknown; license auto-fix will skip these.)
📄 Using LICENSE: .../shenyu-admin-dist/src/main/release-docs/LICENSE

============================================================
  License Check Report
============================================================

  Total dependencies:      324
  Checked:                 294
  Skipped (project):       30
  Skipped (test/provided): 0
  Matched in LICENSE:       290
  NOT matched in LICENSE:    4

❌ 4 dependency(ies) NOT declared in LICENSE
   (grouped by groupId, 3 groups)
────────────────────────────────────────────────────────────
   (2 artifact(s))
    • jna:5.18.1
    • jna-platform:5.18.1
  com.github.oshi (1 artifact(s))
    • oshi-core:6.10.0
  jakarta.mail (1 artifact(s))
    • jakarta.mail-api:2.1.3

Result: FAIL — 4 dependency(ies) missing from LICENSE
```

> `jna` and `jna-platform` show no groupId because those jars had no embedded
> `pom.properties` and were resolved from their filenames. License **matching**
> still works for them (by artifactId + version). For `--fix`, the groupId is
> recovered from Maven Central (by artifactId + version) so the license can be
> fetched; if that lookup also fails, the dependency is skipped with a warning.

Auto-fix and re-verify:

```bash
python shenyu_watcher.py --fix /path/to/shenyu/shenyu-dist/shenyu-admin-dist
```

```
🔍 Resolving license info for 4 missing dependencies...
   jna 5.18.1: groupId resolved from Maven Central -> net.java.dev.jna
   jna 5.18.1 → LGPL licenses (LGPL-2.1-or-later)
   jna-platform 5.18.1: groupId resolved from Maven Central -> net.java.dev.jna
   jna-platform 5.18.1 → LGPL licenses (LGPL-2.1-or-later)
   oshi-core 6.10.0 → MIT licenses (MIT)
   jakarta.mail-api 2.1.3 → EPL licenses (EPL 2.0)

✅ Added 4 entries to LICENSE

Added entries by section:
  EPL licenses:
    + jakarta.mail-api 2.1.3: https://mvnrepository.com/artifact/jakarta.mail/jakarta.mail-api, EPL 2.0
  LGPL licenses:
    + jna 5.18.1: https://mvnrepository.com/artifact/net.java.dev.jna/jna, LGPL-2.1-or-later
    + jna-platform 5.18.1: https://mvnrepository.com/artifact/net.java.dev.jna/jna-platform, LGPL-2.1-or-later
  MIT licenses:
    + oshi-core 6.10.0: https://github.com/oshi/oshi, MIT

============================================================
  Re-checking after fix...
============================================================
```

### How `--fix` Works

1. Runs the normal check to find unmatched dependencies
2. For each unmatched dependency, fetches its POM from Maven Central
   - Resolves parent POMs recursively (up to 3 levels) since license info is often in parent POMs
   - Extracts `<license><name>`, `<url>`, and `<scm><url>` from the POM
   - Uses `<url>` (project homepage) over `<scm><url>` (may point to a personal fork)
   - Detects GitHub fork URLs and falls back to mvnrepository for accurate attribution
   - For dependencies resolved from filename only (no groupId), the groupId is recovered from Maven Central by artifactId + version before fetching the POM; if that lookup fails, the dependency is skipped with a warning
3. Maps the POM license name to the correct LICENSE section header (e.g., "Apache License, Version 2.0" → "Apache 2.0 licenses")
4. Appends the entry in the format `    artifactId version: url, LicenseName` to the correct section
5. Re-runs the check to verify all entries now pass

With `--pr`, the flow additionally:

6. **Before** writing to disk, creates a new git branch (auto-named `license-fix-<timestamp>`, or the name passed to `--branch`) from the current HEAD — so the only commit on the new branch is the LICENSE change
7. Writes the LICENSE change, then commits **only the LICENSE path** (`git commit -- <relpath>`) — unrelated dirty files in the working tree are never swept in
8. Pushes the branch to `--remote` (default `origin`) with upstream tracking
9. Runs `gh pr create` with a title and a body listing the added entries and the check context
10. If no entries were actually added (all duplicates or all skipped), the branch is rolled back and **no PR is opened**

### Opening a PR with `--pr`

`--pr` turns the in-place LICENSE edit into a reviewable pull request. It implies `--fix`, so you can run it directly:

```bash
python shenyu_watcher.py --pr /path/to/shenyu/shenyu-dist/shenyu-admin-dist
```

What it does:

- **Auto-detects the git root** from the project directory (`git rev-parse --show-toplevel`), so it works when the project is a subdirectory of a larger repo (e.g. a `shenyu` monorepo module whose LICENSE lives at `src/main/release-docs/LICENSE`).
- **Commits only the LICENSE file** — a path-scoped commit means build artifacts or other uncommitted changes in the working tree are left alone. It only requires the LICENSE file itself to be clean beforehand.
- **Pushes and opens the PR** via `gh`. The PR body lists each added entry (grouped by LICENSE section) plus the check totals and any dependencies still unmatched after the fix (for manual review).

Pre-flight checks (exit code `2` on failure): the project must be inside a git repository; the `--remote` (default `origin`) must exist; the LICENSE file must be inside the repo and not already have uncommitted changes; `gh` must be installed and authenticated.

If you lack push access to `origin`, fork the repo (`gh repo fork --remote --remote-name fork`) and re-run with `--remote fork`.

Example output:

```
🔧 PR target: /path/to/shenyu
   branch: license-fix-20260708-143012 (from main)  remote: origin  base: <repo default>

✅ Pull request opened: https://github.com/apache/shenyu/pull/NNNN
   branch: license-fix-20260708-143012  base: <repo default>
```

### Fork Detection

When a POM's `<url>` or `<scm><url>` points to a GitHub repository that appears to be a personal fork (e.g., `at.yawk.lz4:lz4-java` has `https://github.com/yawkat/lz4-java`), the tool detects this and uses `https://mvnrepository.com/artifact/...` instead, which always links to the canonical project information. The heuristic checks whether the GitHub owner matches the groupId or artifactId — if not, it's treated as a likely fork.

### License Section Mapping

The tool recognizes these section headers in the LICENSE file:

| POM License Name Contains | LICENSE Section |
|---|---|
| `Apache` | Apache 2.0 licenses |
| `MIT` | MIT licenses |
| `BSD` / `Go License` | BSD licenses |
| `EPL` / `Eclipse Public License` | EPL licenses |
| `EDL` / `Eclipse Distribution License` | EDL License |
| `MPL` / `Mozilla Public License` | MPL licenses |
| `CDDL` | CDDL licenses |
| `CC0` | CC0 licenses |
| `Public Domain` | Public Domain licenses |
| `Bouncy Castle` | Bouncy Castle licenses |
| `ISC` | ISC licenses |
| Unknown ⚠️ | Apache 2.0 licenses (with warning) |

When a license name cannot be mapped to any known section, the tool prints a warning and defaults to Apache 2.0 — review these entries manually.

### Entry Format

Each appended entry follows the format used by the existing LICENSE file:

```
    <artifactId> <version>[-<classifier>]: <url>, <LicenseName>
```

Examples:
- `zookeeper 3.9.5: https://gitbox.apache.org/repos/asf/zookeeper, Apache 2.0`
- `oshi-core 6.7.0: https://github.com/oshi/oshi, MIT`
- `bcprov-jdk18on 1.84: https://github.com/bcgit/bc-java, Bouncy Castle`
- `shiro-core 1.13.0-jakarta: https://shiro.apache.org, Apache 2.0` (with classifier)

### Exit Codes

| Code | Meaning |
|---|---|
| 0 | All dependencies matched (PASS) |
| 1 | Missing dependencies found (FAIL), or Maven/LICENSE not found |
| 2 | `--pr` requested but preconditions failed (not a git repo, `gh` missing/unauthenticated, LICENSE dirty or outside repo, push failed) — see message |

### CI Integration

```bash
# In CI pipeline — exit code 1 if any dependency is missing
python shenyu_watcher.py /path/to/project

# Auto-fix in CI (modifies LICENSE in place — review the diff in version control)
python shenyu_watcher.py --fix /path/to/project
```

---

## Java Tool (`ShenyuWatcher.java`)

### Build

```bash
mvn clean package
```

### Use

```bash
java -jar target/shenyu-watcher-1.0-SNAPSHOT.jar filePath/xxx.tar.gz
```

Checks a `.tar.gz` release archive: extracts it, scans `lib/` for JAR filenames, parses each into package name + version, and verifies against the LICENSE file inside the archive.

### Limitations

- Requires Python ≥ 3.8 installed (checked at startup but not actually used)
- JAR filename parsing uses a regex that can fail for artifact IDs containing digits (e.g., `commons-lang3-3.14.0.jar`)
- Uses regex matching for LICENSE checks — `.` in version strings matches any character, causing potential false matches
- No transitive dependency resolution — only sees what's physically in the `lib/` folder
- No auto-fix capability

---

## Comparison

| Feature | Python (`shenyu_watcher.py`) | Java (`ShenyuWatcher.java`) |
|---|---|---|
| Check timing | **Before packaging** | After packaging |
| Dependency source | `mvn dependency:list` (all runtime deps) | JAR filenames in `lib/` |
| Transitive deps | ✅ Yes | ❌ Only what's in `lib/` |
| JAR name parsing | Not needed — Maven provides exact coordinates | Regex-based, breaks on names like `commons-lang3` |
| LICENSE matching | Literal string match (no false positives) | Regex match (`.` matches any char) |
| Classifier support | ✅ Handles `shiro-core:jakarta`, native variants | ❌ Not supported |
| Auto-fix | ✅ `--fix` flag | ❌ None |
| Open PR | ✅ `--pr` (via `gh`) | ❌ None |
| License discovery | ✅ Auto-finds LICENSE in project | ❌ Must be in tar.gz |
| Fork detection | ✅ Detects fork POMs, uses mvnrepository fallback | ❌ N/A |
| Unknown license warning | ✅ Warns on unmappable licenses | ❌ N/A |
| Python dependency | None | Python ≥ 3.8 (checked but unused) |
| Maven dependency | Recommended (falls back to pom.xml) | None |
