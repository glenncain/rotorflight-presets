#!/usr/bin/env python3
"""Split a Rotorflight `diff all` / `dump all` into one preset file per concern.

A dump is one big blob that reconfigures everything at once. A preset is
supposed to isolate a single concern so it can be picked, reviewed and applied
on its own. This routes every CLI line of a dump into a named bucket and writes
each non-empty bucket out as a preset file with its `#$` metadata header.

    python3 tools/split_dump.py DUMP --author NAME --model M7R --out presets/NAME

The generated DESCRIPTION lines are a factual inventory of what each file
changes. They are a starting point: edit them afterwards to say *why*.
Identity and per-airframe calibration (mcu_id, signature, acc_calibration, ...)
are dropped rather than filed, and `batch`/`save`/`defaults` are dropped too --
the Configurator wraps the snippet and offers its own Save.
"""

import argparse
import os
import re
import sys
from collections import OrderedDict

# --- what never goes into a preset -----------------------------------------
# Board identity and per-airframe sensor calibration. Sharing these either does
# nothing (the target board overwrites them) or is actively wrong on another
# airframe.
DROP_COMMANDS = {
    "batch", "save", "defaults", "board_name", "board_design",
    "manufacturer_id", "mcu_id", "signature", "profile", "rateprofile",
}
DROP_SETTINGS = {
    "acc_calibration",      # this airframe's accelerometer zero
    "stats_total_flights",  # this airframe's odometer, not configuration
    "stats_total_time_s",
}

# `diff all` prints every aux and adjfunc slot, touched or not. Carrying the
# untouched ones would clear whatever the target had in those slots and gain
# nothing; upstream's own presets list only the rows they actually set.
DEFAULT_ROWS = (
    re.compile(r"^aux \d+ 0 0 900 900 0 0$"),
    re.compile(r"^adjfunc \d+ 0 0 1500 1500 0 1500 1500 1500 1500 0 0 100$"),
)

# --- bucket definitions -----------------------------------------------------
# Ordered: the first bucket whose rule matches a line claims it. `master` lines
# that no bucket claims fall through to "misc".
#
# key: (category, title suffix, description of scope, rule)
#   rule is a dict with any of:
#     settings  - exact `set` names
#     prefixes  - `set` name prefixes
#     features  - feature names (matches `feature X` and `feature -X`)
#     commands  - bare CLI verbs (servo, aux, timer, ...)

BUCKETS = OrderedDict([
    ("remapping", dict(
        file="board-remapping",
        category="REMAPPING",
        title="board remapping",
        blurb="pin, timer and DMA assignments that differ from the board's "
              "stock target definition",
        commands={"resource", "timer", "dma", "map"},
        board_specific=True,
    )),
    ("swash", dict(
        file="swashplate-and-servos",
        category="SETUP",
        title="swashplate and servos",
        blurb="servo travel, centring and speed, mixer input ranges, and "
              "swashplate geometry",
        commands={"servo", "mixer"},
        settings={"tail_center_trim", "collective_tilt_correction_neg",
                  "collective_tilt_correction_pos"},
        prefixes=("swash_",),
    )),
    ("drivetrain", dict(
        file="drivetrain-ratios",
        category="SETUP",
        title="drivetrain ratios",
        blurb="motor pole count and main/tail gear ratios, which is what turns "
              "ESC eRPM into a real headspeed",
        settings={"motor_poles", "main_rotor_gear_ratio", "tail_rotor_gear_ratio"},
    )),
    ("esc", dict(
        file="esc-link",
        category="SETUP",
        title="ESC link and RPM telemetry",
        blurb="the ESC signal protocol, the serial port the ESC talks back on, "
              "and the RPM/headspeed sensor",
        commands={"serial"},
        features={"ESC_SENSOR", "FREQ_SENSOR"},
        settings={"motor_pwm_protocol", "use_unsynced_pwm", "dshot_bidir"},
        prefixes=("esc_sensor_",),
    )),
    ("battery", dict(
        file="battery",
        category="SETUP",
        title="battery and power metering",
        blurb="pack size, cell count, and where voltage and current are measured",
        settings={"bat_capacity", "bat_profile", "battery_cell_count",
                  "current_meter", "battery_meter", "smartfuel"},
        prefixes=("vbat_", "smartfuel_", "ibat_"),
    )),
    ("governor", dict(
        file="governor",
        category="SETUP",
        title="governor and throttle range",
        blurb="governor mode, spool-up behaviour and the throttle endpoints it "
              "works against",
        features={"GOVERNOR"},
        settings={"min_throttle", "max_throttle", "rc_arm_throttle",
                  "rc_min_throttle", "rc_max_throttle", "tail_motor_idle"},
        prefixes=("gov_",),
    )),
    ("telemetry", dict(
        file="telemetry",
        category="SETUP",
        title="RC link telemetry",
        blurb="the telemetry mode and rate on the RC link, and which sensors "
              "are sent down it",
        features={"TELEMETRY"},
        prefixes=("crsf_", "telemetry_", "frsky_", "smartport_"),
    )),
    ("modes", dict(
        file="modes-and-adjustments",
        category="SETUP",
        title="switches, adjustments and deadband",
        blurb="switch assignments, in-flight adjustment functions and stick "
              "deadband",
        commands={"aux", "adjfunc", "rxfail", "rxrange"},
        settings={"deadband", "yaw_deadband"},
    )),
    ("filters", dict(
        file="gyro-filtering",
        category="FILTERS",
        title="gyro filtering",
        blurb="gyro lowpass, dynamic notch and RPM-based notch filtering",
        features={"RPM_FILTER", "DYN_NOTCH"},
        settings={"pid_process_denom"},
        prefixes=("gyro_lpf", "gyro_rpm_notch", "dyn_notch_", "rpm_filter_"),
    )),
    ("blackbox", dict(
        file="blackbox",
        category="OTHER",
        title="blackbox logging",
        blurb="what the blackbox records, at what rate, and how the flash is "
              "erased",
        prefixes=("blackbox_",),
    )),
    ("identity", dict(
        file="identity",
        category="OTHER",
        title="craft name and model match",
        blurb="the craft name and the radio model-match id, which are personal "
              "to one airframe and one transmitter",
        settings={"name", "model_id", "model_set_name"},
    )),
    ("misc", dict(
        file="sensors-and-debug",
        category="OTHER",
        title="remaining master settings",
        blurb="master settings that do not belong to any of the other presets",
    )),
])

