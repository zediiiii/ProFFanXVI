import wave
import array
import sys


def rms_envelope(path, win_sec=0.005):
    w = wave.open(path, 'rb')
    sr = w.getframerate()
    n = w.getnframes()
    frames = w.readframes(n)
    arr = array.array('h')
    arr.frombytes(frames)
    win = max(1, int(sr * win_sec))
    envelope = []
    for i in range(0, len(arr), win):
        chunk = arr[i:i + win]
        if not chunk:
            break
        rms = (sum(x * x for x in chunk) / len(chunk)) ** 0.5
        envelope.append((i / sr, rms))
    return sr, envelope


def refine_word_end(envelope, whisper_start, whisper_end, noise_floor=60.0, sustain_sec=0.08, win_sec=0.005):
    """Walk forward from whisper_start; find first point where RMS drops below
    noise_floor and STAYS below it for at least sustain_sec (avoids stopping on
    a brief consonant dip inside the word)."""
    sustain_windows = max(1, int(sustain_sec / win_sec))
    idx_start = next((i for i, (t, _) in enumerate(envelope) if t >= whisper_start), 0)
    idx_limit = next((i for i, (t, _) in enumerate(envelope) if t >= whisper_end + 1.0), len(envelope))
    for i in range(idx_start, min(idx_limit, len(envelope) - sustain_windows)):
        window = envelope[i:i + sustain_windows]
        if all(rms < noise_floor for _, rms in window):
            return envelope[i][0]
    return whisper_end  # fallback: trust whisper if no clear silence found


def refine_word_start(envelope, whisper_start, noise_floor=60.0, lookback_sec=0.15, win_sec=0.005):
    """Walk backward from whisper_start to find the actual onset (first rise
    above noise floor), within a bounded lookback window."""
    idx_anchor = next((i for i, (t, _) in enumerate(envelope) if t >= whisper_start), 0)
    idx_floor = next((i for i, (t, _) in enumerate(envelope) if t >= whisper_start - lookback_sec), 0)
    for i in range(idx_anchor, idx_floor, -1):
        if envelope[i][1] < noise_floor:
            return envelope[i][0]
    return whisper_start


if __name__ == "__main__":
    wav_path = sys.argv[1]
    whisper_start = float(sys.argv[2])
    whisper_end = float(sys.argv[3])

    sr, env = rms_envelope(wav_path)
    refined_start = refine_word_start(env, whisper_start)
    refined_end = refine_word_end(env, whisper_start, whisper_end)
    print(f"whisper range:  {whisper_start:.3f} - {whisper_end:.3f}")
    print(f"refined range:  {refined_start:.3f} - {refined_end:.3f}")
