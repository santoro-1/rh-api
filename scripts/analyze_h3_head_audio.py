from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import numpy as np
from scipy import signal


SAMPLE_RATE = 32_000


def decode_mono(path: Path) -> np.ndarray:
    completed = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-f",
            "f32le",
            "pipe:1",
        ],
        check=True,
        stdout=subprocess.PIPE,
    )
    return np.frombuffer(completed.stdout, dtype="<f4").astype(np.float64)


def estimate_lag(generated: np.ndarray, source: np.ndarray) -> tuple[int, float]:
    analysis_samples = min(len(source), SAMPLE_RATE * 6)
    source_window = source[:analysis_samples]
    generated_window = generated[: min(len(generated), analysis_samples + SAMPLE_RATE)]
    source_window = source_window - np.mean(source_window)
    generated_window = generated_window - np.mean(generated_window)
    correlation = signal.correlate(generated_window, source_window, mode="full", method="fft")
    lags = signal.correlation_lags(len(generated_window), len(source_window), mode="full")
    allowed = np.abs(lags) <= SAMPLE_RATE
    correlation = correlation[allowed]
    lags = lags[allowed]
    lag = int(lags[int(np.argmax(np.abs(correlation)))])

    if lag >= 0:
        generated_overlap = generated[lag : lag + analysis_samples]
        source_overlap = source[: len(generated_overlap)]
    else:
        source_overlap = source[-lag : -lag + analysis_samples]
        generated_overlap = generated[: len(source_overlap)]
    usable = min(len(generated_overlap), len(source_overlap))
    generated_overlap = generated_overlap[:usable]
    source_overlap = source_overlap[:usable]
    coefficient = float(np.corrcoef(generated_overlap, source_overlap)[0, 1])
    return lag, coefficient


def rms(values: np.ndarray) -> float:
    if not len(values):
        return 0.0
    return float(np.sqrt(np.mean(values * values)))


def peak(values: np.ndarray) -> float:
    if not len(values):
        return 0.0
    return float(np.max(np.abs(values)))


def step_metrics(values: np.ndarray, seconds: float = 0.5) -> dict[str, float]:
    window = values[: int(SAMPLE_RATE * seconds)]
    derivatives = np.diff(np.concatenate(([0.0], window)))
    if not len(derivatives):
        return {"max_sample_step": 0.0, "max_sample_step_at_ms": 0.0}
    index = int(np.argmax(np.abs(derivatives)))
    return {
        "max_sample_step": float(np.abs(derivatives[index])),
        "max_sample_step_at_ms": index * 1000.0 / SAMPLE_RATE,
    }


def first_onset_ms(values: np.ndarray, threshold_dbfs: float = -45.0) -> float | None:
    frame = SAMPLE_RATE // 1000 * 5
    threshold = 10 ** (threshold_dbfs / 20)
    for offset in range(0, max(0, len(values) - frame + 1), frame):
        if rms(values[offset : offset + frame]) >= threshold:
            return offset * 1000.0 / SAMPLE_RATE
    return None


def overload_metrics(values: np.ndarray) -> dict[str, float | int | None]:
    absolute = np.abs(values)
    peak_index = int(np.argmax(absolute)) if len(absolute) else 0
    overloaded = np.flatnonzero(absolute >= 1.0)
    return {
        "full_track_peak_dbfs": 20 * np.log10(max(peak(values), 1e-12)),
        "full_track_peak_at_seconds": peak_index / SAMPLE_RATE,
        "samples_at_or_above_0_dbfs": int(len(overloaded)),
        "first_overload_at_seconds": (
            float(overloaded[0] / SAMPLE_RATE) if len(overloaded) else None
        ),
        "last_overload_at_seconds": (
            float(overloaded[-1] / SAMPLE_RATE) if len(overloaded) else None
        ),
        "overloaded_samples_after_1_second": int(np.count_nonzero(overloaded >= SAMPLE_RATE)),
    }


def analyze_pair(generated_path: Path, source_path: Path) -> dict[str, object]:
    generated = decode_mono(generated_path)
    source = decode_mono(source_path)
    lag, correlation = estimate_lag(generated, source)
    first_20_ms = generated[: int(SAMPLE_RATE * 0.020)]
    first_100_ms = generated[: int(SAMPLE_RATE * 0.100)]
    first_500_ms = generated[: int(SAMPLE_RATE * 0.500)]
    front = generated[: max(0, lag)]
    generated_steps = step_metrics(generated)
    source_steps = step_metrics(source)
    generated_overload = overload_metrics(generated)
    source_overload = overload_metrics(source)
    return {
        "source_path": str(source_path),
        "generated_path": str(generated_path),
        "source_duration_seconds": len(source) / SAMPLE_RATE,
        "generated_audio_duration_seconds": len(generated) / SAMPLE_RATE,
        "generated_minus_source_seconds": (len(generated) - len(source)) / SAMPLE_RATE,
        "alignment_lag_ms": lag * 1000.0 / SAMPLE_RATE,
        "alignment_correlation": correlation,
        "source_onset_ms_at_minus_45_dbfs": first_onset_ms(source),
        "generated_onset_ms_at_minus_45_dbfs": first_onset_ms(generated),
        "front_before_aligned_source_ms": max(0, lag) * 1000.0 / SAMPLE_RATE,
        "front_before_aligned_source_rms_dbfs": (
            20 * np.log10(max(rms(front), 1e-12)) if len(front) else None
        ),
        "front_before_aligned_source_peak_dbfs": (
            20 * np.log10(max(peak(front), 1e-12)) if len(front) else None
        ),
        "first_sample": float(generated[0]) if len(generated) else None,
        "first_20_ms_peak_dbfs": 20 * np.log10(max(peak(first_20_ms), 1e-12)),
        "first_100_ms_peak_dbfs": 20 * np.log10(max(peak(first_100_ms), 1e-12)),
        "generated_first_500_ms_max_sample_step": generated_steps["max_sample_step"],
        "generated_first_500_ms_max_sample_step_at_ms": generated_steps["max_sample_step_at_ms"],
        "source_first_500_ms_max_sample_step": source_steps["max_sample_step"],
        "source_first_500_ms_max_sample_step_at_ms": source_steps["max_sample_step_at_ms"],
        "generated_first_500_ms_peak_dbfs": 20 * np.log10(max(peak(first_500_ms), 1e-12)),
        "source_first_500_ms_peak_dbfs": 20
        * np.log10(max(peak(source[: int(SAMPLE_RATE * 0.500)]), 1e-12)),
        "generated_overload": generated_overload,
        "source_overload": source_overload,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("state_json", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    state = json.loads(args.state_json.read_text(encoding="utf-8"))
    sources_by_row = {int(source["row"]): source for source in state["plan"]["sources"]}
    results = []
    for call in state["calls"]:
        source = sources_by_row[int(call["row"])]
        if source.get("segments"):
            segment = next(
                segment
                for segment in source["segments"]
                if int(segment["segment_index"]) == int(call["segment_index"])
            )
        else:
            segment = source
        result = analyze_pair(Path(call["output_path"]), Path(segment["audio_path"]))
        result["key"] = call.get("key") or f"row_{int(call['row']):02d}"
        results.append(result)

    payload = {"sample_rate": SAMPLE_RATE, "results": results}
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