# Lines default to `master` scope until a `profile N` / `rateprofile N` selector
# appears. `diff all` restores the original selection at the end, which means a
# selector can legitimately repeat -- always trust the most recent one.
SELECTOR_RE = re.compile(r"^(profile|rateprofile)\s+(\d+)\s*$")
SET_RE = re.compile(r"^set\s+([A-Za-z0-9_]+)\s*=\s*(.+?)\s*$")
FEATURE_RE = re.compile(r"^feature\s+(-?)([A-Z0-9_]+)\s*$")
VERSION_RE = re.compile(r"^#\s*Rotorflight\s*/\s*\S+\s*\([^)]*\)\s*(\d+)\.(\d+)\.(\d+)")
BOARD_RE = re.compile(r"^board_name\s+(\S+)")
NAME_RE = re.compile(r"^set\s+name\s*=\s*(.+?)\s*$")


def should_drop(line):
    """True for lines no preset should ever carry, in any scope.

    Scope matters here. A `diff all` signs off with "restore original
    rateprofile selection", a `rateprofile N` selector and then `save`, so the
    trailing `save` falls inside the last rate profile rather than at master
    scope. A preset carrying it would commit to flash on apply, before the user
    has reviewed anything.
    """
    if line.split()[0] in DROP_COMMANDS:
        return True
    if any(r.match(line) for r in DEFAULT_ROWS):
        return True
    m = SET_RE.match(line)
    return bool(m) and m.group(1) in DROP_SETTINGS


def classify(line):
    """Return the bucket key for a master-scope line, or None to drop it."""
    verb = line.split()[0]
    m = SET_RE.match(line)
    name = m.group(1) if m else None

    feat = FEATURE_RE.match(line)
    feat_name = feat.group(2) if feat else None

    for key, spec in BUCKETS.items():
        if key == "misc":
            continue
        if verb in spec.get("commands", ()):
            return key
        if feat_name and feat_name in spec.get("features", ()):
            return key
        if name:
            if name in spec.get("settings", ()):
                return key
            if any(name.startswith(p) for p in spec.get("prefixes", ())):
                return key
    return "misc" if (name or feat_name) else None


def parse(path):
    """Split the dump into master buckets, profiles and rateprofiles."""
    master = OrderedDict((k, []) for k in BUCKETS)
    profiles, rateprofiles = {}, {}
    meta = {"firmware": None, "board": None, "craft": None}
    scope = ("master", None)

    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()

            v = VERSION_RE.match(line)
            if v:
                meta["firmware"] = "%s.%s" % (v.group(1), v.group(2))
            if line.startswith("#") or not line:
                continue

            b = BOARD_RE.match(line)
            if b:
                meta["board"] = b.group(1)
            n = NAME_RE.match(line)
            if n:
                meta["craft"] = n.group(1)

            sel = SELECTOR_RE.match(line)
            if sel:
                scope = (sel.group(1), int(sel.group(2)))
                continue

            if should_drop(line):
                continue

            if scope[0] == "profile":
                profiles.setdefault(scope[1], []).append(line)
            elif scope[0] == "rateprofile":
                rateprofiles.setdefault(scope[1], []).append(line)
            else:
                key = classify(line)
                if key:
                    master[key].append(line)

    return master, profiles, rateprofiles, meta


