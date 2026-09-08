#!/usr/bin/env python3
"""Public API tracker for java-rs-driver.

Enumerates the public API this driver must preserve while its transport is
re-implemented on the Rust core, joins it to the current state of the rewrite,
and renders a workbook modelled on the sibling "C# over Rust driver API.xlsx".

The tracked state lives in CSVs next to this script, one per sheet, so it is
diffable and reviewable in git; the .xlsx is a render of them into out/.

    generate.py baseline    refresh the generated columns from the built tree
    generate.py check       fail if the built tree has drifted from the CSVs
    generate.py report      render out/*.csv and the .xlsx (no build needed)
    generate.py import      fold a human's workbook edits back into the CSVs

Standard library only, Python >= 3.11.  Run --help for the full usage.
"""

import argparse
import csv
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, OrderedDict, defaultdict
from pathlib import Path

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]                      # tools/api-tracker/ -> repo root
OUT = HERE / "out"

# The four sheets whose state is committed, one CSV each.
FUNCTION_CSV = HERE / "api-tracker.csv"
IT_CSV = HERE / "integration-tests.csv"
ERROR_CSV = HERE / "error-mapping.csv"
CONFIG_CSV = HERE / "config-options.csv"
# Where the carried snapshots came from.  Not row-shaped, so it does not
# belong in any of the CSVs; rendered onto the Notes sheet.
PROVENANCE = HERE / "provenance.json"

WORKBOOK = OUT / "Java rs-driver - public API tracker.xlsx"

MODULES = ["core", "query-builder", "mapper-runtime"]

# Columns the humans own.  `baseline` preserves them; `import` is what updates
# them.  Everything else on a sheet is regenerated from the tree.
HUMAN_COLUMNS = ["Github issue?", "Implemented and waiting as PRs",
                 "Merged to master", "Comment"]
CONFIG_HUMAN_COLUMNS = ["Disposition", "Comment"]
# The Rust<->Java correspondence is a judgement call, never inferred, so the
# whole right-hand side of an error row is the human's (see README).
ERROR_HUMAN_COLUMNS = ["Java exception", "Explicitly implemented",
                       "All variants mapped", "Comment"]

# Columns carried across regeneration because they cannot be recomputed from
# this repo alone (see README: the upstream remote and the Rust core live
# elsewhere).  Refresh them deliberately with --upstream-repo / --rust-src.
CARRIED_COLUMNS = ["Scylla-only?"]

# Scope tiers.  Longest matching package prefix wins.  Sources:
#   the design doc section 5 (scope tiers T1/T2/drop)
#   Java-RS-Driver-plan.md section 3 (module surgery) and section 4 (cut line)
PRIORITY_BY_PACKAGE = [
    ("com.datastax.oss.driver.api.core.cql.reactive", "T2", "design S5: reactive is a T2 adapter over executeAsync"),
    ("com.datastax.oss.driver.api.core.metrics", "T2", "design S5: metrics bridged from the Rust core in T2"),
    ("com.datastax.oss.driver.api.core.specex", "T2", "design S5: speculative execution deferred to T2"),
]
DEFAULT_PRIORITY = ("T1", "")

# Behaviour that survives the transport cut untouched (plan S4 "keep working"),
# i.e. members that already work and are not part of the bridging backlog.
HOST_SIDE_PACKAGES = [
    "com.datastax.oss.driver.api.core.type",          # the whole codec/type matrix
    "com.datastax.oss.driver.api.core.data",          # UdtValue / TupleValue / accessors
    "com.datastax.oss.driver.api.core.detach",
    "com.datastax.oss.driver.api.core.uuid",
    "com.datastax.oss.driver.api.core.config",        # Typesafe loader + reference.conf
    "com.datastax.oss.driver.api.core.time",
    "com.datastax.oss.driver.api.querybuilder",       # emits statements, never touches transport
    "com.datastax.oss.driver.api.mapper",             # annotations + generated code target the API
]
HOST_SIDE_MODULES = {"query-builder", "mapper-runtime"}

# The internal choke points that throw NOT YET IMPLEMENTED (java-rs), mapped to
# the api types whose behaviour they gate.  Type-level, deliberately: the marker
# sits in internal code, never inside an api/ source file.
STUB_ANCHORS = {
    "com.datastax.oss.driver.api.core.session.Session": "DefaultSession",
    "com.datastax.oss.driver.api.core.session.SessionBuilder": "DefaultSession",
    "com.datastax.oss.driver.api.core.CqlSession": "DefaultSession",
    "com.datastax.oss.driver.api.core.CqlSessionBuilder": "DefaultSession",
    "com.datastax.oss.driver.api.core.metadata.Metadata": "DefaultMetadata",
    "com.datastax.oss.driver.api.core.context.DriverContext": "DefaultDriverContext",
    "com.datastax.oss.driver.api.core.cql.PreparedStatement": "CqlPrepareAsyncProcessor",
    "com.datastax.oss.driver.api.core.cql.ResultSet": "CqlRequestAsyncProcessor",
    "com.datastax.oss.driver.api.core.cql.AsyncResultSet": "CqlRequestAsyncProcessor",
}

# The two interfaces that declare execute*/prepare* -- and so carry most of the
# backlog -- are gated by two different processors, so they get a member-level
# rule rather than one type-level anchor.
SESSION_REQUEST_TYPES = {
    "com.datastax.oss.driver.api.core.cql.SyncCqlSession",
    "com.datastax.oss.driver.api.core.cql.AsyncCqlSession",
}


def stub_anchor(fqn, member):
    """The internal choke point that currently throws NOT YET IMPLEMENTED."""
    if fqn in SESSION_REQUEST_TYPES:
        return ("CqlPrepareAsyncProcessor" if member.startswith("prepare")
                else "CqlRequestAsyncProcessor")
    return STUB_ANCHORS.get(fqn, "")

# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def run(cmd, cwd=None, check=True):
    """Run a command, return stdout as text."""
    p = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, text=True)
    if check and p.returncode != 0:
        raise RuntimeError("%s failed (%d):\n%s" % (" ".join(cmd[:4]), p.returncode, p.stderr[:2000]))
    return p.stdout


def git(args, cwd, check=True):
    return run(["git"] + args, cwd=str(cwd), check=check)


def find_javap():
    """javap from JAVA_HOME if set, else from PATH."""
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        candidate = Path(java_home) / "bin" / "javap"
        if candidate.exists():
            return str(candidate)
    found = shutil.which("javap")
    if not found:
        sys.exit("javap not found: set JAVA_HOME to a JDK (11 or newer) or put javap on PATH")
    return found


def classes_dir(module):
    return REPO / module / "target" / "classes"


def require_build():
    missing = [m for m in MODULES if not classes_dir(m).is_dir()]
    if missing:
        sys.exit("no compiled classes for %s -- run `make compile-all` first"
                 % ", ".join(missing))


def simple(type_name):
    """java.util.Map<K,V>[] -> Map[]  (erased, display form)."""
    t = type_name.strip()
    suffix = ""
    while t.endswith("[]") or t.endswith("..."):
        if t.endswith("..."):
            suffix = "..." + suffix
            t = t[:-3]
        else:
            suffix = "[]" + suffix
            t = t[:-2]
    depth, base = 0, []
    for ch in t:                      # drop the generic argument list
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        elif depth == 0:
            base.append(ch)
    t = "".join(base)
    if "." in t:
        t = t.rsplit(".", 1)[1]
    if "$" in t:
        t = t.rsplit("$", 1)[1]
    return t + suffix


def row_key(r):
    return "%s|%s.%s|%s" % (r["Module"], r["Package"], r["Class"], r["Method/Property name"])


def split_params(text):
    """Split a parameter list on top-level commas."""
    out, depth, cur = [], 0, []
    for ch in text:
        if ch in "<(":
            depth += 1
        elif ch in ">)":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        out.append("".join(cur))
    return [p.strip() for p in out if p.strip()]


# --------------------------------------------------------------------------
# Stage 1 -- enumerate the API from bytecode
# --------------------------------------------------------------------------

MODIFIERS = ("public", "protected", "private", "static", "final", "abstract",
             "default", "synchronized", "native", "strictfp", "transient",
             "volatile")

CLASS_DECL = re.compile(
    r"^(?P<mods>(?:\w+\s+)*)(?P<kind>class|interface|enum|@interface|record)\s+(?P<name>[\w.$]+)")


