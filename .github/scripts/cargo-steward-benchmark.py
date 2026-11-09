import json
import os
from pathlib import Path
import statistics
import subprocess
import time

ROOT = Path.cwd()
OUT = Path(os.environ["RUNNER_TEMP"]) / "steward-benchmark"
OUT.mkdir(exist_ok=True)
TARGET = os.environ["BENCH_TARGET"]
CRATES = ["resource-monitor"] if "windows" in TARGET else [
    "resource-monitor", "kde-snap-shot", "hyprland-snap-shot"
]
RESULTS = []
BASE_ENV = os.environ.copy()
BASE_ENV["CARGO_INCREMENTAL"] = "0"
BASE_ENV["CARGO_TERM_COLOR"] = "never"
BASE_ENV.pop("RUSTC_WRAPPER", None)
BASE_ENV.pop("CARGO_BUILD_RUSTC_WRAPPER", None)


def run(args, env, log):
    started = time.perf_counter()
    with log.open("w") as output:
        result = subprocess.run(args, env=env, stdout=output, stderr=subprocess.STDOUT)
    elapsed = time.perf_counter() - started
    if result.returncode:
        print(log.read_text(), flush=True)
        raise RuntimeError(f"{args} exited {result.returncode}")
    return elapsed


def measure(policy, trial, crate):
    target_dir = OUT / f"{policy}-{trial}-{crate}"
    env = BASE_ENV | {"CARGO_TARGET_DIR": str(target_dir)}
    if policy == "steward":
        env["RUSTC_WRAPPER"] = "cargo-steward"
    manifest = ROOT / "native" / crate / "Cargo.toml"
    prefix = ["cargo", "xwin", "build"] if "windows" in TARGET else ["cargo", "build"]
    build = prefix + [
        "--locked", "--release", "--manifest-path", str(manifest), "--target", TARGET
    ]
    row = {"policy": policy, "trial": trial, "crate": crate, "target": TARGET}
    tag = f"{policy}-{trial}-{crate}"
    row["start_s"] = run(
        ["cargo-steward", "ci", "start", "--target-dir", str(target_dir)],
        env, OUT / f"{tag}-start.log"
    ) if policy == "steward" else 0
    row["cold_s"] = run(build, env, OUT / f"{tag}-cold.log")
    row["noop_s"] = run(build, env, OUT / f"{tag}-noop.log")
    source = ROOT / "native" / crate / "src" / "main.rs"
    original = source.read_bytes()
    try:
        source.write_bytes(original + f"\n// CI benchmark rebuild {trial}.\n".encode())
        row["changed_s"] = run(build, env, OUT / f"{tag}-changed.log")
    finally:
        source.write_bytes(original)
    # Retain the same current work in both arms; this isolates wrapper and trim overhead.
    row["trim_s"] = run(
        ["cargo-steward", "ci", "trim", "--target-dir", str(target_dir),
         "--keep-workspace", "--keep-incremental", "--budget", "512MiB"],
        env, OUT / f"{tag}-trim.log"
    ) if policy == "steward" else 0
    archive = OUT / f"{tag}.tar.zst"
    row["archive_s"] = run(
        ["tar", "--zstd", "-cf", str(archive), "-C", str(target_dir), "."],
        env, OUT / f"{tag}-archive.log"
    )
    row["archive_bytes"] = archive.stat().st_size
    row["target_bytes"] = int(subprocess.check_output(
        ["du", "-sb", str(target_dir)], text=True
    ).split()[0])
    target_dir.rename(target_dir.with_name(target_dir.name + "-before-restore"))
    target_dir.mkdir()
    row["restore_s"] = run(
        ["tar", "--zstd", "-xf", str(archive), "-C", str(target_dir)],
        env, OUT / f"{tag}-restore.log"
    )
    row["restored_changed_s"] = run(build, env, OUT / f"{tag}-restored-changed.log")
    RESULTS.append(row)
    (OUT / "results.json").write_text(json.dumps(RESULTS, indent=2))
    print(json.dumps(row), flush=True)


for crate in CRATES:
    run(
        ["cargo", "fetch", "--locked", "--manifest-path",
         str(ROOT / "native" / crate / "Cargo.toml"), "--target", TARGET],
        BASE_ENV, OUT / f"fetch-{crate}.log"
    )
for trial in range(3):
    policies = ["plain", "steward"] if trial % 2 == 0 else ["steward", "plain"]
    for policy in policies:
        for crate in CRATES:
            measure(policy, trial, crate)

summary = [
    f"### {TARGET}",
    "",
    "Three trials; same runner, alternating policy order. Release flags match T3 Code.",
    "Crate downloads are prefetched; Windows MSVC download happens in its first cold build.",
    "Archive timings use local zstd compression/extraction, excluding GitHub cache network transfer.",
    "Restored rebuild restores the original source after the measured edit.",
    "",
    "| Policy | Cold | Source edit | No-op | Start + trim | Archive size |",
    "| --- | ---: | ---: | ---: | ---: | ---: |",
]
for policy in ["plain", "steward"]:
    trials = [
        [r for r in RESULTS if r["policy"] == policy and r["trial"] == trial]
        for trial in range(3)
    ]
    def median_sum(key):
        return statistics.median(sum(row[key] for row in rows) for rows in trials)
    overhead = statistics.median(
        sum(row["start_s"] + row["trim_s"] for row in rows) for rows in trials
    )
    summary.append(
        f"| {policy} | {median_sum('cold_s'):.2f}s | {median_sum('changed_s'):.2f}s | "
        f"{median_sum('noop_s'):.2f}s | {overhead:.2f}s | "
        f"{median_sum('archive_bytes') / 1048576:.1f} MiB |"
    )
with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as output:
    output.write("\n".join(summary) + "\n")
print("\n".join(summary), flush=True)
