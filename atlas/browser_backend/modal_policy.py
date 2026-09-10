"""Generic in-page dialog / modal handling policy (source-agnostic).

The Copilot company agent occasionally meets an interstitial *before* it can
search: a native JavaScript dialog (``window.alert`` / ``confirm`` — handled by
the browser), or an HTML overlay modal (Angular Material / CDK, cookie banners,
"Important Notice" notices) that intercepts a click. This module is a **policy**,
not a workaround for any one employer: it classifies the interstitial, decides a
*safe* action, and records the event. It never performs the action itself.

Safety contract (mirrors the product safety rules):

* only *informational* / *cookie* / *notice* modals may be dismissed, and only
  through a benign affordance (Close / Ok / Got it / Accept cookies / Dismiss);
* never click a *consequential* affordance (Apply, Sign In, Register, Submit,
  Validate / Accept Offer, Continue to application, Delete, or any consent whose
  effect is not a pure dismissal);
* an HTML modal reference goes stale after close, so a fresh snapshot is always
  required before the agent proceeds;
* an unresolved *unsafe* modal is an **internal** interaction block
  (retryable / hand-to-human), never an employer "access blocked" verdict.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from atlas.pilot.status_v4 import CompanySearchStatus

# Dialog kinds.
KIND_NATIVE_JS = "native_js_dialog"          # window.alert / confirm / prompt / beforeunload
KIND_HTML_MODAL = "html_modal"               # role=dialog / cdk-overlay / .modal overlay
KIND_NONE = "none"

# Dispositions.
DISPOSITION_SAFE_DISMISS = "safe_dismiss"
DISPOSITION_UNSAFE_BLOCK = "unsafe_block"
DISPOSITION_NONE = "none"

# Policy outcome label for an unresolved unsafe modal. This is an *internal*
# interaction block, mapped to a retryable status (never an external/employer
# block). ``WAITING_FOR_HUMAN`` is the escalation alias when a human must decide.
INTERNAL_INTERACTION_BLOCKED = "INTERNAL_INTERACTION_BLOCKED"
WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"

# A native JS dialog is a distinct browser-level construct; the Playwright MCP
# surfaces it via a dialog event / "modal state", not a DOM overlay.
_NATIVE_MARKERS = (
    "javascript dialog", "js dialog", "page.on(\"dialog\")", "dialog message",
    "modal state", "alert dialog message", "beforeunload", "confirm(",
    "window.alert", "window.confirm", "type: alert", "type: confirm",
    "handle_dialog", "browser_handle_dialog",
)

# HTML overlay markers (Angular Material / CDK, Bootstrap, generic).
_HTML_MODAL_MARKERS = (
    "role=dialog", "role=\"dialog\"", "role: dialog", "aria-modal",
    "mat-dialog", "mat-dialog-title", "mat-dialog-container",
    "cdk-overlay", "cdk-overlay-backdrop", "cdk-overlay-container",
    "modal-backdrop", "modal-dialog", ".modal ", "class=\"modal", "overlay-backdrop",
    "alertdialog",
)
_INTERCEPT_MARKERS = (
    "intercepts pointer events", "would receive the click", "element is not clickable",
    "click intercepted", "subtree intercepts", "obscures", "is blocked by",
    "overlay intercept",
)

# Benign affordances (pure dismissal).
_SAFE_BUTTONS = (
    "close", "ok", "okay", "got it", "gotit", "dismiss", "accept cookies",
    "accept all cookies", "accept all", "i understand", "i agree to cookies",
    "continue", "no thanks", "not now", "maybe later", "skip", "acknowledge",
    "agree & close", "agree and close",
)
# Consequential affordances — never auto-clicked.
_UNSAFE_BUTTONS = (
    "apply", "apply now", "sign in", "signin", "log in", "login", "register",
    "sign up", "signup", "create account", "submit", "submit application",
    "validate", "accept offer", "validate offer", "confirm application",
    "continue to application", "start application", "delete", "pay", "checkout",
    "authorize", "authorise", "consent to share", "give consent", "opt in",
)

# Informational / notice / cookie modal signals (safe class of modal).
_INFORMATIONAL_MARKERS = (
    "important notice", "notice", "cookie", "cookies", "privacy", "disclaimer",
    "terms of use", "announcement", "welcome", "for information", "this website uses",
    "we use cookies", "advisory", "beware", "fraud", "recruitment fraud",
)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\u2192", "->")).strip().lower()


@dataclass
class DialogObservation:
    """One classified dialog/modal observation from a page snapshot / event."""

    kind: str = KIND_NONE
    is_modal: bool = False
    role_dialog: bool = False
    has_overlay: bool = False
    click_intercepted: bool = False
    informational: bool = False
    title: str = ""
    buttons: tuple[str, ...] = ()
    safe_buttons: tuple[str, ...] = ()
    unsafe_buttons: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "kind": self.kind, "is_modal": self.is_modal, "role_dialog": self.role_dialog,
            "has_overlay": self.has_overlay, "click_intercepted": self.click_intercepted,
            "informational": self.informational, "title": self.title,
            "buttons": list(self.buttons), "safe_buttons": list(self.safe_buttons),
            "unsafe_buttons": list(self.unsafe_buttons),
        }


@dataclass
class DialogAction:
    """The policy decision for a :class:`DialogObservation`."""

    disposition: str = DISPOSITION_NONE
    button: str = ""
    requires_resnapshot: bool = False
    status_hint: str = ""          # CompanySearchStatus value when blocked
    policy_label: str = ""         # INTERNAL_INTERACTION_BLOCKED / WAITING_FOR_HUMAN
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "disposition": self.disposition, "button": self.button,
            "requires_resnapshot": self.requires_resnapshot,
            "status_hint": self.status_hint, "policy_label": self.policy_label,
            "reason": self.reason,
        }


def classify_dialog(text: str, buttons: Optional[list[str]] = None,
                    *, title: str = "") -> DialogObservation:
    """Classify a snapshot / event fragment as native JS dialog, HTML modal, or none."""
    low = _norm(text)
    btns = tuple(b for b in (buttons or []) if str(b).strip())
    if not btns:
        # Best-effort: harvest button-like tokens from the fragment.
        btns = tuple(sorted({m.strip() for m in re.findall(
            r'button\s+"([^"]{1,40})"', text or "", flags=re.I)}))

    native = any(m in low for m in _NATIVE_MARKERS)
    role_dialog = any(m in low for m in ("role=dialog", "role=\"dialog\"", "role: dialog",
                                         "alertdialog", "aria-modal"))
    overlay = any(m in low for m in _HTML_MODAL_MARKERS)
    intercepted = any(m in low for m in _INTERCEPT_MARKERS)

    kind = KIND_NONE
    if native and not (role_dialog or overlay):
        kind = KIND_NATIVE_JS
    elif role_dialog or overlay:
        kind = KIND_HTML_MODAL
    elif native:
        kind = KIND_NATIVE_JS

    low_btns = [_norm(b) for b in btns]
    safe = tuple(b for b, lb in zip(btns, low_btns) if any(s == lb or s in lb for s in _SAFE_BUTTONS))
    unsafe = tuple(b for b, lb in zip(btns, low_btns) if any(u == lb or u in lb for u in _UNSAFE_BUTTONS))
    informational = any(m in low for m in _INFORMATIONAL_MARKERS)

    return DialogObservation(
        kind=kind,
        is_modal=kind in (KIND_HTML_MODAL, KIND_NATIVE_JS),
        role_dialog=role_dialog,
        has_overlay=overlay,
        click_intercepted=intercepted,
        informational=informational,
        title=title or "",
        buttons=btns,
        safe_buttons=safe,
        unsafe_buttons=unsafe,
    )


def decide_dialog_action(obs: DialogObservation) -> DialogAction:
    """Decide a *safe* action for a classified dialog. Never returns a
    consequential click."""
    if obs.kind == KIND_NONE:
        return DialogAction(disposition=DISPOSITION_NONE, reason="no dialog present")

    # A native JS dialog is dismissed at the browser layer (accept/dismiss the
    # dialog event). Treat an informational native alert as safe-dismiss; a
    # native confirm that gates a consequential action is left to a human.
    if obs.kind == KIND_NATIVE_JS:
        if obs.unsafe_buttons:
            return DialogAction(
                disposition=DISPOSITION_UNSAFE_BLOCK,
                requires_resnapshot=False,
                status_hint=CompanySearchStatus.BROWSER_TOOL_ERROR.value,
                policy_label=WAITING_FOR_HUMAN,
                reason="native dialog gates a consequential action",
            )
        return DialogAction(
            disposition=DISPOSITION_SAFE_DISMISS, button="dismiss",
            requires_resnapshot=True, reason="benign native dialog dismissed",
        )

    # HTML modal.
    # A safe dismissal is allowed only for an informational/cookie/notice modal
    # that exposes a benign affordance and no consequential one is required.
    if obs.safe_buttons and (obs.informational or not obs.unsafe_buttons):
        # Prefer an explicit Close/Ok; never choose an unsafe button.
        preferred = _preferred_safe_button(obs.safe_buttons)
        return DialogAction(
            disposition=DISPOSITION_SAFE_DISMISS, button=preferred,
            requires_resnapshot=True,  # modal ref goes stale after close
            reason="informational/notice modal dismissed via benign affordance",
        )

    # Otherwise: unsafe or unclassifiable modal -> internal interaction block.
    return DialogAction(
        disposition=DISPOSITION_UNSAFE_BLOCK,
        requires_resnapshot=False,
        status_hint=CompanySearchStatus.BROWSER_TOOL_ERROR.value,
        policy_label=INTERNAL_INTERACTION_BLOCKED,
        reason="modal exposes only consequential/unknown affordances; not an employer block",
    )


def _preferred_safe_button(safe_buttons: tuple[str, ...]) -> str:
    order = ("close", "ok", "okay", "got it", "dismiss", "acknowledge")
    low = {_norm(b): b for b in safe_buttons}
    for want in order:
        for lb, orig in low.items():
            if lb == want or want in lb:
                return orig
    return safe_buttons[0]


def dialog_event_record(obs: DialogObservation, action: DialogAction,
                        *, snapshot_ref: str = "") -> dict:
    """A recordable audit row for the dialog event and the action taken."""
    return {
        "event": "dialog_observed",
        "snapshot_ref": snapshot_ref,
        "observation": obs.to_dict(),
        "action": action.to_dict(),
    }


__all__ = [
    "KIND_NATIVE_JS", "KIND_HTML_MODAL", "KIND_NONE",
    "DISPOSITION_SAFE_DISMISS", "DISPOSITION_UNSAFE_BLOCK", "DISPOSITION_NONE",
    "INTERNAL_INTERACTION_BLOCKED", "WAITING_FOR_HUMAN",
    "DialogObservation", "DialogAction",
    "classify_dialog", "decide_dialog_action", "dialog_event_record",
]