def javap_dump(classes, classpath):
    """Run javap -v over every class name, return the concatenated stdout.

    Invoked in chunks rather than through xargs: its --delimiter and --arg-file
    options are GNU-only and this has to run on macOS too.
    """
    if not classes:
        sys.exit("no compiled api classes found under %s -- run `make compile-all` first"
                 % classpath)
    javap = find_javap()
    chunks, size = [], 200
    try:
        for i in range(0, len(classes), size):
            chunks.append(run([javap, "-v", "-s", "-protected", "-cp", classpath]
                              + classes[i:i + size]))
    except RuntimeError as exc:
        # javap reports an unreadable class file on stderr and exits non-zero,
        # so this is the only place the version mismatch can be caught.
        sys.exit("javap failed to read the compiled classes -- it is probably older than the "
                 "JDK they were built with; point JAVA_HOME at that JDK.\n\n%s" % exc)
    return "".join(chunks)


def parse_javap(text):
    """Parse a javap -v dump into a list of type dicts, each with its members."""
    types, cur = [], None
    lines = text.splitlines()
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("Classfile "):
            cur = {"classfile": line[len("Classfile "):].strip(), "members": [],
                   "deprecated": False, "kind": "class", "fqn": None, "flags": ""}
            types.append(cur)
            i += 1
            continue
        if cur is None:
            i += 1
            continue
        if cur["fqn"] is None:
            m = CLASS_DECL.match(line)
            if m:
                cur["kind"] = {"@interface": "annotation"}.get(m.group("kind"), m.group("kind"))
                cur["fqn"] = m.group("name")
                cur["class_mods"] = m.group("mods").split()
                cur["decl"] = line.rstrip()
            i += 1
            continue
        if line.startswith("Constant pool:"):
            while i < n and lines[i].rstrip() != "{":
                i += 1
            continue
        if line.rstrip() == "{":
            i += 1
            # member block
            while i < n and lines[i].rstrip() != "}":
                ml = lines[i]
                if ml.startswith("  ") and not ml.startswith("   ") and ml.rstrip().endswith(";"):
                    # Only three facts are ever read back out of a member's
                    # attribute block, and under -v that block is the whole
                    # disassembly -- so test the lines and drop them.
                    member = {"decl": ml.strip().rstrip(";"), "deprecated": False,
                              "generated": False, "enum": False}
                    cur["members"].append(member)
                    i += 1
                    while i < n and lines[i].startswith("    "):
                        a = lines[i].strip()
                        if a == "Deprecated: true":
                            member["deprecated"] = True
                        if "ACC_SYNTHETIC" in a or "ACC_BRIDGE" in a:
                            member["generated"] = True
                        if "ACC_ENUM" in a:
                            member["enum"] = True
                        i += 1
                    continue
                i += 1
            i += 1
            # class-level attributes follow the closing brace
            while i < n and not lines[i].startswith("Classfile "):
                if lines[i].strip() == "Deprecated: true":
                    cur["deprecated"] = True
                i += 1
            continue
        i += 1
    return [t for t in types if t["fqn"]]


def member_signature(decl, fqn):
    """(kind, display name, visibility, modifiers) for one javap declaration."""
    text = decl.strip()
    mods = []
    while True:
        head = text.split(" ", 1)[0] if " " in text else text
        if head in MODIFIERS:
            mods.append(head)
            text = text.split(" ", 1)[1].strip()
        else:
            break
    visibility = "public"
    for v in ("public", "protected", "private"):
        if v in mods:
            visibility = v
            break
    if "(" not in text:                                   # a field
        name = text.rsplit(" ", 1)[-1]
        return "field", name, visibility, mods
    head, rest = text.split("(", 1)
    params = rest.rsplit(")", 1)[0]
    head = head.strip()
    simple_owner = fqn.rsplit(".", 1)[-1]
    if " " in head:
        name = head.rsplit(" ", 1)[-1]
        kind = "method"
    else:                                                 # no return type -> constructor
        name = simple_owner.rsplit("$", 1)[-1]
        kind = "constructor"
    rendered = "%s(%s)" % (name, ", ".join(simple(p) for p in split_params(params)))
    return kind, rendered, visibility, mods


def enumerate_api():
    """Every public/protected member of every api/** type on the baseline ref."""
    cp = os.pathsep.join(str(classes_dir(m)) for m in MODULES)
    class_to_module, class_names = {}, []
    for m in MODULES:
        root = str(classes_dir(m))
        for dirpath, _dirs, files in os.walk(root):
            if "/driver/api/" not in dirpath + "/":
                continue
            for f in files:
                if not f.endswith(".class"):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, f), root)[:-len(".class")]
                fqn = rel.replace("/", ".")
                class_names.append(fqn)
                class_to_module[fqn] = m
    class_names.sort()
    types = parse_javap(javap_dump(class_names, cp))

    rows = []
    for t in types:
        fqn = t["fqn"]
        first = len(rows)
        # anonymous inner classes (Outer$1) and package-info are not API
        if re.search(r"\$\d+$", fqn) or fqn.endswith(".package"):
            continue
        # a non-public nested type is not API even though javap lists it
        if not ({"public", "protected"} & set(t.get("class_mods", []))):
            continue
        # javap names a nested type pkg.Outer$Inner, the same form the class
        # file paths give class_to_module, so the package split is unambiguous.
        module = class_to_module.get(fqn)
        pkg, _, cls = fqn.rpartition(".")
        emitted = 0
        for mem in t["members"]:
            if mem["decl"].startswith("static {}"):
                continue
            if mem["generated"]:
                continue
            kind, rendered, visibility, mods = member_signature(mem["decl"], fqn)
            if visibility == "private":
                continue
            if kind == "field" and mem["enum"]:
                kind = "enum-constant"
            emitted += 1
            rows.append(OrderedDict([
                ("Visibility", visibility),
                ("Module", module or "?"),
                ("Package", pkg),
                ("Class", cls.replace("$", ".")),
                ("Method/Property name", rendered),
                ("Kind", kind),
                ("Deprecated?", 1 if (mem["deprecated"] or t["deprecated"]) else 0),
                ("_fqn", fqn),
                ("_typekind", t["kind"]),
            ]))
        if emitted:
            rows[first:] = stable_member_order(rows[first:])
        if emitted == 0:
            # a marker interface or element-less annotation: still public API,
            # so give the type itself one row rather than losing it
            rows.append(OrderedDict([
                ("Visibility", "public" if "public" in t.get("class_mods", []) else "protected"),
                ("Module", module or "?"),
                ("Package", pkg),
                ("Class", cls.replace("$", ".")),
                ("Method/Property name", "(%s, no declared members)" % t["kind"]),
                ("Kind", "type"),
                ("Deprecated?", 1 if t["deprecated"] else 0),
                ("_fqn", fqn),
                ("_typekind", t["kind"]),
            ]))
    return rows, types


# javac does not place an enum's two mandated methods at a fixed point in the
# class file -- recompiling the same sources can emit them before or after the
# enum's own methods.  Everything else javap lists in declaration order, which
# is worth keeping, so pin just these two to the end of their type and leave
# the rest alone.  Without this a plain rebuild churns the committed CSV.
MANDATED_ENUM_METHODS = ("values()", "valueOf(String)")


def stable_member_order(rows):
    """Pin an enum's two mandated methods to the end of its member list.

    Keyed on the enum constants, not the type kind: javap decompiles an enum as
    `final class ... extends java.lang.Enum`, so the declaration never says so.
    """
    mandated = [r for r in rows if r["Method/Property name"] in MANDATED_ENUM_METHODS]
    if not mandated or not any(r["Kind"] == "enum-constant" for r in rows):
        return rows
    rest = [r for r in rows if r["Method/Property name"] not in MANDATED_ENUM_METHODS]
    order = {name: i for i, name in enumerate(MANDATED_ENUM_METHODS)}
    return rest + sorted(mandated, key=lambda r: order[r["Method/Property name"]])


# --------------------------------------------------------------------------
# Stage 2 -- annotate the API rows
# --------------------------------------------------------------------------


def source_path(module, fqn):
    top = fqn.split("$", 1)[0]
    return "%s/src/main/java/%s.java" % (module, top.replace(".", "/"))


