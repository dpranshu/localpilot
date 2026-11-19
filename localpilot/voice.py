"""Voice for LocalPilot -- another way to talk to the SAME agent session (not a second AI).

  microphone -> Silero VAD (is someone speaking? when did they stop?)
             -> Whisper (faster-whisper, local) -> Session.submit(text, "voice")
  Session "say" events -> Piper TTS (local), sentence by sentence, interruptible

Everything runs on this PC; no audio leaves it. Whisper's model (base.en, ~140 MB) lives in
%APPDATA%\\LocalPilot\\models. Speech out uses Windows' built-in voices by default; Piper
(GPL-3.0) is an optional add-on run as a separate process. If the mic or the model is missing,
voice reports why and text keeps working.
"""
from __future__ import annotations

import os
import queue
import re
import threading
import time

import numpy as np

MODELS = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "LocalPilot", "models")
SR = 16000
FRAME = 512                       # 32 ms at 16 kHz (what Silero VAD expects)


# ============================== speech out ==============================
class WindowsTTS:
    """Windows' built-in voices (licensed with Windows: no download, no licence risk).
    Rendered to memory, then played by us -- so stop/barge-in work exactly like other engines."""
    name = "windows"

    def __init__(self, voice_hint: str = "zira"):
        self.voice_hint = voice_hint.lower()
        self._v = None

    def synth(self, text: str) -> tuple[np.ndarray, int]:
        import comtypes
        import comtypes.client
        if self._v is None:
            comtypes.CoInitialize()
            self._v = comtypes.client.CreateObject("SAPI.SpVoice")
            if self.voice_hint:
                vs = self._v.GetVoices()
                for i in range(vs.Count):
                    if self.voice_hint in vs.Item(i).GetDescription().lower():
                        self._v.Voice = vs.Item(i); break
        ms = comtypes.client.CreateObject("SAPI.SpMemoryStream")
        fmt = comtypes.client.CreateObject("SAPI.SpAudioFormat")
        fmt.Type = 22                                    # 22 kHz, 16-bit, mono
        ms.Format = fmt
        self._v.AudioOutputStream = ms
        self._v.Speak(text, 0)
        return np.frombuffer(bytes(ms.GetData()), dtype=np.int16), 22050


class PiperTTS:
    """OPTIONAL natural voice. Piper is GPL-3.0, so it runs as a separate process
    (piper_worker.py) and only if the user installed it -- LocalPilot never bundles it."""
    name = "piper"

    def __init__(self, voice: str = "en_US-lessac-medium"):
        import subprocess
        import sys
        model = os.path.join(MODELS, "piper", voice + ".onnx")
        if getattr(sys, "frozen", False) or not os.path.exists(model):
            raise RuntimeError("Piper voice not installed")
        worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "piper_worker.py")
        self.p = subprocess.Popen([sys.executable, worker, model], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  creationflags=0x08000000)
        if self.p.stdout.readline().strip() != b"READY":
            raise RuntimeError("Piper worker failed to start (is piper-tts installed?)")
        self._lock = threading.Lock()

    def synth(self, text: str) -> tuple[np.ndarray, int]:
        import struct
        with self._lock:
            self.p.stdin.write(text.replace("\n", " ").encode("utf-8") + b"\n"); self.p.stdin.flush()
            n, sr = struct.unpack("<II", self.p.stdout.read(8))
            return np.frombuffer(self.p.stdout.read(2 * n), dtype=np.int16), sr


class _WorkerTTS:
    """A voice engine running in its own process (text in, audio out over a pipe)."""

    def __init__(self, cmd: list[str]):
        import subprocess
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, creationflags=0x08000000)
        if self.p.stdout.readline().strip() != b"READY":
            raise RuntimeError("voice worker failed to start")
        self._lock = threading.Lock()

    def _ask(self, line: str) -> tuple[np.ndarray, int]:
        import struct
        with self._lock:
            self.p.stdin.write(line.replace("\n", " ").encode("utf-8") + b"\n"); self.p.stdin.flush()
            n, sr = struct.unpack("<II", self.p.stdout.read(8))
            return np.frombuffer(self.p.stdout.read(2 * n), dtype=np.int16), sr


KOKORO_VOICES = {"af_heart": "Heart (US, warm)", "af_bella": "Bella (US, bright)",
                 "af_nicole": "Nicole (US, soft)", "bf_emma": "Emma (British)"}