def dedupe(groups):
    """Collapse identical profiles into one entry, keeping every slot number.

    A six-profile dump routinely has three or four slots holding the same tune;
    writing that out four times would give the user four identical presets to
    choose between.
    """
    merged = OrderedDict()
    for idx in sorted(groups):
        body = tuple(groups[idx])
        if not body:
            continue
        merged.setdefault(body, []).append(idx)
    return [(slots, list(body)) for body, slots in merged.items()]


def header(title, firmware, category, author, descriptions,
           board=None, keywords=None, warning=None, priority=0):
    out = ["#$ TITLE: %s" % title, ""]
    out.append("#$ FIRMWARE_VERSION: %s" % firmware)
    if board:
        out.append("#$ BOARD_NAME: %s" % board)
    out += ["", "#$ CATEGORY: %s" % category, "#$ STATUS: COMMUNITY"]
    if keywords:
        out.append("#$ KEYWORDS: %s" % keywords)
    if author:
        out.append("#$ AUTHOR: %s" % author)
    for d in descriptions:
        out.append("#$ DESCRIPTION: %s" % d)
    out.append("#$ PRIORITY: %d" % priority)
    if warning:
        out.append("#$ WARNING: %s" % warning)
    out.append("")
    return out


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def preset_path(out, category, firmware, model_slug, concern):
    """<author>/<category>/<firmware_version>/<model>/<concern>.txt

    HOWTO.md recommends `presets/<author>/<category>/<firmware_version>/
    (board_name)`. The model sits where board_name would, because one board
    carries many different helicopters and it is the helicopter a reader is
    looking for. Board compatibility is carried by the BOARD_NAME tag, which is
    what the Configurator actually filters on -- the indexer only walks the tree
    collecting .txt files, so the layout is for humans, not for the parser.
    """
    return os.path.join(out, category.lower(), firmware, model_slug,
                        concern + ".txt")


def write(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines).rstrip() + "\n")
    print("wrote %s" % path)


def gov_headspeed(body):
    for line in body:
        m = re.match(r"^set gov_headspeed\s*=\s*(\d+)", line)
        if m:
            return int(m.group(1))
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dump")
    ap.add_argument("--author", required=True)
    ap.add_argument("--model", help="model name for titles (default: craft name in the dump)")
    ap.add_argument("--out", required=True, help="output directory, e.g. presets/glenncain")
    ap.add_argument("--firmware", help="override the firmware version in the dump banner")
    args = ap.parse_args()

    master, profiles, rateprofiles, meta = parse(args.dump)
    firmware = args.firmware or meta["firmware"]
    if not firmware:
        sys.exit("no firmware version in the dump banner; pass --firmware")
    model = args.model or meta["craft"] or "preset"
    slug = slugify(model)
    board = meta["board"]

    for key, spec in BUCKETS.items():
        body = master[key]
        if not body:
            continue
        lines = header(
            title="%s %s" % (model, spec["title"]),
            firmware=firmware,
            category=spec["category"],
            author=args.author,
            descriptions=["Sets %s." % spec["blurb"],
                          "Leaves every other setting alone."],
            board=board if spec.get("board_specific") else None,
            keywords="%s %s" % (model, key),
        ) + body
        write(preset_path(args.out, spec["category"], firmware, slug, spec["file"]), lines)

    for slots, body in dedupe(profiles):
        rpm = gov_headspeed(body)
        tag = "%drpm" % rpm if rpm else "p%d" % (slots[0] + 1)
        lines = header(
            title="%s PID profile %s" % (model, tag),
            firmware=firmware,
            category="PROFILE",
            author=args.author,
            descriptions=[
                "PID gains, filtering cutoffs and governor gains for this profile.",
                "From profile slot%s %s of the source dump."
                % ("s" if len(slots) > 1 else "",
                   ", ".join(str(s + 1) for s in slots)),
            ],
            keywords="%s profile %s" % (model, tag),
            warning="Applied to the PID profile currently selected on the "
                    "flight controller. Select the target profile first.",
        ) + body
        write(preset_path(args.out, "PROFILE", firmware, slug, tag), lines)

    for slots, body in dedupe(rateprofiles):
        tag = "rates-%d" % (slots[0] + 1)
        lines = header(
            title="%s rates %s" % (model, ", ".join(str(s + 1) for s in slots)),
            firmware=firmware,
            category="RATEPROFILE",
            author=args.author,
            descriptions=[
                "Stick rates, expo and setpoint boost for this rate profile.",
                "From rate profile slot%s %s of the source dump."
                % ("s" if len(slots) > 1 else "",
                   ", ".join(str(s + 1) for s in slots)),
            ],
            keywords="%s rates" % model,
            warning="Applied to the rate profile currently selected on the "
                    "flight controller. Select the target rate profile first.",
        ) + body
        write(preset_path(args.out, "RATEPROFILE", firmware, slug, tag), lines)


if __name__ == "__main__":
    main()
