"""Independent queue consumer: python -m services.whatsapp_worker --workspace main."""

import argparse
import os
import signal
import threading
import time


def embedded_worker_enabled() -> bool:
    """External is the production/default mode; embedded is an explicit dev opt-in."""
    mode = os.getenv("WHATSAPP_WORKER_MODE", "external").strip().lower()
    if mode not in {"external", "embedded"}:
        raise ValueError("WHATSAPP_WORKER_MODE must be external or embedded")
    return mode == "embedded"


def run_worker(flow, stop: threading.Event) -> None:
    """Replies keep moving even while every outbound HTTP request is slow."""
    if not flow.live or getattr(flow, "remote_worker", False) or not getattr(flow, "messaging_enabled", True):
        raise ValueError("Worker requires a locally enabled live queue; remote/messaging-off/preview modes cannot send")
    flow.dispatch_control.check()

    def incoming() -> None:
        while not stop.is_set():
            try:
                flow.process_events()
                flow.expire_offers()
            except Exception as exc:
                print(f"WhatsApp reply worker error type={type(exc).__name__}", flush=True)
            stop.wait(0.1)

    def acknowledgments() -> None:
        while not stop.is_set():
            try:
                flow._dispatch_batch(1, kinds=("TEXT",), acknowledgments=True)
            except Exception as exc:
                print(f"WhatsApp acknowledgment worker error type={type(exc).__name__}", flush=True)
            stop.wait(0.2)

    events = threading.Thread(target=incoming, name="whatsapp-replies")
    replies = threading.Thread(target=acknowledgments, name="whatsapp-acknowledgments")
    events.start()
    replies.start()
    print("WhatsApp worker ready; replies have a separate processing lane.", flush=True)
    next_reminder_scan = 0.0
    try:
        while not stop.is_set():
            try:
                if time.monotonic() >= next_reminder_scan:
                    flow.queue_template_reminders()
                    next_reminder_scan = time.monotonic() + 30
                flow._dispatch_batch(flow.send_batch_size, kinds=("OFFER",))
            except Exception as exc:
                print(f"WhatsApp sender error type={type(exc).__name__}", flush=True)
            stop.wait(0.2)
    finally:
        stop.set()
        events.join()
        replies.join()


def main() -> None:
    import config  # Load .env before choosing the workspace.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", choices=("main", "test", "legacy"), required=True)
    args = parser.parse_args()
    os.environ["WHATSAPP_WORKER_MODE"] = "external"
    stop = threading.Event()
    for name in ("SIGINT", "SIGTERM"):
        signal.signal(getattr(signal, name), lambda *_: stop.set())
    if args.workspace == "legacy":
        from database.db import engine, SessionLocal
        from services.whatsapp_dispatch import DispatchControl
        from services.whatsapp_help_service import deliver_next_help_reply
        from services.whatsapp_service import sender_settings_from_environment
        sender_settings_from_environment()
        control = DispatchControl(engine)
        try:
            control.check()
            while not stop.is_set():
                try:
                    deliver_next_help_reply(SessionLocal, control=control)
                except Exception as exc:
                    print(f"WhatsApp Help worker error type={type(exc).__name__}", flush=True)
                stop.wait(0.2)
        finally:
            control.engine.dispose()
        return
    if args.workspace == "main":
        from main_workflow.mount import runtime
        from main_workflow.config import enabled
        if not enabled():
            parser.error("MAIN_WORKFLOW_ENABLED=true is required for the main worker")
        instance = runtime()
    else:
        from flow_test.mount import MountedFlow
        instance = MountedFlow()
    try:
        run_worker(instance.flow, stop)
    finally:
        instance.close()
        if "dispatch_control" in instance.flow.__dict__:
            instance.flow.dispatch_control.engine.dispose()


if __name__ == "__main__":
    main()
