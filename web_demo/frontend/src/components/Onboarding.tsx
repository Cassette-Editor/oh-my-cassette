import { useCallback, useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from "react";
import { useApp } from "../useCassette";

const STORAGE_KEY = "omc_web_onboarded";
const MOBILE_QUERY = "(max-width: 860px)";

type Rect = { top: number; left: number; width: number; height: number };

interface StepDef {
  /** `data-tour` value of the element to spotlight on desktop. */
  target: string;
  /** Optional override target for the mobile layout (where the desktop element is off-screen). */
  mobileTarget?: string;
  titleKey: string;
  bodyKey: string;
  /** Optional body copy swapped in on the mobile layout. */
  mobileBodyKey?: string;
  /** Breathing room around the target, in px. */
  pad: number;
}

const STEPS: StepDef[] = [
  { target: "brand", titleKey: "onbWelcomeTitle", bodyKey: "onbWelcomeBody", pad: 10 },
  { target: "upload", titleKey: "onbUploadTitle", bodyKey: "onbUploadBody", pad: 8 },
  { target: "composer", titleKey: "onbComposeTitle", bodyKey: "onbComposeBody", pad: 8 },
  {
    target: "sidepanel",
    mobileTarget: "status-toggle",
    titleKey: "onbStatusTitle",
    bodyKey: "onbStatusBody",
    mobileBodyKey: "onbStatusBodyMobile",
    pad: 10,
  },
];

function hasOnboarded(): boolean {
  try {
    return localStorage.getItem(STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

function markOnboarded(): void {
  try {
    localStorage.setItem(STORAGE_KEY, "1");
  } catch {
    /* storage may be unavailable (private mode); the tour simply replays next visit */
  }
}

export function Onboarding() {
  const { t, language } = useApp();
  const [active, setActive] = useState(false);
  const [step, setStep] = useState(0);
  const [isMobile, setIsMobile] = useState(
    () => typeof window !== "undefined" && window.matchMedia(MOBILE_QUERY).matches,
  );
  const [rect, setRect] = useState<Rect | null>(null);
  const cardRef = useRef<HTMLDivElement>(null);
  const primaryRef = useRef<HTMLButtonElement>(null);

  const total = STEPS.length;
  const def = STEPS[step];
  const isLast = step === total - 1;
  const isFirst = step === 0;

  // First-run gate: reveal after a beat so layout (and fonts) settle for an accurate measure.
  useEffect(() => {
    if (hasOnboarded()) return;
    const id = window.setTimeout(() => setActive(true), 380);
    return () => window.clearTimeout(id);
  }, []);

  // Track the mobile breakpoint so step 4 can retarget the Status toggle.
  useEffect(() => {
    const mq = window.matchMedia(MOBILE_QUERY);
    const onChange = () => setIsMobile(mq.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  const measure = useCallback(() => {
    if (!active) return;
    const name = isMobile && def.mobileTarget ? def.mobileTarget : def.target;
    const el = document.querySelector<HTMLElement>(`[data-tour="${name}"]`);
    if (!el) {
      setRect(null);
      return;
    }
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) {
      setRect(null);
      return;
    }
    // Clamp every edge a few px inside the viewport so the full ring (incl. its
    // 2px stroke) stays visible even when the target is flush with a screen edge
    // — e.g. the composer pinned to the bottom of the window.
    const EDGE = 8;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const top = Math.max(EDGE, r.top - def.pad);
    const left = Math.max(EDGE, r.left - def.pad);
    const right = Math.min(vw - EDGE, r.right + def.pad);
    const bottom = Math.min(vh - EDGE, r.bottom + def.pad);
    setRect({
      top,
      left,
      width: Math.max(0, right - left),
      height: Math.max(0, bottom - top),
    });
  }, [active, isMobile, def]);

  // Re-measure on step / breakpoint / locale change; rAF + a short delay catch late reflow.
  useLayoutEffect(() => {
    if (!active) return;
    measure();
    const raf = requestAnimationFrame(measure);
    const delayed = window.setTimeout(measure, 200);
    return () => {
      cancelAnimationFrame(raf);
      window.clearTimeout(delayed);
    };
  }, [active, step, isMobile, language, measure]);

  useEffect(() => {
    if (!active) return;
    const onReflow = () => measure();
    window.addEventListener("resize", onReflow);
    window.addEventListener("orientationchange", onReflow);
    window.addEventListener("scroll", onReflow, true);
    return () => {
      window.removeEventListener("resize", onReflow);
      window.removeEventListener("orientationchange", onReflow);
      window.removeEventListener("scroll", onReflow, true);
    };
  }, [active, measure]);

  const finish = useCallback(() => {
    setActive(false);
    markOnboarded();
  }, []);

  const next = useCallback(() => {
    if (isLast) finish();
    else setStep((s) => Math.min(total - 1, s + 1));
  }, [isLast, finish, total]);

  const back = useCallback(() => {
    setStep((s) => Math.max(0, s - 1));
  }, []);

  // Move focus onto the primary control whenever the step changes.
  useEffect(() => {
    if (!active) return;
    const id = window.setTimeout(() => primaryRef.current?.focus(), 80);
    return () => window.clearTimeout(id);
  }, [active, step]);

  // Keyboard: Esc skips, arrows step, Tab is trapped inside the card.
  useEffect(() => {
    if (!active) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        finish();
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        next();
      } else if (e.key === "ArrowLeft") {
        if (!isFirst) {
          e.preventDefault();
          back();
        }
      } else if (e.key === "Tab") {
        const card = cardRef.current;
        if (!card) return;
        const focusables = Array.from(
          card.querySelectorAll<HTMLElement>("button:not([disabled])"),
        );
        if (focusables.length === 0) return;
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        const activeEl = document.activeElement;
        if (e.shiftKey && activeEl === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && activeEl === last) {
          e.preventDefault();
          first.focus();
        } else if (!card.contains(activeEl)) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [active, next, back, finish, isFirst]);

  if (!active) return null;

  const vh = typeof window !== "undefined" ? window.innerHeight : 0;
  // Anchor the card to the bottom when the target sits up top; otherwise float it just above the target.
  const anchorBottom = !rect || rect.top < vh * 0.45;
  const cardStyle: CSSProperties = anchorBottom
    ? {}
    : { bottom: Math.max(16, vh - rect.top + 14), top: "auto" };

  const titleId = "onb-title";
  const bodyId = "onb-body";
  const bodyKey = isMobile && def.mobileBodyKey ? def.mobileBodyKey : def.bodyKey;

  return (
    <div
      className="onb"
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      aria-describedby={bodyId}
      aria-label={t("onbAria")}
    >
      <div className="onb-backdrop" />
      {rect && (
        <div
          className="onb-spotlight"
          style={{ top: rect.top, left: rect.left, width: rect.width, height: rect.height }}
          aria-hidden="true"
        />
      )}
      <div
        ref={cardRef}
        className={`onb-card ${anchorBottom ? "anchor-bottom" : "anchor-target"}`}
        style={cardStyle}
      >
        <div className="onb-head">
          <span className="onb-step mono" aria-hidden="true">
            {step + 1}
          </span>
          <div className="onb-copy">
            <h2 id={titleId} className="onb-title">
              {t(def.titleKey)}
            </h2>
            <p id={bodyId} className="onb-body">
              {t(bodyKey)}
            </p>
          </div>
        </div>
        <div className="onb-foot">
          <button type="button" className="onb-skip" onClick={isFirst ? finish : back}>
            {isFirst ? t("onbSkip") : t("onbBack")}
          </button>
          <div className="onb-dots" role="group" aria-label={`${t("onbProgressAria")} — ${t("onbStep")} ${step + 1}/${total}`}>
            {STEPS.map((s, i) => (
              <span
                key={s.target}
                className={`onb-dot ${i === step ? "is-active" : ""} ${i < step ? "is-done" : ""}`}
                aria-hidden="true"
              />
            ))}
          </div>
          <button type="button" className="onb-next" ref={primaryRef} onClick={next}>
            {isLast ? t("onbStart") : t("onbNext")}
          </button>
        </div>
      </div>
    </div>
  );
}
