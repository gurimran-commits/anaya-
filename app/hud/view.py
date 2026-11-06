"""Pure Tkinter presentation layer and dedicated GUI thread for the Floating Assistant HUD."""

import logging
import threading
from typing import Callable

from app.hud.models import (
    HudErrorEvent,
    HudEvent,
    HudOperationCompletedEvent,
    HudShutdownEvent,
    HudStatus,
    HudStatusChangedEvent,
    HudViewConfig,
    HudVisibilityChangedEvent,
)
from app.hud.queue import HudEventMailbox

logger = logging.getLogger(__name__)


class HudGuiThread(threading.Thread):
    """Dedicated GUI thread exclusively owning Tkinter root, widgets, and mainloop.

    Thread Confinement Invariant:
        All Tkinter widget creation, mutation, geometry management, polling,
        and destruction execute strictly on this thread. Background threads
        (tray, hotkey, controller workers) communicate solely by publishing
        immutable events to the HudEventMailbox.
    """

    def __init__(
        self,
        mailbox: HudEventMailbox,
        config: HudViewConfig,
        on_text_submit: Callable[[str], None],
        on_voice_click: Callable[[], None],
    ) -> None:
        super().__init__(name="AnayaHudGuiThread", daemon=False)
        self._mailbox = mailbox
        self._config = config
        self._on_text_submit = on_text_submit
        self._on_voice_click = on_voice_click

        # Lifecycle synchronization
        self._init_event = threading.Event()
        self._init_error: Exception | None = None
        self._exit_event = threading.Event()
        self._stop_requested = False

        # GUI Components (confined to GUI thread)
        self._root = None
        self._status_label = None
        self._history_text = None
        self._input_entry = None
        self._voice_button = None
        self._send_button = None

        # State tracking on GUI thread
        self._current_operation_id: str | None = None
        self._is_visible: bool = True

    @property
    def init_event(self) -> threading.Event:
        """Event signaled when Tk initialization completes (either success or failure)."""
        return self._init_event

    @property
    def init_error(self) -> Exception | None:
        """Exception captured if Tk initialization failed."""
        return self._init_error

    @property
    def exit_event(self) -> threading.Event:
        """Event signaled after mainloop exits and all Tk resources are destroyed."""
        return self._exit_event

    def run(self) -> None:
        """Execute GUI initialization and message dispatch loop on dedicated thread."""
        try:
            import tkinter as tk
            from tkinter import scrolledtext, ttk
        except (ImportError, Exception) as exc:
            logger.warning("Tkinter import failed: %s", exc)
            self._init_error = exc
            self._init_event.set()
            self._exit_event.set()
            return

        try:
            # 1. Initialize Tk root strictly on this thread
            self._root = tk.Tk()
            self._root.title(self._config.title)
            self._root.geometry(f"{self._config.width}x{self._config.height}")
            self._root.minsize(200, 150)

            # Window attributes
            if self._config.always_on_top:
                try:
                    self._root.attributes("-topmost", True)
                except Exception as exc:
                    logger.debug("Failed to set -topmost attribute: %s", exc)

            try:
                self._root.attributes("-alpha", self._config.alpha)
            except Exception as exc:
                logger.debug("Failed to set -alpha attribute: %s", exc)

            # Intercept window close (X button): hide instead of destroying
            self._root.protocol("WM_DELETE_WINDOW", self._on_wm_delete_window)

            # 2. Build widget layout
            self._build_widgets(tk, ttk, scrolledtext)

            # Signal initialization success to caller
            self._init_event.set()

        except Exception as exc:
            logger.warning("Tkinter root initialization failed: %s", exc)
            self._init_error = exc
            self._init_event.set()
            self._cleanup_root()
            self._exit_event.set()
            return

        # 3. Schedule first polling iteration and enter mainloop
        try:
            self._schedule_poll()
            self._root.mainloop()
        except Exception as exc:
            logger.error("Unhandled exception in Tkinter mainloop: %s", exc, exc_info=True)
        finally:
            self._cleanup_root()
            self._exit_event.set()

    def _build_widgets(self, tk, ttk, scrolledtext) -> None:
        """Construct the HUD widget layout."""
        main_frame = ttk.Frame(self._root, padding=6)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Header bar: Status indicator
        header_frame = ttk.Frame(main_frame)
        header_frame.pack(fill=tk.X, pady=(0, 4))

        self._status_label = ttk.Label(
            header_frame,
            text=f"Status: {HudStatus.IDLE.value.upper()}",
            font=("Segoe UI", 9, "bold"),
        )
        self._status_label.pack(side=tk.LEFT)

        # Conversation & response display
        self._history_text = scrolledtext.ScrolledText(
            main_frame,
            wrap=tk.WORD,
            height=10,
            font=("Segoe UI", 9),
            state=tk.DISABLED,
        )
        self._history_text.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

        # Bottom control panel: Input, Send, Voice
        control_frame = ttk.Frame(main_frame)
        control_frame.pack(fill=tk.X)

        self._input_entry = ttk.Entry(control_frame, font=("Segoe UI", 9))
        self._input_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        self._input_entry.bind("<Return>", lambda event: self._handle_send_clicked())

        self._send_button = ttk.Button(
            control_frame,
            text="Send",
            width=6,
            command=self._handle_send_clicked,
        )
        self._send_button.pack(side=tk.LEFT, padx=(0, 4))

        self._voice_button = ttk.Button(
            control_frame,
            text="Listen",
            width=8,
            command=self._handle_voice_clicked,
        )
        self._voice_button.pack(side=tk.LEFT)

    def _schedule_poll(self) -> None:
        """Schedule next mailbox poll strictly on the GUI thread."""
        if not self._stop_requested and self._root:
            self._poll_mailbox()
            try:
                self._root.after(self._config.poll_interval_ms, self._schedule_poll)
            except Exception as exc:
                logger.debug("Failed to schedule poll: %s", exc)

    def _poll_mailbox(self) -> None:
        """Drain up to a bounded batch of events from the mailbox."""
        # Process at most 4 events per tick to prevent UI starvation
        for _ in range(4):
            event = self._mailbox.poll_next()
            if event is None:
                break
            self._dispatch_event(event)

    def _dispatch_event(self, event: HudEvent) -> None:
        """Process an immutable event on the GUI thread."""
        if isinstance(event, HudShutdownEvent):
            logger.debug("HudShutdownEvent received; initiating mainloop exit")
            self._stop_requested = True
            if self._root:
                try:
                    self._root.quit()
                except Exception as exc:
                    logger.debug("Error during root.quit(): %s", exc)
            return

        if isinstance(event, HudVisibilityChangedEvent):
            self._is_visible = event.visible
            if self._root:
                try:
                    if event.visible:
                        self._root.deiconify()
                    else:
                        self._root.withdraw()
                except Exception as exc:
                    logger.debug("Error toggling window visibility: %s", exc)
            return

        if isinstance(event, HudStatusChangedEvent):
            self._update_status_display(event.status, event.details)
            return

        if isinstance(event, HudOperationCompletedEvent):
            self._append_message("Anaya", event.message)
            self._update_status_display(HudStatus.IDLE)
            self._set_controls_enabled(True)
            return

        if isinstance(event, HudErrorEvent):
            display_err = event.user_message
            if event.sanitized_details:
                display_err = f"{display_err} ({event.sanitized_details})"
            self._append_message("Error", display_err)
            self._update_status_display(HudStatus.ERROR, display_err)
            self._set_controls_enabled(True)
            return

    def _handle_send_clicked(self) -> None:
        """Handle user clicking Send or pressing Enter."""
        if not self._input_entry:
            return
        text = self._input_entry.get().strip()
        if not text:
            return

        self._input_entry.delete(0, "end")
        self._append_message("You", text)
        self._update_status_display(HudStatus.PROCESSING, "Processing query...")
        self._set_controls_enabled(False)

        try:
            self._on_text_submit(text)
        except Exception as exc:
            logger.error("Error invoking on_text_submit: %s", exc, exc_info=True)
            self._update_status_display(HudStatus.ERROR, "Failed to submit request")
            self._set_controls_enabled(True)

    def _handle_voice_clicked(self) -> None:
        """Handle user clicking the push-to-talk button."""
        self._update_status_display(HudStatus.LISTENING, "Listening...")
        self._set_controls_enabled(False)

        try:
            self._on_voice_click()
        except Exception as exc:
            logger.error("Error invoking on_voice_click: %s", exc, exc_info=True)
            self._update_status_display(HudStatus.ERROR, "Voice session unavailable")
            self._set_controls_enabled(True)

    def _on_wm_delete_window(self) -> None:
        """Intercept the window close button to hide rather than terminate."""
        logger.debug("HUD window close intercepted: hiding window")
        self._is_visible = False
        if self._root:
            try:
                self._root.withdraw()
            except Exception as exc:
                logger.debug("Error withdrawing window: %s", exc)

    def _update_status_display(self, status: HudStatus, details: str = "") -> None:
        """Update status label text and styling."""
        if not self._status_label:
            return
        status_text = f"Status: {status.value.upper()}"
        if details:
            status_text += f" - {details}"
        try:
            self._status_label.config(text=status_text)
        except Exception:
            pass

    def _append_message(self, sender: str, text: str) -> None:
        """Append formatted text into conversation history display."""
        if not self._history_text:
            return
        try:
            import tkinter as tk

            self._history_text.config(state=tk.NORMAL)
            self._history_text.insert(tk.END, f"{sender}: {text}\n\n")
            self._history_text.see(tk.END)
            self._history_text.config(state=tk.DISABLED)
        except Exception as exc:
            logger.debug("Failed to append message to history text: %s", exc)

    def _set_controls_enabled(self, enabled: bool) -> None:
        """Enable or disable interactive widgets during in-flight operations."""
        import tkinter as tk

        state = tk.NORMAL if enabled else tk.DISABLED
        try:
            if self._input_entry:
                self._input_entry.config(state=state)
            if self._send_button:
                self._send_button.config(state=state)
            if self._voice_button:
                self._voice_button.config(state=state)
        except Exception:
            pass

    def _cleanup_root(self) -> None:
        """Safely destroy Tk root and unregister commands exclusively on this GUI thread."""
        if self._root is not None:
            try:
                if self._input_entry:
                    try:
                        self._input_entry.unbind("<Return>")
                    except Exception:
                        pass
                if self._send_button:
                    try:
                        self._send_button.config(command="")
                    except Exception:
                        pass
                if self._voice_button:
                    try:
                        self._voice_button.config(command="")
                    except Exception:
                        pass
                if hasattr(self._root, "_tclCommands") and self._root._tclCommands:
                    for cmd in list(self._root._tclCommands):
                        try:
                            self._root.deletecommand(cmd)
                        except Exception:
                            pass
                    self._root._tclCommands.clear()
            except Exception:
                pass

            try:
                self._root.destroy()
            except Exception as exc:
                logger.debug("Error destroying Tk root: %s", exc)
            finally:
                self._root = None
                self._status_label = None
                self._history_text = None
                self._input_entry = None
                self._voice_button = None
                self._send_button = None

            # Collect Tcl garbage while still on the owning GUI thread
            try:
                import gc
                gc.collect()
            except Exception:
                pass