class KokoroTTS(_WorkerTTS):
    """Natural female voices (Kokoro, Apache-2.0 model). Its pronunciation library espeak-ng
    is GPL, so it runs as a separate program: kokoro_worker.py, or KokoroVoice.exe when packaged."""
    name = "kokoro"

    def __init__(self, voice: str = "af_heart"):
        import sys
        d = os.path.join(MODELS, "kokoro")
        model, voices = os.path.join(d, "kokoro-v1.0.int8.onnx"), os.path.join(d, "voices-v1.0.bin")
        if not (os.path.exists(model) and os.path.exists(voices)):
            raise RuntimeError("Kokoro voice not downloaded")
        if getattr(sys, "frozen", False):
            exe = os.path.join(os.path.dirname(sys.executable), "voice", "KokoroVoice.exe")
            if not os.path.exists(exe):
                raise RuntimeError("KokoroVoice.exe missing")
            cmd = [exe, model, voices]
        else:
            cmd = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                "kokoro_worker.py"), model, voices]
        self.voice = voice if voice in KOKORO_VOICES else "af_heart"
        super().__init__(cmd)

    def synth(self, text: str) -> tuple[np.ndarray, int]:
        return self._ask(f"{self.voice}\t{text}")


def make_tts(engine: str = "kokoro", voice: str = "af_heart"):
    """kokoro (default: natural female voice) -> piper (optional) -> Windows 'Zira' (female)."""
    order = {"kokoro": ("kokoro",), "piper": ("piper", "kokoro")}.get(engine, ())
    for e in order:
        try:
            return KokoroTTS(voice) if e == "kokoro" else PiperTTS()
        except Exception:
            continue
    return WindowsTTS("zira")


class Speaker:
    """Speaks replies sentence by sentence. The next sentence is synthesised while the current
    one plays (no gaps); stop() silences it and drops everything queued, instantly."""

    def __init__(self, engine: str = "kokoro", voice: str = "af_heart"):
        self.engine_name, self.voice = engine, voice
        self._tts = None
        self._text: "queue.Queue[tuple[int, str]]" = queue.Queue()
        self._audio: "queue.Queue[tuple[int, np.ndarray, int]]" = queue.Queue(maxsize=3)
        self._gen = 0                       # bumped by stop(): stale audio is thrown away
        self._pending = 0
        self.speaking = False
        self.last_error = ""
        threading.Thread(target=self._producer, daemon=True).start()
        threading.Thread(target=self._player, daemon=True).start()

    @property
    def engine(self) -> str:
        return getattr(self._tts, "name", self.engine_name)

    def say(self, text: str, interrupt: bool = False):
        if interrupt:
            self.stop()
        for sent in _sentences(text):
            self._pending += 1
            self._text.put((self._gen, sent))
        self.speaking = self._pending > 0

    def stop(self):
        self._gen += 1
        for q in (self._text, self._audio):
            try:
                while True:
                    q.get_nowait()
            except queue.Empty:
                pass
        self._pending = 0
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass
        self.speaking = False

    def synth(self, text: str) -> tuple[np.ndarray, int]:
        if self._tts is None:
            self._tts = make_tts(self.engine_name, self.voice)
        return self._tts.synth(text)

    def _producer(self):
        while True:
            gen, text = self._text.get()
            if gen != self._gen:
                continue
            try:
                audio, sr = self.synth(text)
            except Exception as e:
                self.last_error = str(e)[:160]
                self._pending = max(0, self._pending - 1); continue
            if gen == self._gen:
                self._audio.put((gen, audio, sr))

    def _player(self):
        import sounddevice as sd
        while True:
            gen, audio, sr = self._audio.get()
            try:
                if gen == self._gen and len(audio):
                    sd.play(audio, sr)
                    while sd.get_stream().active and gen == self._gen:
                        time.sleep(0.02)
            except Exception as e:
                self.last_error = str(e)[:160]
            finally:
                self._pending = max(0, self._pending - 1)
                self.speaking = self._pending > 0


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return [p for p in parts if p.strip()]


def _sapi_speak(text: str) -> bool:
    try:
        import comtypes
        import comtypes.client
        comtypes.CoInitialize()
        comtypes.client.CreateObject("SAPI.SpVoice").Speak(text, 0)
        return True
    except Exception:
        return False


