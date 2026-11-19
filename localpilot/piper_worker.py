# SPDX-License-Identifier: GPL-3.0-or-later
"""OPTIONAL Piper voice worker -- a SEPARATE program, licensed GPL-3.0-or-later.

Piper (piper-tts >= 1.3) is GPL-3.0. LocalPilot never imports it: when the user has installed
Piper themselves, LocalPilot starts this file as its own process and talks to it over a pipe
(text in, audio out). It is excluded from LocalPilot's packaged build.

Protocol (stdin/stdout, binary): request  = one UTF-8 line of text
                                 response = 4-byte little-endian sample count, 4-byte sample
                                            rate, then int16 mono samples
"""
import struct
import sys


def main():
    from piper import PiperVoice
    import numpy as np
    voice = PiperVoice.load(sys.argv[1])
    out = sys.stdout.buffer
    out.write(b"READY\n"); out.flush()
    for line in sys.stdin.buffer:
        text = line.decode("utf-8", "replace").strip()
        chunks = list(voice.synthesize(text)) if text else []
        if chunks:
            audio = np.concatenate([c.audio_int16_array for c in chunks]).astype("<i2")
            sr = chunks[0].sample_rate
        else:
            audio, sr = np.zeros(0, "<i2"), 22050
        out.write(struct.pack("<II", len(audio), sr)); out.write(audio.tobytes()); out.flush()


if __name__ == "__main__":
    main()
