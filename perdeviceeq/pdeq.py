# -*- coding: utf-8 -*-
"""One-file .pdeq profile package: the exchange artifact.

A package is the store's OWN canonical body, bare -- no envelope.
Schema v3 already made the profile self-contained (playback body
plus provenance, device, fit and the full measurement canvas), so
a wrapper had nothing left to add: review peeled off an embedded
sha256 first (an address is computed by whoever receives or
indexes the bytes, never carried inside them -- the git/nix/OCI
rule), then a wrapper version (one number, one contract: the
body's own schema), then the wrapper itself. What remains is
procedures, each with one job.

pack() writes canonical bytes: the store's _body, sorted keys,
tight separators, UTF-8, no trailing newline. Deterministic bytes
make one profile own one address -- sha256 over the file bytes IS
the package address, and `sha256sum *.pdeq` is the whole index
tooling. The address is content-level: unpack() computes it from
the parsed payload, so a re-formatted copy of the same profile
still resolves to the same address.

unpack() validates with directional refusals: not JSON, not a
profile (no schema version), a newer body (points at a newer
build), an older body (points at the one-shot migration tool).

absorb() is the import semantics: an import never destroys what
is already in the store. A free id is kept and the payload stays
byte-identical; an id collision with identical content (the same
computed address) is a spoken no-op instead of a minted
duplicate; a collision with different content remints the copy
and writes original_id plus the computed package sha into
provenance. RUNTIME_KEYS exist for one reason: the store
decorates records in RAM with path and builtin (never on disk),
and payload_sha256() must give a live store record the same
address as its package -- the dedup comparison stands on that.

package_report() makes the payload's claims readable for the
human in the import dialog (and later, the package card in an
index): origin, corrected device, fit provenance, canvas span,
rig with per-channel cal hashes, computed address. It proves
nothing; data does. Trust in a submission is earned by the canvas
itself -- take coherence, physical structure, cross-checks
against published measurements -- which an auditor reads from the
payload. No container can add a bit to that.

When the schema bumps (a v4 day), the no-legacy doctrine holds:
the app carries no old loaders. pack() refuses anything but the
current schema, so every package ever produced is current-schema
at birth; unpack() in a v4 build refuses a v3 body with a
sentence pointing at the one-shot migration tool, and that tool
learns to eat .pdeq alongside store files: unpack the old,
migrate the body, pack the new. Migration is a transformation, so
it honestly mints a NEW package with a NEW address; the old bytes
stay a valid v3 object forever under their old address, and the
lineage slot is provenance (migrated_from) when that day comes.

If signatures ever come, they ride detached (a .minisig next to
the file or in the index), consistent with objects that never
attest themselves.

No GTK. JSON only.
"""

import hashlib
import json

from .config import SCHEMA_VERSION
from .profiles import ProfileStore

RUNTIME_KEYS = ("builtin", "path")


def _stripped(profile):
    return {k: v for k, v in profile.items() if k not in RUNTIME_KEYS}


