# SPDX-License-Identifier: GPL-3.0-or-later
"""Kokoro voice worker -- a SEPARATE program, licensed GPL-3.0-or-later.

Kokoro's model is Apache-2.0, but kokoro-onnx pronounces words with espeak-ng (GPL-3.0). So
LocalPilot never imports it: it starts this file as its own process and talks to it over a
pipe (text in, audio out), exactly like the optional Piper worker.

Protocol (binary, stdin/stdout): request  = one UTF-8 line "voice<TAB>text"
                                 response = <II> sample count + sample rate, int16 samples
"""
import os
import struct
import sys


def main():
    import numpy as np
    from kokoro_onnx import Kokoro
    model, voices = sys.argv[1], sys.argv[2]
    k = Kokoro(model, voices)
    out = sys.stdout.buffer
    out.write(b"READY\n"); out.flush()
    for line in sys.stdin.buffer:
        voice, _, text = line.decode("utf-8", "replace").rstrip("\r\n").partition("\t")
        audio, sr = np.zeros(0, "<i2"), 24000
        if text.strip():
            try:
                lang = "en-gb" if voice.startswith("b") else "en-us"
                s, sr = k.create(text, voice=voice or "af_heart", speed=1.0, lang=lang)
                audio = (np.clip(s, -1, 1) * 32767).astype("<i2")
            except Exception:
                pass
        out.write(struct.pack("<II", len(audio), sr)); out.write(audio.tobytes()); out.flush()


if __name__ == "__main__":
    main()