def scylla_touched_files(upstream_repo, upstream_ref):
    """Api source files added or modified by the Scylla fork vs upstream DataStax.

    Needs a clone that has the upstream remote fetched; this repo has only
    `origin`, so the answer is normally carried in the committed sheet and only
    refreshed when --upstream-repo is given.
    """
    out = git(["diff", "--name-status", upstream_ref, "--",
               "*/src/main/java/com/datastax/*/driver/api/*"], cwd=upstream_repo)
    touched = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0][:1] in ("A", "M"):
            touched[parts[-1]] = "added" if parts[0][:1] == "A" else "modified"
    return touched


def priority_for(pkg):
    best = DEFAULT_PRIORITY
    best_len = -1
    for prefix, tier, why in PRIORITY_BY_PACKAGE:
        if (pkg == prefix or pkg.startswith(prefix + ".")) and len(prefix) > best_len:
            best, best_len = (tier, why), len(prefix)
    return best


def is_host_side(module, pkg):
    if module in HOST_SIDE_MODULES:
        return True
    return any(pkg == p or pkg.startswith(p + ".") for p in HOST_SIDE_PACKAGES)


def annotate(rows, carried=None, upstream=None):
    """Fill in the derived columns, and the two that have to be carried.

    `carried` maps a row key to the previously committed values of the columns
    this repo cannot recompute; `upstream` is (repo, ref) to recompute them.
    """
    carried = carried or {}
    touched = scylla_touched_files(*upstream) if upstream else None
    unknown = 0
    for r in rows:
        pkg, module = r["Package"], r["Module"]
        tier, _why = priority_for(pkg)
        host = is_host_side(module, pkg)
        path = source_path(module, r["_fqn"])
        key = row_key(r)
        if touched is not None:
            r["Scylla-only?"] = 1 if touched.get(path) == "added" else 0
        elif key in carried:
            r["Scylla-only?"] = carried[key].get("Scylla-only?", "")
        else:
            r["Scylla-only?"] = ""
            unknown += 1
        # every enumerated member counts today; the tier is the hook for
        # taking a package out of scope, and no package uses it yet
        r["Counted to total sum"] = 0 if tier == "Never" else 1
        r["Priority"] = tier
        r["Status"] = "host-side" if host else "needs-bridge"
        r["Stub anchor"] = stub_anchor(r["_fqn"], r["Method/Property name"])
        for col in HUMAN_COLUMNS:
            if key in carried:
                r.setdefault(col, carried[key].get(col, ""))
            else:
                r.setdefault(col, "")
    return unknown


# --------------------------------------------------------------------------
# Stage 3 -- the integration-test sheet
# --------------------------------------------------------------------------

FUNCTION_COLUMNS = ["Visibility", "Module", "Package", "Class", "Method/Property name",
                    "Kind", "Deprecated?", "Scylla-only?", "Counted to total sum",
                    "Priority", "Status", "Stub anchor",
                    "Github issue?", "Implemented and waiting as PRs",
                    "Merged to master", "Comment"]

# "ITs" is recomputed for the workbook rather than committed: it is a pure view
# over the integration tests, and one test moving would otherwise rewrite
# hundreds of rows of the tracked CSV and bury the real change in `check`.
REPORT_FUNCTION_COLUMNS = (FUNCTION_COLUMNS[:FUNCTION_COLUMNS.index("Github issue?")]
                           + ["ITs"] + FUNCTION_COLUMNS[FUNCTION_COLUMNS.index("Github issue?"):])

IT_COLUMNS = ["Package", "Test class", "@Test (declared)", "State",
              "Failsafe group", "Related API", "Info"]

# Passed/Failed/Skipped are read from whatever failsafe run happens to be on
# disk, so like "ITs" they are rendered into the workbook and never committed:
# they describe the machine, not the tree.
RESULT_COLUMNS = ["Passed", "Failed", "Skipped"]
REPORT_IT_COLUMNS = (IT_COLUMNS[:IT_COLUMNS.index("State")] + RESULT_COLUMNS
                     + IT_COLUMNS[IT_COLUMNS.index("State"):])