# ============================== voice activity ==============================
class StreamingVAD:
    """Silero VAD (shipped with faster-whisper) run frame by frame with its state kept."""

    def __init__(self):
        import onnxruntime
        from faster_whisper.vad import get_assets_path
        o = onnxruntime.SessionOptions()
        o.inter_op_num_threads = o.intra_op_num_threads = 1
        o.log_severity_level = 4
        self.sess = onnxruntime.InferenceSession(os.path.join(get_assets_path(), "silero_vad_v6.onnx"),
                                                 providers=["CPUExecutionProvider"], sess_options=o)
        self.reset()

    def reset(self):
        self.h = np.zeros((1, 1, 128), np.float32)
        self.c = np.zeros((1, 1, 128), np.float32)
        self.ctx = np.zeros((1, 64), np.float32)

    def __call__(self, frame: np.ndarray) -> float:
        x = np.concatenate([self.ctx, frame.reshape(1, -1).astype(np.float32)], axis=1)
        out, self.h, self.c = self.sess.run(None, {"input": x, "h": self.h, "c": self.c})
        self.ctx = x[:, -64:]
        return float(np.asarray(out).reshape(-1)[0])


# ============================== speech in ==============================
_EXPECTED_MB = {"tiny.en": 75, "base.en": 148, "small.en": 484}


class Transcriber:
    """Whisper speech-to-text. preload() gets it ready in the background (downloading it once
    with visible progress if needed), so the first spoken command isn't slow. Transcribing
    before it's ready just waits for it -- the user can start talking straight away."""

    def __init__(self, model: str = "base.en"):
        self.model_name = model
        self._m = None
        self._lock = threading.Lock()
        self.state = "idle"            # idle | downloading | loading | ready | error
        self.progress = 0              # 0-100 while downloading
        self.error = ""

    @property
    def root(self) -> str:
        return os.path.join(MODELS, "whisper")

    def _repo_dir(self) -> str:
        return os.path.join(self.root, "models--Systran--faster-whisper-" + self.model_name)

    def is_cached(self) -> bool:
        snap = os.path.join(self._repo_dir(), "snapshots")
        if not os.path.isdir(snap):
            return False
        return any(os.path.exists(os.path.join(snap, d, "model.bin")) for d in os.listdir(snap))

    def _downloaded_mb(self) -> float:
        total = 0
        for dp, _, fn in os.walk(self._repo_dir()):
            for f in fn:
                try:
                    total += os.path.getsize(os.path.join(dp, f))
                except OSError:
                    pass
        return total / 2**20

    def _load(self, progress=lambda msg: None):
        with self._lock:                                  # one loader; others wait for it
            if self._m is not None:
                return self._m
            try:
                from faster_whisper import WhisperModel
                path = self.model_name
                if not self.is_cached():
                    self.state = "downloading"
                    from faster_whisper.utils import download_model
                    done = threading.Event()
                    want = _EXPECTED_MB.get(self.model_name, 150)

                    def watch():
                        while not done.wait(0.5):
                            self.progress = min(99, int(self._downloaded_mb() * 100 / want))
                            progress(f"Downloading speech recognition (one time)... {self.progress}%")
                    threading.Thread(target=watch, daemon=True).start()
                    try:
                        path = download_model(self.model_name, cache_dir=self.root)
                    finally:
                        done.set()
                    self.progress = 100
                self.state = "loading"
                progress("Loading speech recognition...")
                self._m = WhisperModel(path, device="cpu", compute_type="int8", download_root=self.root)
                self.state = "ready"
                progress("Speech recognition ready.")
            except Exception as e:
                self.state, self.error = "error", str(e)[:200]
                progress(f"Speech recognition could not start: {self.error}")
                raise
            return self._m

    def preload(self, progress=lambda msg: None) -> threading.Thread:
        """Get ready in the background. Safe to call more than once."""
        t = threading.Thread(target=lambda: self._try_load(progress), daemon=True)
        t.start()
        return t

    def _try_load(self, progress):
        try:
            self._load(progress)
        except Exception:
            pass


    def __call__(self, audio16k: np.ndarray, hotwords: str = "") -> str:
        segs, _ = self._load().transcribe(audio16k.astype(np.float32), language="en", beam_size=1,
                                          vad_filter=False, condition_on_previous_text=False,
                                          hotwords=hotwords or None,
                                          initial_prompt=(f"Names: {hotwords}." if hotwords else None))
        text = normalize(" ".join(s.text.strip() for s in segs).strip())
        # Whisper's classic hallucinations on silence/noise
        if text.lower().strip(" .!") in ("", "you", "thank you", "thanks for watching", "bye"):
            return ""
        return text


_TRANSCRIBERS: dict = {}


def get_transcriber(model: str = "base.en") -> Transcriber:
    """One shared engine per model: preloading and listening use the same instance."""
    if model not in _TRANSCRIBERS:
        _TRANSCRIBERS[model] = Transcriber(model)
    return _TRANSCRIBERS[model]



_FIX = [(r"\b(?:u|you)\s*tube\b", "YouTube"), (r"\bvs\s*code\b", "VS Code"),
        (r"\blocal\s*pilot\b", "LocalPilot"), (r"\bnote\s*pad\b", "Notepad"),
        (r"\blow[\s-]?f(?:i|ee|y|ai)\b", "lofi")]


