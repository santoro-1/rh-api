from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import numpy as np
from scipy import signal

from analyze_h3_head_audio import SAMPLE_RATE, decode_mono, first_onset_ms, peak, rms


def resolve_data_path(value: str, data_dir: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else data_dir / path


def frame_dbfs(values: np.ndarray, milliseconds: float) -> tuple[float, float]:
    window = values[: int(SAMPLE_RATE * milliseconds / 1000.0)]
    return (
        20 * np.log10(max(rms(window), 1e-12)),
        20 * np.log10(max(peak(window), 1e-12)),
    )


def high_band_dbfs(values: np.ndarray, milliseconds: float, cutoff_hz: float = 6_000) -> float:
    window = values[: int(SAMPLE_RATE * milliseconds / 1000.0)]
    if not len(window):
        return -240.0
    sos = signal.butter(6, cutoff_hz, btype="highpass", fs=SAMPLE_RATE, output="sos")
    filtered = signal.sosfilt(sos, window)
    return 20 * np.log10(max(rms(filtered), 1e-12))


def rms_envelope_dbfs(
    values: np.ndarray, *, window_ms: float = 5.0, hop_ms: float = 2.5, limit_ms: float = 500.0
) -> tuple[np.ndarray, np.ndarray]:
    window = max(1, int(SAMPLE_RATE * window_ms / 1000.0))
    hop = max(1, int(SAMPLE_RATE * hop_ms / 1000.0))
    limit = min(len(values), int(SAMPLE_RATE * limit_ms / 1000.0))
    starts = np.arange(0, max(0, limit - window + 1), hop, dtype=int)
    levels = np.array(
        [20 * np.log10(max(rms(values[start : start + window]), 1e-12)) for start in starts]
    )
    return starts * 1000.0 / SAMPLE_RATE, levels


def estimate_envelope_lag_ms(generated: np.ndarray, source: np.ndarray) -> tuple[float, float]:
    _, generated_db = rms_envelope_dbfs(
        generated, window_ms=10.0, hop_ms=5.0, limit_ms=6_000.0
    )
    _, source_db = rms_envelope_dbfs(source, window_ms=10.0, hop_ms=5.0, limit_ms=6_000.0)
    generated_values = np.clip(generated_db, -80.0, 0.0)
    source_values = np.clip(source_db, -80.0, 0.0)
    generated_values -= np.mean(generated_values)
    source_values -= np.mean(source_values)
    correlation = signal.correlate(generated_values, source_values, mode="full", method="fft")
    lags = signal.correlation_lags(len(generated_values), len(source_values), mode="full")
    allowed = np.abs(lags) <= 200
    correlation = correlation[allowed]
    lags = lags[allowed]
    index = int(np.argmax(correlation))
    lag_frames = int(lags[index])
    denominator = np.linalg.norm(generated_values) * np.linalg.norm(source_values)
    coefficient = float(correlation[index] / denominator) if denominator else 0.0
    return lag_frames * 5.0, coefficient


def initial_extra_noise_end_ms(generated: np.ndarray, source: np.ndarray) -> float | None:
    times, generated_db = rms_envelope_dbfs(generated)
    _, source_db = rms_envelope_dbfs(source)
    usable = min(len(times), len(generated_db), len(source_db))
    times = times[:usable]
    generated_db = generated_db[:usable]
    source_db = source_db[:usable]
    extra = (generated_db > -55.0) & ((source_db < -55.0) | ((generated_db - source_db) > 15.0))
    start_candidates = np.flatnonzero(extra & (times <= 50.0))
    if not len(start_candidates):
        return None
    start = int(start_candidates[0])
    quiet_frames = 8  # 20 ms at the 2.5 ms hop.
    for index in range(start + 1, max(start + 1, len(extra) - quiet_frames + 1)):
        if not np.any(extra[index : index + quiet_frames]):
            return float(times[index])
    return float(times[-1]) if len(times) else None


def analyze_head(generated_path: Path, source_path: Path) -> dict[str, object]:
    generated = decode_mono(generated_path)
    source = decode_mono(source_path)
    envelope_lag_ms, envelope_correlation = estimate_envelope_lag_ms(generated, source)
    result: dict[str, object] = {
        "generated_path": str(generated_path),
        "source_path": str(source_path),
        "generated_onset_ms_minus_55": first_onset_ms(generated, -55.0),
        "source_onset_ms_minus_55": first_onset_ms(source, -55.0),
        "generated_onset_ms_minus_45": first_onset_ms(generated, -45.0),
        "source_onset_ms_minus_45": first_onset_ms(source, -45.0),
        "initial_extra_noise_end_ms": initial_extra_noise_end_ms(generated, source),
        "envelope_alignment_lag_ms": envelope_lag_ms,
        "envelope_alignment_correlation": envelope_correlation,
    }
    for milliseconds in (25, 50, 75, 100, 150, 200, 300, 500):
        generated_rms, generated_peak = frame_dbfs(generated, milliseconds)
        source_rms, source_peak = frame_dbfs(source, milliseconds)
        result[f"first_{milliseconds}ms"] = {
            "generated_rms_dbfs": generated_rms,
            "source_rms_dbfs": source_rms,
            "generated_minus_source_rms_db": generated_rms - source_rms,
            "generated_peak_dbfs": generated_peak,
            "source_peak_dbfs": source_peak,
            "generated_high_band_rms_dbfs": high_band_dbfs(generated, milliseconds),
            "source_high_band_rms_dbfs": high_band_dbfs(source, milliseconds),
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("collection_json", type=Path)
    parser.add_argument("--database", type=Path, default=Path("data/app.db"))
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--plot-output", type=Path)
    args = parser.parse_args()

    collection = json.loads(args.collection_json.read_text(encoding="utf-8"))
    rows_by_batch = {str(row["batch_id"]): int(row["row"]) for row in collection["rows"]}
    placeholders = ",".join("?" for _ in rows_by_batch)
    query = f"""
        SELECT b.id, s.segment_index, s.audio_path, t.result_path
        FROM generation_batches AS b
        JOIN generation_batch_items AS i ON i.batch_id = b.id
        JOIN generation_segments AS s ON s.batch_item_id = i.id
        JOIN generation_tasks AS t ON t.segment_id = s.id
        WHERE b.id IN ({placeholders}) AND t.status = 'SUCCESS'
        ORDER BY b.id, s.segment_index
    """
    with sqlite3.connect(args.database) as connection:
        records = connection.execute(query, tuple(rows_by_batch)).fetchall()

    results = []
    decoded_for_plot: list[tuple[int, int, np.ndarray, np.ndarray]] = []
    for batch_id, segment_index, audio_path, result_path in records:
        row = rows_by_batch[str(batch_id)]
        generated = resolve_data_path(str(result_path), args.data_dir).resolve()
        source = resolve_data_path(str(audio_path), args.data_dir).resolve()
        result = analyze_head(generated, source)
        result.update(
            {
                "row": row,
                "segment_index": int(segment_index),
                "key": f"row_{row:02d}_segment_{int(segment_index) + 1:02d}",
            }
        )
        results.append(result)
        if args.plot_output and int(segment_index) == 0:
            decoded_for_plot.append((row, int(segment_index), decode_mono(generated), decode_mono(source)))

    results.sort(key=lambda item: (int(item["row"]), int(item["segment_index"])))
    if args.plot_output:
        import matplotlib.pyplot as plt

        decoded_for_plot.sort(key=lambda item: item[0])
        figure, axes = plt.subplots(len(decoded_for_plot), 1, figsize=(12, 18), sharex=True)
        if len(decoded_for_plot) == 1:
            axes = [axes]
        for axis, (row, _segment, generated_values, source_values) in zip(
            axes, decoded_for_plot, strict=True
        ):
            generated_times, generated_db = rms_envelope_dbfs(generated_values)
            source_times, source_db = rms_envelope_dbfs(source_values)
            axis.plot(source_times, source_db, color="#888888", linewidth=1.0, label="source")
            axis.plot(generated_times, generated_db, color="#0077cc", linewidth=1.0, label="H3")
            axis.set_ylim(-105, 5)
            axis.set_xlim(0, 500)
            axis.set_ylabel(f"row {row:02d}")
            axis.grid(alpha=0.2)
        axes[0].legend(loc="upper right")
        axes[-1].set_xlabel("milliseconds from segment start")
        figure.suptitle("H3 first-segment head RMS envelope: generated vs source")
        figure.tight_layout()
        args.plot_output.parent.mkdir(parents=True, exist_ok=True)
        figure.savefig(args.plot_output, dpi=160)
        plt.close(figure)
    print(json.dumps({"result_count": len(results), "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