def scan_many(root, patterns):
    """One walk of a source tree, one {relative path: [lines]} per pattern."""
    rxs = [re.compile(pat) for pat in patterns]
    hits = [defaultdict(list) for _ in rxs]
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if not name.endswith(".java"):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, REPO)
            with open(full, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    for rx, out in zip(rxs, hits):
                        if rx.search(line):
                            out[rel].append(line.rstrip("\n"))
    return hits


def scan_tests(root, pattern):
    """{relative path: [matching lines]} over a source tree."""
    return scan_many(root, [pattern])[0]


def failsafe_summaries():
    """passed/failed/skipped per test class from any failsafe run on disk."""
    totals = {}
    root = str(REPO / "integration-tests" / "target" / "failsafe-reports")
    if not os.path.isdir(root):
        return totals
    for f in sorted(os.listdir(root)):
        if not (f.startswith("TEST-") and f.endswith(".xml")):
            continue
        try:
            tree = ET.parse(os.path.join(root, f))
        except ET.ParseError:
            continue
        suite = tree.getroot()
        name = suite.get("name", "").rsplit(".", 1)[-1]
        totals[name] = {
            "tests": int(suite.get("tests", 0) or 0),
            "failures": int(suite.get("failures", 0) or 0) + int(suite.get("errors", 0) or 0),
            "skipped": int(suite.get("skipped", 0) or 0),
        }
    return totals


def integration_tests(api_fqns, previous=None):
    """One row per IT class in the working tree.

    Classes that the committed sheet knows about but the tree no longer has are
    kept with state "deleted", so dropping an integration test stays visible
    instead of silently shrinking the denominator.
    """
    spec = REPO / "integration-tests" / "src" / "test" / "java"
    if not spec.is_dir():
        return []
    present = {}
    for dirpath, _dirs, files in os.walk(spec):
        for name in files:
            if name.endswith("IT.java"):
                full = os.path.join(dirpath, name)
                present[os.path.relpath(full, REPO)] = full
    tests, cats, imports = scan_many(spec, [
        r"^\s*@Test",
        r"@Category\(",
        r"^import (static )?com\.datastax\.oss\.driver\.api\.",
    ])

    gone = OrderedDict()
    for row in previous or []:
        key = row.get("_path") or ""
        if key and key not in present:
            gone[key] = row

    rows = []
    for path in sorted(set(present) | set(gone)):
        alive = path in present
        cls = os.path.basename(path)[:-len(".java")]
        pkg = os.path.dirname(path)
        marker = "integration-tests/src/test/java/"
        pkg = pkg[pkg.index(marker) + len(marker):].replace("/", ".") if marker in pkg + "/" else ""
        if not alive:
            old = gone[path]
            rows.append(OrderedDict([
                ("Package", old.get("Package", pkg)),
                ("Test class", old.get("Test class", cls)),
                ("@Test (declared)", old.get("@Test (declared)", "")),
                ("State", "deleted"), ("Failsafe group", ""),
                ("Related API", old.get("Related API", "")),
                ("Info", "no longer in the tree -- tested a subsystem the rewrite drops"),
                ("_path", path),
            ]))
            continue
        cat_text = " ".join(cats.get(path, []))
        state = "quarantined" if "BrokenTests" in cat_text else "active"
        if "IsolatedTests" in cat_text:
            group = "isolated"
        elif "ParallelizableTests" in cat_text:
            group = "parallelizable"
        else:
            group = "serial"
        api_types = []
        for imp in imports.get(path, []):
            m = re.search(r"com\.datastax\.oss\.driver\.api\.[\w.]+", imp)
            if not m:
                continue
            name = m.group(0)
            if name in api_fqns:
                api_types.append(name)
            else:                       # a static import of a member
                owner = name.rsplit(".", 1)[0]
                if owner in api_fqns:
                    api_types.append(owner)
        api_types = sorted(set(api_types))
        ntests = len(tests.get(path, []))
        info = []
        if ntests == 0:
            info.append("no own @Test (inherits from a *ITBase)")
        rows.append(OrderedDict([
            ("Package", pkg),
            ("Test class", cls),
            ("@Test (declared)", ntests),
            ("State", state),
            ("Failsafe group", group),
            ("Related API", ", ".join(t.rsplit(".", 1)[-1] for t in api_types)),
            ("Info", "; ".join(info)),
            ("_path", path),
        ]))
    return rows


# --------------------------------------------------------------------------
# Stage 4 -- error mapping
# --------------------------------------------------------------------------

ERROR_COLUMNS = ["Side", "Rust type / error code", "Rust kind", "Variants",
                 "Java exception", "Java supertype", "Module",
                 "Explicitly implemented", "All variants mapped", "Comment"]


def error_key(row):
    """Identity of an error row.

    Rust and wire rows are named by their Rust type or error code; java rows
    have neither, so they fall back to the exception name.  Deliberately not
    keyed on 'Java exception' for the first two: that cell is the human's, and
    filling it in must not read as one row removed and another added.
    """
    return "%s|%s" % (row.get("Side", ""),
                      row.get("Rust type / error code", "") or row.get("Java exception", ""))


def read_provenance():
    if not PROVENANCE.exists():
        return {}
    try:
        return json.loads(PROVENANCE.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def write_provenance(rust_src):
    """Record which scylla-rust-driver the error snapshot came from.

    The repo name and commit only -- never the checkout path, which is one
    developer's laptop and would be carried into everyone else's diff.
    """
    prov = read_provenance()
    prov["rust_repo"] = Path(rust_src).resolve().name
    prov["rust_commit"] = git(["rev-parse", "--short", "HEAD"],
                              cwd=rust_src, check=False).strip() or "unknown"
    prov["rust_captured"] = datetime.date.today().isoformat()
    PROVENANCE.write_text(json.dumps(prov, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def rust_errors(rust_src):
    """Types exported from scylla::errors, with variant counts.

    `rust_src` is a scylla-rust-driver checkout.  That repo is not here, so the
    result is normally read back from the committed sheet instead; pass
    --rust-src to refresh it.
    """
    path = os.path.join(rust_src, "scylla/src/errors.rs")
    if not os.path.exists(path):
        sys.exit("no scylla/src/errors.rs under %s" % rust_src)
    lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    out, i, n = [], 0, len(lines)
    reexports = []
    while i < n:
        line = lines[i]
        m = re.match(r"^pub use ([\w:]+)(::\{)?(.*)$", line)
        if m:
            block = m.group(3)
            j = i
            while "};" not in block and ";" not in block and j + 1 < n:
                j += 1
                block += lines[j]
            for name in re.findall(r"\b([A-Z]\w+)\b", block):
                reexports.append((name, m.group(1)))
            i = j + 1
            continue
        m = re.match(r"^pub (enum|struct) (\w+)", line)
        if m:
            kind, name = m.group(1), m.group(2)
            variants = 0
            if kind == "enum" and line.rstrip().endswith("{"):
                depth, j = 1, i + 1
                while j < n and depth > 0:
                    body = lines[j]
                    depth += body.count("{") - body.count("}")
                    if depth >= 1 and re.match(r"^    [A-Z]\w*", body):
                        variants += 1
                    j += 1
                i = j
            else:
                i += 1
            out.append((name, kind, variants))
            continue
        i += 1
    seen = {n for n, _k, _v in out}
    for name, origin in reexports:
        if name not in seen and name[0].isupper():
            out.append((name, "re-export (%s)" % origin.split("::")[-1], 0))
            seen.add(name)
    return sorted(out)


def java_exceptions(types):
    """Public API exception types on the baseline, with their supertype."""
    out = []
    for t in types:
        fqn = t["fqn"]
        simple_name = fqn.rsplit(".", 1)[-1]
        if not (simple_name.endswith("Exception") or simple_name.endswith("Error")):
            continue
        if not ({"public", "protected"} & set(t.get("class_mods", []))):
            continue
        m = re.search(r"\bextends\s+([\w.$<>, ]+?)(\s+implements|\s*\{|$)", t.get("decl", ""))
        parent = simple(m.group(1)) if m else ""
        out.append({
            "fqn": fqn,
            "name": simple_name,
            "parent": parent,
            "abstract": "abstract" in t.get("class_mods", []),
        })
    return sorted(out, key=lambda e: (e["parent"], e["name"]))


def wire_error_codes():
    """The ProtocolConstants.ErrorCode -> exception switch in Conversions."""
    path = REPO / "core/src/main/java/com/datastax/oss/driver/internal/core/cql/Conversions.java"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    out, current = [], None
    for line in lines:
        m = re.search(r"case ProtocolConstants\.ErrorCode\.(\w+):", line)
        if m:
            current = m.group(1)
            continue
        if current:
            m = re.search(r"(?:return|throw) new (\w+)\(", line)
            if m:
                out.append((current, m.group(1)))
                current = None
    return out


def error_rows(types, previous=None, rust_src=None):
    """The Rust error surface, the Java exception surface, and the wire table.

    The Rust block cannot be recomputed from this repo, so it is carried from
    the committed sheet unless --rust-src points at a scylla-rust-driver
    checkout.  Human cells on those rows are carried either way.
    """
    previous = previous or []
    prior = {error_key(r): r for r in previous}

    def carried(side, key, **generated):
        """One error row: generated cells from the caller, human cells carried."""
        row = OrderedDict([("Side", side), ("Rust type / error code", ""), ("Rust kind", ""),
                           ("Variants", ""), ("Java exception", ""), ("Java supertype", ""),
                           ("Module", ""), ("Explicitly implemented", ""),
                           ("All variants mapped", ""), ("Comment", "")])
        row.update(generated)
        was = prior.get("%s|%s" % (side, key), {})
        for c in ERROR_HUMAN_COLUMNS:
            if was.get(c) not in (None, ""):
                row[c] = was[c]
        return row

    rows = []

    if rust_src:
        rust = [(name, kind, variants) for name, kind, variants in rust_errors(rust_src)]
        write_provenance(rust_src)
    else:
        rust = []
        for r in previous:
            if r.get("Side") == "rust":
                rust.append((r.get("Rust type / error code", ""), r.get("Rust kind", ""),
                             r.get("Variants", "")))
    for name, kind, variants in rust:
        rows.append(carried("rust", name, **{
            "Rust type / error code": name, "Rust kind": kind, "Variants": variants or ""}))
    for e in java_exceptions(types):
        note = ""
        if e["parent"] == "RuntimeException" and e["name"] != "DriverException":
            note = "does not extend DriverException -- outlier in the hierarchy"
        name = e["name"] + (" (abstract)" if e["abstract"] else "")
        rows.append(carried("java", name, **{
            "Java exception": name, "Java supertype": e["parent"],
            "Module": e["fqn"].rsplit(".", 1)[0], "Comment": note}))

    for code, exc in wire_error_codes():
        rows.append(carried("wire", code, **{
            "Rust type / error code": code, "Rust kind": "ErrorCode", "Java exception": exc,
            "Comment": "Conversions.toThrowable -- must survive byte-for-byte"}))
    return rows


# --------------------------------------------------------------------------
# Stage 5 -- config options
# --------------------------------------------------------------------------

CONFIG_COLUMNS = ["Option", "Config path", "Typed?", "Default (reference.conf)",
                  "Disposition", "Comment"]

# design S9.2 / plan S4: how each family of options behaves on the Rust core.
DISPOSITION_RULES = [
    ("advanced.netty", "warn-no-op", "Netty is gone; the option is accepted and warned about"),
    ("advanced.connection.pool", "reinterpreted", "Rust pools per shard, not per node"),
    ("advanced.connection.max-requests-per-connection", "reinterpreted", "per-shard connection model"),
    ("advanced.ssl-engine-factory", "reinterpreted", "TLS terminated by the Rust stack, not JSSE"),
    ("advanced.protocol.compression", "honored", ""),
    ("advanced.metrics", "T2", "metrics bridged from the Rust core in T2"),
    ("advanced.socket", "reinterpreted", "socket options are set by the Rust core"),
    ("advanced.heartbeat", "reinterpreted", "keepalive handled by the Rust core"),
]


def hocon_defaults(path):
    """Flatten reference.conf into {dotted path: value}."""
    out, stack = {}, []
    if not os.path.exists(path):
        return out
    for raw in open(path, encoding="utf-8", errors="replace"):
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        m = re.match(r"^([\w.\-\"]+)\s*(=|\{)\s*(.*)$", stripped)
        if m:
            key, sep, rest = m.group(1).strip('"'), m.group(2), m.group(3)
            if sep == "{" or rest.strip() == "{":
                stack.append(key)
                continue
            full = ".".join(stack[1:] + [key]) if len(stack) > 1 else key
            value = rest.strip().rstrip(",")
            # a list or object opened here and closed on a later line has no
            # literal default to report; "[" is not one (see README).
            out[full] = "" if value in ("[", "{", "") else value
            continue
        if stripped.startswith("}"):
            if stack:
                stack.pop()
    return out


def config_rows(previous=None):
    prior = {r.get("Option"): r for r in (previous or [])}
    base = str(REPO / "core" / "src" / "main")
    ddo = os.path.join(base, "java/com/datastax/oss/driver/api/core/config/DefaultDriverOption.java")
    tdo = os.path.join(base, "java/com/datastax/oss/driver/api/core/config/TypedDriverOption.java")
    conf = os.path.join(base, "resources/reference.conf")
    typed = set(re.findall(r"DefaultDriverOption\.(\w+)", open(tdo, encoding="utf-8").read()))
    defaults = hocon_defaults(conf)
    rows = []
    for m in re.finditer(r"^\s{2}([A-Z][A-Z0-9_]*)\(\s*\"([^\"]+)\"",
                         open(ddo, encoding="utf-8").read(), re.M):
        name, path = m.group(1), m.group(2)
        disp, why = "", ""
        best = -1
        for prefix, d, w in DISPOSITION_RULES:
            if path.startswith(prefix) and len(prefix) > best:
                disp, why, best = d, w, len(prefix)
        prev = prior.get(name, {})
        rows.append(OrderedDict([
            ("Option", name), ("Config path", path),
            ("Typed?", 1 if name in typed else 0),
            ("Default (reference.conf)", defaults.get(path, "")),
            ("Disposition", prev.get("Disposition") or disp),
            ("Comment", prev.get("Comment") or why),
        ]))
    return rows


# --------------------------------------------------------------------------
# Stage 6 -- rollups
# --------------------------------------------------------------------------

CATEGORY_COLUMNS = ["Scope", "Name", "Counted to total sum", "host-side",
                    "needs-bridge", "Implemented and waiting as PRs",
                    "Merged to master", "Still to bridge"]


def category_rows(func_rows):
    """Per-class then per-package rollups.

    The four columns that depend on the hand-maintained tracking columns are
    written as live formulas (with the computed value cached), so ticking a row
    on 'Function list' updates the rollup without re-running the generator.
    """
    # REPORT_FUNCTION_COLUMNS, not FUNCTION_COLUMNS: the rendered sheet carries
    # the extra "ITs" column, and every reference past it would be off by one.
    col = {name: col_letter(i) for i, name in enumerate(REPORT_FUNCTION_COLUMNS)}
    last = len(func_rows) + 1
    rng = lambda name: "'Function list'!$%s$2:$%s$%d" % (col[name], col[name], last)

    def criteria(pkg, cls):
        c = "%s,\"%s\"" % (rng("Package"), pkg)
        if cls is not None:
            c += ",%s,\"%s\"" % (rng("Class"), cls)
        return c

    def rollup(scope, key):
        buckets = OrderedDict()
        for r in func_rows:
            buckets.setdefault(key(r), []).append(r)
        out = []
        for name in sorted(buckets):
            rs = buckets[name]
            pkg = rs[0]["Package"]
            cls = rs[0]["Class"] if scope == "class" else None
            crit = criteria(pkg, cls)
            counted = sum(r["Counted to total sum"] for r in rs)
            host = sum(1 for r in rs if r["Counted to total sum"] and r["Status"] == "host-side")
            bridge = sum(1 for r in rs if r["Counted to total sum"] and r["Status"] == "needs-bridge")
            inpr = sum(1 for r in rs if str(r.get("Implemented and waiting as PRs", "")).strip() not in ("", "0"))
            merged = sum(1 for r in rs if str(r.get("Merged to master", "")).strip() not in ("", "0"))
            n = len(out) + 2                        # this row's number on the Categories sheet
            out.append(OrderedDict([
                ("Scope", scope), ("Name", name),
                ("Counted to total sum", ("SUMIFS(%s,%s)" % (rng("Counted to total sum"), crit), counted)),
                ("host-side", ("COUNTIFS(%s,%s,\"host-side\")" % (crit, rng("Status")), host)),
                ("needs-bridge", ("COUNTIFS(%s,%s,\"needs-bridge\")" % (crit, rng("Status")), bridge)),
                ("Implemented and waiting as PRs",
                 ("COUNTIFS(%s,%s,\"<>\")" % (crit, rng("Implemented and waiting as PRs")), inpr)),
                ("Merged to master",
                 ("COUNTIFS(%s,%s,\"<>\")" % (crit, rng("Merged to master")), merged)),
                ("Still to bridge", ("E{n}-G{n}".replace("{n}", str(n)), bridge - merged)),
            ]))
        return out

    rows = rollup("package", lambda r: r["Package"])
    n_pkg = len(rows)
    class_rows = rollup("class", lambda r: "%s.%s" % (r["Package"], r["Class"]))
    # the 'Still to bridge' formula was numbered from 2; shift the class block down
    for i, r in enumerate(class_rows):
        n = n_pkg + i + 2
        r["Still to bridge"] = ("E%d-G%d" % (n, n), r["Still to bridge"][1])
    return rows + class_rows


def summary_pairs(func_rows, it_rows):
    counted = [r for r in func_rows if r["Counted to total sum"]]
    host = sum(1 for r in counted if r["Status"] == "host-side")
    bridge = len(counted) - host
    merged = sum(1 for r in counted if str(r.get("Merged to master", "")).strip() not in ("", "0"))
    inpr = sum(1 for r in counted if str(r.get("Implemented and waiting as PRs", "")).strip() not in ("", "0"))
    quarantined = sum(1 for r in it_rows if r["State"] == "quarantined")
    active = sum(1 for r in it_rows if r["State"] == "active")
    total_it = quarantined + active
    return [
        ("API members counted", len(counted)),
        ("  host-side (survive the transport cut)", host),
        ("  need the Rust bridge", bridge),
        ("Bridged and merged", merged),
        ("Bridged, waiting in PRs", inpr),
        ("% of the bridging backlog done", round(merged / bridge, 4) if bridge else 0),
        ("% of the API needing no bridge", round(host / len(counted), 4) if counted else 0),
        ("Integration tests kept", total_it),
        ("  of which quarantined (BrokenTests)", quarantined),
        ("  of which running", active),
        ("% of the IT suite migrated back", round(active / total_it, 4) if total_it else 0),
    ]


# --------------------------------------------------------------------------
# Stage 7 -- xlsx writer (SpreadsheetML over zipfile, no dependencies)
# --------------------------------------------------------------------------


def col_letter(idx):
    s = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        s = chr(65 + rem) + s
    return s


def xml_escape(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def cell_xml(ref, value, style=None, formula=None):
    st = ' s="%d"' % style if style else ""
    if formula is not None:
        return '<c r="%s"%s><f>%s</f><v>%s</v></c>' % (
            ref, st, xml_escape(formula), xml_escape(value))
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, (int, float)):
        return '<c r="%s"%s><v>%s</v></c>' % (ref, st, value)
    if value is None or value == "":
        return '<c r="%s"%s/>' % (ref, st)
    return '<c r="%s"%s t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>' % (
        ref, st, xml_escape(value))


def sheet_xml(columns, rows, freeze=True, autofilter=True):
    parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">']
    # element order is fixed by the schema: sheetViews, then cols, then sheetData
    if freeze:
        parts.append('<sheetViews><sheetView workbookViewId="0">'
                     '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
                     '</sheetView></sheetViews>')
    widths = []
    for i, c in enumerate(columns):
        w = max(len(str(c)) + 3, 10)
        sample = [len(str(r.get(c, ""))) for r in rows[:400]]
        if sample:
            w = min(max(w, sorted(sample)[int(len(sample) * 0.9)] + 2), 60)
        widths.append('<col min="%d" max="%d" width="%d" customWidth="1"/>' % (i + 1, i + 1, w))
    parts.append("<cols>%s</cols>" % "".join(widths))
    parts.append("<sheetData>")
    header = "".join(cell_xml("%s1" % col_letter(i), c, style=1) for i, c in enumerate(columns))
    parts.append('<row r="1">%s</row>' % header)
    for n, row in enumerate(rows, start=2):
        cells = []
        for i, c in enumerate(columns):
            v = row.get(c, "")
            ref = "%s%d" % (col_letter(i), n)
            if isinstance(v, tuple):        # (formula, cached value)
                cells.append(cell_xml(ref, v[1], formula=v[0]))
            else:
                cells.append(cell_xml(ref, v))
        parts.append('<row r="%d">%s</row>' % (n, "".join(cells)))
    parts.append("</sheetData>")
    if autofilter and rows:
        parts.append('<autoFilter ref="A1:%s%d"/>' % (col_letter(len(columns) - 1), len(rows) + 1))
    parts.append("</worksheet>")
    return "".join(parts)


STYLES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>
<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="2"><fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>
</styleSheet>"""


def write_xlsx(path, sheets):
    """sheets: list of (name, columns, rows)."""
    n = len(sheets)
    content_types = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
                     '<Default Extension="xml" ContentType="application/xml"/>',
                     '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
                     '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>']
    for i in range(n):
        content_types.append('<Override PartName="/xl/worksheets/sheet%d.xml" '
                             'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' % (i + 1))
    content_types.append("</Types>")

    wb = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">',
          '<sheets>']
    for i, (name, _c, _r) in enumerate(sheets):
        wb.append('<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (xml_escape(name), i + 1, i + 1))
    wb += ["</sheets>", '<calcPr fullCalcOnLoad="1"/>', "</workbook>"]

    wb_rels = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
               '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
    for i in range(n):
        wb_rels.append('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/'
                       'officeDocument/2006/relationships/worksheet" Target="worksheets/sheet%d.xml"/>'
                       % (i + 1, i + 1))
    wb_rels.append('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/'
                   'officeDocument/2006/relationships/styles" Target="styles.xml"/>' % (n + 1))
    wb_rels.append("</Relationships>")

    root_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                 '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
                 '2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "".join(content_types))
        z.writestr("_rels/.rels", root_rels)
        z.writestr("xl/workbook.xml", "".join(wb))
        z.writestr("xl/_rels/workbook.xml.rels", "".join(wb_rels))
        z.writestr("xl/styles.xml", STYLES_XML)
        for i, (_name, columns, rows) in enumerate(sheets):
            z.writestr("xl/worksheets/sheet%d.xml" % (i + 1), sheet_xml(columns, rows))


# --------------------------------------------------------------------------
# Stage 8 -- read an existing workbook (carry-forward + --check)
# --------------------------------------------------------------------------

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def read_xlsx(path):
    """{sheet name: [dict per row]} -- tolerates shared strings and inline strings."""
    if not os.path.exists(path):
        return {}
    out = {}
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            sst = ET.fromstring(z.read("xl/sharedStrings.xml"))
            shared = ["".join(t.text or "" for t in si.iter(NS + "t")) for si in sst]
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        target = {}
        for rel in rels:
            target[rel.get("Id")] = rel.get("Target")
        rid = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        for sh in wb.find(NS + "sheets"):
            name = sh.get("name")
            tgt = target.get(sh.get(rid), "")
            part = "xl/" + tgt.lstrip("/")
            if part not in z.namelist():
                continue
            grid = []
            for row in ET.fromstring(z.read(part)).iter(NS + "row"):
                cells = {}
                for c in row.iter(NS + "c"):
                    ref = c.get("r") or ""
                    col = "".join(ch for ch in ref if ch.isalpha())
                    t, v, isel = c.get("t"), c.find(NS + "v"), c.find(NS + "is")
                    if t == "s" and v is not None:
                        val = shared[int(v.text)]
                    elif isel is not None:
                        val = "".join(x.text or "" for x in isel.iter(NS + "t"))
                    elif v is not None:
                        val = v.text
                    else:
                        val = ""
                    cells[col] = val
                grid.append(cells)
            if not grid:
                out[name] = []
                continue
            header = grid[0]
            cols = [(k, header[k]) for k in sorted(header, key=lambda s: (len(s), s))]
            out[name] = [{label: r.get(letter, "") for letter, label in cols} for r in grid[1:]]
    return out


# --------------------------------------------------------------------------
# Stage 8 -- the committed CSVs
# --------------------------------------------------------------------------

NOTES_COLUMNS = ["Item", "Value"]

# Internal columns (leading underscore) never reach a CSV or a sheet.
def public_columns(columns):
    return [c for c in columns if not c.startswith("_")]


def read_csv(path):
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return [OrderedDict(r) for r in csv.DictReader(fh)]


def write_csv(path, columns, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=public_columns(columns), extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: (r.get(c, "")[1] if isinstance(r.get(c), tuple) else r.get(c, ""))
                        for c in public_columns(columns)})


def committed():
    """Everything the repo currently records, as it was last committed."""
    return {
        "function": read_csv(FUNCTION_CSV),
        "it": read_csv(IT_CSV),
        "error": read_csv(ERROR_CSV),
        "config": read_csv(CONFIG_CSV),
    }


def it_key(row):
    return "%s.%s" % (row.get("Package", ""), row.get("Test class", ""))


def restore_it_paths(rows):
    """The committed IT sheet has no _path column; rebuild it from the package."""
    for r in rows:
        pkg = (r.get("Package") or "").replace(".", "/")
        r["_path"] = "integration-tests/src/test/java/%s/%s.java" % (pkg, r.get("Test class", ""))
    return rows


# --------------------------------------------------------------------------
# Stage 9 -- assembly
# --------------------------------------------------------------------------


def regenerate(prev, upstream=None, rust_src=None):
    """Recompute every sheet from the built tree, carrying the human columns."""
    require_build()
    api_rows, types = enumerate_api()
    prev_it = restore_it_paths(list(prev["it"]))
    it_rows = integration_tests({r["_fqn"] for r in api_rows}, prev_it)
    carried = {row_key(r): r for r in prev["function"]}
    unknown = annotate(api_rows, carried, upstream)
    err = error_rows(types, prev["error"], rust_src)
    cfg = config_rows(prev["config"])

    warnings = []
    if unknown:
        warnings.append("%d member(s) have no 'Scylla-only?' value -- they are new since the "
                        "sheet was last refreshed with --upstream-repo" % unknown)
    if not any(r.get("Side") == "rust" for r in err):
        warnings.append("the Rust error block is empty -- refresh it with --rust-src <path to "
                        "a scylla-rust-driver checkout>")
    orphans = [row_key(r) for r in prev["function"]
               if any(r.get(c) for c in HUMAN_COLUMNS)
               and row_key(r) not in {row_key(x) for x in api_rows}]
    return {"function": api_rows, "it": it_rows, "error": err, "config": cfg}, warnings, orphans


def sheets_for_report(state, warnings=(), orphans=()):
    api_rows = state["function"]
    it_rows = state["it"]
    cats = category_rows(api_rows)
    summary = summary_pairs(api_rows, it_rows)
    notes = notes_rows(state, summary, orphans, warnings)
    return [
        ("Function list", REPORT_FUNCTION_COLUMNS, api_rows),
        ("Categories", CATEGORY_COLUMNS, cats),
        ("Integration tests", REPORT_IT_COLUMNS, it_rows),
        ("Error mapping", ERROR_COLUMNS, state["error"]),
        ("Config options", CONFIG_COLUMNS, state["config"]),
        ("Notes", NOTES_COLUMNS, notes),
    ], summary


def fill_it_results(state):
    """Join whatever failsafe run is on disk onto the IT rows, for the report."""
    summaries = failsafe_summaries()
    for r in state["it"]:
        s_ = summaries.get(r["Test class"]) if r.get("State") != "deleted" else None
        r["Passed"] = (s_["tests"] - s_["failures"] - s_["skipped"]) if s_ else ""
        r["Failed"] = s_["failures"] if s_ else ""
        r["Skipped"] = s_["skipped"] if s_ else ""


def fill_it_usage(state):
    """Count, per API type, how many live integration tests import it.

    Keyed on the fully-qualified name: four api simple names (Insert, Select,
    Update, Delete) exist in both the mapper and query-builder packages, so a
    simple-name join would miscount exactly the types people use most.
    """
    spec = REPO / "integration-tests" / "src" / "test" / "java"
    usage = Counter()
    if spec.is_dir():
        live = {r["_path"] for r in state["it"] if r.get("State") != "deleted"}
        imports = scan_tests(spec, r"^import (static )?com\.datastax\.oss\.driver\.api\.")
        known = {"%s.%s" % (r["Package"], r["Class"].split(".", 1)[0]) for r in state["function"]}
        for path, lines in imports.items():
            if path not in live:
                continue
            seen = set()
            for imp in lines:
                m = re.search(r"com\.datastax\.oss\.driver\.api\.[\w.]+", imp)
                if not m:
                    continue
                name = m.group(0)
                if name not in known:              # a static import of a member
                    name = name.rsplit(".", 1)[0]
                if name in known:
                    seen.add(name)
            for name in seen:
                usage[name] += 1
    for r in state["function"]:
        r["ITs"] = usage.get("%s.%s" % (r["Package"], r["Class"].split(".", 1)[0]), 0)


def head_sha():
    return git(["rev-parse", "--short", "HEAD"], cwd=REPO, check=False).strip() or "unknown"


def head_describe():
    branch = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=REPO, check=False).strip()
    return "%s @ %s" % (branch or "detached", head_sha())


def rust_snapshot_note():
    prov = read_provenance()
    if not prov.get("rust_commit"):
        return "not recorded -- refresh with `make api-baseline API_TRACKER_ARGS=\"--rust-src ...\"`"
    return "%s @ %s, taken %s" % (prov.get("rust_repo", "scylla-rust-driver"),
                                  prov["rust_commit"], prov.get("rust_captured", "?"))


def notes_rows(state, summary, orphans, warnings):
    func_rows, it_rows = state["function"], state["it"]
    rows = [("Generated", datetime.datetime.now().strftime("%Y-%m-%d %H:%M")),
            ("From", head_describe()),
            ("Generator", "tools/api-tracker/generate.py -- `make api-report` to refresh"),
            ("Tracked state", "tools/api-tracker/*.csv, committed; this workbook is a render"),
            ("", "")]
    rows += [(k, v) for k, v in summary]
    rows += [("", ""),
             ("Modules enumerated", ", ".join(MODULES)),
             ("Modules excluded", "test-infra (test scaffolding), examples, mapper-processor and "
                                  "metrics/* (no api package)"),
             ("DSE", "deleted from this driver (decision D1); no DSE row exists here"),
             ("Rust error snapshot", rust_snapshot_note()),
             ("", ""),
             ("Priority tiers",
              "; ".join("%s -> %s (%s)" % (pkg, tier, why)
                        for pkg, tier, why in PRIORITY_BY_PACKAGE)
              + "; everything else T1"),
             ("How 'Status' is derived",
              "host-side = the declaring package survives the transport cut untouched (plan S4 "
              "'keep working': type/data/config/detach/uuid/time, all of query-builder and "
              "mapper-runtime) and therefore works today. needs-bridge = reachable only through "
              "the session/request/metadata path, i.e. dead until the Rust core is wired in."),
             ("Why not a per-member stub scan",
              "NOT YET IMPLEMENTED (java-rs) lives in a handful of internal files and in no api/ "
              "source file at all -- it marks choke points (DefaultSession, MetadataManager, "
              "CqlRequestAsyncProcessor, ...), not individual API members. 'Stub anchor' names "
              "that choke point where the api type maps to one unambiguously."),
             ("'Scylla-only?' granularity",
              "file-level, and carried in api-tracker.csv: this repo has no upstream remote, so "
              "the value is refreshed only with --upstream-repo. A Scylla-added member inside a "
              "file DataStax also ships is not flagged."),
             ("'@Test (declared)' granularity",
              "declarations, not executions: some IT classes use @DataProviderRunner, and classes "
              "showing 0 inherit their tests from a *ITBase. Passed/Failed/Skipped stay blank "
              "until integration-tests/target/failsafe-reports exists."),
             ("Human columns",
              "Function list: Github issue? / Implemented and waiting as PRs / Merged to master / "
              "Comment. Config options: Disposition / Comment. Error mapping: Java exception / "
              "Explicitly implemented / All variants mapped / Comment. Edit them here and run "
              "`make api-import` to fold them back into the CSVs, or edit the CSVs directly; "
              "either way `make api-baseline` carries them and `make api-check` ignores them."),
             ("", "")]
    rows += [("Row counts", ""),
             ("  Function list", len(func_rows)),
             ("  Integration tests", len(it_rows)),
             ("  Error mapping", len(state["error"])),
             ("  Config options", len(state["config"]))]
    if warnings:
        rows += [("", ""), ("Warnings", "")]
        rows += [("  ", w) for w in warnings]
    if orphans:
        rows += [("", ""), ("Orphaned annotations (row key no longer in the API)", "")]
        rows += [("  ", o) for o in orphans]
    return [OrderedDict([("Item", k), ("Value", v)]) for k, v in rows]


# --------------------------------------------------------------------------
# Stage 10 -- commands
# --------------------------------------------------------------------------

SHEET_CSV = [
    ("function", FUNCTION_CSV, "FUNCTION_COLUMNS"),
    ("it", IT_CSV, "IT_COLUMNS"),
    ("error", ERROR_CSV, "ERROR_COLUMNS"),
    ("config", CONFIG_CSV, "CONFIG_COLUMNS"),
]


def columns_for(name):
    return {"function": FUNCTION_COLUMNS, "it": IT_COLUMNS,
            "error": ERROR_COLUMNS, "config": CONFIG_COLUMNS}[name]


def cmd_baseline(args):
    state, warnings, orphans = regenerate(committed(), args.upstream, args.rust_src)
    for name, path, _ in SHEET_CSV:
        write_csv(path, columns_for(name), state[name])
        print("%-22s %5d rows" % (path.name, len(state[name])))
    for w in warnings:
        print("WARNING: %s" % w)
    for o in orphans:
        print("ORPHANED ANNOTATION: %s" % o)
    return 0


# What `check` must NOT compare, per sheet: cells a human owns, cells carried
# from a checkout this repo does not have, and cells that only reflect whether
# someone happens to have run the integration tests locally.
NON_GENERATED = {
    "function": HUMAN_COLUMNS + CARRIED_COLUMNS,
    "it": [],
    "error": ERROR_HUMAN_COLUMNS,
    "config": CONFIG_HUMAN_COLUMNS,
}


def generated_columns(name):
    skip = NON_GENERATED[name]
    return [c for c in public_columns(columns_for(name)) if c not in skip]


def cmd_check(args):
    prev = committed()
    if not prev["function"]:
        sys.exit("no committed sheet at %s -- run `make api-baseline` first" % FUNCTION_CSV)
    state, _warnings, orphans = regenerate(prev, args.upstream, args.rust_src)
    problems = []

    def compare(name, key):
        cols = generated_columns(name)
        old = {key(r): r for r in prev[name]}
        new = {key(r): r for r in state[name]}
        for k in sorted(set(old) - set(new)):
            problems.append("%s: REMOVED %s" % (name, k))
        for k in sorted(set(new) - set(old)):
            problems.append("%s: ADDED   %s" % (name, k))
        for k in sorted(set(old) & set(new)):
            for c in cols:
                a, b = str(old[k].get(c, "")), str(new[k].get(c, ""))
                if a != b:
                    problems.append("%s: CHANGED %s -- %s: %r -> %r" % (name, k, c, a, b))

    compare("function", row_key)
    compare("it", it_key)
    compare("config", lambda r: r.get("Option", ""))
    compare("error", error_key)
    for o in orphans:
        problems.append("orphaned annotation: %s" % o)

    for line in problems[:200]:
        print(line)
    if len(problems) > 200:
        print("... and %d more" % (len(problems) - 200))
    if problems:
        print("\n%d difference(s) -- run `make api-baseline` and review the diff" % len(problems))
        return 1
    print("api tracker up to date (%d members, %d integration tests)"
          % (len(state["function"]), len(state["it"])))
    return 0


def cmd_report(args):
    state = committed()
    if not state["function"]:
        sys.exit("no committed sheet at %s -- run `make api-baseline` first" % FUNCTION_CSV)
    restore_it_paths(state["it"])
    for r in state["function"]:
        for c in ("Deprecated?", "Scylla-only?", "Counted to total sum"):
            r[c] = int(r[c]) if str(r.get(c, "")).strip().lstrip("-").isdigit() else r.get(c, "")
    fill_it_results(state)
    for r in state["it"]:
        for c in ["@Test (declared)"] + RESULT_COLUMNS:
            r[c] = int(r[c]) if str(r.get(c, "")).strip().lstrip("-").isdigit() else r.get(c, "")
    fill_it_usage(state)
    sheets, summary = sheets_for_report(state)
    OUT.mkdir(parents=True, exist_ok=True)
    for name, columns, rows in sheets:
        write_csv(OUT / (name.lower().replace(" ", "-") + ".csv"), columns, rows)
    write_xlsx(str(WORKBOOK), [(n, public_columns(c), r) for n, c, r in sheets])
    for name, _c, rows in sheets:
        print("%-20s %5d rows" % (name, len(rows)))
    print()
    for k, v in summary:
        print("%-40s %s" % (k, v))
    print("\nwrote %s" % WORKBOOK)
    problems = self_check(sheets)
    for pr in problems:
        print("FAIL %s" % pr)
    return 1 if problems else 0


def cmd_import(args):
    if not WORKBOOK.exists():
        sys.exit("no workbook at %s -- run `make api-report`, edit it, then import" % WORKBOOK)
    book = read_xlsx(str(WORKBOOK))
    state = committed()
    if not state["function"]:
        sys.exit("no committed sheet at %s" % FUNCTION_CSV)
    changed, cleared, unmatched = [], [], []

    def fold(sheet_name, csv_name, key, human):
        edited = {key(r): r for r in book.get(sheet_name, [])}
        known = {key(r) for r in state[csv_name]}
        # A workbook rendered before a rename carries annotations whose row no
        # longer exists.  Say so -- dropping them silently is what the CSVs
        # exist to prevent.
        for k in sorted(set(edited) - known):
            if any((edited[k].get(c) or "").strip() for c in human):
                unmatched.append("%s: %s" % (csv_name, k))
        for row in state[csv_name]:
            k = key(row)
            if k not in edited:
                continue
            for c in human:
                new, old = (edited[k].get(c) or "").strip(), (row.get(c) or "").strip()
                if new == old:
                    continue
                if old and not new and not args.force:
                    cleared.append("%s: %s -- %s: %r would be cleared" % (csv_name, k, c, old))
                    continue
                row[c] = new
                changed.append("%s: %s -- %s: %r -> %r" % (csv_name, k, c, old, new))

    fold("Function list", "function", row_key, HUMAN_COLUMNS)
    fold("Config options", "config", lambda r: r.get("Option", ""), CONFIG_HUMAN_COLUMNS)

    for u in unmatched:
        print("UNMATCHED %s" % u)
    if cleared:
        for c in cleared:
            print("REFUSED %s" % c)
        print("\n%d annotation(s) would be cleared; re-run with --force to accept" % len(cleared))
        return 1
    if unmatched:
        print("\n%d annotated workbook row(s) match nothing in the CSVs -- the workbook predates a "
              "rename or removal; re-run `make api-report` and redo those" % len(unmatched))
        return 1
    for name, path, _ in SHEET_CSV:
        write_csv(path, columns_for(name), state[name])
    for c in changed:
        print(c)
    print("\n%d change(s) imported into tools/api-tracker/*.csv" % len(changed))
    return 0


def check_formulas():
    """Evaluate every Categories formula against the Function list and compare
    it with the value cached in the file, so a recalculation in Excel or Sheets
    cannot silently disagree with what a reader sees."""
    problems = []
    with zipfile.ZipFile(WORKBOOK) as z:
        fl_xml = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
        cat_xml = ET.fromstring(z.read("xl/worksheets/sheet2.xml"))

    def grid(sheet):
        out = {}
        for row in sheet.iter(NS + "row"):
            r = int(row.get("r"))
            for c in row.iter(NS + "c"):
                ref = c.get("r")
                col = "".join(ch for ch in ref if ch.isalpha())
                isel, v = c.find(NS + "is"), c.find(NS + "v")
                if isel is not None:
                    out[(col, r)] = "".join(x.text or "" for x in isel.iter(NS + "t"))
                elif v is not None:
                    out[(col, r)] = v.text
                else:
                    out[(col, r)] = ""
        return out

    fl, cat = grid(fl_xml), grid(cat_xml)
    n_fl = max(r for _c, r in fl)
    pair = re.compile(r"'Function list'!\$([A-Z]+)\$\d+:\$[A-Z]+\$\d+,&?\??\"?([^\",]*)\"?")

    def args_of(formula):
        """[(column, literal), ...] from the range/criterion pairs."""
        return re.findall(r"'Function list'!\$([A-Z]+)\$2:\$[A-Z]+\$\d+,\"([^\"]*)\"", formula)

    for row in cat_xml.iter(NS + "row"):
        r = int(row.get("r"))
        if r == 1:
            continue
        for c in row.iter(NS + "c"):
            f = c.find(NS + "f")
            v = c.find(NS + "v")
            if f is None or f.text is None or v is None:
                continue
            formula, cached = f.text, v.text
            if formula.startswith("SUMIFS("):
                sum_col = re.match(r"SUMIFS\('Function list'!\$([A-Z]+)", formula).group(1)
                conds = args_of(formula)
                total = sum(int(fl.get((sum_col, i)) or 0) for i in range(2, n_fl + 1)
                            if all(fl.get((col, i)) == lit for col, lit in conds))
                got = int(cached)
            elif formula.startswith("COUNTIFS("):
                conds = args_of(formula)
                nonblank = re.findall(r"'Function list'!\$([A-Z]+)\$2:\$[A-Z]+\$\d+,\"<>\"", formula)
                conds = [(col, lit) for col, lit in conds if lit != "<>"]
                total = 0
                for i in range(2, n_fl + 1):
                    if not all(fl.get((col, i)) == lit for col, lit in conds):
                        continue
                    if all((fl.get((col, i)) or "") != "" for col in nonblank):
                        total += 1
                got = int(cached)
            else:
                continue
            if total != got:
                problems.append("Categories %s%d: formula evaluates to %d, cached %d"
                                % (c.get("r")[:1], r, total, got))
    return problems


def self_check(sheets):
    """Assert the rendered workbook agrees with what we just wrote."""
    problems = []
    by_name = {n: (c, r) for n, c, r in sheets}
    book = read_xlsx(str(WORKBOOK))
    for name in by_name:
        if name not in book:
            problems.append("sheet missing from the workbook: %s" % name)
            continue
        if len(book[name]) != len(by_name[name][1]):
            problems.append("%s: %d rows in memory, %d in the workbook"
                            % (name, len(by_name[name][1]), len(book[name])))
    fl = book.get("Function list", [])
    for i, r in enumerate(fl, start=2):
        if not r.get("Class") or not r.get("Method/Property name"):
            problems.append("Function list row %d has an empty Class or member" % i)
            break
    cats = book.get("Categories", [])
    total = sum(int(r["Counted to total sum"] or 0) for r in cats if r.get("Scope") == "class")
    counted = sum(int(r.get("Counted to total sum") or 0) for r in fl)
    if total != counted:
        problems.append("Categories class rollup sums to %d, Function list counts %d"
                        % (total, counted))
    problems += check_formulas()
    return problems


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    def with_refresh(parser):
        parser.add_argument("--upstream-repo", dest="upstream_repo", metavar="PATH",
                            help="a driver clone that has the upstream DataStax remote fetched, "
                                 "used to refresh the 'Scylla-only?' column")
        parser.add_argument("--upstream-ref", dest="upstream_ref", default="apache/4.x",
                            metavar="REF", help="the upstream ref to diff against "
                                                "(default: apache/4.x)")
        parser.add_argument("--rust-src", dest="rust_src", metavar="PATH",
                            help="a scylla-rust-driver checkout, used to refresh the Rust half "
                                 "of the error mapping")
        return parser

    with_refresh(sub.add_parser("baseline", help="refresh the generated columns of the CSVs"))
    with_refresh(sub.add_parser("check", help="fail if the built tree has drifted from the CSVs"))
    sub.add_parser("report", help="render out/*.csv and the workbook (no build needed)")
    imp = sub.add_parser("import", help="fold workbook edits back into the CSVs")
    imp.add_argument("--force", action="store_true",
                     help="accept annotations being cleared")

    args = ap.parse_args()
    args.upstream = None
    if getattr(args, "upstream_repo", None):
        args.upstream = (args.upstream_repo, args.upstream_ref)
    if not hasattr(args, "rust_src"):
        args.rust_src = None
    return {"baseline": cmd_baseline, "check": cmd_check,
            "report": cmd_report, "import": cmd_import}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
