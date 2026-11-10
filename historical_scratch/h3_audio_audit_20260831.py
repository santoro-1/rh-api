"""Read-only PCM audit; never rewrites source media or production data."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import numpy as np


def decode(path):
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path),
         "-map", "0:a:0", "-vn", "-acodec", "pcm_f32le", "-f", "f32le", "-"],
        check=True, capture_output=True, creationflags=0x08000000,
    )
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=sample_rate,channels,start_time,duration", "-of", "json", str(path)],
        check=True, capture_output=True, creationflags=0x08000000,
    )
    info = json.loads(probe.stdout)["streams"][0]
    samples = np.frombuffer(result.stdout, dtype="<f4").reshape(-1, info["channels"])
    return samples, int(info["sample_rate"]), info, hashlib.sha256(result.stdout).hexdigest()


if sys.argv[1:2] == ["--compare"]:
    from scipy.signal import correlate
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    master, rate, _, _ = decode(Path(sys.argv[2]))
    full = master.mean(axis=1).astype(float)
    fig, axes = plt.subplots(len(sys.argv[3:]) + 1, 1, figsize=(14, 9))
    step = round(rate * 0.005)
    envelope = np.max(np.abs(full[:len(full)//step*step].reshape(-1, step)), axis=1)
    axes[0].plot(np.arange(len(envelope))*step/rate, envelope)
    axes[0].set_title("Draft authoritative audio (no BGM)")
    axes[0].set_xlim(0, len(full)/rate)
    for index, filename in enumerate(sys.argv[3:], 1):
        source, source_rate, _, _ = decode(Path(filename))
        assert source_rate == rate
        mono = source.mean(axis=1).astype(float)
        corr = correlate(full, mono, mode="valid", method="fft")
        offset = int(corr.argmax())
        aligned = full[offset:offset+len(mono)]
        coefficient = float(np.corrcoef(mono, aligned)[0, 1])
        head = mono[:rate]
        frame = round(rate*0.01)
        head_blocks = head[:len(head)//frame*frame].reshape(-1, frame)
        rms_db = 20*np.log10(np.maximum(np.sqrt(np.mean(head_blocks**2,axis=1)),1e-12))
        axes[index].plot(np.arange(len(head))/rate, head, linewidth=.5)
        axes[index].set_title(f"Original segment {index}: first second; match in draft audio at {offset/rate:.3f}s, corr={coefficient:.5f}")
        axes[index].set_xlim(0,1)
        print(json.dumps({"segment":index, "file":str(Path(filename).parent.name),
            "best_offset_in_master_s":round(offset/rate,4), "pcm_correlation":round(coefficient,6),
            "first_500ms_rms_db_per_10ms":[round(float(x),1) for x in rms_db[:50]]}))
    for axis in axes:
        axis.set_xlabel("Seconds")
        axis.grid(alpha=.2)
    fig.tight_layout()
    destination = Path(__file__).with_suffix(".png")
    fig.savefig(destination, dpi=120)
    print(str(destination))
    sys.exit(0)

if sys.argv[1:2] == ["--intervals"]:
    samples, rate, _, _ = decode(Path(sys.argv[2]))
    mono = samples.mean(axis=1).astype(float)
    for value in sys.argv[3:]:
        start, end = (float(item) for item in value.split(":"))
        block = mono[round(start * rate):round(end * rate)]
        rms = float(np.sqrt(np.mean(block ** 2))) if len(block) else 0.0
        peak = float(np.max(np.abs(block))) if len(block) else 0.0
        print(json.dumps({"interval_s": [start, end], "samples": len(block),
                          "rms_dbfs": round(20*np.log10(max(rms, 1e-12)), 3),
                          "peak_dbfs": round(20*np.log10(max(peak, 1e-12)), 3)}))
    sys.exit(0)

for filename in sys.argv[1:]:
    path = Path(filename)
    samples, rate, info, digest = decode(path)
    mono = samples.mean(axis=1)
    block = max(1, round(rate * 0.01))
    windows = mono[:len(mono) // block * block].reshape(-1, block)
    rms_db = 20 * np.log10(np.maximum(np.sqrt(np.mean(windows**2, axis=1)), 1e-12))
    active = rms_db > -40
    edges = np.diff(np.r_[False, active, False].astype(int))
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    islands = []
    for i, (start, end) in enumerate(zip(starts, ends)):
        if start * 0.01 > 2.0:
            break
        islands.append({"start_s":round(start * 0.01, 3), "end_s":round(end * 0.01, 3),
                        "rms_peak_db":round(float(rms_db[start:end].max()), 2),
                        "next_gap_s":round((starts[i+1]-end)*0.01, 3) if i+1<len(starts) else None})
    peak = float(np.max(np.abs(samples)))
    normalized = path.with_name("result.normalized.mp4")
    comparison = None
    if path.name == "result.mp4" and normalized.is_file():
        other, _, _, other_digest = decode(normalized)
        comparison = {"decoded_pcm_identical":digest == other_digest,
                      "raw_samples":len(samples), "normalized_samples":len(other)}
    print(json.dumps({"task":path.parent.name, "file":path.name, "sample_rate":rate,
                      "channels":info["channels"], "duration_s":round(len(samples)/rate, 4),
                      "peak_dbfs":round(20*np.log10(max(peak, 1e-12)), 3),
                      "samples_at_or_over_full_scale":int(np.count_nonzero(np.abs(samples)>=1.0)),
                      "first_2s_energy_islands_not_noise_labels":islands,
                      "normalization_check":comparison}, ensure_ascii=False))