def normalize(text: str) -> str:
    for pat, rep in _FIX:
        text = re.sub(pat, rep, text, flags=re.IGNORECASE)
    return text


class Listener:
    """Turns a stream of 32 ms frames into utterances (speech start -> 0.6 s of silence)."""

    def __init__(self, on_utterance, on_speech_start=lambda: None, transcriber: Transcriber | None = None,
                 hotwords=lambda: ""):
        self.vad = StreamingVAD()
        self.stt = transcriber or get_transcriber()
        self.on_utterance = on_utterance
        self.on_speech_start = on_speech_start
        self.hotwords = hotwords
        self.start_frames, self.end_frames = 3, 19      # ~0.1 s to start, ~0.6 s silence to end
        self.on_th, self.off_th = 0.5, 0.35
        self.max_frames = int(15 * SR / FRAME)
        self._pre = []                                   # 0.3 s pre-roll
        self._buf: list[np.ndarray] = []
        self._run = self._quiet = 0
        self.in_speech = False
        self._stream = None
        self._frames: "queue.Queue[np.ndarray]" = queue.Queue()
        self._alive = False
        self._rest = np.zeros(0, np.float32)
        self._todo: "queue.Queue[np.ndarray]" = queue.Queue()
        threading.Thread(target=self._stt_worker, daemon=True).start()

    def _stt_worker(self):
        while True:
            self._transcribe(self._todo.get())

    def feed(self, frame: np.ndarray):
        """Process one 512-sample float32 frame (mic callback thread or tests)."""
        p = self.vad(frame)
        if not self.in_speech:
            self._pre = (self._pre + [frame])[-10:]
            self._run = self._run + 1 if p >= self.on_th else 0
            if self._run >= self.start_frames:
                self.in_speech, self._buf, self._quiet = True, list(self._pre), 0
                self.on_speech_start()
            return
        self._buf.append(frame)
        self._quiet = self._quiet + 1 if p < self.off_th else 0
        if self._quiet >= self.end_frames or len(self._buf) >= self.max_frames:
            audio = np.concatenate(self._buf)
            self.in_speech, self._buf, self._run, self._pre = False, [], 0, []
            self.vad.reset()
            self._todo.put(audio)                    # one worker: transcripts stay in order

    def feed_audio(self, audio16k: np.ndarray):
        x = np.concatenate([self._rest, audio16k.astype(np.float32)])
        n = len(x) // FRAME * FRAME
        for i in range(0, n, FRAME):
            self.feed(x[i:i + FRAME])
        self._rest = x[n:]

    def _transcribe(self, audio):
        try:
            text = self.stt(audio, self.hotwords())
        except Exception as e:
            text = ""
            self.on_utterance("", error=str(e)) if _accepts_error(self.on_utterance) else None
            return
        self.on_utterance(text)            # '' too: lets the voice glue release a hold

    # ----- microphone -----
    def start(self, device=None):
        import sounddevice as sd
        self._alive = True

        def cb(indata, frames, t, status):
            self._frames.put(indata[:, 0].copy())

        self._stream = sd.InputStream(samplerate=SR, channels=1, dtype="float32", blocksize=FRAME,
                                      device=device, callback=cb)
        self._stream.start()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        while self._alive:
            try:
                self.feed_audio(self._frames.get(timeout=0.2))
            except queue.Empty:
                continue

    def stop(self):
        self._alive = False
        try:
            if self._stream is not None:
                self._stream.stop(); self._stream.close()
        except Exception:
            pass
        self._stream = None


def _accepts_error(fn) -> bool:
    import inspect
    try:
        return "error" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


