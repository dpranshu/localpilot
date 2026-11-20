"""Central configuration for LocalPilot."""
import os
from dataclasses import dataclass, field


@dataclass
class Config:
    # --- Model / Ollama ---
    model: str = "qwen2.5:7b-instruct-q4_K_M"   # swap to qwen2.5:3b-instruct if slow
    ollama_url: str = "http://127.0.0.1:11434"   # not "localhost": IPv6 lookup costs ~2s per call
    temperature: float = 0.1
    request_timeout: int = 120                    # seconds per model call
    keep_alive: str = "30m"                       # keep the model in memory between tasks

    def __post_init__(self):
        self.ollama_url = (self.ollama_url or "").replace("://localhost", "://127.0.0.1")

    # --- Agent loop ---
    max_steps: int = 40                           # hard cap on actions per task
    max_elements: int = 70                        # UI nodes sent to the model per step
    settle_seconds: float = 0.8                   # wait after each action for UI to update
    history_len: int = 8                          # recent actions fed back to the model
    verify_goal: bool = True                      # when the window title changes, ask "is the goal met?"
    # Personal memory: past runs + the user's own notes, recalled for similar tasks
    use_memory: bool = True
    think_first: bool = True                      # the brain: understand, plan, answer, talk
    speak_replies: bool = True                    # say replies aloud (offline Windows voice)
    tts_engine: str = "kokoro"                    # kokoro (natural) | piper (optional) | windows
    tts_voice: str = "af_heart"                   # kokoro voice: af_heart, af_bella, af_nicole, bf_emma
    whisper_model: str = "base.en"                # "small.en" = more accurate, ~3x slower
    research_enabled: bool = True                 # live questions -> research in the agent-browser
    research_mode: str = "quick"                  # default mode: quick | research | deep
    research_max_seconds: int = 0                 # 0 = the mode's own limit
    research_max_pages: int = 0                   # 0 = the mode's own limit
    browser_profile: str = "balanced"             # eco | balanced | performance
    hold_event: object = None                     # set while the user is speaking: actions wait
    memory_path: str = ""                         # "" = %APPDATA%\LocalPilot\memory.json
    user_context: str = ""                        # filled per run from memory (not a user knob)

    # --- Perception ---
    max_tree_depth: int = 28                      # how deep to walk the UIA tree
    include_offscreen: bool = False               # skip controls not currently visible

    # --- Safety ---
    kill_key_vk: int = 0x7B                       # F12 = emergency stop between steps
    mouse_failsafe: bool = True                   # mouse into the top-left corner aborts
    confirm_each_step: bool = False               # if True, press Enter in console before each act
    dry_run: bool = False                         # plan + log actions but do NOT execute them
    # Harness (permission gate): ask before paying, sending, deleting, publishing,
    # signing out, or typing a password / card number. Yes=allow, No=skip, Cancel=stop.
    harness_enabled: bool = True
    harness_extra_sensitive: tuple = ()           # extra words that should also ask first
    harness_block: tuple = ()                     # words that are never allowed (no prompt)
    harness_confirm_callback: object = None       # callable(desc) -> "allow"/"skip"/"stop" or bool

    # --- Live viewer ---
    show_overlay: bool = True          # transparent on-screen highlight of each action
    save_screenshots: bool = False     # also save a PNG of the screen each step
    # --- Agent cursor ---
    show_cursor: bool = True           # the agent's own visible cursor glides to each target
    cursor_move_time: float = 0.35     # pause so you can see the cursor travel before it acts
    real_cursor: bool = False          # physically move+click the OS cursor for UIA actions too
                                       # (more visible, but Invoke/SetValue is more reliable)

    # --- Backend ---
    backend: str = "uia"               # "uia" text tree | "vision" UI-TARS | "hybrid" auto-switch
    # Hybrid: use fast UIA text by default; fall back to vision per-step when the
    # accessibility tree is too thin to act on, or when text keeps stalling.
    hybrid_min_content: int = 3        # below this many non-chrome controls -> use vision
    hybrid_escalate_after: int = 2     # consecutive text stalls -> switch to vision
    hybrid_vision_sticky: int = 2      # once switched, stay on vision this many steps
    # Vision backend (UI-TARS-1.5). Default pulls a GGUF build from HuggingFace.
    vision_model: str = "hf.co/Mungert/UI-TARS-1.5-7B-GGUF:Q4_K_M"
    vision_max_side: int = 1092        # resize screenshot's long side (lower = faster, less precise)
    # How to interpret the model's <point>x y</point> numbers:
    #   "image"    = absolute pixels in the image we SENT (we scale back to screen)
    #   "norm1000" = 0-1000 normalized (we scale by screen size)
    vision_coord_space: str = "image"
    vision_crop_to_window: bool = True # crop the screenshot to the focused window (focus + speed)
    language: str = "English"          # UI-TARS 'Thought' language
    # --- Vision speed/stop tuning ---
    vision_num_predict: int = 160      # cap generated tokens/step (the Thought is long -> slow)
    vision_terse: bool = True          # ask for a one-sentence Thought (fewer tokens = faster)
    vision_keep_alive: str = "15m"     # keep the model warm in VRAM between steps/runs
    # Seeing (localpilot/see.py): Windows' built-in OCR + a local vision model for "read this",
    # "analyze this image", image files. Empty see_model = use vision_model (UI-TARS).
    see_enabled: bool = True
    see_model: str = ""
    see_keep_alive: str = "3m"         # free the vision model's memory soon after (Qwen stays loaded)
    see_max_side: int = 1280
    vision_max_steps: int = 14         # step cap for the vision backend (steps are costly)
    vision_stop_on_repeat: int = 2     # stop after this many identical no-progress actions

    # --- Logging ---
    log_dir: str = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "LocalPilot", "logs")
    verbose: bool = True

    # --- Runtime hooks (set by the GUI; not CLI flags) ---
    orb_size: int = 150                # diameter (px) of the agent's dot-sphere face
    open_app_callback: object = None   # callable() -> bring the app/control panel to the front
    stop_event: object = None          # threading.Event -> abort between steps
    log_callback: object = None        # callable(record: dict) -> stream events to a UI


DEFAULT = Config()


def resource_path(name: str) -> str:
    """Path to a bundled file (works from source and from the PyInstaller build)."""
    import sys
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)
