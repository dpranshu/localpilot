"""LocalPilot entry point.

Usage:
    python run.py "open notepad and type hello world"
    python run.py --dry-run "find the save button"
    python run.py --model qwen2.5:3b-instruct --steps 20 "your goal"
    python run.py --check            # just verify Ollama + model are reachable
    python run.py --probe            # print what the agent currently sees (no model, no actions)
"""
import argparse
import sys

from localpilot.config import Config
from localpilot import llm
from localpilot.agent import UnfinishedRun, run
from localpilot.perception import perceive


def main():
    p = argparse.ArgumentParser(description="LocalPilot -- local open-model Windows agent")
    p.add_argument("goal", nargs="*", help="natural-language task")
    p.add_argument("--model", default=None, help="Ollama model tag")
    p.add_argument("--steps", type=int, default=None, help="max steps")
    p.add_argument("--dry-run", action="store_true", help="plan + log but don't execute")
    p.add_argument("--confirm", action="store_true", help="ask before each action")
    p.add_argument("--no-overlay", action="store_true", help="disable the live on-screen viewer")
    p.add_argument("--no-cursor", action="store_true", help="hide the agent's own cursor")
    p.add_argument("--real-cursor", action="store_true",
                   help="physically move+click the OS cursor for UIA actions (more visible)")
    p.add_argument("--screenshots", action="store_true", help="save a PNG of each step")
    p.add_argument("--no-harness", action="store_true",
                   help="do NOT ask before risky actions (pay/send/delete/post/passwords)")
    p.add_argument("--backend", choices=["uia", "vision", "hybrid"], default=None,
                   help="uia = text tree (default); vision = UI-TARS; hybrid = auto-switch per step")
    p.add_argument("--coord-space", choices=["image", "norm1000"], default=None,
                   help="vision: how to read UI-TARS <point> coords (default image)")
    p.add_argument("--vision-probe", action="store_true",
                   help="vision: capture screen, print the model's raw reply + mapped action, no execution")
    p.add_argument("--check", action="store_true", help="verify Ollama/model and exit")
    p.add_argument("-i", "--interactive", action="store_true",
                   help="REPL: enter tasks one after another (model stays warm between tasks)")
    p.add_argument("--probe", action="store_true",
                   help="print the current perceived controls and exit (no model)")
    args = p.parse_args()

    cfg = Config()
    if args.model:
        cfg.model = args.model
    if args.steps:
        cfg.max_steps = args.steps
    if args.dry_run:
        cfg.dry_run = True
    if args.confirm:
        cfg.confirm_each_step = True
    if args.no_overlay:
        cfg.show_overlay = False
    if args.no_cursor:
        cfg.show_cursor = False
    if args.real_cursor:
        cfg.real_cursor = True
    if args.screenshots:
        cfg.save_screenshots = True
    if args.no_harness:
        cfg.harness_enabled = False
    if args.backend:
        cfg.backend = args.backend
    if args.coord_space:
        cfg.vision_coord_space = args.coord_space

    if args.vision_probe:
        from localpilot import vision
        print(f"Vision probe in 3s (model={cfg.vision_model}) -- switch to target window...")
        import time
        time.sleep(3)
        goal = " ".join(args.goal).strip() or "describe the next action to take"
        action, raw, screen = vision.decide_vision(cfg, goal, [])
        print("\n--- RAW MODEL REPLY ---\n" + raw)
        print("\n--- PARSED + MAPPED ACTION ---\n", action)
        print("screen:", screen)
        sys.exit(0)

    if args.check:
        ok, msg = llm.ping(cfg)
        print(msg)
        sys.exit(0 if ok else 1)

    if args.probe:
        print("Probing the focused window in 3s -- switch to your target window now...")
        import time
        time.sleep(3)
        title, elements, rendered = perceive(cfg)
        print(f"\nACTIVE WINDOW: {title}\n")
        print(rendered)
        print(f"\n({len(elements)} actionable controls)")
        sys.exit(0)

    if args.interactive:
        print(f"LocalPilot interactive  |  backend={cfg.backend}  "
              f"model={cfg.model if cfg.backend!='vision' else cfg.vision_model}")
        print("Type a task and press Enter. Blank line or 'quit' to exit. Ctrl+C to abort a task.\n")
        while True:
            try:
                goal = input("task> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nbye"); break
            if not goal or goal.lower() in ("quit", "exit", "q"):
                print("bye"); break
            try:
                run(goal, cfg)
            except KeyboardInterrupt:
                print("\n(task aborted)")
        return

    goal = " ".join(args.goal).strip()
    if not goal:
        p.error("provide a goal, or use --check / --probe / --interactive")
    if isinstance(run(goal, cfg), UnfinishedRun):
        sys.exit(1)


if __name__ == "__main__":
    main()