# ============================== glue ==============================
class Voice:
    """Connects mic + speaker to a Session. Text and voice share that session.

    mode 'push' : listen for one utterance (🎤 button), then stop listening.
    mode 'hands-free': keep listening; speaking over LocalPilot interrupts it (barge-in).
    """

    def __init__(self, session, speak: bool = True, log=lambda msg: None, engine: str = "kokoro",
                 voice: str = "af_heart"):
        self.session = session
        self.speak_enabled = speak
        self.log = log
        self.speaker = Speaker(engine, voice)
        self.listener: Listener | None = None
        self.mode = "off"

    # session events -> speech
    def on_session_event(self, kind, data):
        if kind == "stop_speaking":
            self.speaker.stop()
        elif kind == "say" and self.speak_enabled:
            self.speaker.say(data.get("text", ""), interrupt=bool(data.get("interrupt")))

    def _hotwords(self) -> str:
        """Prime Whisper with names it would otherwise mishear (from the user's notes)."""
        m = getattr(self.session, "memory", None)
        prefs = m.preferences() if m else ""
        # Only TYPED text is ground truth. Spoken transcripts (and task history built from them)
        # can contain mishearings -- learning from them would teach Whisper its own mistakes.
        past = ""
        talk = " ".join(t["content"] for t in getattr(self.session, "conversation", [])
                        if t.get("role") == "user" and t.get("source") == "text")
        common = {"Open", "Search", "Play", "The", "And", "For", "What", "Who", "How", "Then", "Go",
                  "Find", "Type", "Notepad", "Settings", "YouTube", "Google", "Brave", "Wikipedia",
                  "Amazon", "Calculator", "File", "Explorer", "Downloads", "Documents", "My", "Stop"}
        from collections import Counter

        def cap(t):
            return [w for w in re.findall(r"\b[A-Z][a-zA-Z]{2,}\b", t) if w not in common]
        # the user's own notes first, then names used REPEATEDLY (a one-off mishearing that got
        # saved in history must not out-vote the real name)
        ranked = [w for w, n in Counter(cap(f"{talk} {past}")).most_common()]
        return " ".join(dict.fromkeys(cap(prefs) + ranked))[:200]

    def _heard(self, text: str, error: str = ""):
        if error or not text:
            unheard = getattr(self.session, "unheard", None)
            if unheard:
                unheard()                         # stay paused; never resume on unclear audio
            if error:
                self.log(f"(voice: could not transcribe: {error})")
            return
        self.log(f"(heard) {text}")
        if self.mode == "push":
            self.stop_listening()
        self.session.submit(text, "voice")

    def _speech_started(self):
        if self.speaker.speaking:                 # barge-in: the user talks over LocalPilot
            self.speaker.stop()
        hold = getattr(self.session, "hold", None)
        if hold:
            hold()                                # and no clicks/typing while they talk

    def start_listening(self, mode: str = "push") -> str:
        if self.listener is not None:
            return ""
        try:
            self.listener = Listener(self._heard, self._speech_started, hotwords=self._hotwords,
                                     transcriber=get_transcriber(getattr(self, "whisper_model", "base.en")))
            self.listener.start()
            self.mode = mode
            return ""
        except Exception as e:
            self.listener = None
            return f"microphone/voice models unavailable: {e}"

    def stop_listening(self):
        if self.listener is not None:
            self.listener.stop()
        self.listener, self.mode = None, "off"


KOKORO_FILES = {
    "kokoro-v1.0.int8.onnx": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx",
    "voices-v1.0.bin": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
}


def ensure_kokoro(progress=lambda msg: None) -> bool:
    """Download the natural voice once (~120 MB) into %APPDATA%/LocalPilot/models/kokoro."""
    import requests
    d = os.path.join(MODELS, "kokoro")
    os.makedirs(d, exist_ok=True)
    for name, url in KOKORO_FILES.items():
        path = os.path.join(d, name)
        if os.path.exists(path) and os.path.getsize(path) > 1_000_000:
            continue
        tmp = path + ".part"
        try:
            with requests.get(url, stream=True, timeout=30) as r:
                r.raise_for_status()
                total, done = int(r.headers.get("content-length") or 0), 0
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk); done += len(chunk)
                        if total:
                            progress(f"Downloading the voice... {done * 100 // total}%")
            os.replace(tmp, path)
        except Exception as e:
            progress(f"Voice download failed: {e}")
            return False
    return True


def available() -> dict:
    """What works on this PC (for the setup check)."""
    out = {"kokoro_voice": os.path.exists(os.path.join(MODELS, "kokoro", "voices-v1.0.bin")),
           "piper_voice": os.path.exists(os.path.join(MODELS, "piper", "en_US-lessac-medium.onnx")),
           "whisper": os.path.isdir(os.path.join(MODELS, "whisper")), "mic": False, "speaker": False}
    try:
        import sounddevice as sd
        d = sd.query_devices()
        out["mic"] = any(x["max_input_channels"] > 0 for x in d)
        out["speaker"] = any(x["max_output_channels"] > 0 for x in d)
    except Exception:
        pass
    return out


# ---- backwards-compatible helpers (older panel code) ----
_default_speaker: Speaker | None = None


def speak(text: str, wait: bool = False) -> bool:
    global _default_speaker
    try:
        if _default_speaker is None:
            _default_speaker = Speaker()
        _default_speaker.say(text, interrupt=True)
        return True
    except Exception:
        return _sapi_speak(text)


def stop_speaking():
    if _default_speaker is not None:
        _default_speaker.stop()