def _canon(profile):
    return json.dumps(profile, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def payload_sha256(profile):
    """Hex sha256 over the canonical payload -- the package's
    address in any sha-addressed index. Always computed from the
    bytes at hand, never read from the file: edited bytes simply
    ARE a different package with a different address, so a package
    cannot lie about its identity -- it does not claim one."""
    return hashlib.sha256(_canon(_stripped(profile))).hexdigest()


def pdeq_pack(profile):
    """The profile as .pdeq text: the store's canonical body,
    bare. sha256 over these bytes IS the package address, so
    `sha256sum *.pdeq` indexes a directory of packages. Refuses
    foreign schema versions (the store's _body would silently
    rebrand them); a package always carries a body the current
    store can load and will save back byte-identically."""
    if profile.get("version") != SCHEMA_VERSION:
        raise ValueError("profile schema v%s; the package format "
                         "carries v%s bodies only"
                         % (profile.get("version"), SCHEMA_VERSION))
    return _canon(ProfileStore._body(profile)).decode("utf-8")


def pdeq_unpack(text):
    """(profile, sha) from .pdeq text, the sha computed from the
    payload at hand. Raises ValueError with a human sentence on
    anything unfit: not a package (truncation and transport
    mangling die here, at the JSON parse), not a profile, or a
    body from a foreign schema -- a newer body points at a newer
    build, an older one at the one-shot migration tool."""
    try:
        prof = json.loads(text)
    except Exception:
        raise ValueError("not a .pdeq package: the file is not JSON")
    if not isinstance(prof, dict) or "version" not in prof:
        raise ValueError("not a per-device-eq profile: no schema "
                         "version")
    prof = _stripped(prof)
    sha = payload_sha256(prof)
    v = prof.get("version")
    if v != SCHEMA_VERSION:
        if isinstance(v, int) and v > SCHEMA_VERSION:
            raise ValueError("body schema v%s needs a newer build "
                             "(this one loads v%s)"
                             % (v, SCHEMA_VERSION))
        raise ValueError("body schema v%s: convert the package "
                         "once with the migration tool (this "
                         "build loads v%s)" % (v, SCHEMA_VERSION))
    return prof, sha


def package_report(profile, sha):
    """Provenance lines the import dialog shows verbatim: what this
    is, what it corrects, how the bands were derived, on which rig
    the canvas was captured, and the package identity."""
    lines = []
    lines.append("Profile \u201c%s\u201d"
                 % (profile.get("name") or profile.get("id")))
    prov = profile.get("provenance") or {}
    if prov.get("kind"):
        lines.append("Origin: %s" % prov["kind"])
    dev = profile.get("device") or {}
    if dev.get("label"):
        lines.append("Corrects: %s" % dev["label"])
    fit = profile.get("fit") or {}
    if fit:
        bits = []
        if fit.get("algo"):
            bits.append(str(fit["algo"]))
        if fit.get("target"):
            bits.append("target %s" % fit["target"])
        takes = fit.get("takes")
        if isinstance(takes, (list, tuple)) and takes:
            bits.append("%d takes" % len(takes))
        if fit.get("at"):
            bits.append("at %s" % fit["at"])
        if bits:
            lines.append("Fit: " + ", ".join(bits))
        if fit.get("edited"):
            lines.append("Bands were edited by hand after the fit.")
    meas = profile.get("measurement") or {}
    if meas:
        grid = meas.get("grid") or {}
        if grid:
            lines.append("Canvas: %g-%g Hz, %s pts/oct, "
                         "%d takes"
                         % (grid.get("f_lo", 0.0),
                            grid.get("f_hi", 0.0),
                            grid.get("ppo", "?"),
                            len(meas.get("takes") or [])))
        src = meas.get("source") or {}
        if src.get("name"):
            rig = "Rig: %s" % src["name"]
            cal = src.get("cal") or {}
            shas = sorted(str((c or {}).get("sha256", ""))[:8]
                          for c in cal.values() if c)
            shas = [s for s in shas if s]
            if shas:
                rig += " (cal %s)" % ", ".join(shas)
            lines.append(rig)
    elif not profile.get("measurement"):
        lines.append("No measurement canvas travelled with "
                     "this profile -- bands only.")
    lines.append("Package sha256 %s" % sha[:16])
    return lines


def absorb(store, text):
    """Validate a package and land it in the user store. The
    payload keeps its id when it is free. An id collision with
    IDENTICAL content (same computed address) is a no-op that says
    so -- importing the same package twice must not mint a
    duplicate. A collision with different content remints the copy
    and moves the original id into provenance, next to the
    computed package sha -- where this copy came from stays
    written down. The untouched paths keep the payload
    byte-identical, so a re-export equals the original package.
    Returns (pid, report_lines)."""
    prof, sha = pdeq_unpack(text)
    pid = prof.get("id")
    if pid and store.has(pid):
        if payload_sha256(store.get(pid)) == sha:
            lines = package_report(store.get(pid), sha)
            lines.append("Identical copy already in the store; "
                         "nothing imported.")
            return pid, lines
        prof = dict(prof, provenance=dict(
            prof.get("provenance") or {},
            original_id=pid, package_sha256=sha))
        prof.pop("id", None)
    elif not pid:
        prof = dict(prof, provenance=dict(
            prof.get("provenance") or {}, package_sha256=sha))
    new_pid = store.save_user(prof)
    return new_pid, package_report(store.get(new_pid), sha)
